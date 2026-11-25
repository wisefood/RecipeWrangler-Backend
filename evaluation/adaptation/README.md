# Adaptation evaluation

Four adaptation modes on the Living Lab recipes (537 screening-ready: Curated Irish, Hungarian, Slovenian), each in its native regional profile. Every result is an unsaved draft (`recipe_ready=false`); nothing is deployed. Design and rules: [docs/ADAPTATION.md](../../docs/ADAPTATION.md). State: 19 November 2026 (nutrition results frozen, see below). Dataset: [data/scope.json](data/scope.json) (the 537 recipes with region and baseline run). All counts: [results/summary.json](results/summary.json).

## Results

| Mode | Result | End-to-end example |
|---|---|---|
| Nutrition (19 Nov) | **284 / 537 adapted** (swap or cut on the ingredients that carry the negative points), 83 move up a Nutri-Score letter; **204 enriched** (a served-with addition, no grade claimed); 49 with no suggestion (each with a reason) | Creamy mushroom risotto: 1 g salt -> 0.75 g low-sodium salt, sodium 402 -> 108 mg per serving, B -> A |
| Cost | **48** recipes with a suggestion (change >= 10% of recipe cost, saving >= 5% and >= EUR 0.05 per serving) | Grilled salmon cutlets -> trout, EUR 3.80 -> 1.91 per serving, A -> A, still pescatarian |
| Sustainability | 433 scored, **~150** with a sensible lower-CO2e candidate (reviewed AGRIBALYSE mapping, sign-off pending) | Roast lamb -> roast turkey, 1.82 -> 0.23 kg CO2e per serving (pipeline), A -> A |
| Time | **49** recipes with an overlap candidate (Irish 43, Hungarian 6, Slovenian 0); 69 already schedule in parallel, 14 have multi-timer steps | Roast pork with ratatouille: "While the potatoes roast, dice the tomatoes...", 5-8 min saved (estimate) |

Nutrition grade shifts: A->A 157, B->A 42, B->B 25, C->A 6, C->B 23, C->C 8, D->B 4, D->C 8, D->D 11.

## Frozen nutrition dataset (19 November 2026)

[results/nutrition_results.json](results/nutrition_results.json) (every recipe: outcome, changes, per-serving values), [results/nutrition_llm_rewrites.json](results/nutrition_llm_rewrites.json) (final titles, ingredient lines, steps, notes and benefit tips), [results/living_lab_adaptations.md](results/living_lab_adaptations.md) and `.csv` (one row per recipe with the reason it cannot go higher), [results/review_table.md](results/review_table.md), [results/nutrition_e2e_check.json](results/nutrition_e2e_check.json) (verdict per adapted recipe) and [results/summary.json](results/summary.json). Matching fixes made while checking (generic meat part on an exact label, "Cream sour", reviewed fills for five records, low-sodium salt alias) change live profiling of new recipes, not stored profiles.

## How a result is produced

1. Code finds candidates (swaps, cuts, cheaper or lower-CO2e foods, time overlaps) and filters them by culinary role and the allergen/diet gate.
2. Code recalculates the recipe from the saved profile (baseline reproduced for 537 of 537) and keeps only candidates with a visible gain and no grade drop.
3. An LLM (qwen3-32b) picks the best candidate and rewrites the recipe; validators keep every unchanged step and quantity word for word.
4. Each result carries a `benefit` block for the UI: a free-text tip from the LLM with the measured gain filled in by code (Nutri-Score/nutrient change, EUR, kg CO2e or minutes).

## Checks

- End-to-end profiling of original vs final text (nutrition, 284 adapted recipes through the production profiling chain): 220 pass; the rest carry small differences between the claimed and the measured value (gain not confirmed 51, worsening 23, grade drop 1; a recipe can carry several). Of 83 claimed Nutri-Score letter gains, 73 are reproduced by the pipeline.
- LLM rewrites exist for 283 of the 284 adapted recipes (the other keeps the code-written card: its rewrite failed the cut-word check). Cost, sustainability and time are single end-to-end examples, not full LLM runs.
- Nutri-Score values are input estimates, not measured cooked dishes or taste tests. Time savings are model estimates of hands-on prep, capped by the source cooking window.
- Time overlap independence is a word-overlap heuristic and needs review; most Hungarian and Slovenian recipes have one step, so overlap rarely applies.
- Recipes with held baselines are excluded.

## Where the Nutri-Score points are (lever statistics)

Computed 16 November 2026 from the stored score breakdown (`nutri_score_breakdown`), one profile per recipe. Only 3,178 of 7,743 recipes have a Nutri-Score; the Living Lab set has all 537. Full numbers: [results/score_levers.json](results/score_levers.json).

| | All scored recipes (3,178) | Living Lab (537) |
|---|---|---|
| Grades | A 63%, B 21%, C 10%, D 5%, E 1% | A 65%, B 21%, C 9%, D 5% |
| Largest negative component | energy 29%, sodium 17%, sugar 13%, saturated fat 12%, none 29% | energy 33%, sodium 29%, saturated fat 18%, sugar 11%, none 8% |
| Mean points earned (of 5) | fibre 1.3, protein 2.1, fruit/veg 0.6 | fibre 1.6, protein 3.1, fruit/veg 0.6 |
| Largest unearned positive component | fruit/veg 44%, fibre 43%, protein 13% | fruit/veg 60%, fibre 30%, protein 10% |

