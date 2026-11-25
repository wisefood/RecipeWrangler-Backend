# Allergen and diet evaluation

Last updated 9 November 2026. Local code only; stored diet data was not refreshed (stored diets v1, code v10).

## Data

| Test | Data | Reference labels |
|---|---|---|
| Allergens | EU Allergen Dataset (Hugging Face, restaurant menus, revision `179fafa1`): 1,300 recipes sampled, 100 pilot + 1,000 random test + 200 challenge | Supplied automated labels (silver). 11,697 of 55,638 rows have a primary label missing from the broad label, so the labels contradict themselves. |
| Diets | 100 TASTEset recipes, run from saved parsed output through the graph | Assistant-reviewed from ingredient text, not expert labels |

Allergen ingredient text is not stored here: fetch the dataset at the revision above and select rows by `csv_line`.

## Allergen results (1,100 pilot + random recipes, raw ingredient lines)

| Measure | Result |
|---|---:|
| Confirmed allergens, precision | **98.99%** (2,146/2,168) |
| Confirmed allergens, recall | **82.13%** (2,146/2,613) |
| Missed positives that still get a "possible" warning | 275 |
| Missed and unflagged | 192 (168 recipes) |
| Warnings vs broad labels: recall / precision | 96.04% / 79.81% |

- Weakest allergens by recall: sulphites 22%, gluten 76%, soy 86%.
- The 200 challenge recipes found all 10 supplied lupin positives.
- Before the 9 Nov fixes: 98.94% precision, 81.78% recall.

## Diet results (100 TASTEset recipes)

| Diet | Positive recall, 9 Nov projection | Stored graph | Unknown, projection |
|---|---:|---:|---:|
| Vegan | 19/22 (86%) | 16/22 | 10/100 |
| Vegetarian | 40/61 (66%) | 37/61 | 35/100 |

- Suitable precision on definite references: no false "suitable" calls in the graph run (16/16 vegan, 37/37 vegetarian).
- The projection is an offline update over saved traces, not a fresh graph run. Negative cells carry over unchanged.
- Raw-text NutriCuisine replay (1,100 recipes): no positive coverage, because raw lines don't satisfy the parsed-name rules. 28/28 negative controls were rejected.

## Limits

- Every label is silver or assistant-reviewed, and the detectors were fixed against these same sets.
- Not a safety certification. Possible warnings are not absence claims.
- Other tags (course, cuisine, nutritional claims) are not evaluated here.

## Files

| File | Contents |
|---|---|
| `data/allergens_gold.csv` | 1,300 recipes with primary and broad reference allergens |
| `data/diets_gold.json` | 100 recipes: original lines, expected vegan/vegetarian, reason |
| `results/allergens_predictions.csv` | Confirmed and possible allergens per recipe, before and after the 9 Nov fixes |
| `results/diets_predictions.csv` | Per recipe and diet: expected, stored graph decision, 9 Nov projection |
| `results/scores.json` | Full metrics, per allergen |
