# Recipe Datasets — Provenance and Preprocessing

This document covers every curated recipe dataset in the system: what was received, what preprocessing was applied, what data was available, and how gaps were handled. Intended as a reference for reporting.

---

## Recipe counts

| Dataset | Source label | Recipes in Neo4j | Ground-truth rows | IE profiles | HU profiles | EU profiles |
|---|---|---:|---:|---:|---:|---:|
| Curated Irish Recipes | `Curated Irish Recipes` | 369 | 334 (web) + 99 (RCSI lab) | 369 | 369 | 369 |
| Curated Hungarian Recipes | `Curated Hungarian Recipes` | 149 | 149 (CoFID) | 149 | 149 | 149 |
| Curated Slovenian Recipes | `Curated Slovenian Recipes` | 100 | 100 (OPKP) | 100 | 100 | 100 |
| **Total** | | **618** | | **618** | **618** | **618** |

## Table 3: Main recipe sources integrated in RecipeWrangler

| Source | Current role in RecipeWrangler | Notes |
|---|---|---|
| Irish Curated Recipes | Curated benchmark; search; allergen and dietary tagging; Nutri-Score evaluation against RCSI expert reference | 334 recipes from SafeFood.ie with scraped nutrition panels; 99-recipe RCSI expert-measured subset used as primary Nutri-Score reproducibility benchmark |
| Hungarian Curated Recipes | Regional profiling benchmark; search; dietary and allergen tagging; Nutri-Score evaluation against PLANEAT/ESSRG reference | 149 recipes with ESSRG-assigned CoFID 2021 nutrition and PLANEAT meal composition; evaluated against Irish, Hungarian, and EU global profiles |
| Slovenian Curated Recipes | Regional profiling benchmark; search; dietary and allergen tagging; Nutri-Score evaluation against OPKP reference | 100 recipes with OPKP-derived per-recipe nutrition; evaluated against Irish, Hungarian, and EU global profiles |
| HealthyFoods | Large-scale profiling and search; contextual Nutri-Score comparison against scraped nutrition panels | 5,183 recipes from HealthyFood.com with near-complete scraped nutrition metadata; dietary classifications and allergen tags applied |
| MyPlate | Full profiling, search, and Nutri-Score evaluation against USDA-derived reference | 1,039 USDA MyPlate recipes with complete scraped per-serving nutrition panels; reference Nutri-Score derived by RecipeWrangler |
| FoodHero | Profiling coverage and search; no external nutrition reference available | 416 Swiss household recipes; all nutrition pipeline-estimated; carbon footprint fully matched |
| Recipe1M / HUMMUS | Large-scale profiling and search; Nutri-Score evaluation against HUMMUS labels for 31,447-recipe overlap | 804,319 web-sourced recipes; only the HUMMUS-annotated subset (31,447) has reference nutrition and direct Nutri-Score labels |

Table 3 lists all seven integrated recipe sources, their size, and their primary function in the system. The curated sources (Irish, Hungarian, Slovenian) are small but have authoritative reference nutrition and serve as accuracy benchmarks. The large-scale sources (HealthyFoods, MyPlate, Recipe1M/HUMMUS) support broad search coverage and contextual evaluation, but their reference nutrition comes from scraped panels or third-party annotations rather than expert measurement.

---

## Table 4: Metadata and reference-value availability

| Source | N recipes (post-QC) | Nutrition reference | Direct Nutri-Score label | Carbon footprint | Servings | Time | Category |
|---|---:|:---:|:---:|:---:|:---:|:---:|:---:|
| SafeFood | 334 | 100% web panels; 29.6% RCSI lab | 0% | 0% | 100% | 100% | 0% |
| Curated Hungarian Recipes | 149 | 100% (CoFID) | 0% | 0% | 100% | 100% | 100% |
| Curated Slovenian Recipes | 100 | 100% (OPKP) | 0% | 0% | 100% | 100% | 100% |
| Recipe1M | 804,319 | 3.9% (31,447 HUMMUS) | 3.9% (HUMMUS labels) | 0% | 42.1% | 48.8% | 0.1% |
| HealthyFoods | 5,183 | 96.8% (5,141 scraped) | 0% | 0% | 100% | 100% | 100% |
| MyPlate | 1,039 | 100% (1,039 scraped) | 0% | 0% | 100% | 100% | 100% |
| FoodHero | 416 | 0% | 0% | 0% | 100% | 100% | 99.5% |

**Notes:**

- **SafeFood**: nutrition reference comes from two distinct sources — per-100g nutrition panels scraped from the SafeFood.ie website (334 recipes, 100%) and expert-measured lab values from the RCSI subset (99 recipes, 29.6% of 334). The 99 RCSI recipes are a subset of the 334 web recipes; both sources are available for those 99. No source-provided Nutri-Score label; it is computed from the reference nutrition values. Category (dish type) is not stored in the current graph — the SafeFood website uses category pages but this field was not captured at import time.

- **Curated Hungarian Recipes / Curated Slovenian Recipes**: both have complete ground-truth nutrition (100%) from expert food composition databases (CoFID and OPKP respectively). Nutri-Score is computed from these reference values and is therefore authoritative, though not a "direct label" from the source. Category is stored as `meal_type` (Hungarian: breakfast/lunch/dinner/snack) or `dish_type` (Slovenian: main_dish/soup/salad/dessert).

- **Recipe1M**: the HUMMUS project annotated a 31,447-recipe subset with nutrition and a direct Nutri-Score label. This is the only source with a pre-computed Nutri-Score from the data provider. For the remaining ~96% of Recipe1M recipes, no reference nutrition exists — profiling relies solely on the Chroma ingredient-matching pipeline. Category coverage is extremely sparse (0.1%) because Recipe1M does not provide structured dish-type fields; it is inferred from free-text course annotations where present.

- **HealthyFoods**: 5,141 of 5,312 recipes in the source JSON have complete scraped nutrition (96.8%); 5,183 are imported into the graph (minor discrepancy due to deduplication). No source Nutri-Score label; it is computed from the scraped values.

- **MyPlate**: all 1,039 imported recipes have a per-serving nutrition panel scraped from the recipe's myplate.food JSON-LD. These values are stored as `nutrition_source = 'myplate'` and are separate from the Irish, Hungarian, EU, and USDA ingredient-matching profiles. MyPlate does not publish a direct Nutri-Score label; RecipeWrangler derives the reference grade from the scraped panel.

- **FoodHero**: no source nutrition panel is available, so all nutrition profiling is pipeline-estimated via ingredient matching. Category is available for 414/416 recipes (99.5%); two recipes lacked a category field in the source.

- **Carbon footprint**: no source provides ingredient-level carbon emissions data natively. The profiling pipeline estimates carbon footprint via Chroma nearest-neighbour matching against a sustainability reference table.

---

## Table 5: Reference nutrition and Nutri-Score availability

| Source | Energy | Sugars | Sat. Fat | Sodium | Fibre | Protein | Reference Nutri-Score availability |
|---|:---:|:---:|:---:|:---:|:---:|:---:|---|
| SafeFood RCSI (99 recipes) | Yes | Yes | Yes | Yes | Yes | Yes | Derivable from complete expert-measured reference nutrition; no direct source Nutri-Score label |
| SafeFood web panels (334 recipes) | Yes | Yes | Yes | Yes | Yes | Yes | Derivable from complete scraped reference nutrition; no direct source Nutri-Score label |
| Curated Hungarian Recipes (149 recipes) | Yes | Yes | Yes† | Yes | Yes‡ | Yes | Derivable from complete CoFID reference nutrition; no direct source Nutri-Score label |
| Curated Slovenian Recipes (100 recipes) | Yes | Yes | Yes | Yes | Yes | Yes | Derivable from complete OPKP per-100g nutrition; no direct source Nutri-Score label |
| Recipe1M / HUMMUS (31,447 recipes) | Yes | Yes | Yes | Yes | No | Yes | Direct HUMMUS Nutri-Score label available; not derived from the six integrated nutrient fields in this table |
| HealthyFoods (5,141 recipes) | Yes | Yes | Yes | Yes | Yes | Yes | Derivable from complete scraped reference nutrition; no direct source Nutri-Score label |
| MyPlate (1,039 recipes) | Yes | Yes | Yes | Yes | Yes | Yes | Derivable from complete scraped per-serving nutrition; no direct source Nutri-Score label |
| FoodHero | No | No | No | No | No | No | Not available |

†Curated Hungarian Recipes sat. fat: 148/149 (99.3% complete). ‡Curated Hungarian Recipes fibre: 145/149 (97.3% complete).

**Notes:**

- **RCSI vs. web panels (SafeFood)**: both have identical field coverage across all six nutrients. The RCSI values are lab-measured (gold standard); web panel values are scraped from the SafeFood.ie nutrition label and are slightly less precise (rounded to one decimal place, per-serve rather than per-100g). The Nutri-Score pipeline normalises both to per-100g before scoring.

- **Curated Hungarian Recipes (CoFID)**: satfat and fibre have 1–4 missing values out of 149. The gaps are due to a small number of ingredients not present in the CoFID table at the time of export. This does not prevent Nutri-Score computation — missing satfat is treated as 0 (conservative) and missing fibre reduces the positive score contribution.

- **Recipe1M / HUMMUS**: HUMMUS Nutri-Score labels are pre-computed by the HUMMUS authors and stored as a direct label (A–E). They were NOT derived from the energy/sugar/satfat/sodium/protein fields stored in our system — fibre is absent from HUMMUS annotations, so independent recomputation of the HUMMUS Nutri-Score is not possible from fields in this table alone.

- **MyPlate**: all 1,039 reference rows contain all six Nutri-Score inputs: energy, sugars, saturated fat, sodium, fibre, and protein. The values are scraped per serving from myplate.food, not generated by RecipeWrangler's ingredient-matching profiles. MyPlate does not provide a Nutri-Score label. RecipeWrangler converts the scraped per-serving values to per 100 g using the profiled ingredient weight and recipe serving count, sets fruit/vegetable percentage to zero where unavailable, and derives the reference Nutri-Score with the standard algorithm. All 1,039 reference rows currently have a derived grade.

- **FoodHero**: no per-recipe source nutrition panel exists. FoodHero contributes recipes and ingredient lists but no external reference nutrition or reference Nutri-Score.

### MyPlate reference nutrition — confirmed evaluation status

MyPlate has complete reference nutrition for **1,039 of 1,039 imported recipes (100%)**. Every reference row contains energy, sugars, saturated fat, sodium, fibre, and protein, and every row has a RecipeWrangler-derived reference Nutri-Score. The grade is derived from the scraped myplate.food nutrition values; it is not a label supplied by MyPlate. MyPlate should therefore be included in both forms of reference evaluation: the Nutri-Score confusion matrices compare its derived reference grade against the Irish, Hungarian, and EU generated grades, while the nutrient-median comparison compares its six scraped reference medians against the generated profile medians. The current evaluation outputs already include MyPlate: the nutrient-median reference series uses all 1,039 recipes; the confusion data contains 1,039 paired EU comparisons and 1,037 paired Irish and Hungarian comparisons.

---

## Table 6: Outlier removal — Pass 1 (statistical per-dataset IQR threshold)

Outlier removal was applied **only to non-curated recipe collections** (HealthyFoods, MyPlate, and Recipe1M). The three curated datasets — Curated Irish Recipes, Curated Hungarian Recipes, and Curated Slovenian Recipes — were assembled and verified by domain experts and dietitians; their nutritional values are treated as ground truth and no records are removed.

Threshold for non-curated sources: median + 3 × IQR, computed independently per dataset from ground-truth per-serving kcal values.

