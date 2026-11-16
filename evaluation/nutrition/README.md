# Nutrition evaluation

Calculated nutrients per recipe, compared with the nutrition panels published with the recipes. Last updated 8 November 2026 (metrics); repair work through 11 November is listed at the end.

## Data

| Reference set | Recipes scored (energy) | Reference |
|---|---:|---|
| RCSI (SafeFood, Irish) | 98 | Source workbook panels, per serving |
| HealthyFoods | 4,755 | Published panels, per serving |
| MyPlate | 1,008 | Published panels, per serving |
| SafeFood web | 320 | Published panels, per serving |
| Hungarian (CoFID control) | 68 of 149 | Calculated from the same CoFID table the pipeline matches |
| Slovenian | 100 | Same, per 100 g |

- Panels are source-provided, not laboratory measurements. 111 HealthyFoods and 5 SafeFood panels were removed as unreliable.
- Hungarian and Slovenian agreement only shows the calculation reproduces its source tables. It does not test parsing or weights.
- All recipes are development-exposed.

## Results: mean absolute error per serving (median % error in brackets)

| Nutrient | RCSI | HealthyFoods | MyPlate | SafeFood web |
|---|---|---|---|---|
| Energy kcal | 41.5 (9%) | 61.4 (12%) | 36.1 (13%) | 52.1 (11%) |
| Protein g | 3.13 (10%) | 3.62 (11%) | 2.20 (17%) | n/a |
| Carbs g | 4.83 (11%) | 8.59 (17%) | 6.93 (23%) | n/a |
| Fat g | 2.73 (24%) | 3.75 (23%) | 1.87 (24%) | 3.04 (25%) |
| Sat. fat g | 0.84 (22%) | 1.37 (30%) | 0.73 (29%) | 1.05 (31%) |
| Sugar g | 2.50 (22%) | 3.37 (22%) | 2.09 (21%) | 2.47 (20%) |
| Fibre g | 1.64 (20%) | 2.26 (25%) | 1.20 (24%) | n/a |
| Sodium mg | 121.8 (38%) | 154.5 (32%) | 83.6 (28%) | 193.7 (32%) |

- Strongest: energy, protein, carbs (about 10-13% median error). Weakest: sodium, fat and saturated fat (20-38%).
- RCSI via the actual API, same 98 recipes, before and after the 8 Nov fixes: energy 45.9 -> 41.5, carbs 6.1 -> 4.8, protein 3.4 -> 3.1, sodium 121.0 -> 121.8 (slightly worse).
- Sodium bias on RCSI is +58 mg per serving (stock, "salt to taste"). On SafeFood web it is -21 mg, so the direction differs by set.
- Hungarian and Slovenian: 87-100% of nutrient cells within 20%.

## Files

| File | Contents |
|---|---|
| `data/references.csv` | Reference value per recipe and nutrient (HealthyFoods, MyPlate, SafeFood web) |
| `results/predictions.csv` | Our value for the same cells, with status and absolute error |
| `results/metrics.csv` | MAE, bias, median, P90, within-10/20% shares per dataset and nutrient, all six sets |

RCSI, Hungarian and Slovenian per-recipe values are in `analysis/nutrition-api-e2e-2026-11-08/` and `analysis/nutrition-e2e-2026-11-05/`.

## Repair work, 10-11 November

Baseline fixes for the 576 adaptation recipes are summarised under [Baseline repairs](#baseline-repairs) below. They are not evaluation results. `held-baseline-repair-2026-11-11/` is still in progress.

## Baseline repairs


Data fixes to the stored nutrition profiles of the 576 Living Lab recipes (Irish, Hungarian, Slovenian), so the adaptation work has complete baselines. These are not accuracy evaluations and do not change the results in the README. Each batch was rehearsed with a rollback, tried on one recipe, applied transactionally and read back exactly. No hosted model calls. Full evidence, per-recipe before/after profiles and rollback backups are in `analysis/nutrition-repairs-2026-11-12/<batch>/`.

## Batches

| Batch | What changed | Scope |
|---|---|---|
| unmatched-repair (10 Nov) | 38 of 44 unmatched ingredients matched (aliases, spelling, proxies); 2 total-mass errors fixed | 35 Irish recipes, 41 profiles |
| record-rematch (10 Nov) | Same-food records with missing nutrients replaced by complete ones (for example sea salt now has sodium) | 96 records, 82 profiles |
| source-mass-repair (10 Nov) | Wrong gram amounts replaced with the amount stated in the recipe (4 chicken fillets 4 g -> 520 g) | 18 corrections, 15 recipes, 60 profiles |
| strict-record-fallback (10 Nov) | Same-label record swaps under a strict selector | 28 records, 23 profiles |
| reviewed-fill-refresh (11 Nov) | Existing reviewed estimates applied to missing nutrient values, enabling a Nutri-Score | 47 profiles |
| reviewed-fill-refresh-round2 (11 Nov) | Second pass of the same | 21 profiles |
| occurrence-reconstruction (11 Nov) | Rebuilt 8 Hungarian recipes from the source archive; restored 14 omitted source lines in 12 Irish recipes | 8 + 12 recipes |
| fill-provenance-repair (11 Nov) | Source labels added to 10 stored values; no values changed | 2 profiles |
| score-tolerance-refresh (11 Nov) | Scores recomputed under the existing 2% missing-mass rule | 2 profiles |
| living-lab-gaps (10 Nov) | Read-only list of recipes with incomplete nutrition (superseded) | 219 profiles |

## Effect on the 576-recipe cohort

| Stage | Complete | Incomplete | Minor gaps | Ready for adaptation screening |
|---|---:|---:|---:|---:|
| Start, 10 Nov | 357 | 218 | 1 | – |
| After record-rematch | 369 | 191 | 16 | – |
| After strict fallback | 382 | 174 | 20 | 346 |
| After first fill refresh | 426 | 125 | 25 | – |
| After occurrence reconstruction | 458 | 87 | 31 | 467 (109 held) |

"Ready" means the source text, score context and targets are usable. It is not a successful adaptation.

## Rules applied

- Missing values are never set to zero. Unknown stays unknown (`minor_gaps` only under the 2% missing-mass rule).
- Fills are reviewed estimates with per-value provenance, not measurements.
- Recipe names, source text and servings are never changed. Only the nutrition record, weight or restored line is.

## Still open

- 109 recipes held (36 text disagreements, 36 score contexts, 32 missing text, 4 alignment, 1 repeated line). Work is in `evaluation/nutrition/held-baseline-repair-2026-11-11/`; 19 graph text fixes have been applied there.
- Known wrong matches among the ready recipes: korma cream -> ice cream, passata -> tomato puree, tinned tomatoes -> raw, Cajun spice -> curry powder. Soy sauce and low-salt stock have no record, so sodium is missing.
- A projection worker omits the source-revision stamp on profile activation. It needs a service update.
- Other regional copies of these recipes were not refreshed.
