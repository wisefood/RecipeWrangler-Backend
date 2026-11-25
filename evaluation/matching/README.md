# Matching evaluation

Did the matcher pick the same food record as the expert for each ingredient line? Last updated 11 November 2026.

## Data

Expert-labelled recipes from two living labs: for each ingredient line, the expert's chosen composition record and grams.

| Lab | Recipes | Ingredient lines | Expert's table |
|---|---:|---:|---|
| Hungarian (ESSRG) | 149 | 980 | CoFID 2021 (UK) |
| Slovenian | 100 | 787 | Slovenian food table 2025 |

Example gold row: `80 g Pasta, white, dried, raw` -> `cofid:11-716`, 80 g. The matcher was tuned on these recipes, so this is development evidence. Living-lab lines are copied table descriptions, which is easier than real user wording.

## Results (current code, 6 Nov)

| Outcome | Hungarian | Slovenian |
|---|---:|---:|
| Exact same record | 690 | 633 |
| Same food, other table | 256 | 135 |
| Same food, different state or part | 0 | 9 |
| Different food | 11 | 3 |
| No match / gold not in catalogue / not aligned | 23 | 7 |
| **Correct food, of aligned lines** | **946/957 (98.9%)** | **768/780 (98.5%)** |

- 120 Slovenian "same food" verdicts are LLM-judged and unreviewed.
- Most "harmful" nutrient flags are table differences for the same food (garlic 158 kcal in NEVO against 98 in CoFID).
- Names only, without the source line, the real error rate is about 4%.

Full corpus, release 2 against the served release 1 (309,536 ingredient occurrences, 30,972 profiles): matched 92.3% -> 94.3%; complete energy 42.9% -> 51.7%; Nutri-Score grade kept in 2,188 of 2,355 profiles graded in both, improved in 120, worsened in 47.

Frozen 100-line sample outside the labs (silver, LLM-judged): 86 correct, 3 close, 1 wrong state, 4 wrong food, 6 unmatched.

## Files

| File | Contents |
|---|---|
| `data/raw/` | The two experts' original workbooks (local only: Living Lab partner data, gitignored) |
| `data/gold.csv` | One row per expert-labelled line: gold record, code, grams (local only until partners agree to publication) |
| `results/predictions.csv` | Our match per line, with the identity class and nutrient class (local only, contains partner recipe lines) |
| `results/scores.json` | Summaries: gold identity (current and v31), release 1 vs 2 comparison |
| `results/corpus_sample.json` | The 100-line sample with judge labels |

Join `gold.csv` and `predictions.csv` on `(lab, recipe_id, original_line)`.
