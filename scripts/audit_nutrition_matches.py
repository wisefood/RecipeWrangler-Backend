#!/usr/bin/env python3
"""Audit the recipe-ingredient -> composition-table (Chroma) matches.

Samples Ingredient names from Neo4j, runs them through the same Chroma lookup
the profiling pipeline uses (Irish + USDA candidate collections), records the
chosen match and similarity, and flags ones that look wrong. Read-only.

    python3 scripts/audit_nutrition_matches.py --sample 400
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.utils.neo4j_utils import driver  # noqa: E402
from recipe_wrangler.repositories.chroma_matchers import (  # noqa: E402
    query_irish_nutrition_candidates,
    query_usda_nutrition_candidates,
)
from recipe_wrangler.tools.nutritional_calculator import (  # noqa: E402
    _candidate_name,
    _select_usda_match,
    _tokenize,
)
from recipe_wrangler.tools.nutrition_match import best_nutrition_match  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "data/processed/nutrition_match_audit.csv"
LOW_SIM = 0.78  # similarity below this is "weak"
_WORD_RE = re.compile(r"[a-z]+")
_CLEAN_DROP = re.compile(
    r"\([^)]*\)|,.*$|\b(?:fresh|ripe|raw|cooked|chopped|minced|diced|sliced|grated|"
    r"shredded|crushed|ground|peeled|trimmed|drained|rinsed|melted|softened|"
    r"to taste|optional|divided|finely|roughly|thinly|large|small|medium|"
    r"organic|low-fat|nonfat|reduced-fat|skim|whole)\b",
    re.IGNORECASE,
)


def _clean(name: str) -> str:
    s = _CLEAN_DROP.sub(" ", str(name or "").lower())
    return re.sub(r"\s+", " ", s).strip(" -")


def _top_by_distance(cands: list[dict]) -> dict | None:
    cands = [c for c in (cands or []) if isinstance(c, dict)]
    if not cands:
        return None
    return min(cands, key=lambda c: float(c.get("distance") if c.get("distance") is not None else 9))


def _sim(match: dict | None) -> float | None:
    if not match:
        return None
    d = match.get("distance")
    return None if d is None else 1.0 - float(d)


def _flag(query: str, matched: str, sim: float | None) -> str | None:
    q_words = set(_WORD_RE.findall(query.lower()))
    m_words = set(_WORD_RE.findall(matched.lower()))
    q_tok = _tokenize(query)
    m_tok = _tokenize(matched)
    # A query word that appears in the match only *inside a longer word*
    # (egg -> eggplant, butter -> buttermilk, cream -> creamer, flour -> cauliflower).
    for w in q_words:
        if len(w) < 4 or w in m_words:
            continue
        if any(w in mw and mw != w for mw in m_words):
            return f"substring_confusion:{w}->{[mw for mw in m_words if w in mw and mw != w][0]}"
    if q_tok and not (q_tok & m_tok):
        return "no_token_overlap"
    if sim is not None and sim < LOW_SIM:
        return f"weak_similarity:{sim:.2f}"
    if not matched:
        return "empty_match"
    return None


SAMPLE_QUERY = """
MATCH (i:Ingredient)
WITH i, count{ (:Recipe)-[:HAS_INGREDIENT]->(i) } AS freq
WHERE freq >= $min_freq AND size(i.name) <= 60
RETURN i.name AS name, freq
"""


def _old_match(q: str):
    irish_top = _top_by_distance(query_irish_nutrition_candidates(q))
    usda_pick = _select_usda_match(q, query_usda_nutrition_candidates(q))
    cands = [c for c in (irish_top, usda_pick) if c]
    if not cands:
        return None, None, None
    best = min(cands, key=lambda c: float(c.get("distance") if c.get("distance") is not None else 9))
    return _candidate_name(best), _sim(best), ("irish" if best is irish_top else "usda")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=400)
    parser.add_argument("--min-freq", type=int, default=2)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--matcher", choices=["new", "old"], default="new")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    with driver.session() as session:
        pool = [(r["name"], r["freq"]) for r in session.run(SAMPLE_QUERY, min_freq=args.min_freq).data()]
    rng = random.Random(args.seed)
    if 0 < args.sample < len(pool):
        pool = rng.sample(pool, args.sample)
    print(f"sampled ingredient names: {len(pool)}  matcher={args.matcher}")

    rows = []
    flagged = 0
    for raw_name, freq in pool:
        try:
            if args.matcher == "new":
                m = best_nutrition_match(raw_name, "irish")
                cleaned, matched, sim, source, conf = (
                    m["cleaned_query"], m.get("matched_name") or "", m.get("similarity"),
                    m.get("source_key") or "", m.get("confidence"),
                )
            else:
                cleaned = _clean(raw_name) or raw_name.lower()
                matched_, sim_, source_ = _old_match(cleaned)
                matched, sim, source, conf = (matched_ or ""), sim_, (source_ or ""), ""
        except Exception as exc:  # pragma: no cover
            rows.append({"ingredient": raw_name, "freq": freq, "cleaned_query": "", "source": "ERROR",
                         "matched_name": str(exc), "similarity": "", "confidence": "", "flag": "lookup_error"})
            flagged += 1
            continue
        # confidence-aware flag: "none" is itself a flag; "weak" is a softer one
        if conf == "none" or not matched:
            flag = "no_match"
        elif conf == "weak":
            flag = "weak_match" + ((":" + (_flag(cleaned, matched, sim) or "")) if _flag(cleaned, matched, sim) else "")
        else:
            flag = _flag(cleaned, matched, sim) or ""
        if flag:
            flagged += 1
        rows.append({"ingredient": raw_name, "freq": freq, "cleaned_query": cleaned, "source": source,
                     "matched_name": matched, "similarity": "" if sim is None else f"{sim:.3f}",
                     "confidence": conf or "", "flag": flag})

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ingredient", "freq", "cleaned_query", "source",
                                          "matched_name", "similarity", "confidence", "flag"])
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    fc = Counter(r["flag"].split(":")[0] for r in rows if r["flag"])
    cc = Counter(r["confidence"] for r in rows if r["confidence"])
    sims = [float(r["similarity"]) for r in rows if r["similarity"]]
    print(f"total: {len(rows)}  flagged: {flagged} ({100*flagged/max(1,len(rows)):.1f}%)")
    print("flag families:", dict(fc))
    print("confidence:", dict(cc))
    if sims:
        sims.sort(); n = len(sims)
        print(f"similarity (matched only): min={sims[0]:.3f} p25={sims[n//4]:.3f} median={sims[n//2]:.3f} p75={sims[3*n//4]:.3f} max={sims[-1]:.3f}")
    print()
    print("--- sample flagged (ingredient -> matched_name [sim, conf] flag) ---")
    shown = 0
    for r in sorted(rows, key=lambda r: -int(r["freq"])):
        if not r["flag"]:
            continue
        print(f"  {r['ingredient']!r:<42} -> {r['matched_name']!r:<42} [{r['similarity'] or '-'},{r['confidence'] or '-'}] {r['flag']}")
        shown += 1
        if shown >= 70:
            break
    print(f"\noutput: {args.output}")


if __name__ == "__main__":
    main()
