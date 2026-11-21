"""Capture graph evidence read-only and preview allergen reconciliation offline.

No mutation/apply mode exists. Missing support stays unresolved, never absent.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Callable

from recipe_wrangler.utils.food_ontology import (
    ALLERGEN_DETECTION_RULES, ALLERGEN_EXCLUSION_REGEXES, CLASSIFICATION_VERSION,
)


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def read_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt") as stream:
        return [json.loads(line) for line in stream]


# Page before expanding relationships; retain every edge, including duplicates.
QUERIES = {
    "ingredients": """
MATCH (i:Ingredient) WHERE elementId(i) > $after
WITH i ORDER BY elementId(i) LIMIT $limit
RETURN elementId(i) AS element_id, properties(i) AS properties,
 [(i)-[l:HAS_CLASS]->(f:FoodOnClass) | {
   element_id:elementId(l), properties:properties(l), target:elementId(f),
   foodon_id:f.foodon_id, label:f.label}] AS links,
 [(i)-[l:HAS_ALLERGEN]->(a:Allergen) | {
   element_id:elementId(l), properties:properties(l), target:elementId(a),
   allergen:a.name}] AS edges,
 [(i)-[l:HAS_DECLARATION]->(d:AllergenDeclaration) | {
   element_id:elementId(d), properties:properties(d), edge_id:elementId(l),
   edge_properties:properties(l), concerns:[(d)-[c:CONCERNS]->(a) | {
     element_id:elementId(c), properties:properties(c), target:elementId(a),
     allergen:a.name}]}] AS declarations
ORDER BY element_id
""",
    "classes": """
MATCH (f:FoodOnClass) WHERE elementId(f) > $after
WITH f ORDER BY elementId(f) LIMIT $limit
RETURN elementId(f) AS element_id, properties(f) AS properties,
 [(f)-[l:SUBCLASS_OF]->(p) | {element_id:elementId(l),
   properties:properties(l), target:elementId(p)}] AS parents
ORDER BY element_id
""",
    "recipes": """
MATCH (r:Recipe) WHERE elementId(r) > $after
WITH r ORDER BY elementId(r) LIMIT $limit
RETURN elementId(r) AS element_id,
 {recipe_id:r.recipe_id, id:r.id, title:r.title, source:r.source,
  status:r.status, declared_allergens:r.declared_allergens,
  declared_allergens_source:r.declared_allergens_source,
  declared_allergens_updated_at:r.declared_allergens_updated_at} AS properties,
 [(r)-[l:HAS_INGREDIENT]->(i:Ingredient) | {
   element_id:elementId(l), properties:properties(l), target:elementId(i)}] AS ingredients,
 [(r)-[l:SUITABILITY_FOR]->(g) | {element_id:elementId(l),
   properties:properties(l), target:elementId(g), name:g.name}] AS suitability,
 [(r)-[l:HAS_TAG]->(t) | {element_id:elementId(l),
   properties:properties(l), target:elementId(t), name:t.name, category:t.category}] AS tags