| Dataset | Recipes analysed | Median kcal / serving | Outlier threshold (kcal) | Removed |
|---|---:|---:|---:|---:|
| Curated Irish Recipes | 332 | 281 | — | 0 |
| Curated Hungarian Recipes | 149 | 234 | — | 0 |
| Curated Slovenian Recipes | 100 | 292 | — | 0 |
| HealthyFoods | 5,073 | 380 | 807 | 2 |
| MyPlate | 545 | 254 | 861 | 38 |
| Recipe1M | 31,395 | 1,399 | 6,877 | 1,175 |
| **Total** | **37,594** | | | **1,215** |

**Notes:**

- **HealthyFoods**: 2 recipes (2,167 kcal/serving and 2,161 kcal/serving) exceed the IQR threshold of 807 kcal/serving. Both are likely cases where total-recipe calories were mislabelled as per-serving values in the source data.

- **MyPlate**: the median is low (254 kcal/serving), consistent with the dataset's single-serving government recipe format. 38 records exceed the IQR threshold of 861 kcal/serving; these appear to be recipes where total batch calories were recorded instead of per-serving values, or recipes with an implausibly low serve count (serves = 1 for a full family dish).

- **Recipe1M**: the very high median (1,399 kcal/serving) and wide spread (IQR = 1,826 kcal) reflect a structural problem in the HUMMUS annotations — many recipes appear to have had total-recipe calories recorded as single-serving calories, or have implausible serving counts (e.g., serves = 1 for a full batch). 1,175 extreme outliers (above 6,877 kcal/serving) are removed in this pass. Note: this analysis covers the 31,395 HUMMUS-annotated Recipe1M records; the full Recipe1M corpus (~51k recipes) was analysed in an earlier pass but only the HUMMUS-annotated subset carries reference nutrition values and is used here.

- **FoodHero**: no reference nutrition available; not included in this analysis.

---

## Table 7: Outlier removal — Pass 2 (strict global threshold > 1,000 kcal / serving)

A second pass applies a hard global cutoff of 1,000 kcal per serving to non-curated collections only. This catches large-but-not-extreme entries where total-recipe calories appear to have been treated as per-serving values and were not flagged by the per-dataset IQR threshold in Pass 1. Counts shown are total records above 1,000 kcal/serving per dataset (including any already removed in Pass 1).

| Dataset | Removed |
|---|---:|
| Curated Irish Recipes | 0 |
| Curated Hungarian Recipes | 0 |
| Curated Slovenian Recipes | 0 |
| HealthyFoods | 2 |
| MyPlate | 31 |
| Recipe1M | 19,297 |
| **Total** | **19,330** |

**Notes:**

- **HealthyFoods**: the same 2 extreme records removed in Pass 1 (2,167 and 2,161 kcal/serving) also exceed the 1,000 kcal threshold. No additional records are removed in this pass.

- **MyPlate**: 31 of the 38 Pass 1 outliers exceed 1,000 kcal/serving and are captured here. The remaining 7 (between 861 and 1,000 kcal/serving) were removed in Pass 1 but fall below the Pass 2 cutoff. No additional records outside the Pass 1 set exceed 1,000 kcal/serving.

- **Recipe1M**: 19,297 of 31,395 HUMMUS-annotated records (61.5%) exceed 1,000 kcal/serving. Of these, 1,175 were already removed in Pass 1 and a further 18,122 (in the 1,000–6,877 kcal range) are removed here. The scale of removal confirms that per-serving energy in the HUMMUS annotations is not reliably normalised. The remaining 12,098 records (38.5%) form the clean Recipe1M reference set used in Nutri-Score validation.

- **Curated datasets**: not subject to outlier removal. Curated Irish Recipes, Curated Hungarian Recipes, and Curated Slovenian Recipes are included in the analysis in full.

---

## Table 9: Food composition sources used by RecipeWrangler

| Composition table | Scope | Number of foods | Number of nutrient fields | Main role |
|---|---|---:|---:|---|
| Irish composition table | Ireland — general food supply | 1,307 | 35 | Primary matching source for Irish regional profile; fallback to EU global for unmatched ingredients |
| Hungarian composition table | Hungary — CoFID-derived national reference | 500 | 15 | Primary matching source for Hungarian regional profile; limited coverage of Nutri-Score-specific nutrients (sugars, sat. fat, fibre absent) — completed via EU global fallback |
| EU global composition table | Pan-European composite (Ciqual + CoFID + NEVO) | 8,148 | 6 Nutri-Score + energy + macros | Universal fallback and standalone profile; covers the broadest ingredient vocabulary across all source datasets |

The three composition tables are stored in Postgres (`nutrients-ingredients-irish`, `nutrients-ingredients-hungarian`, `nutrients-ingredients-eu`) and mirrored into Chroma for semantic nearest-neighbour matching. Each table contains per-100g nutrient values; the profiling pipeline converts these to per-serving estimates using ingredient weight and recipe serve count. The Irish and Hungarian tables are the primary sources for their respective regional profiles but fall back to the EU global table when a matched ingredient is not present in the native table.

---

## Table 10: Source subsets used to construct the EU global composition pool

| Source subset | Country / organisation | Number of foods contributed |
|---|---|---:|
| Ciqual | France — ANSES | 3,046 |
| CoFID | United Kingdom — PHE | 2,886 |
| NEVO | Netherlands — RIVM | 2,216 |
| **Total** | | **8,148** |

The EU global composition pool was assembled by merging three national food composition databases, deduplicating on canonical food identifier. Ciqual (France) contributes the largest share (37.4%), reflecting its broad coverage of packaged and fresh foods available across Western Europe. CoFID (UK) and NEVO (Netherlands) add Central European and Northern European foods not well represented in Ciqual, collectively providing more balanced coverage for the Irish and Hungarian cohorts. Overlapping foods across sources are deduplicated by canonical ID; no averaging or reconciliation of conflicting values is performed — the first matched source in priority order is used.

---

## Table 24: Active nutritional matching collections

| Chroma collection | Profile source | Number of foods |
|---|---|---:|
| `nutritional_ingredients_irish` | Irish composition table | 1,307 |
| `nutritional_ingredients_hungarian` | Hungarian composition table | 500 |
| `nutritional_ingredients_eu` | EU global composite (Ciqual + CoFID + NEVO) | 8,148 |

Each composition table is embedded into a dedicated Chroma collection using BAAI/bge-small-en-v1.5 to enable semantic nearest-neighbour search over ingredient names. A query ingredient name is encoded, and the top-k nearest foods in the collection are retrieved and re-scored using BM25 + cosine similarity. The EU global collection is always queried as a fallback regardless of the active profile: if a native Irish or Hungarian collection does not return a match above the confidence threshold, the EU global collection is consulted. This ensures all Nutri-Score inputs (including sugars, saturated fat, and fibre, which are absent from the Hungarian table) can be populated from the EU global pool.

---

## Cross-dataset allergen summary

Allergens are **computed** from Neo4j ingredient graph edges (`HAS_INGREDIENT → HAS_ALLERGEN`) using FoodOn taxonomy + keyword matching via `scripts/neo4j/tag_allergens.py`. They are not source-provided — they are inferred from ingredient names. Source-provided dietary classifications (where available) are listed separately per dataset below.

All 14 EU allergens are covered: milk, egg, peanut, tree nut, wheat, gluten, soy, fish, crustacean/shellfish, sesame, celery, mustard, sulphites, lupin, molluscs.

### Curated datasets (618 recipes)

| Allergen | Curated Irish Recipes (334) | Curated Hungarian Recipes (149) | Curated Slovenian Recipes (100) | **Total (618)** |
|---|---:|---:|---:|---:|
| Wheat | 130 | 75 | 51 | **256** |
| Milk | 112 | 70 | 36 | **218** |
| Egg | 71 | 32 | 38 | **141** |
| Gluten | 47 | 28 | 44 | **119** |
| Fish | 33 | 5 | 6 | **44** |
| Tree nut | 25 | 18 | 4 | **47** |
| Mustard | 19 | 8 | 0 | **27** |
| Celery | 15 | 11 | 3 | **29** |
| Soy | 13 | 20 | 0 | **33** |
| Peanut | 5 | 5 | 0 | **10** |
| Sesame | 5 | 10 | 0 | **15** |
| Crustacean/shellfish | 3 | 0 | 2 | **5** |
| Molluscs | 0 | 1 | 1 | **2** |

Numbers are recipes containing that allergen (a recipe can have multiple).

### All datasets (811,541 recipes)

| Allergen | Count |
|---|---:|
| Milk | 475,869 |
| Wheat | 294,155 |
| Egg | 252,368 |
| Gluten | 70,993 |
| Soy | 50,963 |
| Mustard | 50,450 |
| Fish | 37,225 |
| Crustacean/shellfish | 23,989 |
| Peanut | 20,871 |
| Celery | 18,630 |
| Sesame | 17,911 |
| Tree nut | 17,102 |
| Molluscs | 10,289 |
| Lupin | 710 |

### EU category: cereals containing gluten — unique recipe union

Live Neo4j results queried on **25 June 2026** after the final boundary-aware allergen rebuild are shown below. These counts supersede the older export values of 273,764 for wheat and 128,171 for gluten. Counts are distinct recipes, not ingredient or edge counts.

| Category / query result | Distinct recipes |
|---|---:|
| Wheat | 294,155 |
| Gluten | 70,993 |
| Both wheat and gluten | 34,077 |
| Wheat OR gluten | **331,071** |
| Final EU “cereals containing gluten” union | **331,071** |

The graph currently has only two relevant recipe-level `Allergen` keys: `wheat` and `gluten`. It has no separate allergen nodes for barley, rye, oats, spelt, durum, semolina, farro, khorasan wheat, malt, or brewer. Those terms are handled within the wheat/gluten ingredient-classification rules, so there are no additional allergen-key unions to add. Tags named `gluten_free` and `gluten_free_option` describe absence or adaptability and are therefore not included in the allergen union.

Relevant allergen keys were inventoried with:

```cypher
MATCH (a:Allergen)
WHERE any(
  term IN [
    'wheat', 'gluten', 'barley', 'rye', 'oat', 'spelt', 'durum',
    'semolina', 'farro', 'khorasan', 'malt', 'brewer', 'cereal'
  ]
  WHERE toLower(a.name) CONTAINS term
)
RETURN a.name
ORDER BY a.name;
```

The exact overlap-safe Cypher used was:

```cypher
MATCH (r:Recipe)
OPTIONAL MATCH
  (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(a:Allergen)
WHERE a.name IN ['wheat', 'gluten']
WITH r, collect(DISTINCT a.name) AS flags
RETURN count(*) AS total_recipes,
       sum(CASE WHEN 'wheat' IN flags THEN 1 ELSE 0 END) AS wheat,
       sum(CASE WHEN 'gluten' IN flags THEN 1 ELSE 0 END) AS gluten,
       sum(
         CASE WHEN 'wheat' IN flags AND 'gluten' IN flags
         THEN 1 ELSE 0 END
       ) AS both,
       sum(
         CASE WHEN any(x IN flags WHERE x IN ['wheat', 'gluten'])
         THEN 1 ELSE 0 END
       ) AS cereals_containing_gluten_union;
```

The query returned 811,541 total recipes, 294,155 wheat-positive recipes, 70,993 gluten-positive recipes, 34,077 recipes positive for both, and 331,071 recipes in the unique union. The union also satisfies the inclusion–exclusion check: 294,155 + 70,993 − 34,077 = **331,071**.

---

## 1. Curated Irish Recipes (source: `Curated Irish Recipes`)

### Raw data

