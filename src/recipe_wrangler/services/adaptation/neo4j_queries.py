"""Neo4j queries for the adaptation service.

Standalone module — does not modify or import from the existing
`repositories/neo4j_recipes.py`. Reuses only the shared `run_query` helper.
"""

from __future__ import annotations

import re
from typing import Any

from recipe_wrangler.utils.neo4j_utils import run_query


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"and", "or", "the", "of", "with", "from", "made", "for"}


def _tokens(s: str | None) -> list[str]:
    """Lower-case alphanumeric tokens of length ≥ 2, stopwords removed.

    Each token that looks pluralised (ends in -s, len > 3, not -ss) is also
    expanded to its singular form so FCT names like ``"sugars, powdered"`` can
    match Neo4j nodes like ``"powdered sugar"`` (plural↔singular crossover).
    """

    if not s:
        return []
    raw = [t for t in _TOKEN_RE.findall(s.lower()) if len(t) >= 2 and t not in _STOPWORDS]
    out: list[str] = []
    for t in raw:
        if t not in out:
            out.append(t)
        if t.endswith("s") and len(t) > 3 and not t.endswith("ss"):
            singular = t[:-1]
            if singular not in out:
                out.append(singular)
    return out


_DEPTH_TO_DISTANCE = {1: "low", 2: "medium", 3: "high"}


def _miskg_candidates(name: str) -> list[dict[str, Any]]:
    """Direct MISKG HAS_SUBSTITUTION edges plus single-word-qualifier variants."""

    direct = run_query(
        """
        MATCH (i:Ingredient)
        WHERE toLower(i.name) = toLower($name)
        MATCH (i)-[:HAS_SUBSTITUTION]->(sub:Ingredient)
        RETURN DISTINCT sub.name AS name
        LIMIT 20
        """,
        {"name": name},
    )
    out = [
        {"name": r["name"], "source": "miskg", "category_distance": "low"}
        for r in direct
        if r.get("name")
    ]

    variant = run_query(
        """
        MATCH (i:Ingredient)
        WHERE toLower(i.name) ENDS WITH (' ' + toLower($name))
          AND size(split(i.name, ' ')) = 2
        MATCH (i)-[:HAS_SUBSTITUTION]->(sub:Ingredient)
        WHERE toLower(sub.name) <> toLower($name)
        RETURN DISTINCT sub.name AS name
        LIMIT 20
        """,
        {"name": name},
    )
    out.extend(
        {"name": r["name"], "source": "miskg", "category_distance": "low"}
        for r in variant
        if r.get("name")
    )
    return out


def _foodon_candidates(name: str) -> list[dict[str, Any]]:
    """FoodOn taxonomy siblings — tightest fit (depth 1) first; stop at first hit."""

    template = """
        MATCH (i:Ingredient)
        WHERE toLower(i.name) = toLower($name)
        MATCH (i)-[:HAS_CLASS]->(c:FoodOnClass)
        MATCH (c)-[:SUBCLASS_OF*{depth}]->(ancestor:FoodOnClass)
        MATCH (sib:FoodOnClass)-[:SUBCLASS_OF*1..{depth}]->(ancestor)
        WHERE sib <> c
        MATCH (cand:Ingredient)-[:HAS_CLASS]->(sib)
        WHERE toLower(cand.name) <> toLower($name)
        RETURN DISTINCT cand.name AS name
        LIMIT 20
    """
    for depth in (1, 2, 3):
        query = template.replace("{depth}", str(depth))
        rows = run_query(query, {"name": name})
        names = [r["name"] for r in rows if r.get("name")]
        if names:
            return [
                {
                    "name": n,
                    "source": "foodon",
                    "category_distance": _DEPTH_TO_DISTANCE[depth],
                }
                for n in names
            ]
    return []


def find_substitute_candidates(name: str) -> list[dict[str, Any]]:
    """Merged MISKG + FoodOn candidate list, deduplicated by lowercase name.

    MISKG wins on duplicates because it is curated.
    """

    seen: dict[str, dict[str, Any]] = {}
    for cand in _miskg_candidates(name) + _foodon_candidates(name):
        key = (cand["name"] or "").strip().lower()
        if not key or key == name.strip().lower():
            continue
        seen.setdefault(key, cand)
    return list(seen.values())


