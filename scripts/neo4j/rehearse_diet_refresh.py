"""Plan or rehearse a bounded stored-diet refresh. Every write is rolled back.

No schema/origin setup, allergen changes, rematching, models or projections.
The rehearsal uses existing allergen evidence and is not a combined refresh.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import resource
from typing import Any

from neo4j import Driver, Query, Session, Transaction

ROOT = Path(__file__).resolve().parents[2]
MAX_SCOPE = 99  # A 100th result is an overflow sentinel, never a partial plan.
MAX_SNAPSHOT_ROWS = 5000
CODE = (
    "scripts/neo4j/rehearse_diet_refresh.py",
    "src/recipe_wrangler/utils/dietary_classification.py",
    "src/recipe_wrangler/utils/consumer_suitability.py",
    "src/recipe_wrangler/utils/food_ontology.py",
    "src/recipe_wrangler/utils/foodon_matching.py",
    "src/recipe_wrangler/utils/food_identity.py",
    "src/recipe_wrangler/utils/es_recipe_evidence.py",
    "src/recipe_wrangler/utils/dietary_ingredient_evidence.py",
    "src/recipe_wrangler/utils/ingredient_context.py",
    "src/recipe_wrangler/utils/data/dietary_ingredients.json",
)


def canonical(value: Any) -> Any:
    """Retain Neo4j temporal properties in reproducible JSON snapshots."""
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True).encode()).hexdigest()


def code_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CODE}


def rows(reader: Session | Transaction, query: str, **params: Any) -> list[dict]:
    statement = Query(query, timeout=60) if isinstance(reader, Session) else query
    return [canonical(dict(row)) for row in reader.run(statement, **params)]


def bounded(reader: Session | Transaction, query: str, **params: Any) -> list[dict]:
    result = rows(reader, query + " LIMIT 100", **params)
    if len(result) > MAX_SCOPE:
        raise ValueError("Scope exceeds 99 rows; no writes permitted")
    return result


def paged(reader: Session | Transaction, query: str, **params: Any) -> list[dict]:
    result, after = [], ""
    while True:
        batch = rows(reader, query, after=after, **params)
        if not batch:
            return result
        if len(result) + len(batch) > MAX_SNAPSHOT_ROWS:
            raise ValueError("Snapshot row budget exceeded")
        if batch[-1]["id"] <= after:
            raise ValueError("Snapshot pagination did not advance")
        result.extend(batch)
        after = batch[-1]["id"]


def capture(reader: Session | Transaction, recipe_ids: list[str], groups: list[str],
            selected_ingredient_ids: list[str] | None = None) -> dict:
    """Read the touched ingredients and all recipes sharing them, without expansion."""
    from recipe_wrangler.utils.consumer_suitability import DIETARY_ORIGINS, ORIGIN_CLASSIFICATION_VERSION

    if not recipe_ids or len(recipe_ids) > MAX_SCOPE or len(set(recipe_ids)) != len(recipe_ids):
        raise ValueError("Require 1..99 distinct seed recipe IDs")
    if not groups or len(set(groups)) != len(groups) or set(groups) - {"vegan", "vegetarian", "pescatarian"}:
        raise ValueError("Unsupported or duplicate consumer groups")
    seeds = bounded(reader, """UNWIND $ids AS rid MATCH (r:Recipe {recipe_id:rid})
        RETURN r.recipe_id AS recipe_id, elementId(r) AS id ORDER BY id""", ids=recipe_ids)
    if Counter(row["recipe_id"] for row in seeds) != Counter(recipe_ids):
        raise ValueError("Seed recipe is missing or duplicated")
    ingredients = bounded(reader, """UNWIND $ids AS rid MATCH (:Recipe {recipe_id:rid})-[:HAS_INGREDIENT]->(i:Ingredient)
        RETURN DISTINCT elementId(i) AS id ORDER BY id""", ids=recipe_ids)
    if not ingredients:
        raise ValueError("No stored ingredients in seed scope")
    ingredient_ids = [row["id"] for row in ingredients]
    if selected_ingredient_ids is not None:
        if not selected_ingredient_ids or len(set(selected_ingredient_ids)) != len(selected_ingredient_ids) or set(selected_ingredient_ids) - set(ingredient_ids):
            raise ValueError("Selected ingredients must be distinct members of the seed recipes")
        ingredient_ids = sorted(selected_ingredient_ids)
    affected = bounded(reader, """UNWIND $ids AS iid MATCH (i:Ingredient) WHERE elementId(i)=iid
        MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i)
        RETURN DISTINCT r.recipe_id AS recipe_id, elementId(r) AS id ORDER BY id""", ids=ingredient_ids)
    if any(row["recipe_id"] is None for row in affected) or len({r["recipe_id"] for r in affected}) != len(affected):
        raise ValueError("Affected recipe IDs are missing or duplicated")
    recipe_node_ids = [row["id"] for row in affected]
    dependencies = bounded(reader, """UNWIND $ids AS rid MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
        WHERE elementId(r)=rid RETURN DISTINCT elementId(i) AS id ORDER BY id""", ids=recipe_node_ids)
    input_ids = sorted(set(ingredient_ids + recipe_node_ids + [r["id"] for r in dependencies]))
    declarations = bounded(reader, """UNWIND $ids AS nid MATCH (n)-[:HAS_DECLARATION]->(d:AllergenDeclaration)
        WHERE elementId(n)=nid RETURN DISTINCT elementId(d) AS id ORDER BY id""", ids=input_ids)
    taxonomy = bounded(reader, """UNWIND $ids AS iid MATCH (i:Ingredient)-[:HAS_CLASS]->(:FoodOnClass)
        -[:SUBCLASS_OF*0..]->(f:FoodOnClass) WHERE elementId(i)=iid
        RETURN DISTINCT elementId(f) AS id ORDER BY id""", ids=ingredient_ids)
    dependency_ids = sorted({r["id"] for r in dependencies + declarations + taxonomy} - set(ingredient_ids))
    if len(dependency_ids) > MAX_SCOPE:
        raise ValueError("Read-only dependency scope exceeds 99 nodes")
    group_rows = bounded(reader, """MATCH (g:ConsumerGroup) WHERE g.name IN $groups
        RETURN elementId(g) AS id, properties(g) AS properties ORDER BY id""", groups=groups)
    if sorted(row["properties"]["name"] for row in group_rows) != sorted(groups):
        raise ValueError("Consumer groups must exist uniquely; no implicit setup")
    origins = bounded(reader, """MATCH (o:DietaryOrigin)
        RETURN elementId(o) AS id, properties(o) AS properties ORDER BY id""")
    by_name = {row["properties"].get("name"): row["properties"] for row in origins}
    if len(by_name) != len(origins):
        raise ValueError("Duplicate dietary origins")
    for origin in DIETARY_ORIGINS:
        saved = by_name.get(origin["name"], {})
        expected = {key: origin[key] for key in ("vegan_status", "vegetarian_status")}
        expected["classification_version"] = ORIGIN_CLASSIFICATION_VERSION
        if any(saved.get(key) != value for key, value in expected.items()):
            raise ValueError("Dietary origin prerequisites differ; no implicit setup")
    origin_links = bounded(reader, """MATCH (f:FoodOnClass)-[h:INDICATES_ORIGIN]->(o:DietaryOrigin)
        RETURN elementId(h) AS id, f.foodon_id AS root, o.name AS origin,
        properties(h) AS properties ORDER BY id""")
    expected_links = {(root, o["name"]) for o in DIETARY_ORIGINS for root in o["roots"]}
    if any((r["root"], r["origin"]) not in expected_links
           or r["properties"].get("classification_version") != ORIGIN_CLASSIFICATION_VERSION for r in origin_links):
        raise ValueError("Unsupported origin links")
    node_ids = sorted(set(input_ids + dependency_ids))
    nodes = paged(reader, """UNWIND $ids AS nid MATCH (n) WHERE elementId(n)=nid AND elementId(n)>$after
        RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS properties ORDER BY id LIMIT 100""", ids=node_ids)
    relationships = paged(reader, """UNWIND $ids AS nid MATCH (n)-[h]->(t)
        WHERE elementId(n)=nid AND elementId(h)>$after
        RETURN elementId(h) AS id, elementId(n) AS start, elementId(t) AS end,
        type(h) AS type, properties(h) AS properties, labels(t) AS target_labels,
        properties(t) AS target_properties ORDER BY id LIMIT 100""", ids=node_ids)
    if len(nodes) != len(node_ids):
        raise ValueError("Node membership changed during capture")
    return dict(seed_recipe_ids=sorted(recipe_ids), selected_ingredient_ids=selected_ingredient_ids,
        ingredient_ids=ingredient_ids, recipe_node_ids=recipe_node_ids, dependency_ids=dependency_ids,
        affected_recipe_ids=sorted(r["recipe_id"] for r in affected), nodes=nodes,
        relationships=relationships, groups=sorted(groups), consumer_groups=group_rows,
        origins=origins, origin_links=origin_links,
        missing_origin_links=sorted([list(pair) for pair in expected_links - {(r["root"], r["origin"]) for r in origin_links}]))


def assessments(snapshot: dict) -> list[dict]:
    writable = set(snapshot["ingredient_ids"] + snapshot["recipe_node_ids"])
    return [row for row in snapshot["relationships"] if row["type"] == "SUITABILITY_FOR"
            and row["start"] in writable and row["target_properties"].get("name") in snapshot["groups"]]


def check_ownership(snapshot: dict) -> None:
    from recipe_wrangler.utils.consumer_suitability import classification_version

    for row in assessments(snapshot):
        prop = row["properties"]
        group = row["target_properties"]["name"]
        prefix = "pescatarian" if group == "pescatarian" else "vegan-vegetarian"
        scope = "ingredient_composition" if row["start"] in snapshot["ingredient_ids"] else "recipe_composition"
        if prop.get("scope") != scope or not re.fullmatch(prefix + r"-v[1-9][0-9]*", prop.get("classification_version", "")):
            raise ValueError("Unowned assessment cannot be refreshed")
        if int(prop["classification_version"].rsplit("v", 1)[1]) > int(classification_version(group).rsplit("v", 1)[1]):
            raise ValueError("Refusing to downgrade a newer stored assessment")
        if set(prop.get("sources") or []) - {"rules", "foodon", "open_food_facts_ingredients", "allergen_evidence",
                                             "ingredient_suitability", "recipe_declaration"}:
            raise ValueError("Foreign assessment provenance cannot be refreshed")
    keys = [(r["start"], r["end"]) for r in assessments(snapshot)]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate suitability relationships")


def protected(snapshot: dict) -> dict:
    owned_ids = {row["id"] for row in assessments(snapshot)}
    return {**snapshot, "relationships": [r for r in snapshot["relationships"] if r["id"] not in owned_ids]}


def make_plan(reader: Session | Transaction, recipe_ids: list[str], groups: list[str],
              selected_ingredient_ids: list[str] | None = None) -> dict:
    snapshot = capture(reader, recipe_ids, groups, selected_ingredient_ids)
    check_ownership(snapshot)
    return dict(kind="stored_diet_rollback_only_v1", code_sha256=code_hashes(),
        before_sha256=digest(snapshot), before=snapshot, allergen_evidence="existing_stored_evidence_only",
        limits=dict(max_seed_recipes=99, max_ingredients=99, max_affected_recipes=99,
                    max_dependency_nodes=99, read_batch_rows=100,
                    max_snapshot_rows=MAX_SNAPSHOT_ROWS, transaction_seconds=60))


def require_bounded_job() -> None:
    """Refuse transient writes outside the reviewed one-core/1-GiB/no-swap scope."""
    group = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().split("::", 1)[1].strip().lstrip("/")
    memory = {line.split(":")[0]: int(line.split()[1]) * 1024
              for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith(("MemTotal:", "MemAvailable:"))}
    ceiling = min(1024**3, memory["MemTotal"] // 2, (memory["MemAvailable"] - 10 * 1024**3) // 1024**3 * 1024**3)
    maximum = (group / "memory.max").read_text().strip()
    quota, period = (group / "cpu.max").read_text().split()
    if (ceiling <= 0 or maximum == "max" or int(maximum) > ceiling
            or (group / "memory.swap.max").read_text().strip() != "0"
            or quota == "max" or int(quota) > int(period)
            or len(os.sched_getaffinity(0)) > 2
            or resource.getrlimit(resource.RLIMIT_CPU)[1] not in range(1, 901)):
        raise ValueError("Private one-core/1-GiB/no-swap scope, <=2 pinned CPUs and <=900 CPU seconds required")


def rehearse(driver: Driver, plan: dict, *, inject_failure: bool = False) -> dict:
    """Never commit, including on errors; verify rollback using a fresh session."""
    require_bounded_job()
    from recipe_wrangler.utils import dietary_classification as dc
    from recipe_wrangler.utils.consumer_suitability import classification_version

    if plan.get("kind") != "stored_diet_rollback_only_v1" or plan["code_sha256"] != code_hashes():
        raise ValueError("Wrong plan kind or changed code")
    before = plan["before"]
    if digest(before) != plan["before_sha256"]:
        raise ValueError("Plan fingerprint mismatch")
    report = dict(kind=plan["kind"], status="failed", persisted_writes=None,
        before_sha256=plan["before_sha256"], code_sha256=plan["code_sha256"],
        seed_recipe_ids=before["seed_recipe_ids"], affected_recipe_ids=before["affected_recipe_ids"],
        ingredient_ids=before["ingredient_ids"], groups=before["groups"],
        allergen_evidence=plan["allergen_evidence"], injected_failure=inject_failure)
    try:
        with driver.session(fetch_size=100) as session:
            tx = session.begin_transaction(timeout=60)
            try:
                current = capture(tx, before["seed_recipe_ids"], before["groups"], before.get("selected_ingredient_ids"))
                if digest(current) != plan["before_sha256"]:
                    raise ValueError("Graph state changed since plan; no writes permitted")
                check_ownership(current)
                for group in before["groups"]:
                    if dc._initialize_unknown(tx, group, before["ingredient_ids"]) != len(before["ingredient_ids"]):
                        raise ValueError("Ingredient initialization incomplete")
                    if inject_failure:
                        raise ValueError("Injected failure after initialization")
                    dc._classify_negative(tx, group, before["ingredient_ids"])
                    dc._classify_positive(tx, group, before["ingredient_ids"])
                    if dc._aggregate_recipe_batch(tx, before["affected_recipe_ids"], group) != len(before["affected_recipe_ids"]):
                        raise ValueError("Recipe aggregation incomplete")
                after = capture(tx, before["seed_recipe_ids"], before["groups"], before.get("selected_ingredient_ids"))
                if protected(after) != protected(before):
                    raise ValueError("Protected source/graph state changed")
                expected_count = (len(before["ingredient_ids"]) + len(before["affected_recipe_ids"])) * len(before["groups"])
                if len(assessments(after)) != expected_count:
                    raise ValueError("Assessment scope incomplete")
                for row in assessments(after):
                    group = row["target_properties"]["name"]
                    if row["properties"].get("classification_version") != classification_version(group) or row["properties"].get("status") not in {"suitable", "not_suitable", "unknown"}:
                        raise ValueError("Invalid refreshed assessment")
                report.update(status="passed", after=after)
            finally:
                try:
                    tx.rollback()
                finally:
                    tx.close()
    except Exception as error:
        report.update(status="failed", error=str(error) if isinstance(error, ValueError) else type(error).__name__)
    try:
        with driver.session(default_access_mode="READ", fetch_size=100) as session:
            restored = capture(session, before["seed_recipe_ids"], before["groups"], before.get("selected_ingredient_ids"))
        report["rollback_verified"] = digest(restored) == plan["before_sha256"]
    except Exception as error:
        report["rollback_verified"] = False
        report["verification_error"] = type(error).__name__
    if not report["rollback_verified"]:
        report.update(status="failed", error="Post-rollback graph state differs; concurrent changes or rollback failure")
    else:
        report["persisted_writes"] = False
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "rehearse"))
    parser.add_argument("--recipe-id", action="append", default=[])
    parser.add_argument("--ingredient-id", action="append", help="Narrow the smoke scope to these seed ingredient element IDs")
    parser.add_argument("--group", action="append", choices=("vegan", "vegetarian", "pescatarian"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inject-failure", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; refusing to overwrite evidence")
    if args.action == "rehearse" and (not args.plan or args.recipe_id or args.group or args.ingredient_id):
        parser.error("Rehearse requires a frozen --plan and takes no scope overrides")
    os.environ.update(EMBED_WARM_ON_IMPORT="false", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      LIVE_WEIGHT_LLM_ENABLED="false", RECIPE1M_LLM_FALLBACK_ENABLED="false")
    from scripts.neo4j.classify_vegan_vegetarian import _driver
    driver = _driver()
    try:
        if args.action == "plan":
            if args.inject_failure:
                parser.error("Failure injection applies only to rehearsal")
            with driver.session(default_access_mode="READ", fetch_size=100) as session:
                result = make_plan(session, args.recipe_id, args.group or ["vegan", "vegetarian"], args.ingredient_id)
        else:
            result = rehearse(driver, json.loads(args.plan.read_text()), inject_failure=args.inject_failure)
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
        print(json.dumps({k: v for k, v in result.items() if k not in {"before", "after", "code_sha256", "ingredient_ids"}}, indent=2))
        if result.get("status") == "failed":
            raise SystemExit(1)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