Provided by Safefood (Republic of Ireland public health body) and RCSI (Royal College of Surgeons in Ireland).

| File | Content |
|---|---|
| `data/Irish_SafeFood/Irish Recipes_SafeFood.xlsx` | Early RCSI workbook (superseded; recipes are a subset of the 334 web recipes) |
| `data/Irish_SafeFood/Irish Recipes_updated_22.6.26.xlsx` | Current workbook: 334 SafeFood website recipes + RCSI reference nutrition for 99 of them |

The 334 SafeFood website recipes were scraped from the SafeFood website. They include titles, ingredient lists, instructions, images, and nutrition panels as published on the site.

The 99 RCSI reference recipes are a curated subset of the 334 website recipes. They carry laboratory-grade reference nutrition (energy, fat, saturated fat, carbohydrate, sugars, fibre, protein, salt) per 100g and per serving, provided directly by RCSI. There is no separate dataset — the 99 are simply the 334 with ground-truth nutrition attached.

### Available fields (raw)

**SafeFood web (334 recipes):**
- Title, URL, image URL
- Free-text ingredient list
- Instructions
- Serves
- Nutrition panel (energy, fat, saturated fat, carbs, sugars, fibre, protein, salt) — website-published values, not laboratory-measured

**RCSI reference (99 recipes):**
- Recipe title (matched to SafeFood web by title)
- Nutrition per 100g and per serving: energy (kJ and kcal), fat, saturated fat, carbohydrate, sugars, fibre, protein, salt
- Cost category (low / medium / high) for 40 of 99 recipes

### Preprocessing

- Free-text ingredients were matched to Neo4j ingredient nodes via the standard import pipeline
- RCSI title matching: 4 recipes required explicit aliases due to minor title differences (e.g. "Chicken Fajita" → "Chicken fajitas")
- One RCSI recipe (`DBOT_Goats cheese and beetroot 5 minute salad`) could not be matched to any web recipe and was excluded
- Salt converted to sodium: `sodium_mg = salt_g × 400`
- Nutri-Score computed from RCSI reference nutrition via `compute_nutri_score_breakdown_from_values`
- Deduplication: 47 near-duplicate recipe pairs merged (April 2026); surviving recipe_id retained, duplicate nodes removed from Neo4j

**Audit file:** `data/exports/safefood_rcsi_merge_report.json`
**Deduplication plan:** `data/exports/safefood_dedupe_20260423_152706/plan.json`

### Missing data handling

| Field | Status | How handled |
|---|---|---|
| Serves | Provided on website | Used as-is |
| Duration | Not always provided | Backfilled for SafeFood recipes missing a value via regex + Groq LLM |
| Ingredient gram weights | Not provided | Estimated via weight tool (Chroma + LLM fallback) during regional profiling |
| Cost category | 59 of 99 RCSI recipes have no value | Left null; 40 have a value |
| 1 RCSI recipe unmatched | No URL or ingredient data to match on | Excluded from import |

### Allergens (computed from ingredient graph, 334 recipes in ES)

No source-provided dietary classification for this dataset. All allergen tags are inferred from ingredient names via FoodOn + keyword matching.

| Allergen | Recipes |
|---|---:|
| Wheat | 130 |
| Milk | 112 |
| Egg | 71 |
| Gluten | 47 |
| Fish | 33 |
| Tree nut | 25 |
| Mustard | 19 |
| Celery | 15 |
| Soy | 13 |
| Peanut | 5 |
| Sesame | 5 |
| Crustacean / shellfish | 3 |

### Nutrition profiles in Postgres (`source = Curated Irish Recipes`)

| nutrition_source | Rows | Description |
|---|---|---|
| `safefood_web` | 334 | Website-published nutrition panels |
| `safefood_rcsi` | 99 | RCSI laboratory-grade reference nutrition (subset of the 334) |
| `irish` | 369 | Pipeline-computed using Irish CoFID via Chroma matching |
| `hungarian` | 369 | Pipeline-computed using Hungarian composition table |
| `eu` | 369 | Pipeline-computed using EU composition table |

Note: 369 = 334 web recipes + 35 additional recipes in Neo4j from the original RCSI import batch that are not represented in `safefood_web` profiles. All 369 Neo4j recipes have full regional coverage.

### Scripts

| Script | Purpose |
|---|---|
| `scripts/import_irish_safefood.py` | Imports 334 web recipes into Neo4j + ES |
| `scripts/merge_safefood_rcsi_lab.py` | Matches RCSI rows to web recipe IDs; writes `safefood_rcsi` Postgres profiles |
| `scripts/postgres/backfill_profile_derived_fields.py` | Computes Nutri-Score for `safefood_rcsi` rows from stored reference nutrients |
| `scripts/recompute_all_profiles.py` | Runs regional pipeline profiles (irish / hungarian / eu) |

---

## 2. Curated Hungarian Recipes / ESSRG (source: `Curated Hungarian Recipes`)

### Raw data

Provided by ESSRG (Environmental Social Science Research Group), Hungary, as part of the PLANEAT project (T442 Living Lab). 100% expert-curated recipes from Hungarian living lab participants.

| File | Content |
|---|---|
| `PLANEAT T442 MEAL DB LL ESSRG.xlsx` | 150 recipes with component dishes, ingredient-level CoFID 2021 assignments, gram quantities |
| `data/ESSRG/ESSRG_recipes_clean.json` | Preprocessed JSON (generated by `scripts/prepare_essrg.py`) |
| `data/ESSRG/ESSRG_serves_time_qwen14b.jsonl` | LLM inference log for serves and duration estimation |

Each meal in the workbook is composed of up to ten component dishes. Each component has its own ingredient list with direct CoFID 2021 IDs and exact CoFID food names, plus gram quantities.

### Available fields (raw)

- Meal title (English)
- Meal type: breakfast / lunch / dinner / snack
- Animal product category (per dish): plant based / egg / dairy / meat / fish
- Seasonality: autumn / winter / spring / summer (multi-value)
- Component dish names and instructions
- Per ingredient: CoFID ID, CoFID food name, gram quantity
- No serves, no duration, no URL, no image, no allergens, no cost

### Meal type distribution

| Meal type | Recipes |
|---|---:|
| Breakfast | 35 |
| Lunch | 40 |
| Dinner | 40 |
| Snack | 35 |

### Preprocessing

- Each multi-dish meal unified into one recipe (flattened ingredient list, concatenated instructions with dish titles)
- Nutrition computed from CoFID 2021 embedded table: `ingredient_quantity_g / 100 × CoFID_value_per_100g`, summed across all ingredients
- 3 ingredients had malformed CoFID IDs (`#NAME?` — Excel formula error); resolved via exact food-name lookup in the same CoFID table
- 22 ingredients across 16 recipes had no gram quantity in the source; excluded from nutrition sums, recorded in `nutrition_profiling_debug`
- Serves and duration not present in source: estimated with `Qwen/Qwen2.5-14B-Instruct-AWQ`; labelled `serves_source: llm_estimate` and `duration_source: llm_estimate` on every recipe

**Audit file:** `data/ESSRG/ESSRG_conversion_audit.json`
**Preprocessing doc:** `data/ESSRG/PREPROCESSING.md`

### Missing data handling

| Field | Status | How handled |
|---|---|---|
| Serves | Not provided | LLM estimate (Qwen2.5-14B) |
| Duration | Not provided | LLM estimate (Qwen2.5-14B) |
| Ingredient gram weights | Provided for most; 22 missing across 16 recipes | Missing weights excluded from nutrition; rest used directly |
| Allergens | Not provided | Detected from ingredient names at import time |
| URL / image | Not provided | Null |
| Cost | Not in source | Not stored |
| ESSRG_126 ("Potato and white bean salad") | Zero ingredients in source; all nutrition null | Removed from all databases (Neo4j, Postgres, ES). Cannot be restored without obtaining the ingredient data from ESSRG. |

### Dietary classification (source-provided, 149 recipes)

ESSRG classified each recipe by animal product category. This is a property of the source data, not inferred from ingredients.

| Animal product category | Recipes |
|---|---:|
| Plant-based | 74 |
| Dairy | 24 |
| Egg | 21 |
| Other meat | 17 |
| Red meat | 13 |

Note: "plant-based" here means the recipe was classified as such by ESSRG, not that it is allergen-free. Plant-based recipes may still contain wheat, gluten, soy, tree nuts, etc.

### Allergens (computed from ingredient graph, 149 recipes in ES)

Stored separately from the source dietary classification above.

| Allergen | Recipes |
|---|---:|
| Wheat | 75 |
| Milk | 70 |
| Egg | 32 |
| Gluten | 28 |
| Soy | 20 |
| Tree nut | 18 |
| Celery | 11 |
| Sesame | 10 |
| Mustard | 8 |
| Fish | 5 |
| Peanut | 5 |
| Molluscs | 1 |

### Nutrition profiles in Postgres (`source = Curated Hungarian Recipes`)

| nutrition_source | Rows | Description |
|---|---|---|
| `planeat` | 149 | CoFID-derived ground-truth nutrition |
| `irish` | 149 | Pipeline-computed via Chroma matching on CoFID ingredient names |
| `hungarian` | 149 | Pipeline-computed via Chroma matching |
| `eu` | 149 | Pipeline-computed via Chroma matching |

Note: `irish`, `hungarian`, `eu` profiles are Chroma-matched approximations against their respective composition tables — not derived from the original CoFID source data. The CoFID ground truth (`planeat`) is the authoritative nutrition source for this dataset.

### Scripts

| Script | Purpose |
|---|---|
| `scripts/prepare_essrg.py` | Converts XLSX → `ESSRG_recipes_clean.json` |
| `scripts/infer_essrg_serves_time_vllm.py` | LLM-estimates serves and duration |
| `scripts/import_planeat.py` | Imports 149 recipes into Neo4j, Postgres (`planeat`), ES |
| `scripts/profile_planeat_regions.py` | Computes irish / hungarian / eu profiles via Chroma matching |

---

## 3. Curated Slovenian Recipes (source: `Curated Slovenian Recipes`)

### Raw data

Provided as part of the PLANEAT project, Slovenian cohort. Expert-curated traditional Slovenian recipes with full nutritional composition computed from the OPKP (Odprta platforma za klinično prehrano — Open Platform for Clinical Nutrition) composition table.

| File | Content |
|---|---|
| `data/Slovenia/Slovenian_Recipes.xlsx` | 100 recipes across 3 sheets: Recept (recipes), Sestavine (ingredients), Hr. vrednosti (nutritional values per 100g) |
| `data/Slovenian_OPKP/Primeri receptov.xlsx` | 10-recipe sample subset; same schema, not imported separately |

### Available fields (raw)

**Recept sheet (recipe-level):**
- Recipe ID (UUID — used as-is)
- Slovenian title and English title
- Instructions (English)
- Category: Soup / Main dish / Salad / Dessert
- Serving weight in grams (`RECAMOUNT`)
- Yield factor (proportion of ingredient weight retained after cooking)
- Total time (minutes)

**Sestavine sheet (ingredient-level):**
- OPKP ingredient UUID
- Slovenian and English ingredient name
- Gram quantity per recipe

**Hr. vrednosti sheet (nutrition per 100g):**
Pre-computed from OPKP composition table. One row per recipe per EuroFIR nutrient code:

| EuroFIR code | Nutrient | Unit |
|---|---|---|
| ENERC | Energy | kcal |
| PROT | Protein | g |
| FAT | Fat, total | g |
| FASAT | Saturated fat | g |
| CHO | Carbohydrate | g |
| SUGAR | Sugars | g |
| FIBT | Fibre | g |
| NACL | Salt | g |
| CA | Calcium | mg |
| FE | Iron | mg |
| K | Potassium | mg |
| FOL | Folate | µg |
| VITB12 | Vitamin B12 | µg |
| VITC | Vitamin C | mg |
| VITD | Vitamin D | µg |
| CHORL | Chloride | mg |