def resolve_graph_name(fct_name: str, fallback_hints: list[str] | None = None) -> str | None:
    """Map a long-form FCT row name to the matching Neo4j Ingredient node name.

    Profile rows persist names like ``"Oil, olive, salad or cooking"`` or
    ``"soy sauce made from soy (tamari)"`` — the FCT canonical row — but Neo4j
    Ingredient nodes use everyday names (``"olive oil"``, ``"soy sauce"``).

    Resolution order:

      1. Verbatim case-insensitive match against the FCT name, any hints, and
         each candidate's head segment (first comma-delimited piece).
      2. Token-based match: pick the Ingredient whose every token appears in
         the FCT token set AND at least one token appears in the FCT's head
         segment. The most-specific (most-token) candidate wins, ties broken
         by shorter name.

    The previous longest-substring fallback over-generalised — it would map
    ``"oil, olive, salad or cooking"`` to ``"oil"`` rather than ``"olive oil"``
    because the two-word phrase isn't a contiguous substring of the FCT name.
    Token matching fixes that.
    """

    fct_clean = (fct_name or "").strip()
    if not fct_clean:
        return None

    # --- 1. verbatim exact match on the FCT name and any hints (full strings only).
    # Deliberately skip head-segment verbatim matching — for "oil, olive, ..." we
    # don't want to pre-emptively match the generic Ingredient "oil" and bypass
    # the more specific "olive oil" in step 2.
    seen: set[str] = set()
    for s in [fct_clean, *(fallback_hints or [])]:
        key = (s or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        rows = run_query(
            "MATCH (i:Ingredient) WHERE toLower(i.name) = $name RETURN i.name AS name LIMIT 1",
            {"name": key},
        )
        if rows and rows[0].get("name"):
            return rows[0]["name"]

    # --- 2. token-based match: every Ingredient token must appear in fct_tokens.
    # Among matches, prefer most-specific (most tokens), then tightest cluster
    # (smallest span between first and last matching token in FCT), then earliest
    # appearance, then shorter name.
    fct_tokens = _tokens(fct_clean)
    if not fct_tokens:
        return None

    rows = run_query(
        """
        MATCH (i:Ingredient)
        WITH i, [tok IN split(toLower(i.name), ' ')
                  WHERE size(tok) >= 2
                  AND NOT tok IN ['and','or','the','of','with','from','made','for']
                ] AS itokens
        WHERE size(itokens) >= 1
          AND all(t IN itokens WHERE t IN $fct_tokens)
          AND ((i)-[:HAS_SUBSTITUTION]->() OR (i)-[:HAS_CLASS]->(:FoodOnClass))
        RETURN i.name AS name, itokens
        """,
        {"fct_tokens": fct_tokens},
    )
    if not rows:
        return None

    def _score(row: dict[str, Any]) -> tuple:
        itokens = row["itokens"]
        positions = [fct_tokens.index(t) for t in itokens if t in fct_tokens]
        if not positions:
            return (0, 999, 999, 999)
        first = min(positions)
        span = max(positions) - first
        return (-len(itokens), span, first, len(row["name"]))

    rows.sort(key=_score)
    return rows[0]["name"]


def has_any_substitution_path(name: str) -> bool:
    """True if the ingredient has either MISKG edges or a FoodOn class."""

    # EXISTS-pattern instead of count() over OPTIONAL MATCH: same answer,
    # no AggregationSkippedNull warning spam, and short-circuits on first hit.
    rows = run_query(
        """
        MATCH (i:Ingredient)
        WHERE toLower(i.name) = toLower($name)
        RETURN EXISTS { (i)-[:HAS_SUBSTITUTION]->() } AS has_subs,
               EXISTS { (i)-[:HAS_CLASS]->() } AS has_class
        LIMIT 1
        """,
        {"name": name},
    )
    if not rows:
        return False
    row = rows[0]
    return bool(row.get("has_subs") or row.get("has_class"))


# Minimum mapped flavor-compound count on BOTH ingredients for a Jaccard overlap
# to be trusted. FlavorDB coverage is uneven — sparsely-mapped ingredients
# (e.g. margarine: 4 compounds) produce unreliable similarities, so below this
# floor we return None and let category distance break the tie instead.
_FLAVOR_MIN_COMPOUNDS = 15


def flavor_similarity(name_a: str, name_b: str) -> float | None:
    """Jaccard overlap of two ingredients' FlavorDB flavor-compound sets.

    Maps each Ingredient to its best-matching FlavorDBIngredient (highest
    FLAVORDB_EQUIVALENT cosine) and compares their HAS_FLAVOR_COMPOUND sets.
    Returns a value in [0, 1] (higher = more similar flavor profile), or None
    when either ingredient lacks a FlavorDB mapping or has fewer than
    ``_FLAVOR_MIN_COMPOUNDS`` mapped compounds (signal too sparse to trust).

    Intended only as a tiebreak over an already culinarily-sane candidate set
    (MISKG / FoodOn) — not as a substitute generator.
    """

    rows = run_query(
        """
        MATCH (a:Ingredient) WHERE toLower(a.name) = toLower($a)
        MATCH (a)-[ea:FLAVORDB_EQUIVALENT]->(fa:FlavorDBIngredient)
        WITH fa, ea ORDER BY ea.cosine_similarity DESC LIMIT 1
        MATCH (fa)-[:HAS_FLAVOR_COMPOUND]->(ca:FlavorDBCompound)
        WITH collect(DISTINCT id(ca)) AS aids
        MATCH (b:Ingredient) WHERE toLower(b.name) = toLower($b)
        MATCH (b)-[eb:FLAVORDB_EQUIVALENT]->(fb:FlavorDBIngredient)
        WITH aids, fb, eb ORDER BY eb.cosine_similarity DESC LIMIT 1
        MATCH (fb)-[:HAS_FLAVOR_COMPOUND]->(cb:FlavorDBCompound)
        WITH aids, collect(DISTINCT id(cb)) AS bids
        WITH aids, bids, size([x IN aids WHERE x IN bids]) AS inter
        RETURN size(aids) AS na, size(bids) AS nb, inter,
               (size(aids) + size(bids) - inter) AS uni
        """,
        {"a": name_a, "b": name_b},
    )
    if not rows:
        return None
    r = rows[0]
    na, nb, inter, uni = r.get("na") or 0, r.get("nb") or 0, r.get("inter") or 0, r.get("uni") or 0
    if na < _FLAVOR_MIN_COMPOUNDS or nb < _FLAVOR_MIN_COMPOUNDS or uni <= 0:
        return None
    return inter / uni


def get_ingredient_allergens(name: str) -> list[str]:
    """Return Allergen.name list via HAS_ALLERGEN edges."""

    rows = run_query(
        """
        MATCH (i:Ingredient)
        WHERE toLower(i.name) = toLower($name)
        MATCH (i)-[:HAS_ALLERGEN]->(a:Allergen)
        RETURN DISTINCT a.name AS allergen
        """,
        {"name": name},
    )
    return [r["allergen"] for r in rows if r.get("allergen")]
