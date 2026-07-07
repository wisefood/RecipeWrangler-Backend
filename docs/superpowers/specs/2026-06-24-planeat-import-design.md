# PLANEAT Import Design

**Date:** 2026-06-24
**Status:** Approved

## Overview

Import 150 PLANEAT (ESSRG T442 Hungary Living Lab) recipes into Neo4j, PostgreSQL, and Elasticsearch. All preprocessing and CoFID-based nutrition profiling is already complete. The import script reads `data/ESSRG/ESSRG_recipes_clean.json` and writes directly to all three databases in a single pass.

## Source Data

| File | Content |
|---|---|
| `data/ESSRG/ESSRG_recipes_clean.json` | 150 recipes, all metadata + CoFID-derived nutrition |
| `PLANEAT T442 MEAL DB LL ESSRG.xlsx` | Original source (already fully extracted) |

Key properties of the source data:
- All ingredients are canonical CoFID food names with pre-resolved gram weights — no LLM parsing needed
- `nutrition` field on each recipe has CoFID-derived totals + per-serving values already computed
- `serves` and `duration` are LLM-estimated (Qwen-14b); `serves_source = 'llm_estimate'` for all
- 1 recipe has 0 ingredients: "Potato and white bean salad" — handled as an edge case
- 149/150 have instructions (per-dish cooking steps concatenated from Dishes sheet)
- No cost data exists in the source

## Script

**`scripts/import_planeat.py`**

Single script, one pass through all 150 recipes. Resume-safe via checkpoint file.

### Checkpoint

`scripts/import_planeat.checkpoint.json` — keyed by `recipe_id`. On restart, already-committed recipes are skipped.

### Processing Order Per Recipe

1. Detect allergens from ingredient names using existing allergen detection
2. Compute Nutri-Score from `nutrition` field using `compute_nutri_score_breakdown_from_values`
3. Upsert Neo4j Recipe node + Ingredient nodes + `HAS_INGREDIENT` relationships
4. Write Postgres profile row
5. Index Elasticsearch document
6. Write checkpoint entry

## Database Schema

### Neo4j — Recipe node

`source = 'PLANEAT'`

| Property | Source field | Notes |
|---|---|---|
| `recipe_id` | `recipe_id` | e.g. `ESSRG_1` |
| `title` | `title` | |
| `source` | — | hardcoded `'PLANEAT'` |
| `source_id` | `source_id` | integer from XLSX |
| `description` | `description` | |
| `serves` | `serves` | LLM-estimated |
| `duration` | `duration` | LLM-estimated minutes |
| `meal_type` | `meal_type` | e.g. `breakfast`, `lunch`, `evening meal` |
| `animal_product_category` | `animal_product_category` | e.g. `egg`, `plant based`, `meat` |
| `seasonality` | `seasonality` | list: `['autumn', 'winter', ...]` |
| `dish_types` | `dish_types` | list: `['breakfast']` |
| `tags` | `tags` | list of `key:value` strings |
| `allergens` | detected at import | from ingredient names |
| `url` | — | `null` |
| `image_url` | — | `null` |
| `has_planeat_nutrition` | — | `true` |
| `expert_recipe` | — | `true` (structured dataset) |

**Ingredient nodes:** upserted by canonical CoFID food name. Linked via `HAS_INGREDIENT` relationship with property `weight_g`.

**Instructions:** stored as a `instructions` property on the Recipe node (list of per-dish instruction strings joined with `\n\n`).

### PostgreSQL — `nutrients-recipe-profiles`

One row per recipe.

| Column | Value |
|---|---|
| `recipe_id` | from JSON |
| `title` | from JSON |
| `source` | `'PLANEAT'` |
| `nutrition_source` | `'planeat'` |
| `pipeline_version` | `'cofid_direct'` |
| `total_nutrients` | JSONB: `{energy_kcal, protein_g, fat_g, saturated_fat_g, carbohydrate_g, sugar_g, fibre_g, sodium_mg}` — per-recipe totals |
| `total_nutrients_per_serving` | JSONB: same keys, divided by serves |
| `nutri_score` | JSONB: `{grade, score}` |
| `nutri_score_breakdown` | JSONB: full breakdown from `compute_nutri_score_breakdown_from_values` |
| `nutrition_profiling_details` | JSONB: per-ingredient list from `ingredient_details` (name, weight_g, CoFID ID, component) |
| `nutrition_profiling_debug` | JSONB: coverage stats, calculation method, serves/duration sources, LLM estimation metadata |

**Nutrient field mapping** — `nutrition` field in JSON uses `fibre_g` and `sugar_g`; Postgres JSONB uses the same names used by all other profiles in the system (`fibre_g`, `sugar_g`). No renaming needed.

**Edge case:** "Potato and white bean salad" — `total_nutrients = null`, `nutri_score = null`, `nutrition_profiling_debug` records reason (`zero_ingredients`).

### Elasticsearch — `recipes_v2`

One document per recipe, following the existing index mapping.

| Field | Value |
|---|---|
| `recipe_id` | from JSON |
| `title` | from JSON |
| `source` | `'PLANEAT'` |
| `serves` | from JSON |
| `duration` | from JSON |
| `meal_type` | from JSON |
| `animal_product_category` | from JSON |
| `seasonality` | from JSON (list) |
| `dish_types` | from JSON (list) |
| `tags` | from JSON (list) |
| `allergens` | detected at import |
| `has_planeat_nutrition` | `true` |
| `nutri_score_planeat` | grade string e.g. `'B'` |
| `cost_category` | `null` (no cost data) |

## Allergen Detection

The clean JSON has `allergens: []` for all recipes — not inferred from ingredients. The import script calls the existing allergen detection logic on each recipe's ingredient names, populating Neo4j and ES. The same approach used for SafeFood web.

## Nutri-Score Computation

Calls `compute_nutri_score_breakdown_from_values` with:
- `energy_kcal`, `protein_g`, `fat_g`, `saturated_fat_g`, `carbohydrate_g`, `sugar_g`, `fibre_g`, `sodium_mg`
- `fruits_veg_legumes_pct = 0` (no fruit/veg/legume percentage available from source)

Result stored in both Postgres and ES.

## Error Handling

- Per-recipe try/except: a failure on one recipe logs and continues; the checkpoint is not written for failed recipes so they retry on resume
- Existing records are upserted (not duplicated) — safe to re-run
- The script prints a summary at the end: imported, skipped (checkpoint), failed

## What This Does Not Do

- Does not generate images (no image source available)
- Does not run pipeline profiles for other regions (usda, irish, hungarian, eu) — those are a separate future step if cross-region comparison of PLANEAT is needed
- Does not modify `ESSRG_recipes_clean.json` or any existing output files