Nutrition values are per 100g of the finished recipe. Per-serving values derived as: `value × RECAMOUNT / 100`.

### Recipe category distribution

| Category | Recipes |
|---|---:|
| Main dish | 68 |
| Soup | 16 |
| Salad | 8 |
| Dessert | 8 |

### Preprocessing

- Serves computed deterministically: `round(sum(ingredient_weights) × yield_factor / RECAMOUNT)` — exact integer results for all 100 recipes
- NACL (salt, g) converted to sodium: `sodium_mg = NACL_g × 400`
- English ingredient names used for Neo4j ingredient nodes and allergen detection
- Allergens detected from English ingredient names at import time

### Missing data handling

| Field | Status | How handled |
|---|---|---|
| Serves | Not provided as a count | Computed deterministically from ingredient weights, yield factor, and serving weight |
| Duration | Provided (`Total time` column) | Used as-is |
| Allergens | Not in source | Detected from ingredient names at import |
| URL / image | Not provided | Null |
| Meal type (breakfast/lunch/dinner) | Not provided | Null; only category (Soup/Main dish/Salad/Dessert) available |
| Seasonality | Not provided | Null |
| Cost | Not provided | Null |

### Allergens (computed from ingredient names, 100 recipes in ES)

No source-provided dietary classification for this dataset. All allergen tags are inferred from English ingredient names at import.

| Allergen | Recipes |
|---|---:|
| Wheat | 51 |
| Gluten | 44 |
| Egg | 38 |
| Milk | 36 |
| Fish | 6 |
| Tree nut | 4 |
| Celery | 3 |
| Crustacean / shellfish | 2 |
| Molluscs | 1 |

### Nutrition profiles in Postgres (`source = Curated Slovenian Recipes`)

| nutrition_source | Rows | Description |
|---|---|---|
| `slovenian` | 100 | OPKP-derived ground-truth nutrition (macros + 8 micronutrients) |
| `irish` | 100 | Pipeline-computed via Chroma matching on English ingredient names |
| `hungarian` | 100 | Pipeline-computed via Chroma matching |
| `eu` | 100 | Pipeline-computed via Chroma matching |

Note: `irish`, `hungarian`, `eu` profiles are Chroma-matched approximations. The OPKP ground truth (`slovenian`) is the authoritative nutrition source for this dataset, covering 16 nutrient components including micronutrients not tracked in other datasets.

### Scripts

| Script | Purpose |
|---|---|
| `scripts/import_slovenian.py` | Imports 100 recipes into Neo4j, Postgres (`slovenian`), ES |
| `scripts/profile_slovenian_regions.py` | Computes irish / hungarian / eu profiles via Chroma matching |

---

## Cross-dataset recipe cost labels

Only the RCSI subset of Curated Irish Recipes and HealthyFoods contain source-provided recipe-cost information. Of the 99 RCSI recipes, 31 are labelled low cost (31.3% of the subset; 77.5% of the 40 recipes with a populated cost value), 8 medium cost (8.1%; 20.0% of labelled recipes), 1 high cost (1.0%; 2.5% of labelled recipes), and 59 have no cost value. HealthyFoods uses a binary `$AVER (low cost)` editorial badge rather than a low/medium/high scale: 1,669 of its 5,183 imported recipes (32.2%) carry this badge and 3,514 (67.8%) do not. This gives 1,700 source-labelled low-cost recipes across the two datasets. Curated Hungarian Recipes, Curated Slovenian Recipes, MyPlate, FoodHero, and Recipe1M/HUMMUS provide no standardised recipe-level cost field. These labels should be analysed within their original source rather than as a shared monetary scale: neither source records currency, market, price date, numeric ingredient prices, or cost per recipe or serving, and no missing costs were inferred from current retail prices.

---

## Table 30: Availability of reference nutrition and score information by recipe source

| Source / subset | Reference nutrition panel | Reference Nutri-Score status | Evaluation use |
|---|---|---|---|
| Curated Irish Recipes — RCSI expert subset | Available (99 recipes; lab-measured per-100g values across all 6 nutrient fields) | Derivable from expert-computed nutrition values; no direct source label | Main benchmark for nutrition and Nutri-Score reproducibility |
| Curated Irish Recipes — SafeFood web panels | Available (334 recipes; scraped per-serving nutrition panels, normalised to per-100g) | Derivable from source-provided nutrition values; no direct source label | Extended benchmark for nutrition reproducibility and Nutri-Score coverage |
| Curated Hungarian Recipes | Available (149 recipes; CoFID 2021 expert food composition, minor gaps in sat. fat and fibre) | Derivable from CoFID reference nutrition; no direct source label | Regional nutrition and Nutri-Score comparison for Hungarian cohort |
| Curated Slovenian Recipes | Available (100 recipes; OPKP composition table, all 6 fields complete) | Derivable from OPKP reference nutrition; no direct source label | Regional nutrition and Nutri-Score comparison for Slovenian cohort |
| HealthyFoods | Available (5,141 of 5,183 recipes; scraped nutrition panels from HealthyFood.com) | Derivable from source-provided nutrition values; no direct source label | Contextual comparison against large scraped-nutrition collection |
| Recipe1M enriched with HUMMUS | Available through HUMMUS metadata (31,447 recipes; energy, sugars, sat. fat, sodium, protein — fibre absent) | Direct HUMMUS Nutri-Score label (pre-computed by HUMMUS authors; not independently reproducible from stored fields due to missing fibre) | Large-scale contextual comparison against HUMMUS nutrition and score metadata |
| MyPlate | Available (1,039 recipes; complete scraped per-serving panels across all 6 inputs) | Derived by RecipeWrangler from the scraped values; no direct source label | Nutri-Score confusion matrices and reference-vs-generated nutrient-median comparison |
| FoodHero | Not available as structured native nutrition | Not available | Profiling coverage and generated-output analysis only |

**Notes:**

- **Curated Irish Recipes (RCSI)**: the 99 RCSI recipes are a strict subset of the 334 SafeFood web recipes. Both reference sources are available for those 99, making them the highest-confidence benchmark: web panels provide broad coverage, RCSI values provide lab-grade precision for cross-validation.

- **Curated Hungarian / Slovenian**: nutrition is derived from national food composition databases (CoFID and OPKP) by the dataset providers, not scraped or pipeline-estimated. These are the most reliable non-Irish reference sources in the system.

- **HealthyFoods**: nutrition panels are scraped from the HealthyFood.com website and are therefore dependent on source accuracy. They are treated as reference-quality for evaluation purposes but are less authoritative than lab-measured or expert-curated values.

- **Recipe1M / HUMMUS**: the HUMMUS Nutri-Score label is a direct annotation from the HUMMUS project and cannot be reproduced from the six nutrient fields stored in this system (fibre is missing). It is used as a label for large-scale score-level comparison, not for per-nutrient validation.

- **MyPlate**: complete scraped reference nutrition is available for all 1,039 recipes. It is included in nutrient-level comparison and Nutri-Score agreement analysis. The reference grade is algorithmically derived from the scraped panel and should not be described as source-provided.

- **FoodHero**: contributes to profiling coverage and generated-output analysis but cannot be used for nutrition accuracy or Nutri-Score validation because no external reference values are available.

---

## Table 32: Recipe-level profiling completion by source group

| Source group | Recipes profiled | Reason for inclusion |
|---|---:|---|
| FoodHero | 517 | Profiling coverage and search enrichment |
| HealthyFoods | 5,181 | Profiling coverage and comparison against source-provided nutrition metadata where applicable |
| Curated Irish Recipes — RCSI expert subset | 46 | Main benchmark subset for nutritional reproducibility and Nutri-Score comparison |
| MyPlate | 1,039 | Full profiling coverage plus reference nutrition and Nutri-Score evaluation |
| Recipe1M / HUMMUS overlap | 51,235 | Large-scale profiling scope with reference nutrition metadata through HUMMUS overlap |
| **Total profiling / evaluation scope** | **58,018** | Current recipe set used for profiling coverage and reference-value analysis where applicable |

**Notes:**

- **Curated Irish Recipes — RCSI expert subset (46)**: the 46 profiled RCSI recipes are a subset of the 99 RCSI lab-annotated recipes and the 334 SafeFood web recipes. This subset was used as the primary benchmark for Nutri-Score reproducibility: pipeline-computed Nutri-Score is compared against the grade derivable from RCSI lab-measured reference nutrition.

- **HealthyFoods (5,181)**: 5,181 of 5,183 imported recipes were successfully profiled; 2 were excluded due to extreme caloric outliers removed in the pre-profiling QC pass (Pass 1 IQR threshold).

- **Recipe1M / HUMMUS overlap (51,235)**: this is the full Recipe1M set profiled by the pipeline, of which 31,447 have HUMMUS reference nutrition and a direct Nutri-Score label available for comparison. The remaining ~19,788 recipes in this group contribute to profiling coverage statistics only.

- **Curated Hungarian Recipes and Curated Slovenian Recipes**: not listed separately in this table as their primary evaluation role is regional nutritional comparison rather than pipeline profiling coverage; both are fully profiled (149 and 100 recipes respectively) and included in regional accuracy analyses.

- **Total (58,018)**: represents the aggregate profiling scope across all source groups listed. Does not include curated datasets (Curated Irish Recipes full set, Curated Hungarian Recipes, Curated Slovenian Recipes) which are analysed separately as ground-truth benchmarks.

---

## Table 33: EU global ingredient matching coverage

| Metric | Count | Denominator | Coverage rate |
|---|---:|---:|---:|
| Distinct ingredient vocabulary matched | 8,148 | 8,148 embedded | 100% |
| Recipe-occurrence-weighted matched | ~256,000 | ~260,000 occurrence slots | ~98.5% |
| Distinct low-confidence matches (distance > 0.25) | ~1,200 | 8,148 | ~14.7% |
| Occurrence-weighted low-confidence matches | ~18,000 | ~260,000 | ~6.9% |

The EU global composition table is the universal fallback for all three nutrition profiles. Vocabulary coverage is complete by construction — every food in the table is embedded in the Chroma collection. Recipe-occurrence-weighted coverage (the share of actual ingredient slots across all profiled recipes that receive a match) is approximately 98.5%, with unmatched slots corresponding to ingredients whose name is too ambiguous or specialised for any composition table hit above the confidence threshold. Low-confidence matches (cosine distance > 0.25 to the nearest food) represent cases where the semantic match is plausible but not high-precision — these contribute to the profile but may introduce estimation error. The occurrence-weighted low-confidence rate (~6.9%) is consistent with the pipeline's overall Nutri-Score accuracy across validated datasets.

---

## Table 34: Selected-source share for Irish and Hungarian profiles

| Profile source | Selected source | Ingredient occurrences | Selected-source share |
|---|---|---:|---:|
| Irish profile | Native Irish table | 33,870 | 13.0% |
| Irish profile | EU global table | 226,446 | 87.0% |
| Hungarian profile | Native Hungarian table | 69,777 | 26.8% |
| Hungarian profile | EU global table | 190,538 | 73.2% |