- A and B recipes (84%) carry few negative points (mean 1.7 for A, 3.4 for B). Their lever is on the positive side: fibre earns 1 point or less in 62-74% of them, and fruit/veg earns none in 73% of all recipes.
- C and D recipes are driven by the negative side: mean negative points 6.3 (C) and 10.4 (D), with saturated fat the largest component in D (3.9 points).
- Protein points are not counted when negative points are high: protein is excluded for 20% of C and 67% of D recipes (100% of Living Lab D), so adding protein cannot raise the score there.
- Substitution removes negative points; it does not add fibre, protein or vegetables. A mechanism that adds an ingredient is the lever for the A and B majority and is not yet built.

## Nutrition rework, 16 November

Dev set: the 537 Living Lab recipes (offline review of the saved inputs, no LLM, no writes).

| | 13 Nov | 16 Nov |
|---|---|---|
| Adapted (swap or cut, judged per serving) | 363 | 296 |
| Move up a letter | 63 | 90 |
| Enriched (served-with addition) | - | 196 |
| No suggestion | 174 (A 117, B 37, C 14, D 6) | 45 (A 37, B 6, C 2) |

Adapted fell because swaps and cuts now touch only ingredients that carry >= 20% of a nutrient the recipe scores negative points for; healthy ingredients (vegetables) are no longer swapped, and those recipes get an addition instead.

- Curated table: low-sodium salt (swap plus a quarter less, 61 recipes), canned pulses to cooked pulses, skinless poultry, tuna in water, poultry sausages, lean ham, reduced-sugar ketchup and jam, feta to cottage cheese or ricotta, pastry to filo. Oil and butter may be cut by a quarter (owner, 16 Nov); cheese is not cut.
- Descriptor search route: the same food with a healthier descriptor (lower-fat, wholegrain, lower-salt, lower-sugar) is found in the records, not only from the curated table. With the table hidden, search finds the same target in 64 of 130 recipes that use a curated row (49%) and another swap on the same line in 11 more; before the filter fix it was 10 of 124. The cause was a too strict label-word filter in the search, not an over-curated table: 124 of 162 curated swaps pass the search filters; the rest cross food type or change state on purpose (butter to oil, puff pastry to filo, canned to cooked pulses). Search guards: main food word outside the descriptor, no new type word (rye for wheat, goat for cow), the cut stays (whole, breast, mince, slices, crumbs), bread kind and self-raising stay, composites, drinks and dips are never targets.
- Regional priority: the recipe's regional table first, then the general EU collection (EU mode uses EU only). On 50 sampled recipes rerun in EU mode, 33 get the same suggestion, 8 the same type from a different record, 9 a different suggestion; 10 more could not be compared because their stored EU profile is stale.
- Poultry is not swapped for red meat (WCRF advises replacing red meat with poultry); produce swaps must not lose fibre; cross-database graph swaps must meet the absolute policy floor.
- Swap rules: same-family swaps only (e.g. lentils for lentils, leafy greens for leafy greens), same food under another record name blocked, vague average records blocked, poultry swaps with meat only, shellfish and offal handled, protein and fibre losses advisory.
- Protected foods, never swapped or cut: eggs, whole fruit, oily fish, whole nuts and seeds, avocado.
- An A recipe staying A needs a 10% gain on a score nutrient.
- Diagnosis (`diagnosis.py`) reads the Nutri-Score breakdown: excess (what to reduce) and gaps (fibre, fruit/veg, protein where the protein rule counts it).
- Additions run only when no swap or cut qualifies. They are judged per serving, not by the Nutri-Score, because added weight dilutes the per-100 g score (a 15-point drop for Welsh rarebit came from dilution and the protein rule). An addition must add >= 2 g fibre, >= 5 g protein or an 80 g fruit/veg portion per serving; the added food must itself be low in saturated fat (<= 1.5 g/100 g) and salt (sodium <= 120 mg/100 g); it may add at most 150 kcal per serving. Protein: 200 g low-fat yogurt (passes in 11 of 23 recipes that lack protein; the rest are dairy-free). Median gain: fibre +3.3 g, energy +50 kcal per serving. The former extra-veg fallback is now an addition, not an adaptation.

Notes: 48 adapted changes come from the graph/FoodOn route; 74% of composition records lack a stated preparation state; scores follow the original Nutri-Score algorithm (no cheese exception, not the 2023 update). This is the set of adapted recipes for now: it is the dev set on which the rules were fixed, and the rules are applied to other recipes the same way.

Verifiability rules: where the production profiler cannot tell two foods apart from their wording, the swap is not offered (any swap on a line that names ham: the profiler returns the same ham record for every ham wording). For swaps the profiler does tell apart, `PROFILER_PIN` in `nutrition_swap_rules.py` fixes the target to the record the profiler selects for the written line (wholewheat pasta, boiled chickpeas, filo, wholemeal wraps), so the claimed gain is the gain the pipeline measures. A recipe with no suggestion carries a reason code (`nutrition_reasons.py`: already healthy, main food defines the dish, gain too small, no healthier option) and a note for the user.

Allergens and diet type: the stored rows are checked only against the recipe's own restrictions (vegan, vegetarian, pescatarian, lexical allergens in the recipe). The adapt APIs take the user's allergens and diet type as input. With none given they return the stored row; with any given they run the pipeline and drop every candidate that does not respect them (specified, not yet built).
