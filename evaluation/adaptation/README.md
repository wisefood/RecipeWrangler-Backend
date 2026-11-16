# Adaptation evaluation

Four adaptation modes on the Living Lab recipes (537 screening-ready: Curated Irish, Hungarian, Slovenian), each in its native regional profile. Every result is an unsaved draft (`recipe_ready=false`); nothing is deployed. Design and rules: [docs/ADAPTATION.md](../../docs/ADAPTATION.md). State: 13 November 2026. Dataset: [data/scope.json](data/scope.json) (the 537 recipes with region and baseline run). All counts: [results/summary.json](results/summary.json).

## Results

| Mode | Result | End-to-end example |
|---|---|---|
| Nutrition | **363 / 537 adapted** (Irish 218, Hungarian 84, Slovenian 61); 63 move up a Nutri-Score letter | Pork and apricot kebabs -> turkey and apricot kebabs, B -> A |
| Cost | **48** recipes with a suggestion (change >= 10% of recipe cost, saving >= 5% and >= EUR 0.05 per serving) | Grilled salmon cutlets -> trout, EUR 3.80 -> 1.91 per serving, A -> A, still pescatarian |
| Sustainability | 433 scored, **~150** with a sensible lower-CO2e candidate (reviewed AGRIBALYSE mapping, sign-off pending) | Roast lamb -> roast turkey, 1.82 -> 0.23 kg CO2e per serving (pipeline), A -> A |
| Time | **49** recipes with an overlap candidate (Irish 43, Hungarian 6, Slovenian 0); 69 already schedule in parallel, 14 have multi-timer steps | Roast pork with ratatouille: "While the potatoes roast, dice the tomatoes...", 5-8 min saved (estimate) |

Nutrition grade shifts: A->A 231, B->A 33, B->B 43, C->A 6, C->B 17, C->C 10, D->B 1, D->C 6, D->D 16. Changes: food swaps 95, ingredient replacements 79, extra vegetables 102, amount cuts 61, product variants 27.

## How a result is produced

1. Code finds candidates (swaps, cuts, cheaper or lower-CO2e foods, time overlaps) and filters them by culinary role and the allergen/diet gate.
2. Code recalculates the recipe from the saved profile (baseline reproduced for 537 of 537) and keeps only candidates with a visible gain and no grade drop.
3. An LLM (qwen3-32b) picks the best candidate and rewrites the recipe; validators keep every unchanged step and quantity word for word.
4. Each result carries a `benefit` block for the UI: a free-text tip from the LLM with the measured gain filled in by code (Nutri-Score/nutrient change, EUR, kg CO2e or minutes).

## Checks and limits

- End-to-end profiling of original vs final text (nutrition, 41 recipes): 30 pass, 6 baseline-reproduction only, 4 pipeline noise, 1 fail.
- The LLM text exists for an earlier result set (402 of 441); the current 363 need an LLM re-run. Cost, sustainability and time are single end-to-end examples, not full LLM runs.
- Nutri-Score values are input estimates, not measured cooked dishes or taste tests. Time savings are model estimates of hands-on prep, capped by the source cooking window.
- Time overlap independence is a word-overlap heuristic and needs review; most Hungarian and Slovenian recipes have one step, so overlap rarely applies.
- Recipes with held baselines are excluded.