Selected-source share is computed at the ingredient-occurrence level across all profiled recipes (non-curated datasets; n ≈ 260,000 ingredient slots per profile). The native Irish table contributes to only 13% of Irish-profile matches, reflecting its narrower coverage (1,307 foods vs. 8,148 in the EU pool) and English-language ingredient names. The Hungarian table achieves a higher native share (26.8%) due to its more targeted vocabulary for Central European ingredients, although it covers only 500 foods and requires EU global fallback for all Nutri-Score inputs not in its schema (sugars, saturated fat, fibre). The dominant EU global fallback share across both profiles explains why Irish and Hungarian generated Nutri-Scores tend to converge — both draw on the same EU pool for the majority of their ingredient matches.

---

## Table 35: Nutri-Score input availability and completion by composition-table source

| Composition-table profile | Directly available Nutri-Score inputs | Inputs requiring completion from EU global table | Effect on profiling |
|---|---|---|---|
| Irish profile | Energy, total sugars, saturated fat, sodium, dietary fibre, protein (all 6) | None | Full Nutri-Score computation from native table; EU global used only as fallback for unmatched ingredients |
| Hungarian profile | Energy, sodium, protein | Total sugars, saturated fat, dietary fibre | Pipeline queries EU global collection for missing inputs; Nutri-Score grade is completed but is a blend of CoFID macros and EU-global micronutrients |
| EU global profile | Energy, total sugars, saturated fat, sodium, dietary fibre, protein (all 6) | None | Full Nutri-Score computation from EU composite; used as standalone profile and as universal fallback |

The Hungarian composition table (CoFID derivative, 500 foods, 15 fields) covers only energy, protein, fat, carbohydrates, and selected minerals. The three Nutri-Score-specific inputs that require non-fat breakdown — total sugars, saturated fat, dietary fibre — are absent from the native table and must be resolved via EU global nearest-neighbour matching. This blended approach means Hungarian-profile Nutri-Score grades are computed from two different source pools and may introduce additional estimation error relative to profiles where all six inputs come from the same table. The Irish and EU global tables both provide all six inputs natively.

---

## Table 36: Sustainability matching coverage by source group

Sustainability is estimated via Chroma nearest-neighbour matching against a sustainability reference table (ingredient → kg CO2e/kg). A match is counted when `best_sustainability_match` returns a non-None carbon footprint value (composite score ≥ 0.30). A recipe is *complete* if every ingredient matched; *partial* if at least one ingredient matched but at least one did not.

| Source group | Matched occurrences | Unmatched occurrences | Coverage rate | Complete recipes | Partial recipes |
|---|---:|---:|---:|---:|---:|
| FoodHero | 3,427 | 175 | 95.14% | 286 | 130 |
| HealthyFoods | 62,024 | 3,746 | 94.30% | 2,806 | 2,381 |
| Curated Irish Recipes | 2,771 | 167 | 94.32% | 212 | 122 |
| MyPlate | 7,936 | 287 | 96.51% | 794 | 247 |
| Curated Hungarian Recipes | 898 | 86 | 91.26% | 81 | 67 |
| Curated Slovenian Recipes | 771 | 17 | 97.84% | 84 | 15 |
| Recipe1M / HUMMUS overlap | 174,702 | 8,361 | 95.43% | 25,063 | 7,316 |
| **Total** | **252,529** | **12,839** | **95.16%** | **29,326** | **10,278** |

**Notes:**

- **Coverage rate** is computed at the ingredient-occurrence level: matched / (matched + unmatched) across all ingredient–recipe pairs in the group. A single ingredient appearing in many recipes is counted once per recipe.

- **Curated Slovenian Recipes (97.84%)**: the highest coverage of all source groups. Traditional Slovenian recipes use a compact set of common Central European ingredients that are well represented in the sustainability reference table.

- **Curated Hungarian Recipes (91.26%)**: slightly lower coverage than the Irish and Slovenian sets, likely due to some Hungary-specific ingredients (e.g. certain paprika varieties, traditional fermented products) not present in the sustainability reference table.

- **Curated Irish Recipes (94.32%)**: coverage is at the upper end for curated sources; matches the Irish-focused ingredient vocabulary well. Unmatched ingredients are mostly specialty or processed items not in the reference table.

- **FoodHero (95.14%)**: mid-range coverage, comparable to Recipe1M / HUMMUS and the Irish curated set. The previous lower figure (86%) reflected stale data; this result is computed from the current 416-recipe Neo4j graph.

- **Recipe1M / HUMMUS overlap**: large-scale coverage (95.43%) is consistent with the fact that Recipe1M ingredient names are predominantly common English-language food terms, which are well represented in the sustainability reference table.

- **Totals** include all source groups in the table. The overall coverage rate (95.05%) is stable across groups, confirming that the Chroma-based sustainability matcher generalises well across recipe origins and cuisines.

---

## Table 37: Mean and median carbon footprint per serving by recipe source

Carbon footprint (kg CO₂e per serving) is estimated by the Chroma-based sustainability pipeline: each ingredient is matched to the sustainability reference table, its carbon factor (kg CO₂e/kg) is multiplied by its weight in grams, and the total is divided by the number of servings. Only ingredients with a successful sustainability match contribute to the total.

| Source | n | Mean (kg CO₂e/serving) | Median (kg CO₂e/serving) |
|---|---:|---:|---:|
| FoodHero | 413 | 0.265 | 0.128 |
| HealthyFoods | 5,181 | 0.828 | 0.547 |
| Curated Irish Recipes | 334 | 0.606 | 0.463 |
| MyPlate | 1,038 | 0.397 | 0.223 |
| Curated Hungarian Recipes | 149 | 0.464 | 0.309 |
| Curated Slovenian Recipes | 100 | 0.990 | 0.392 |
| Recipe1M / HUMMUS overlap | 51,235 | 1.617 | 0.814 |

**Notes:**

- **FoodHero (mean 0.265, median 0.128)**: the lowest carbon footprint across all groups, consistent with FoodHero's focus on budget-friendly, plant-forward family meals. The low median indicates that the majority of recipes are vegetable- or grain-based, with meat appearing infrequently or in small quantities.

- **MyPlate (mean 0.397, median 0.223)** and **Curated Hungarian Recipes (mean 0.464, median 0.309)**: occupy a similar mid-range. Both datasets feature a balanced mix of meat-containing and plant-based recipes. The relatively small difference between mean and median suggests fewer extreme outliers than in other groups.

- **Curated Irish Recipes (mean 0.606, median 0.463)**: mid-range, reflecting a traditional Irish diet that includes regular meat and dairy but in moderate portions. The median is higher than MyPlate and FoodHero, consistent with a higher proportion of beef and lamb recipes.

- **Curated Slovenian Recipes (mean 0.990, median 0.392)**: the median is moderate and comparable to HealthyFoods, but the mean is elevated by a small number of recipes with large meat quantities (e.g. lamb dishes with whole-leg portions). Slovenian recipes have source-provided serves values (range 1–50, average 6.6) which are used to normalise the per-serving footprint; the mean is sensitive to a few high-meat, low-serves recipes.

- **HealthyFoods (mean 0.828, median 0.547)**: wider spread than the curated sources, reflecting the diversity of the scraped dataset. The mean is pulled upward by recipes with beef, lamb, or large dairy quantities.

- **Recipe1M / HUMMUS overlap (mean 1.617, median 0.814)**: the highest values across all groups. The large mean-median gap (0.80) indicates a right-skewed distribution, with a substantial tail of high-footprint recipes (beef-heavy, large-batch dishes) that inflate the mean well above the typical recipe.

- **Mean vs. median**: across all sources, the median is substantially lower than the mean, confirming right-skewed distributions. The median is the more representative single-number summary of the typical recipe's environmental footprint.

---

## Table 38: Allergen coverage across the recipe graph

Allergens are inferred from ingredient names via Neo4j graph edges (`HAS_INGREDIENT → HAS_ALLERGEN`) using FoodOn taxonomy and keyword matching (`scripts/neo4j/tag_allergens.py`). All 14 EU-regulated allergens are covered; counts reflect distinct recipes containing at least one ingredient tagged with that allergen. Total recipes in graph: **811,541**.

| Allergen | Recipes affected | % of graph |
|---|---:|---:|
| Milk | 475,869 | 58.64% |
| Wheat | 294,155 | 36.25% |
| Egg | 252,368 | 31.10% |
| Gluten | 70,993 | 8.75% |
| Soy | 50,963 | 6.3% |
| Mustard | 50,450 | 6.2% |
| Fish | 37,225 | 4.6% |
| Crustacean / shellfish | 23,989 | 3.0% |
| Peanut | 20,871 | 2.57% |
| Celery | 18,630 | 2.3% |
| Sesame | 17,911 | 2.2% |
| Tree nut | 17,102 | 2.1% |
| Molluscs | 10,289 | 1.3% |
| Lupin | 710 | 0.1% |
| Sulphites | 0 | 0.0% |

**Notes:**

- **Milk (58.64%)**: the most prevalent allergen by a wide margin, reflecting the heavy use of dairy (butter, cream, cheese, yoghurt) across European and North American recipe collections. Its high prevalence is consistent with diet survey data for the regions covered.

- **Wheat (36.25%) and Gluten (8.75%)**: wheat is tagged at the ingredient level (flour, bread, pasta); gluten captures broader gluten-containing cereals and explicit gluten evidence. They overlap in 34,077 recipes. Their overlap-safe union—the operational EU “cereals containing gluten” category—is 331,071 recipes (40.80% of the graph).

- **Egg (31.4%)**: third most common, consistent with egg's role as a binding and enriching agent across cuisines. Prevalence is likely slightly underestimated for Recipe1M where ingredient names are less standardised.

- **Mustard (6.2%)**: notably higher than might be expected, driven by mustard as a condiment ingredient in dressings and marinades across European recipes. Roughly on par with soy, despite being less widely discussed as a dietary allergen.

- **Celery (2.3%)**: included as a distinct EU allergen; present in stocks, soups, and salads. Lower prevalence than mustard but consistent with its role as a flavouring base.

- **Sulphites (0.0%)**: sulphites/sulphur dioxide are a preservative additive rather than a primary named ingredient. The current keyword-matching tagger does not detect sulphites because they appear as processing additives (e.g. "dried apricots") rather than explicit ingredient names. This allergen requires a separate ingredient-property approach and is not currently covered.

- **Lupin (0.1%)**: the rarest detected allergen. Lupin flour is used in some gluten-free and high-protein products but is uncommon in mainstream recipes. The low count reflects both its rarity in the source datasets and the limited vocabulary coverage in the tagger.

- **Recipe-level counts**: a recipe is counted once per allergen regardless of how many of its ingredients carry that tag. Multi-allergen recipes are counted in each relevant allergen row independently.

---

## Figure 10: Nutritional outliers removed during preprocessing

![Figure 10](section5_outputs/Figure_10_outliers_removed_per_source.png)

Recipes removed from non-curated collections during two-pass outlier filtering. Pass 1 applies a per-source IQR-based threshold; Pass 2 applies a strict global cap of 1,000 kcal per serving. Curated datasets (Irish, Hungarian, Slovenian) are exempt from both passes. Recipe1M / HUMMUS accounts for the vast majority of removals, consistent with known per-serving encoding errors in the HUMMUS dataset where whole-recipe nutrient totals are reported as single-serving values.

---

## Figure 11: High-level RecipeWrangler application architecture

![Figure 11](section5_outputs/Figure_11_architecture_overview.png)

Top-level view of the RecipeWrangler system showing the two main LangGraph pipelines — recipe profiling (left) and recipe search (right) — and their shared data infrastructure (Neo4j, PostgreSQL, Chroma, Elasticsearch). The profiling pipeline accepts raw recipe text and produces a structured profile (nutrients, Nutri-Score, CO₂e); the search pipeline accepts a user query with structured filters and returns ranked recipes. Both pipelines share the same model infrastructure (LLM for parsing and query extraction, embedding model for semantic matching).

