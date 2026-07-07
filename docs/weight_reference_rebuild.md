# Weight Reference Rebuild

This pipeline rebuilds an offline ingredient weight reference dataset for all
loaded recipes, instead of relying on live USDA matching at runtime.

## Stages

1. Export normalized ingredient-unit signatures from Neo4j.
2. Build deterministic candidate weights with the existing weight tool.
3. Filter to `candidate_status == "needs_llm_rebuild"` only — the rows where the
   deterministic cascade had to fall back to a live LLM.
4. Ask the LLM to judge or correct those candidates, then run a verifier on each
   output (rejects suspicious-default weights, `> 5 kg` per unit, deterministic
   verdicts with no parseable candidate, …). Output carries `verifier_status` /
   `verifier_reason`; rejected rows become `verdict="uncertain"` with empty weight.
5. Materialize the final reference dataset for runtime lookup.
6. Import the dataset into the `pipeline_static_data` Postgres table — the
   runtime reads static data from Postgres, not disk.

## Commands

```bash
PYTHONPATH=src uv run python preprocessing/one_off/export_weight_reference_signatures.py

PYTHONPATH=src uv run python preprocessing/one_off/build_weight_reference_candidates.py

# keep only the needs_llm_rebuild rows for the judge (~1.8k of ~46k)
PYTHONPATH=src uv run python - <<'PY'
import csv
src = "data/processed/weight_reference/ingredient_unit_candidates.csv"
dst = "data/processed/weight_reference/ingredient_unit_candidates.needs_llm.csv"
with open(src) as f, open(dst, "w", newline="") as g:
    r = csv.DictReader(f); w = csv.DictWriter(g, fieldnames=r.fieldnames); w.writeheader()
    for row in r:
        if row.get("candidate_status") == "needs_llm_rebuild":
            w.writerow(row)
PY

PYTHONPATH=src uv run python preprocessing/one_off/judge_weight_reference_candidates_vllm.py \
  --base-url http://localhost:8008/v1 \
  --model ingredient-tagger

PYTHONPATH=src uv run python preprocessing/one_off/materialize_weight_reference_dataset.py

PYTHONPATH=src uv run python scripts/postgres/import_pipeline_static_data.py
```

## Outputs

- `data/processed/weight_reference/ingredient_unit_signatures.csv`
  - one row per normalized `(ingredient, unit)` pair observed in Neo4j
- `data/processed/weight_reference/ingredient_unit_candidates.csv`
  - deterministic USDA/common-unit candidates and provenance
- `data/processed/weight_reference/ingredient_unit_llm_reviews.csv`
  - LLM verdicts, corrected weights, and `verifier_status` / `verifier_reason`
- `data/processed/weight_reference/ingredient_unit_reference_dataset.csv`
  - final offline reference dataset; also stored in `pipeline_static_data`
    under `ingredient_unit_reference_dataset`

## Notes

- The signature export is graph-backed, so it covers the recipes currently
  loaded in Neo4j, not just one source snapshot.
- The deterministic stage intentionally marks any LLM-based runtime fallback as
  `needs_llm_rebuild`; those rows are not treated as final deterministic truth.
- **Wired into the runtime as of 2026-05-11**: `ingredient_weight_tool.py`
  `_lookup_offline_reference` short-circuits the cascade before the live USDA /
  embedding lookup with `match_type="offline_reference_dataset"`. Only
  `accepted_deterministic` rows and `llm_rebuilt` rows with confidence ≥ 0.7
  (`OFFLINE_REFERENCE_MIN_CONFIDENCE`) are used; confidence is 0.90 for
  deterministic and 0.70–0.88 for LLM-rebuilt. Disable with
  `OFFLINE_REFERENCE_DATASET_ENABLED=false`.
- The runtime loads it from Postgres (`_csv_rows_from_path_or_pg` ignores the
  path arg and always reads `pipeline_static_data`), so re-run
  `scripts/postgres/import_pipeline_static_data.py` after regenerating the CSV
  or the change has no runtime effect.
