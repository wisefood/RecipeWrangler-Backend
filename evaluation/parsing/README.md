# Parsing evaluation

Ingredient-list parser (Llama 3.3 70B via OpenRouter), scored on public TASTEset recipes. Gold was adjudicated by the assistant, not by independent labellers. Last updated 11 November 2026.

## Results

| Set | Recipes | Quantity | Units | Clean recipes | Parser |
|---|---:|---:|---:|---:|---|
| `baseline-100` (original baseline) | 100 | 95.4% | 95.6% | – | early |
| `fresh-100` (post-fix) | 100 | 99.6% | 99.6% | 63 pass / 28 fail / 9 review | fixed |
| `final-200` (unseen, frozen) | 200 | 99.2% | 99.2% | 184/200 (92.0%) | `fa5453ca` |
| `unseen-100-a` (unseen) | 100 | 98.9% | 99.3% | 94/100 | `2f02b909` |
| `unseen-100-b` (unseen) | 100 | 99.6% | 99.6% | 96/100 | `affd3de2` |

- Quantity and unit counts are eligible fields, not recipes (for example final-200: 1,061/1,070 quantities).
- Clean-recipe rates count the `optional` marker as unspecified. Under the old scorer, final-200 gives 81.5%.
- Sets use different recipes, so rows are not a paired comparison.
- Covers the ingredient-list parser only: English TASTEset, not raw-recipe extraction, other formats or Hungarian/Slovenian text.

## Files

| File | Contents |
|---|---|
| `data/raw.json` | Original TASTEset lines and annotations, all sets (`set`, `case_id`, `line`, `annotations`) |
| `data/gold.json` | Adjudicated gold per ingredient line: food, quantity/unit targets, which fields are scored |
| `results/predictions.json` | Parser output per recipe: name and measurement per ingredient |
| `results/scores.json` | Full metrics per set, with confidence intervals |

Every record carries a `set` field. Join `raw`, `gold` and `predictions` on `(set, case_id)`.
Run scripts, per-case reports and the development history remain under `analysis/parser-*`.