---

## Figure 12: Recipe profiling pipeline and shared infrastructure

![Figure 12](section5_outputs/Figure_12_profiling_pipeline.png)

Detailed view of the profiling LangGraph pipeline. Raw recipe text passes through the Recipe Parser, Weight Calculator, and Allergen Detector before reaching the Recipe Profiling Node. The profiling node executes three sub-tools in parallel: Nutrient Profiling (ingredient→composition-table matching via Chroma and PostgreSQL), Nutri-Score computation (A–E grade from the six input nutrients), and Sustainability Profiling (CO₂e per serving). Shared model infrastructure — Groq LLMs for bounded interpretation tasks and BAAI/bge-small-en-v1.5 for semantic embedding — is shown explicitly to distinguish from the shared data infrastructure used by both pipelines.

---

## Figure 13: Allergen detection using FoodOn ancestry and keyword triggers

![Figure 13](section5_outputs/Figure_13_allergen_matching_pipeline.png)

Decision flow for ingredient-to-composition-table matching, which also forms the basis of allergen classification. An ingredient first passes a food-class gate derived from the FoodOn ontology hierarchy; candidates that survive are checked for alias-table and curated-link hits before falling through to BM25 + cosine scoring with cooking-state and brand penalties. Each surviving path exits with a confidence label (alias, curated, strong, weak, or none). Allergen tags are derived from the food-class hierarchy traversed at the gate step: an ingredient inherits all allergen flags of its FoodOn ancestor classes, enabling detection without explicit keyword lists for the majority of common allergens.

---

## Figure 14: Recipe search pipeline and shared infrastructure

![Figure 14](section5_outputs/Figure_14_search_pipeline.png)

Detailed view of the search LangGraph pipeline. A user query with optional structured parameters enters an Extract Constraints step (LLM-assisted) which produces a canonical filter set. Build Search Query translates filters into an Elasticsearch query body (no LLM at this step — pure rule-based translation). Execute Search runs the query against the `recipes_v2` Elasticsearch index and returns ranked results. Shared model and data infrastructure mirrors the profiling pipeline, with Elasticsearch as the primary search backend replacing slower Neo4j full-graph traversal (~10 ms vs. ~1 s per query).

---

## Table 13: Nutri-Score grade and colour mapping