ORDER BY element_id
""",
}


def capture(out: Path, smoke: bool = False) -> dict:
    from neo4j import GraphDatabase, READ_ACCESS, unit_of_work
    from recipe_wrangler.utils.env_loader import load_runtime_env

    load_runtime_env()
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    auth = os.getenv("NEO4J_AUTH")
    pair = tuple(auth.split("/", 1)) if auth else (
        os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))
    start = time.monotonic()
    metrics = []
    hashes = {}
    with GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://localhost:7687"), auth=pair,
                              max_connection_pool_size=1, max_transaction_retry_time=0,
                              connection_timeout=5, connection_acquisition_timeout=5) as driver:
        with driver.session(default_access_mode=READ_ACCESS, fetch_size=100) as session:
            # Repeated normalized fingerprints detect changes during the capture.
            for iteration in range(1 if smoke else 2):
                for kind, query in QUERIES.items():
                    after = ""
                    rows_count = 0
                    hasher = hashlib.sha256()
                    path = out / f"{kind}.jsonl.gz"
                    if iteration == 0 and path.exists():
                        raise FileExistsError(path)
                    with gzip.open(path if iteration == 0 else out / f"{kind}.check.jsonl.gz", "wt") as stream:
                        while True:
                            if time.monotonic() - start > 900:
                                raise TimeoutError("Capture exceeded 15-minute total budget")
                            tick = time.monotonic()
                            # READ_ACCESS is routing; execute_read supplies an actual read transaction.
                            @unit_of_work(timeout=60)
                            def read_page(tx):
                                return [r.data() for r in tx.run(
                                    query, after=after, limit=3 if smoke else 100)]
                            rows = session.execute_read(read_page)
                            metrics.append({"kind":kind, "pass":iteration, "rows":len(rows),
                                            "seconds":round(time.monotonic()-tick, 4)})
                            if not rows:
                                break
                            for row in rows:
                                # Relationship list order is not a graph invariant.
                                row = canonical(row)
                                line = encoded(row) + "\n"
                                hasher.update(line.encode())
                                stream.write(line)
                            rows_count += len(rows)
                            next_after = rows[-1]["element_id"]
                            if next_after <= after:
                                raise ValueError("Pagination failed to advance")
                            after = next_after
                            if smoke:
                                break
                    key = f"{kind}:{iteration}"
                    hashes[key] = {"sha256":hasher.hexdigest(), "rows":rows_count}
    stable = not smoke and all(hashes[f"{k}:0"] == hashes[f"{k}:1"] for k in QUERIES)
    result = {"stable":stable, "smoke_only":smoke, "writes_executed":False,
              "hashes":hashes, "queries":metrics, "elapsed_seconds":time.monotonic()-start}
    (out / "capture.json").write_text(json.dumps(result, indent=2)+"\n")
    if not smoke and not stable:
        raise RuntimeError("Evidence changed during capture; preview must not proceed")
    return result


def canonical(value: Any, key: str | None = None) -> Any:
    # Property arrays have meaningful order: rollback must preserve them exactly.
    if key in {"properties", "edge_properties"}:
        return value
    if isinstance(value, dict):
        return {k:canonical(v, k) for k,v in value.items()}
    if isinstance(value, list):
        return sorted((canonical(v) for v in value), key=encoded)
    return value


def ancestry(start: str, classes: dict[str, dict]) -> dict[str, list[str]]:
    """Cycle-safe complete closure, with a shortest witness for each ancestor."""
    paths = {start:[start]}
    pending = deque([start])
    while pending:
        node = pending.popleft()
        if node not in classes:
            raise ValueError(f"Incomplete ontology capture: {node}")
        for edge in classes[node]["parents"]:
            target = edge["target"]
            if target not in paths:
                paths[target] = paths[node] + [target]
                pending.append(target)
    return paths


def ontology_support(name: str, links: list[dict], classes: dict[str, dict]) -> dict[str, list[dict]]:
    witnesses = {}
    for link in links:
        paths = ancestry(link["target"], classes)
        for allergen, rule in ALLERGEN_DETECTION_RULES.items():
            if any(re.fullmatch(pattern, name.lower()) for pattern in ALLERGEN_EXCLUSION_REGEXES.get(allergen, [])):
                continue
            for node, path in paths.items():
                foodon_id = classes[node]["properties"].get("foodon_id")
                if foodon_id in rule["roots"]:
                    witnesses.setdefault(allergen, []).append({"root":foodon_id,
                        "direct":link["target"], "path":path})
    return witnesses


def preview_ingredient(
    ingredient: dict, classes: dict[str, dict], proposals: dict[str, dict],
    keyword_hits: Callable[[str, str], list[str]],
) -> dict:
    from recipe_wrangler.utils.foodon_matching import _identity_supported
    props = ingredient["properties"]
    name = props.get("name") or ""
    links = ingredient["links"]
    after = list(links)
    blockers = []
    if re.search(r"\bor\b|and/or", name, re.IGNORECASE):
        blockers.append("alternative_formulation_requires_source_review")
    proposed = proposals.get(props.get("canonical_id"))
    if proposed:
        old = [l for l in links if l["foodon_id"] == proposed["stored_class_id"]]
        targets = [k for k,v in classes.items() if v["properties"].get("foodon_id") == proposed["proposed_class_id"]]
        if name.casefold() != proposed["name"].casefold() or len(old) != 1:
            blockers.append("stored_link_precondition_mismatch")
        elif not proposed["proposed_class_id"]:
            blockers.append("replacement_identity_unresolved")
            # Conditional withdrawal scenario, held for review; never applied here.
            after = [l for l in links if l is not old[0]]
        elif len(targets) != 1:
            blockers.append("replacement_class_missing_or_ambiguous")
        else:
            after = [l for l in links if l is not old[0]] + [{"target":targets[0]}]
    before_support = ontology_support(name, links, classes)
    after_support = ontology_support(name, after, classes)
    link_identity = {link["target"]:_identity_supported(
        name, classes[link["target"]]["properties"].get("label") or "") for link in after}
    output = []
    for allergen in ALLERGEN_DETECTION_RULES:
        hits = keyword_hits(name, allergen)
        edges = [e for e in ingredient["edges"] if e["allergen"] == allergen]
        declarations = [d for d in ingredient["declarations"]
                        if any(c["allergen"] == allergen for c in d["concerns"])]
        stored = edges + declarations
        supported = bool(hits or after_support.get(allergen))
        if not (stored or supported or before_support.get(allergen)):
            continue
        # Never infer ownership from missing provenance; foreign/unknown records stay intact.
        owned = bool(stored) and all(
            row["properties"].get("classification_version") in {"fato-foodon-v1", "fato-foodon-v2", "fato-foodon-v3", "fato-foodon-v4", "fato-foodon-v5", CLASSIFICATION_VERSION}
            and row["properties"].get("presence") == "contains"
            and row["properties"].get("evidence_status") == "inferred"
            and bool(row["properties"].get("sources"))
            and set(row["properties"]["sources"]) <= {"foodon", "keyword"}
            and ("concerns" not in row or (
                len(row["concerns"]) == 1 and row["properties"].get("declaration_type") == "inferred_ingredient_presence"))
            for row in stored)
        action = "retain_supported" if supported else "retain_unresolved"
        if supported and not stored:
            action = "add_supported"
            if not hits and not any(link_identity[w["direct"]] for w in after_support.get(allergen, [])):
                action = "hold_new_positive_identity"
        elif stored and not supported and owned and not blockers:
            action = "retire_derived_candidate"
        stored_roots = {root for row in stored for root in row["properties"].get("foodon_ids") or []}
        next_roots = {w["root"] for w in after_support.get(allergen, [])}
        output.append({"allergen":allergen, "action":action,
                       "keyword_matches":hits, "current_root_support":before_support.get(allergen, []),
                       "proposed_root_support":after_support.get(allergen, []),
                       "before_edges":edges, "before_declarations":declarations,
                       "root_additions":sorted(next_roots - stored_roots),
                       "root_removals_for_review":sorted(stored_roots - next_roots),
                       "ownership_confirmed":owned,
                       "supporting_link_identity_checks":{w["direct"]:link_identity[w["direct"]]
                                                          for w in after_support.get(allergen, [])},
                       "proposed_evidence":{
                           "classification_version":CLASSIFICATION_VERSION,
                           "presence":"contains", "evidence_status":"inferred",
                           "sources":(["keyword"] if hits else []) + (["foodon"] if next_roots else []),
                           "keyword_matches":hits, "foodon_ids":sorted(next_roots),
                       } if supported and action != "hold_new_positive_identity" else None,
                       "assessment":"contains" if supported and action != "hold_new_positive_identity" else "unresolved"})
    return {"ingredient_id":props.get("canonical_id"), "element_id":ingredient["element_id"],
            "name":name, "before":ingredient, "before_sha256":digest(ingredient),
            "proposed_links":after, "link_proposal":proposed, "blockers":blockers, "groups":output}


def preview(out: Path, proposal_path: Path) -> None:
    from recipe_wrangler.utils.es_recipe_evidence import recipe_allergen_fields
    from recipe_wrangler.utils.env_loader import load_runtime_env
    load_runtime_env()
    from recipe_wrangler.repositories.neo4j_recipes import _allergen_keyword_hits

    metadata = json.loads((out / "capture.json").read_text())
    if not metadata["stable"] or metadata["smoke_only"]:
        raise ValueError("A complete stable capture is required")
    for kind in QUERIES:
        hasher = hashlib.sha256()
        with gzip.open(out / f"{kind}.jsonl.gz", "rb") as stream:
            for line in stream:
                hasher.update(line)
        if hasher.hexdigest() != metadata["hashes"][f"{kind}:0"]["sha256"]:
            raise ValueError("Captured input fingerprint mismatch")
    classes = {r["element_id"]:r for r in read_rows(out / "classes.jsonl.gz")}
    class_ids = Counter(r["properties"].get("foodon_id") for r in classes.values())
    if any(class_ids[root] != 1 for rule in ALLERGEN_DETECTION_RULES.values() for root in rule["roots"]):
        raise ValueError("Configured root missing or duplicated in captured ontology")
    ingredients = read_rows(out / "ingredients.jsonl.gz")
    recipes = read_rows(out / "recipes.jsonl.gz")
    with proposal_path.open() as stream:
        proposals = {r["ingredient_id"]:r for r in csv.DictReader(stream)}
    seen = Counter(r["properties"].get("canonical_id") for r in ingredients)
    if any(seen[key] != 1 for key in proposals):
        raise ValueError("Proposed ingredient ID missing or duplicated")
    results = [preview_ingredient(r, classes, proposals, _allergen_keyword_hits) for r in ingredients]
    memberships = {}
    for recipe in recipes:
        for edge in recipe["ingredients"]:
            memberships.setdefault(edge["target"], set()).add(recipe["element_id"])
    for result in results:
        result["guarded_recipe_membership"] = sorted(memberships.get(result["element_id"], set()))
    # Exact captured inputs are also the rollback inputs, including relationship properties/IDs.
    actions = Counter(g["action"] for r in results for g in r["groups"])
    changed = {r["element_id"] for r in results if r["link_proposal"] or any(
        g["action"] != "retain_supported" or g["root_additions"] or
        g["root_removals_for_review"] for g in r["groups"])}
    by_id = {r["element_id"]:r for r in results}
    affected = []
    for recipe in recipes:
        ids = {r["target"] for r in recipe["ingredients"]}
        if not ids & changed:
            continue
        if not ids <= by_id.keys():
            raise ValueError("Recipe refers to uncaptured ingredient")
        groups = {g["allergen"] for key in ids for g in by_id[key]["groups"] if g["assessment"] == "contains"}
        claims = recipe["properties"].get("declared_allergens") or []
        held_groups = [g for key in ids for g in by_id[key]["groups"] if g["action"] == "retain_unresolved"]
        held_evidence = [{**d["properties"], "allergen":g["allergen"]}
                         for g in held_groups for d in g["before_declarations"]]
        held_positives = {g["allergen"] for g in held_groups if g["before_edges"] or any(
            (d["properties"].get("presence") or "contains").casefold() in {"contains", "present"}
            for d in g["before_declarations"])}
        card_allergens = sorted(groups | set(claims) | held_positives)
        affected.append({"before":recipe, "before_sha256":digest(recipe),
                         "changed_ingredients":sorted(ids & changed),
                         "supported_ingredient_groups":sorted(groups), "retained_recipe_claims":claims,
                         "unresolved_groups":sorted(set(ALLERGEN_DETECTION_RULES) - groups - set(claims)),
                         "pending_review_positive_groups":sorted(held_positives),
                         "conditional_card":{"allergens":card_allergens, **recipe_allergen_fields(card_allergens, held_evidence)},
                         "allergen_uncertainty":True})
    for filename, rows in [("ingredient_preview.jsonl.gz", results), ("affected_recipes.jsonl.gz", affected)]:
        with gzip.open(out / filename, "wt") as stream:
            for row in rows:
                stream.write(encoded(row)+"\n")
    summary = {"writes_executed":False, "classification_version":CLASSIFICATION_VERSION,
               "ingredients":len(ingredients), "classes":len(classes), "recipes":len(recipes),
               "actions":dict(actions), "affected_recipes":len(affected),
               "link_proposals":sum(bool(r["link_proposal"]) for r in results),
               "blocked_link_proposals":[{"name":r["name"], "blockers":r["blockers"]} for r in results if r["link_proposal"] and r["blockers"]],
               "ingredient_holds_by_reason":dict(Counter(reason for r in results for reason in r["blockers"])),
               "proposal_sha256":hashlib.sha256(proposal_path.read_bytes()).hexdigest(),
               "rules_sha256":digest(ALLERGEN_DETECTION_RULES),
               "exclusions_sha256":digest(ALLERGEN_EXCLUSION_REGEXES),
               "caveat":"Development reconciliation candidates; no absence claims, independent accuracy or apply authorization."}
    (out / "summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["capture", "smoke", "preview"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--proposals", type=Path, default=Path("analysis/allergen-foodon-identity-2026-10-31/link_review.csv"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.mode == "preview":
        preview(args.out, args.proposals)
    else:
        capture(args.out, smoke=args.mode == "smoke")


if __name__ == "__main__":
    main()
