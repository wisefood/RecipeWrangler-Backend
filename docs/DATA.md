# Data and reference provenance

Updated 30 October 2026. This guide replaces historical dataset inventories and import-status notes. Counts below identify saved evidence and local files, not a fresh live-database census.

## Living-lab reference sets confirmed locally

| Set | Local source and stored reference key | Reference basis and limits |
|---|---|---|
| Irish RCSI | `data/recipes/irish_safefood/Irish Recipes_updated_22.6.26.xlsx`; `safefood_rcsi` | Two recipe sheets supply per-100-g and per-serving nutrition, servings, raw/cooked weight and yield fields. Historical import joined 99 recipes; the final comparison uses 98 reference recipes. Audit the join/exclusion difference. The workbook does not establish that its panels are laboratory-measured. |
| Irish SafeFood web | `data/recipes/safefood_web/`; `safefood_web` | Scraped published per-serving panels, distinct from RCSI. Final comparison has 327 recipes. RCSI overlaps this set; do not pool both as independent recipes. |
| Hungarian ESSRG/PLANEAT | `data/recipes/essrg/PLANEAT T442 MEAL DB LL ESSRG.xlsx`, `ESSRG_recipes_clean.json`; `planeat` | Prepared JSON contains 149 meals. The source assigns CoFID food IDs and g/ml quantities; reference nutrients are calculated from those assignments, not supplied independent dish assays. All 149 prepared rows currently have `serves_source=llm_estimate`. Current prepared per-serving values inherit that uncertainty. The frozen evaluation instead records one serving / given for all 149; its serving basis must be reconciled separately. |
| Slovenian OPKP/EuroFIR | `data/recipes/slovenia/Slovenian_Recipes.xlsx`; `slovenian_original` | Workbook has 100 recipes, ingredient quantities, consumed amount, yield, and component values. Import treats nutrition as per 100 g, derives servings from mass/yield and consumed amount, and derives per-serving values. Verify units and this conversion directly before scoring. |

[CoFID](https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid) is the **UK Composition of Foods Integrated Dataset**. Hungarian recipe provenance and the nationality of a composition table are separate facts. Earlier notes calling CoFID a Hungarian national table or all curated references laboratory ground truth are superseded.

The Slovenian workbook inspection found 100 entries for each core component (energy, fat, saturated fat, carbohydrate, sugar, protein, fibre and salt). This is raw field presence, not a validation of units, values, missing-marker conversions, or cooking basis.

Irish salt fields are converted by `sodium_mg = salt_g × 400`. Retain original salt values, unit, conversion, denominator, and source row. Do not interchange sodium mg with salt g or energy kJ with kcal.

## What can be evaluated without new expert annotation

- Supplied food IDs can support ID-resolution and calculation checks against their source release. They do not independently validate the source food choice or every cross-table alternative.
- Explicit source masses can support quantity checks after raw/edible/drained/cooked and g/ml bases are established. An estimated serving count cannot validate its own per-serving result.
- Source panels support end-to-end reference comparisons after recipe formulation, serving, yield, and units agree. Describe these as reference deviations unless analytical measurement is established.
- Public annotated datasets and internally authored expected cases cover remaining stages. Label origin, authoring method, ambiguity and limitations must remain visible. No expert-review campaign is assumed for this plan.

## Corpus and composition assets

The saved activation evidence records 7,743 recipes, four calculated regions (EU/IE/HU/SI), and 30,972 active profiles. The expanded catalogue contains 46,668 searchable composition IDs: global EU 44,541, Irish 1,307, Hungarian 500, Slovenian 320. These are indexed source representations, not 46,668 biologically unique foods. USDA remains useful for household portion references; it is not the active global nutrition pool.

Current source registries and import instructions are in `src/recipe_wrangler/catalog/sources.py`, [scripts/README.md](../scripts/README.md), and [scripts/import/README.md](../scripts/import/README.md). Recipe1M/HUMMUS are historical resources and are retired from the current serving corpus; old million-recipe counts must not be reused as present statistics.

HealthyFoods and MyPlate have published reference panels useful for secondary evaluation, with their own joins, denominators and source anomalies. FoodHero has no usable independent nutrition reference in the preserved final comparison. Its coverage and cross-region consistency can still be measured.

Operational assets stay under `data/`, `artifacts/`, `exports/`, and dump directories. Do not commit source workbooks, production snapshots, credentials, or bulk recipe exports.

## Preserved baseline evidence

| Location | Purpose |
|---|---|
| `analysis/nutrition_finalization_2026-10-29/reference_comparison/` | Final old/new reference comparison, all nutrients and completeness denominators |
| `analysis/nutrition_finalization_2026-10-29/` | Before/after profile summaries, quantity repairs, reference anomalies, contributors and outliers |
| `analysis/matching_production_2026-10-29/` | Activation, audit, rollback rehearsal and reference fingerprint |
| `analysis/nutrition_inventory_2026-10-26_catalogue_baseline/` | Frozen previous searchable catalogue |
| `analysis/irish_full_catalogue_baseline_2026-10-26/` | Expanded catalogue and controlled Irish evaluation baseline |
| `analysis/regional_nutrient_drift/` | Historical regional drift, with mixed input generations; useful history, not a current regional audit |
| `artifacts/adaptation/{e2e_2026-10-30,real_recipe_readiness_2026-10-30,nutritional_facets_2026-10-30}/` | Synthetic and real adaptation diagnostics, kept distinct |

The final saved audit reports zero serving/profile mismatches, regional quantity mismatches, missing active graph runs, graph quantity mismatches, and pending projection jobs **at its recorded timestamp**. Recheck current state before any production operation.

## Next corpus audit

Work from a versioned read-only snapshot with source/release hashes. Count active/disabled recipes, duplicate IDs/formulations, empty names/instructions, ingredient/measurement length mismatches, unresolved source alignment, missing servings/duration, instruction-only foods, and missing source URLs. Report all by source and region.

For each nutrient, distinguish missing source value, unresolved match, unsupported unit/basis, zero/invalid weight, known source anomaly, and unsupported total. Rank missing contributions by occurrence frequency and nutrient impact. Audit composition units, nutrient definitions, trace markers, duplicate releases, values per food versus per fatty-acid mass, density, and edible/yield factors. Inspect samples of unflagged rows too.

Outliers are review candidates. Preserve source and pre-fix values; exclude only with a documented reason and show sensitivity with/without flagged references. Do not delete a recipe or clip calories solely because it exceeds an arbitrary per-serving cutoff.

## Future ingestion checklist

1. Record source files, hashes, access/usage terms, units, languages, and field meanings.
2. Keep reference labels separate from calculated profiles and inferred metadata.
3. Normalize IDs and ingredient occurrences without losing original text or recipe components.
4. Resolve quantities once; persist provenance, missing markers and serving basis.
5. Validate source-specific conversions before regional profiling.
6. Import through supported save/projection hooks; verify graph, PostgreSQL and catalogue agreement.
7. Compare coverage and reference deviations before activating a refresh. Preserve the previous release and rollback evidence.

## History

Dated implementation notes up to 12 November 2026 are kept locally in `analysis/docs-history-2026-11-12/`.