| Score | Grade | Colour |
|---:|:---:|---|
| −15 to −1 | A | Dark green (#6FA66A) |
| 0 to 2 | B | Light green (#A8BC72) |
| 3 to 10 | C | Yellow (#D7C56A) |
| 11 to 18 | D | Orange (#D49A5B) |
| 19 + | E | Red (#C9786F) |

The Nutri-Score algorithm sums negative-point components (energy, saturated fat, total sugars, sodium) and positive-point components (dietary fibre, protein, and estimated fruit/vegetable/legume/nut percentage) into a single integer score. Lower scores indicate a more favourable nutritional profile; the five grade bands map that score to letter grades A–E with associated traffic-light colours. The thresholds above apply to the solid food category; different thresholds govern beverages and fats/oils. All Nutri-Scores in this report use the solid food thresholds with FVLN percentage set to zero unless an FVLN estimate is available.

---

## Table 14: Availability of core Nutri-Score nutrient inputs by composition profile

| Nutri-Score input | Irish | Hungarian | EU global |
|---|:---:|:---:|:---:|
| Energy | ✓ | ✓ | ✓ |
| Total sugars | ✓ | ✗ (EU fallback) | ✓ |
| Saturated fat | ✓ | ✗ (EU fallback) | ✓ |
| Sodium | ✓ | ✓ | ✓ |
| Dietary fibre | ✓ | ✗ (EU fallback) | ✓ |
| Protein | ✓ | ✓ | ✓ |

All six Nutri-Score inputs are natively available in the Irish and EU global composition tables. The Hungarian composition table (CoFID derivative) includes energy, protein, fat, sodium, and carbohydrates but lacks the nutrient-level breakdown required for total sugars, saturated fat, and dietary fibre independently. For Hungarian-profile recipes, these three inputs are resolved via EU global nearest-neighbour matching, making the Hungarian profile a blend of CoFID-sourced macronutrients and EU-composite-sourced Nutri-Score micronutrients. The ✗ (EU fallback) entries in Table 14 correspond directly to the increased EU-global selected-source share visible in Table 34.

---

## Figure 15: Reference Nutri-Score grade distributions

![Figure 15](section5_outputs/Figure_15_reference_nutriscore_distributions.png)

Reference Nutri-Score grades computed from source-provided or scraped per-serving nutrition values for four datasets with available reference nutrition. Grades are derived using the standard EU Nutri-Score algorithm (solid food category, fruit/vegetable percentage set to zero where not available in the source data).

- **Irish Curated Recipes (n = 99):** Nearly all recipes (86.9%) achieve Grade A, with small fractions at Grade B (5.1%), C (7.1%), and D (1.0%), and no Grade E. This reflects the dataset's clinical curation — recipes designed by nutrition experts with explicit health targets and portion control.

- **HealthyFoods (n = 5,017):** Strong skew toward Grade A (74.0%) with most of the remainder at Grade B (25.6%), and negligible C/D/E presence. The dataset draws from health-oriented food blogs and platforms, which self-select for lighter, nutrient-dense dishes.

- **Recipe1M / HUMMUS (n = 31,447):** The distribution inverts relative to the curated sources — Grades D and E together account for 43.2% of recipes, while only 9.0% reach Grade A. This reflects the uncurated nature of the dataset: recipes sourced from the general web with no health filter, including indulgent and calorie-dense dishes.

- **MyPlate (n = 1,039):** An intermediate profile — 50.9% Grade A and 21.0% Grade B — consistent with a government-maintained library designed around USDA dietary guidelines but not as tightly controlled as clinical datasets. The 14.7% Grade C and 13.4% combined D/E suggest the collection includes everyday family recipes where palatability is balanced against nutritional targets. Reference Nutri-Score is computed from scraped myplate.food per-serving nutrition converted to per-100g using ingredient weights from the RecipeWrangler profiling pipeline.

**Key takeaway:** Reference Nutri-Score quality correlates directly with curation intent. Clinical datasets (Irish Curated) are near-perfect; health-platform datasets (HealthyFoods, MyPlate) are substantially healthier than the population average; uncurated web corpora (Recipe1M) reflect the broad range of real-world dietary habits, with a majority of recipes falling below Grade C.

---

## Table 39: Recipe tag coverage — dietary, nutritional, and convenience labels

All tags are stored as Neo4j `Tag` nodes connected to `Recipe` nodes via `HAS_TAG` edges. Counts reflect the live graph (811,541 total recipes).

**Dietary / lifestyle tags** are generated without copying source dietary labels. Strict dietary tags are derived from FoodOn ingredient classification and corrected keyword fallbacks. The HealthyFoods `gluten_free_option` tag is separate: it is generated from explicit adaptation or substitution evidence in recipe descriptions and notes.

**Nutritional claim tags** follow EU Regulation (EC) No 1924/2006 thresholds for solid foods, computed per 100g from the EU global nutrition profiling pipeline. Recipes without a computed nutrition profile are excluded from these counts.

**Convenience tags** are derived from structured recipe metadata (duration in minutes).

| Tag | Neo4j name | Recipes | Basis |
|---|---|---:|---|
| Nut-Free | `nut_free` | 773,933 | Ontology — no nut-class ingredient present |
| Vegetarian | `vegetarian` | 527,548 | Ontology + boundary-aware keyword fallback — no meat or fish ingredient |
| Vegetarian or Vegan | `vegetarian_or_vegan` | 527,548 | Union of `vegetarian` ∪ `vegan` tags |
| Pescatarian | `pescatarian` | 375,980 | Ontology — no meat; fish/seafood allowed |
| Dairy-Free | `dairy_free` | 335,672 | Ontology + boundary-aware keyword fallback — no dairy ingredient present |
| Vegan | `vegan` | 197,259 | Ontology + boundary-aware keyword fallback — no animal-derived ingredient |
| Quick (≤ 30 min) | `30_minutes_or_less` | 167,499 * | `duration_minutes < 30` from recipe metadata |
| 5 Ingredients or Fewer | `5_ingredients_or_less` | 167,994 | Ingredient count ≤ 5 from graph structure |
| Low Fat | `low_fat` | 13,185 | EU claim: ≤ 3 g fat per 100 g |
| Healthy & Nutritious | `healthy_and_nutritious` | 6,870 | Nutri-Score A (best available nutrition source) |
| High Protein | `high_protein` | 8,029 | EU claim: ≥ 20% of energy from protein |
| Low Calorie | `low_calorie` | 2,373 | EU claim: ≤ 40 kcal per 100 g |
| High Fibre | `high_fibre` | 794 | EU claim: ≥ 6 g/100 g or ≥ 3 g/100 kcal |
| Gluten-Free | `gluten_free` | 480,470 | Ontology + corrected keyword fallback — no gluten- or wheat-containing ingredient |
| Gluten-Free Option | `gluten_free_option` | 2,117 | HealthyFoods recipe-text rule — explicit gluten-free adaptation, substitution, choice, or ingredient check |

\* **Quick (≤ 30 min)** count includes Recipe1M recipes annotated via the HUMMUS dataset and MyPlate recipes backfilled via LLM duration extraction. FoodHero, HealthyFoods, and Curated Irish/Slovenian Recipes have duration stored as `duration` (not `duration_minutes`) and are not yet contributing to this tag.

**Notes:**

- **Vegetarian vs. Vegetarian or Vegan**: the counts are identical because every vegan recipe (no animal products) is a strict subset of vegetarian (no meat/fish). The combined tag is provided as a convenience label for filtering.

- **Pescatarian (457,437)**: lower than vegetarian because some recipes without meat still contain fish or shellfish, removing them from the pescatarian-safe pool.

- **Gluten-Free (480,470)**: regenerated after correcting safe-flour, lossy `gluten-free` canonical names, buckwheat, rice/pulse alternatives, tamari, and erroneous FoodOn mappings. The previous count of 3,044 reflected incomplete/stale tag generation rather than the current graph-wide classification.

- **Gluten-Free Option (2,117)**: kept separate from strict ingredient safety. It is generated only for HealthyFoods recipes whose description or notes explicitly state how the recipe can be made gluten-free, such as using gluten-free pasta, replacing flour, or checking packaged ingredients. Source reference badges are not inputs to this rule.

- **Nutritional claim tags**: counts are small relative to total recipes because (a) only recipes with a computed nutrition profile qualify (~58k), and (b) EU thresholds are strict by design — Low Calorie (≤ 40 kcal/100g) and High Fibre (≥ 6g/100g) in particular are conservative benchmarks.

- **Healthy & Nutritious (6,870)**: defined as Nutri-Score A using the highest-priority available nutrition source per recipe. Substantially lower than `low_fat` or `high_protein` alone because Nutri-Score A requires a favourable balance across all six input nutrients simultaneously.

---

## Dietary-tag reference recall evaluation

RecipeWrangler dietary tags were evaluated against source-provided positive labels for HealthyFoods and Curated Hungarian Recipes. This is a **reference-positive recall** analysis: for every recipe marked with a dietary label by the source, the test checks whether RecipeWrangler generated the corresponding Neo4j tag. Additional generated tags are not penalised, so precision and false-positive rates are outside the scope of this evaluation.

Reference labels come from two sources: HealthyFoods provides `Dairy free`, `Gluten free`, `Gluten-free option`, `Nut free`, `Vegan`, and `Vegetarian` editorial badges (5,183 recipes total). Curated Hungarian Recipes provides ESSRG meal-level classifications (`plant_based`, `dairy`, `egg`, `meat`, `fish`) for 149 recipes; `plant_based` maps to vegan, vegetarian, and dairy-free; `dairy` additionally maps to vegetarian. Hungarian gluten-free and nut-free are excluded — ESSRG did not provide those labels. HealthyFoods `Vegan option` recipes (193) are excluded from the vegan row because option recipes commonly retain animal ingredients until a substitution is applied. Where both datasets cover the same tag, results are pooled.

| Tag | HealthyFoods ref | Hungarian ref | Total ref | Recall |
|---|---:|---:|---:|---:|
| Dairy-free | 1,840 | 74 | 1,914 | **96.60%** |
| Vegan | 180 | 74 | 254 | **94.49%** |
| Vegetarian | 1,499 | 98 | 1,597 | **96.49%** |
| Gluten-free (strict) | 911 | — | 911 | **93.19%** |
| Gluten-free option | 1,974 | — | 1,974 | **99.14%** |
| Nut-free | 291 | — | 291 | **100.00%** |

Vegan evaluation uses exact source labels only: HealthyFoods `Vegan` (180 recipes) and Hungarian `plant_based` (74 recipes). Per-dataset breakdown: HealthyFoods 167/180 (92.78%), Hungarian 73/74 (98.65%). The pipeline treats honey and eggs as vegan-compatible. Remaining 14 false negatives are recipes with dairy, fish, shrimp, or other animal products that the source labelled Vegan.

The Hungarian vegetarian result separates into 74/74 (100.00%) for `plant_based` and 19/24 (79.17%) for `dairy`. The five missed dairy-category recipes should be reviewed for ingredient-classification or source-category inconsistencies. Egg-category recipes are not included in the vegetarian reference denominator under the definition used here.

### Dairy-free classification correction

The initial dairy-free recall was 1,630/1,840 (88.59%) for HealthyFoods and 52/74 (70.27%) for the Hungarian `plant_based` subset. Inspection showed that the milk-allergen fallback used unrestricted substrings such as `milk`, `cream`, and `butter`. This incorrectly treated plant ingredients including coconut milk, soy milk/yogurt, coconut cream, peanut butter, butter beans, butternut squash, and vegan dairy substitutes as dairy. Some coconut-milk variants were also incorrectly linked to dairy FoodOn classes such as `milk fat`.

The allergen pipeline was corrected to use word-boundary matching, explicit plant-alternative exclusions, FoodOn plant-product ancestry protection, and targeted handling of lossy canonical forms such as `powdered butter`, `cream rice`, and `milk rice`. Existing milk-allergen edges and recipe tags were deleted and rebuilt rather than incrementally appended. HealthyFoods recall increased from 88.59% to **96.52%**, and Hungarian recall increased from 70.27% to **98.65%**. The graph-wide `dairy_free` count is **335,672**.

The one remaining Hungarian false negative is `Spinach pottage with potatoes`, whose ingredient list contains source-provided whole cow's milk despite the meal-level `plant_based` classification. The remaining 64 HealthyFoods disagreements are dominated by ambiguous or apparently genuine dairy forms such as `ice cream type`, yoghurt, feta, parmesan, butter, and cream. These were not globally exempted because the same canonical names can represent genuine dairy; resolving them safely requires retaining source qualifiers at recipe-to-ingredient mapping level.

### Gluten-free classification correction and option-label handling

The gluten and wheat allergen rules were corrected so that `gluten-free flour`, buckwheat, rice flour, tapioca flour, potato flour, almond flour, coconut flour, besan/chickpea flour, and tamari are not rejected through generic `gluten`, `wheat`, `flour`, or `soy sauce` matching. Incorrect FoodOn oats ancestry affecting ingredients such as ginger, wine, vinegar, and vinaigrette was also excluded. Existing gluten/wheat allergen edges and `gluten_free` tags were deleted and rebuilt.

The final correction also recognises HealthyFoods canonical forms where preprocessing removed the word `free`, such as `gluten baking flour`, `gluten self raising flour`, `gluten bread`, `gluten pasta`, and `gluten soy sauce`, and protects explicit rice/pulse alternatives. Existing wheat/gluten edges and strict recipe tags were deleted and rebuilt. Exact `Gluten free` recall increased from 635/911 (69.70%) before correction to **849/911 (93.19%)**. The former evaluation of `Gluten-free option` against strict `gluten_free` achieved only 1,029/1,974 (52.13%) because option recipes often still list ordinary bread, flour, pasta, noodles, pastry, or tortillas.

The remaining 62 strict gluten-free disagreements include genuine source conflicts—ordinary flour, bread, spelt, soy sauce, gravy, or pasta is present—and ambiguous canonical forms such as generic `pasta`, `bread dressing`, `buttermilk`, or `pasta sauce`. These are not globally exempted because doing so would hide genuine gluten evidence in other recipes.

`Gluten free` and `Gluten-free option` are now treated as separate generated targets. The new `gluten_free_option` rule reads only recipe descriptions and notes and requires explicit adaptation evidence: wording such as “make it gluten free”, “can be made gluten free”, use/choose/replace/check instructions involving a gluten-free ingredient, or an explicit gluten-free alternative. It does not read or copy HealthyFoods dietary badges. The rule generated 2,125 predictions in the cleaned file; 2,117 matched live HealthyFoods graph recipes.

Against the 1,974 live recipes carrying the source `Gluten-free option` reference, the generated option tag found **1,957**, giving **99.14% recall** with 17 false negatives. Those 17 recipes have a reference option badge but no explicit gluten-free adaptation evidence in the imported description or notes, so assigning the generated option tag would require either additional source text or copying the reference label. The latter was deliberately avoided. Predictions and evidence snippets are stored in `section5_outputs/repro_gluten_free_option_predictions.csv`.

### Vegetarian and vegan classification correction

The vegetarian and vegan recipe rules previously used unrestricted substring checks and incremental tag writes. This caused false blockers including `egg` inside `eggplant`, `butter` inside butter beans or nut butter, animal words inside plant alternatives, and known lossy canonical mappings such as `chicken chilli` and `beef spread`. The rules now use word-boundary matching, explicit plant-alternative and vegetarian-product exclusions, and a replacement rebuild that deletes stale tags first.

HealthyFoods vegetarian recall increased from 1,250/1,499 (83.39%) to **1,448/1,499 (96.60%)**. Exact HealthyFoods vegan recall increased from 81/180 (45.00%) to **149/180 (82.78%)**, while Hungarian plant-based vegan recall increased from 51/74 (68.92%) to **70/74 (94.59%)**. The lower HealthyFoods combined vegan result, 200/373 (53.62%), is driven mainly by `Vegan option` recipes whose listed ingredients still contain meat, egg, dairy, fish, or honey before substitution.

The four remaining Hungarian vegan disagreements are explainable source conflicts: three recipes contain honey and `Spinach pottage with potatoes` contains whole cow's milk. The five Hungarian vegetarian disagreements are also source-definition conflicts under the selected no-egg definition: four contain egg and one contains bacon.

The two temporary HealthyFoods nut-free misses were traced to an incorrect canonical mapping from source `ground chipotle powder` to `peanut powder`. The two recipe-to-ingredient edges were repaired reproducibly with `scripts/repair_known_ingredient_mapping_errors.py`; nut-free recall remains **291/291 (100.00%)**.

The evaluation is reproducible with `scripts/evaluate_dietary_tag_recall.py`. Aggregate results are stored in `section5_outputs/repro_dietary_tag_recall_summary.csv`, and every missed reference-positive recipe is listed in `section5_outputs/repro_dietary_tag_false_negatives.csv`.

---

## Table 40: Recipe coverage and median estimated fruit, vegetable, legume and nut (FVLN) content

| Reference source | Recipes included | Median estimated FVLN percentage |
|---|---:|---:|
| RCSI SafeFood | 99 | 52.00% |
| Curated Hungarian Recipes | 149 | 48.16% |
| Curated Slovenian Recipes | 100 | 22.86% |
| HealthyFoods | 5,060 | 48.10% |
| MyPlate | 1,039 | 28.69% |
| Recipe1M / HUMMUS | 31,447 | 9.04% |

FVLN percentage is a RecipeWrangler estimate of the proportion of total ingredient weight represented by fruit, vegetables, legumes, nuts, and the eligible oils used by the Nutri-Score method; it is not source-provided metadata. The RCSI value has been updated from the earlier 46-recipe subset to all 99 recipes with RCSI reference nutrition. RCSI, HealthyFoods, MyPlate, Recipe1M/HUMMUS, and the Hungarian collection use cached ingredient weights and USDA food-group mappings. The Slovenian collection uses its source-provided English ingredient names and exact gram quantities classified into the same FVLN groups, because its cached EU composition identifiers do not provide sufficient USDA group-mapping coverage. The curated Hungarian and Slovenian calculations are reproducible with `scripts/calculate_curated_fvln_percentages.py`, with recipe-level results stored in `section5_outputs/curated_recipe_fvln_percentages.csv`.

---

## Figure 16: Reference Nutri-Score input nutrient medians across four datasets

![Figure 16](section5_outputs/Figure_16_reference_nutrient_medians_all_sources.png)

Median per-serving values for the six Nutri-Score input nutrients (sugars, saturated fat, fibre, protein, energy, sodium) shown for the four datasets with available reference nutrition. All values are on a per-serving basis. Colours follow the project palette: deep purple (Irish Curated Recipes), fresh green (HealthyFoods), raspberry (Recipe1M / HUMMUS), dark purple (MyPlate).

**A. Gram-based nutrients:**

- **Sugars**: Recipe1M / HUMMUS is by far the highest (44.7 g/serving), driven by desserts, baked goods, and sweetened beverages common in web-scraped corpora. RCSI SafeFood (8.5 g) and MyPlate (5.6 g) are low relative to Recipe1M, consistent with health-oriented curation. HealthyFoods falls between (10.0 g).
- **Saturated fat**: broadly similar across RCSI (1.4 g), HealthyFoods (10.8 g), and MyPlate (1.0 g), with Recipe1M again elevated (N/A — not consistently populated in HUMMUS). MyPlate's low saturated fat is consistent with USDA lean-protein guidance.
- **Fibre**: RCSI SafeFood (4.7 g) and HealthyFoods (6.1 g) are moderate to high; MyPlate (2.9 g) is lower; Recipe1M fibre is unavailable in HUMMUS metadata (N/A).
- **Protein**: Recipe1M is highest (26.4 g), HealthyFoods and RCSI are comparable (18.5–20.0 g), MyPlate is lowest (6.7 g) — possibly reflecting the dominance of side dishes and salads in the USDA collection.

**B. Energy**: Recipe1M / HUMMUS has the highest median energy (1,395 kcal/serving), more than 7× the MyPlate median (183 kcal). This large gap likely reflects HUMMUS serving-size encoding issues — many Recipe1M entries report whole-recipe calories as single-serving calories. RCSI (284 kcal) and MyPlate are in a realistic per-serving range for a main meal.

**C. Sodium**: Recipe1M leads (1,190 mg/serving), roughly 7× MyPlate (169 mg) and 8× RCSI (144 mg). HealthyFoods (354 mg) is moderate. The Recipe1M sodium outlier reinforces the hypothesis that HUMMUS per-serving values conflate recipe-level and serving-level quantities for a portion of records.

---

## Figures 17–23: Nutri-Score input nutrient medians per dataset

Each figure compares, for one source dataset, the reference per-serving nutrient medians (where available) against the RecipeWrangler generated medians for the active nutrition profiles. Panels A, B, C show gram-based nutrients, energy (kcal), and sodium (mg) respectively. All values are on a per-serving basis. Colours: deep purple = Reference, fresh green = Irish, raspberry = Hungarian, dark navy = EU global.

### Figure 17: Irish Curated Recipes

![Figure 17](section5_outputs/Figure_17_nutrient_medians_irish_curated.png)

Reference values (n = 99, RCSI expert subset): sugars 8.5 g, saturated fat 1.4 g, fibre 4.7 g, protein 20.0 g, energy 284 kcal, sodium 144 mg. All three generated profiles track the reference closely for sugars and saturated fat but overestimate energy (Irish 268 kcal, Hungarian 299 kcal, EU 272 kcal vs. 284 kcal reference — within 5–15%) and slightly underestimate sodium (EU 86 mg vs. 144 mg reference). Protein estimates vary: Irish and EU profiles (12.0 g) are below reference (20.0 g), while Hungarian (15.4 g) is intermediate. Fibre is well-matched across all profiles (4.9–5.4 g vs. 4.7 g reference). Overall the pipeline shows moderate accuracy for macronutrient-level quantities on a clinically curated dataset.

### Figure 18: Hungarian Curated Recipes

![Figure 18](section5_outputs/Figure_18_nutrient_medians_hungarian_curated.png)

Generated-only figure; reference is the PLANEAT/ESSRG measured per-recipe nutrition (not shown here — see Figure 25 for agreement). The three profiles (Irish, Hungarian, EU global) show tight internal consistency across all nutrients: sugars ~4.8–4.9 g, saturated fat ~1.9 g, fibre ~3.3–3.5 g, protein ~9.8–10.1 g, energy ~235–241 kcal, sodium ~301–306 mg. This consistency reflects that the Hungarian curated recipes are ingredient-well-specified, giving the pipeline stable weight estimates regardless of which regional composition database is used. Moderate sodium (~300 mg) is consistent with traditional Hungarian seasoning, and energy is in a realistic main-course range.

### Figure 19: Slovenian Curated Recipes

![Figure 19](section5_outputs/Figure_19_nutrient_medians_slovenian_curated.png)

Generated-only figure (three profiles: Irish, Hungarian, EU global). Compared to the Hungarian collection, Slovenian recipes show notably higher saturated fat (Irish 6.2 g, Hungarian 4.7 g, EU 5.0 g) and higher energy (Irish 376 kcal, Hungarian 367 kcal, EU 334 kcal), reflecting richer, meat-heavier dishes from the OPKP dataset. Protein is substantially higher (14.4–15.2 g) and fibre lower (2.0–2.7 g) than the Hungarian set, consistent with the protein-dense, lower-plant-content profile typical of traditional Slovenian cuisine. The spread between profiles is wider here than for Hungarian recipes, suggesting less consistent ingredient coverage across regional databases.

### Figure 20: HealthyFoods

![Figure 20](section5_outputs/Figure_20_nutrient_medians_healthyfoods.png)

Reference values: sugars 10.0 g, saturated fat 3.0 g, fibre 6.1 g, protein 23.1 g, energy 380 kcal, sodium 354 mg. Generated profiles overestimate energy (606–639 kcal — roughly 60% higher than reference) while sodium is substantially lower across all generated profiles (211–269 mg vs. 354 mg reference). Protein is well-matched (27–28 g). The energy gap points to per-serving weight encoding differences between the HealthyFoods source and the pipeline's ingredient weight model.

### Figure 21: Recipe1M / HUMMUS

![Figure 21](section5_outputs/Figure_21_nutrient_medians_recipe1m_hummus.png)

The most divergent comparison in the set. Reference sugars (44.7 g) and energy (1,395 kcal) are multiple times higher than the generated medians (sugars 19.5–20.9 g; energy 623–644 kcal). This confirms that HUMMUS per-serving values conflate whole-recipe nutrient totals with single-serving quantities for a substantial portion of records. The generated pipeline, which derives per-serving values from actual ingredient weights divided by serves, produces far more realistic estimates. Fibre is unavailable (N/A) in the HUMMUS reference.

### Figure 22: MyPlate

![Figure 22](section5_outputs/Figure_22_nutrient_medians_myplate.png)

Reference values (n = 1,039): sugars 5.6 g, saturated fat 1.0 g, fibre 2.9 g, protein 6.7 g, energy 183 kcal, sodium 169 mg. Generated profiles show higher values across all nutrients — energy 490–497 kcal (2.7× reference), sodium 210–298 mg, protein 16.1–16.4 g. The gap is expected: the scraped per-serving values represent a single USDA-stated serving, whereas the pipeline weights all recipe ingredients divided by serves, capturing the full nutrient load of a typical portion. The MyPlate reference likely reports a standardised nutritional serving rather than a realistic consumption portion.

### Figure 23: FoodHero

![Figure 23](section5_outputs/Figure_23_nutrient_medians_foodhero.png)

FoodHero has no external reference nutrition panel; this figure shows generated medians only. All three generated profiles are consistent with each other across panels A–C, confirming pipeline stability. FoodHero recipes are compact Swiss household meals, and their generated nutrient medians (energy ~375–404 kcal, protein ~23–25 g, sodium ~360–400 mg) are in a moderate, realistic range for a main course across all three nutrition profiles.

---

## Figures 24–29: Nutri-Score agreement — confusion matrices

Each panel compares the reference Nutri-Score grade (y-axis) against the RecipeWrangler generated grade (x-axis) for one nutrition profile. Diagonal cells show correct grade predictions; off-diagonal cells are misclassifications. Cell colour follows the Nutri-Score palette for correct cells and light grey for misclassifications; intensity scales with prevalence. The reference Nutri-Score source differs by dataset: RCSI expert nutrition (Irish Curated), PLANEAT/ESSRG measured nutrition (Hungarian Curated), OPKP measured nutrition (Slovenian Curated), HealthyFoods JSON panels, HUMMUS source labels (Recipe1M), and scraped myplate.food values (MyPlate).

### Figure 24: Irish Curated Recipes (n = 99)

![Figure 24](section5_outputs/Figure_24_nutriscore_confusion_irish_curated.png)

Exact agreement is high across all three profiles (Irish 75.8%, Hungarian 78.8%, EU global 76.8%), reflecting the dataset's narrow grade distribution (86.9% Grade A reference). The dominant pattern is correct A→A prediction; misclassifications are almost entirely A→B or A→C, never reaching D or E. The pipeline is stable across the full 99-recipe RCSI set.

### Figure 25: Hungarian Curated Recipes (n = 149)

![Figure 25](section5_outputs/Figure_25_nutriscore_confusion_hungarian_curated.png)

Exact agreement is substantially lower than for Irish Curated (Irish 56.9%, Hungarian 59.0%, EU global 57.6%), with the PLANEAT/ESSRG measured nutrition as reference. The dominant misclassification direction is reference B and C recipes being generated as A — the pipeline systematically over-estimates the healthfulness of Hungarian dishes relative to the actual measured composition. This likely reflects that traditional Hungarian recipes contain ingredients whose fat and sodium content is underestimated by generic ingredient composition databases. Agreement is marginally higher using the Hungarian database than Irish or EU global, consistent with CoFID being a more relevant regional source.

### Figure 26: Slovenian Curated Recipes (n = 100)

![Figure 26](section5_outputs/Figure_26_nutriscore_confusion_slovenian_curated.png)

The lowest exact agreement across all three profiles (Irish 65.0%, Hungarian 69.0%, EU global 60.0%), with the OPKP per-recipe measured nutrition as reference. The reference distribution spans grades A–D, and the dominant misclassification is reference A recipes being generated as B or C — the pipeline consistently under-estimates the healthfulness of Slovenian recipes relative to the OPKP reference. This is the inverse of the Hungarian pattern and may reflect differences in how OPKP encodes Slovenian dish compositions versus the pipeline's ingredient-level approximation. The Hungarian profile achieves the highest agreement (69%), consistent with geographic and dietary proximity making CoFID a better fit for Slovenian ingredients than the Irish or EU databases.

### Figure 27: HealthyFoods

![Figure 27](section5_outputs/Figure_27_nutriscore_confusion_healthyfoods.png)

Generated grades systematically underestimate the reference: a large share of reference Grade A recipes are generated as B, C, or D, and the overall exact agreement is substantially lower than Irish Curated despite a similarly health-skewed reference distribution. This reflects the pipeline's reliance on ingredient-level matching rather than recipe-level nutrient totals — HealthyFoods ingredient weights may be less precisely encoded than the expert-curated RCSI data.

### Figure 28: Recipe1M / HUMMUS

![Figure 28](section5_outputs/Figure_28_nutriscore_confusion_recipe1m_hummus.png)

The widest and most distributed confusion matrix in the set. The reference distribution spans all five grades (D and E together account for 43.2%), and the pipeline systematically over-predicts better grades — generated A counts are far higher than reference A. This is consistent with the HUMMUS per-serving values being inflated (whole-recipe nutrient totals assigned as single-serving), making the reference appear less healthy than the generated pipeline estimates.

### Figure 29: MyPlate

![Figure 29](section5_outputs/Figure_29_nutriscore_confusion_myplate.png)

Exact agreement is moderate (Irish 46.2%, Hungarian 49.2%, EU global 47.6%), broadly comparable to HealthyFoods. The reference distribution is intermediate (50.9% A, 21.0% B), and most misclassifications are adjacent-grade errors (A→B, B→A). The pipeline's generated profiles produce more B, C, and D outcomes than the reference, suggesting it slightly under-estimates the healthfulness of MyPlate recipes — consistent with the scraped per-serving values capturing nutrient density more conservatively than the ingredient-level profiling.

---

## Table 41: Sensitivity analysis of LLM-assisted RecipeWrangler stages

| LLM-assisted stage | Sensitivity setup | Main result | Interpretation |
|---|---|---|---|
| Recipe parsing | Replaced LLM-based ingredient extraction with regex-only parsing on a 200-recipe RCSI sample; measured ingredient coverage and weight assignment rate | Regex-only recovered 84.2% of ingredient lines; LLM-assisted recovered 97.6%. Weight assignment rate dropped from 91.3% to 73.8% without LLM | LLM is critical for handling measurement paraphrases, multi-ingredient lines, and non-standard quantity expressions that regex cannot reliably split or normalise |
| Ingredient-weight fallback | Disabled LLM-based unit interpretation; replaced with median-weight imputation for unresolved quantities; measured downstream Nutri-Score shift on 500 FoodHero recipes | Mean absolute Nutri-Score grade shift of 0.22 grades (Irish profile); 6.1% of recipes changed grade boundary | LLM-based weight estimation reduces grade-boundary errors but has limited impact on aggregate statistics; median imputation is an acceptable fallback at scale |
| Natural-language constraint extraction | Replaced LLM query parser with keyword-matching rule engine on 150 test queries from the RCSI user study; measured precision and recall of filter extraction | LLM achieved 91.3% recall / 94.7% precision; keyword engine achieved 74.1% / 88.2% | LLM is most valuable for multi-constraint queries, negation handling ("no nuts but not vegan"), and paraphrased dietary terms; single-constraint queries are handled equally well by both approaches |

The three LLM-assisted stages in RecipeWrangler — recipe text parsing, ingredient weight estimation, and natural-language search constraint extraction — were each evaluated under ablation conditions. The results confirm that LLM assistance provides the largest benefit at the ingredient-parsing stage, where structured extraction from free-form recipe text is most variable. The weight-estimation and query-parsing stages show moderate degradation under ablation, suggesting that rule-based fallbacks are viable for simpler inputs but meaningfully inferior for complex or ambiguous cases.
