# Ingredient matching

Updated 30 October 2026. Current behavior, preserved results and remaining work are consolidated here. [The working plan](PLAN.md) owns the task list; [OPERATIONS.md](OPERATIONS.md) owns deployment and rollback.

## Activated mechanism

The saved 29 October activation identifies release `2026-10-27-v11`, deployment mode `new`, and API mechanism `occurrence_matching_v13_quality_fixes`. Existing selections retain their original selection-version lineage. This is recorded deployment evidence; inspect current health/state before operating.

The predecessor already used deterministic rules, aliases, lexical/vector retrieval and regional logic. Describe the replacement as an extension of a guarded matcher, not a replacement for a pure nearest-neighbor system.

1. Preserve the occurrence's original wording, parsed identity, quantities and recipe context separately.
2. Extract identity, biological source, part, observed preparation/state, processing, preservation, physical form and supported nutritional qualifiers. Use FoodOn, FoodEx2, LanguaL and local rules; retain uncertainty, alternatives, modifier targets and polarity.
3. Retrieve exact/alias, lexical, facet and vector candidates from the pinned global EU plus corresponding regional catalogue.
4. Check identity, relevant properties, native annotations, state, source linkage, units and nutrient definitions. Unknowns are not contradictions, but unsupported required properties can prevent a calculation.
5. Rank compatible candidates deterministically, retaining a prior supported selection where appropriate. An exact description can recover omitted extraction evidence but cannot override a known contradiction.
6. Store observed facts, selected-record facts, alternatives, blockers, reasons, assumptions, release identity and effective grams. Keep semantic suitability separate from regional source preference.
7. Calculate complete nutrient totals only where all required contributions are supported. Store partial sums separately. An abstention is not a zero-nutrient food.

The shared quantity vector is resolved before regional selection. Saved-release verification found identical ingredient quantities across all four regions. This needs continued invariant testing for new text analysis and imports.

## Representation and graph

PostgreSQL holds the authoritative match record and active profile/run pointers. Neo4j provides projected identities, facets and occurrence links for discovery:

```text
Ingredient -> MatchingIngredientFrame -> MatchingFoodIdentity
                    |                           ^
                    v                           |
              observed facets           MatchingFoodVariant -> source facets
                                                ^
Recipe -> MatchingIngredientOccurrence -----------+
                    |
                    v
              observed facets
```

Chicken breast and thigh can share chicken identity and differ in part. Fried and boiled describe separate processing facts. These paths enable candidate discovery; they do not establish equivalent edible quantities, nutrient benefit or culinary suitability. Do not invent anatomical ontology edges merely to describe a cut.

Observed `not boiled`, intended `for frying`, actual `boiled`, alternatives and sequential transformations require different representations. Keep flat facets for retrieval, with explicit attachment/state evidence where needed. Copying a selected composition's process into observed source facts would erase uncertainty.

Useful code: `src/recipe_wrangler/matching/{models.py,runtime.py,projection_worker.py,source_context.py,evaluation/}`, the engine modules it runs (`contextual_parser.py`, `hybrid.py`, `candidate_gate.py`, `nutrition_policy.py`, `source_suitability.py`, ...; formerly `matching/experimental/`, merged 13 Nov 2026), and `repositories/matching_store.py`. `tools/nutrition_match.py` remains relevant to legacy/fallback behavior; `tools/nutritional_calculator.py` selects legacy/shadow/new mode.

## Preserved end-to-end comparison

Source: `analysis/nutrition_finalization_2026-10-29/reference_comparison/summary.csv`. Energy MAE is **kcal per serving**, on the identical complete subset in both arms.

| Dataset | Common complete / reference recipes | Previous MAE | Activated MAE |
|---|---:|---:|---:|
| SafeFood web | 179 / 327 | 65.802 | 57.717 |
| RCSI | 49 / 98 | 49.480 | 40.257 |
| Hungarian | 129 / 149 | 87.100 | 75.039 |
| Slovenian | 86 / 100 | 70.756 | 37.193 |
| MyPlate | 560 / 1,027 | 40.310 | 36.762 |
| HealthyFoods | 1,798 / 4,981 | 79.783 | 68.760 |

This is an in-house development comparison of the combined matcher, catalogue expansion and quantity corrections. It does not isolate a facet effect. RCSI overlaps SafeFood; reference provenance and serving uncertainties are described in [DATA.md](DATA.md).

All nutrients, medians, denominators and regressions remain in the original CSV. For example, RCSI carbohydrate/sugar/sodium/fibre MAE, Hungarian sodium MAE and MyPlate sodium MAE increase. SafeFood median absolute energy error increases despite lower mean error. A flagged HealthyFoods fat-reference anomaly dominates raw mean fat error; do not silently replace it.

Only 13,288 of 30,972 saved regional profiles have complete energy. The activation report records available Nutri-Scores of EU 742, HU 447, IE 1,013, SI 598. Profile presence and a successfully deployed matcher do not mean complete nutrition or validated accuracy.

## Inventory growth

| Collection | Previous searchable IDs | Activated searchable IDs |
|---|---:|---:|
| Global EU | 8,167 | 44,541 |
| Irish | 1,307 | 1,307 |
| Hungarian | 500 | 500 |
| Slovenian | 320 | 320 |
| Total | 10,294 | 46,668 |

The 36,374 additional searchable IDs comprise 8,934 already stored EU records previously absent from search and 27,440 further catalogue representations. Counts refer to `(collection, candidate_id)`, not unique biological foods or complete nutrient panels.

## Evaluation and development rules

Existing 48-case fixtures are assistant-authored development expectations. The 30-case partner calibration, 150-case pilot and 100-description blank pack are not independent gold sets without the corresponding labels/adjudication. Do not relabel predictions as correctness or tune on an allegedly held-out pack. No new external expert-labeling requirement is imposed by the working plan.

Reuse existing review fields for identity/facet corrections, acceptable source IDs, alternatives/compound disposition, valid abstention and reasons. Preserve label origin. Explicit source-assigned IDs can provide internal reference labels after release/basis checks. For internally authored cases, keep straightforward source-backed assertions separate from ambiguous nutritional equivalence judgments.

Measure retrieval recall@k before gates, accepted-match precision, coverage, no-match precision/recall, unsupported-assertion rate, facet precision/recall by family and modifier target, and quantity-weighted nutrient error. Group related recipes/wording across splits; compare BM25, dense, hybrid and facet-gated variants on fixed quantities and catalogue releases.

Research supports structured retrieval and source-aware validation, but published FoodEx2 coding or food-linking scores do not transfer directly to our nutritional equivalence task. FEAST release diagnostics already exist; inspect their release/code validity before repeating inference. Do not add learned selectors or train embeddings until labeled failures show why a simpler fix is insufficient. See [METHODS.md](../evaluation/METHODS.md) for public resources and research decisions.

## Remaining priorities

Explain incomplete nutrient totals by cause; directly measure parsing, quantities, retrieval and selection; audit observed/source facet separation; ground common unresolved aliases in original text; validate catalogue nutrient definitions; and rerun regional drift with common inputs. Handle yield, retention, drained/edible mass and consumed cooking media as explicit calculation work rather than matcher similarity tweaks.

## History

Dated implementation notes up to 12 November 2026 are kept locally in `analysis/docs-history-2026-11-12/`.
