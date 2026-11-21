# Weight evaluation

Estimated grams per ingredient measurement (`1 medium potato`, `2 tbsp oil`), scored against Fineli reference portion weights (see [Fineli](https://fineli.fi)). Last updated 11 November 2026.

## Data

348 Fineli cases (ingredient + measurement + reference grams): 200 from the original split (80 development, 120 held out) and 148 added later. All 348 were seen during development, so none is a clean test set. Fineli foods are not corpus foods, so read results as direction.

## Results (no LLM fallback)

| Route | Answered | Within 20% of reference, all 348 | Within 20%, answered only |
|---|---:|---:|---:|
| Legacy (served), 8 Nov | 259 | 157 (45.1%) | 60.6% |
| Structured (opt-in), 6 Nov | 60 | 31 (8.9%) | 51.7% |

Answered cases only, split by measure type (5 Nov deterministic answers):

| Legacy | n | Within 20% | Median error |
|---|---:|---:|---:|
| Volume (cup, spoon, ml) | 169 | 67% | 12% |
| Count (1 medium onion) | 95 | 38% | 26% |
| All | 264 | 57% | 18% |

Errors are balanced (median signed error -1%), so over- and under-estimates largely cancel in recipe totals.

## Why weight errors matter less for nutrition

| Weight source | Corpus lines | Corpus energy |
|---|---:|---:|
| Stated mass in recipe | 17.4% | 42.7% |
| Volume density | 33.1% | 28.9% |
| Count or item reference | 42.4% | 25.3% |
| Other | 7.0% | 3.0% |

Count items are the exposure: 25% of energy and the lowest accuracy. No LLM-weighted lines exist in the served corpus.

## Other results

- With hosted LLM fallback (5 Nov code): legacy 160/348 (46.0%), structured 144/348 (41.4%). It adds no accuracy over deterministic weights, so the served route stays legacy.
- Historical structured run on the 200-case split (3 Nov): 41.0% within 20% (development 58.8%, held out 29.2%).

## Files

| File | Contents |
|---|---|
| `data/gold.csv` | 348 cases: ingredient, measurement, reference grams, split (from the Fineli release 20 download, kept locally as `data/nutrition/composition_tables/fineli/Fineli_Rel20.zip`, gitignored) |
| `results/predictions.csv` | Grams per case from the legacy and structured routes, and whether each was deterministic or LLM |
| `results/scores.json` | All summaries above, with per-stratum detail |

Filter `predictions.csv` to `*_origin == deterministic` for the no-fallback results.
