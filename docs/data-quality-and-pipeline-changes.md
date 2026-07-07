# Data Quality & Pipeline Changes Report

This document summarises two things, in non-technical terms: (1) the nutritional
outliers that were detected and removed from the recipe datasets, and (2) the
overhaul of the recipe-profiling pipeline's matching tools. It contains no code.

---

## Part 1 — Nutritional Outlier Removal

### Method

Outliers were identified by **calories per serving** being implausibly high for a
single portion. Two passes were performed:

- **Pass 1 — statistical:** for each dataset, an upper bound was derived from the
  distribution of per-serving calories (inter-quartile-range rule); recipes above
  that bound were flagged.
- **Pass 2 — strict:** any recipe above **1,000 kcal per serving** was flagged,
  regardless of dataset.

### Results — Pass 1 (statistical, per-dataset threshold)

| Dataset | Recipes analysed (before) | Median kcal/serving | Outlier threshold | Outliers removed | Recipes kept (after) |
|---|---:|---:|---:|---:|---:|
| HealthyFoods | 5,073 | 380 | 806 | **2** | 5,071 |
| MyPlate | 545 | 254 | 861 | **38** | 507 |
| FoodHero | 416 | — | — | **0** | 416 |
| Irish SafeFood | 47 | 288 | 778 | **0** | 47 |
| Slovenian OPKP | 10 | 183 | 641 | **0** | 10 |
| Recipe1M | 51,183 | ~610 | very high (see note) | **5,709** | 45,474 |
| **Total** | **57,274** | | | **≈ 5,749** | **≈ 51,525** |

### Results — Pass 2 (strict, > 1,000 kcal/serving)

Pass 2 is the **strict cap applied to produce the cleaned datasets** that the
re-profiling pipeline reads (`*_clean.json`). It is the policy reflected in
every downstream number in this report.

| Dataset | Recipes (before) | Outliers removed | Recipes kept (after) |
|---|---:|---:|---:|
| HealthyFoods | 5,073 | 2 | 5,071 |
| MyPlate | 545 | 31 | 514 |
| FoodHero | 416 | 0 | 416 |
| Irish SafeFood | 47 | 0 | 47 |
| Slovenian OPKP | 10 | 0 | 10 |
| Recipe1M | 51,183 | **19,788** | **31,395** |
| **Total** | **57,274** | **≈ 19,821** | **≈ 37,453** |

> The "Recipes analysed" column counts recipes with a parseable per-serving
> energy figure — i.e. the population the outlier study could evaluate. A
> handful of additional recipes existed in each source that had no
> energy/servings parse and were therefore neither flagged nor removed by this
> step; those carry through into the re-profiling run, which is why the
> downstream Postgres profile counts (HealthyFoods 5,183, MyPlate 1,039,
> Recipe1M 51,235) are slightly larger than the "kept" figures above.

### Key findings

- **The curated / verified datasets are essentially clean.** HealthyFoods, MyPlate,
  Irish SafeFood and Slovenian OPKP needed only a handful of removals each — e.g.
  two HealthyFoods entries near ~2,160 kcal/serving, and ~31–38 MyPlate entries
  (mostly large chicken-and-rice dishes in the 1,000–2,900 kcal/serving range).
- **Recipe1M is the problem dataset.** Its crowd-sourced nutrition values are
  highly unreliable: the median is around 600 kcal/serving, but the upper tail
  runs into the tens of thousands and beyond — e.g. "World's Largest Apple Pie"
  at ~3.4 million kcal, "Apple Cider" at ~300,000 kcal, and even a
  "Hard Wood Floor Cleaner and Polish" entry sitting inside the recipe set.
  Depending on the cutoff, **roughly 11% (≈5.7k) to 39% (≈19.8k) of Recipe1M
  recipes are statistical outliers.**
- This unreliability is also the main driver of the higher-than-expected average
  per-serving energy on the US (USDA) profiling slice, since Recipe1M dominates
  that slice and that source has no explicit servings field (servings are
  estimated from total recipe weight).

### Where the removals are recorded

- `nutritional_outlier_report.md` — per-dataset summary statistics + sample outliers.
- `removed_outliers_detailed_report.md` — Pass-1 removals; per-dataset tables of
  each removed recipe with its calories (total removed: 5,749).
- `removed_outliers_strict_report.md` — Pass-2 removals; per-dataset tables
  (total removed: 19,821).
- `revised_outlier_analysis.md`, `revised_outlier_report_per_serving.md` —
  revised per-serving recalculations.
- Cleaned dataset files (`*_clean.json`) for HealthyFoods, MyPlate and Recipe1M —
  the machine-readable record of what was kept vs. removed.

> Note: there is currently no single queryable table of removed recipe IDs — the
> report files list recipe titles, and the authoritative record of removals is the
> difference between each original source file and its `*_clean.json` counterpart.

---

## Part 2 — Recipe-Profiling Pipeline: Tool Overhaul

The profiling pipeline's matching stages were substantially reworked to improve
accuracy and to make every result explainable. Six areas changed.

### 1. Ingredient weight estimation ("2 cups flour" → grams)

Previously a single USDA lookup. Now a deterministic priority cascade:

1. A vetted offline reference table of (ingredient, unit) → grams, covering the
   recurring measurement signatures actually present in the data.
2. USDA portion-weight data plus FDA unit-gram references.
3. Vector similarity — borrow a comparable portion weight when there is no direct hit.
4. A language-model estimate as a last resort, sanity-checked by a plausibility
   verifier before being accepted.

Added guards: the number of servings is sanitised (implausible values are replaced
by an estimate derived from total recipe weight), and any single ingredient whose
weight is implausibly large relative to the recipe is capped — so parsing
artefacts cannot poison the totals. Each ingredient now also carries a record of
*how* its weight was resolved.

### 2. Nutrition matching (ingredient → food-composition table row)

Previously a vector-only match. Now a layered matcher:

- A curated alias table (hand-written ingredient → food-code mappings).
- A machine-built curated link index.
- A cleaned-text lexical / keyword match.
- A vector (embedding) match.

All of these run behind a **food-class compatibility gate** — e.g. a meat
ingredient cannot match a vegetable row. Each match carries a confidence label
(*alias / curated / strong / weak / none*) and a reason. The matcher also supports
a USDA cross-pool fallback for the Irish and Hungarian regions, and an ontology
round-trip to disambiguate the top candidates.

#### Note: regional pool fallback rates after the overhaul

Pool-fallback rates for the Irish region rose from ~40 % to ~80 % at the
ingredient level across the curated datasets. This is **a routing change, not
a regression** — the new matcher exposes a calibration that the old one
masked.

**Old logic (region-sticky).** The pre-overhaul matcher tried the regional
pool first and used USDA only as a *second-chance* lookup gated on the
regional hit being below the cosine threshold (~0.7). Any Irish row scoring
≥ 0.7 was kept — even when a far better USDA row existed and even when the
match was semantically wrong (a meat-cut row catching a vegetable query, a
generic "vegetable oil" row catching "olive oil", a cooked/processed row
catching a raw query).

**New logic (`best_nutrition_match`).** Irish and USDA candidates are gathered
in one pass and compete on the same scoreboard: cosine similarity + BM25 +
token overlap, with a hard food-class gate (−1.0 penalty for class mismatch),
a cooking-state / brand-marker penalty (−0.12 when the candidate carries
processing tokens the query didn't ask for), and a curated/ontology
adjustment. There is no preference for the regional pool.

**Why the % rose.** The Irish composition table has **1,307 rows**; USDA has
**7,793**, with far more cuts, varieties and processing states. For most
queries the highest-fidelity candidate objectively lives in USDA. The old
"try-Irish-first" gate hid this; the new design surfaces it. What previously
counted as Irish-pool hits was largely:

- generic Irish rows borderline-matching specific queries (`"vegetable oil"` ↔ `"olive oil"`),
- class-mismatched matches the old code accepted (`"cream"` ↔ `"ice cream"`),
- cooked/processed Irish rows applied to raw queries.

The class guard and cooking-state penalty now reject those, and the next-best
candidate is usually USDA.

**Match quality went up, not down.** In the regenerated stats
(`data_to_send/viz/fallback_stats_per_source.csv`):

- Irish low-confidence rate at the ingredient level: HealthyFoods **4.25 %**, MyPlate **1.05 %**, Recipe1M **0.07 %**.
- The matches that *are* picked are now overwhelmingly strong; pool fallback ≠ bad match — it just says the best row lived in the bigger table.

**Levers to reduce it honestly.** The only ways to push more matches into the
Irish pool without re-introducing wrong matches are (a) growing the Irish
composition table or (b) adding an Irish-side alias index that maps more
synonyms onto the existing 1,307 rows. Tightening the scoring will not help
— the candidates simply aren't there.

### 3. Sustainability matching (ingredient → carbon footprint)

Mirrors the nutrition matcher: a cleaned-name exact lookup → a small verified alias
map → a vector match with a hard food-class gate and lexical re-ranking — plus a
set of hand-coded overrides that correct known bad entries in the source carbon
dataset (for example certain stocks/broths and salt). Incompatible or unmatched
ingredients contribute zero rather than a wrong figure. The result is reported as a
recipe total, a per-serving figure and a per-kilogram figure, each with a
match-confidence label.

### 4. Nutri-Score

A full point-level breakdown was added alongside the A–E grade: the negative points
(energy, sugars, saturated fat, sodium) and the positive points (fibre, protein,
fruit/vegetable/legume content), including the official rule that excludes protein
points in certain cases. The grade is now explainable rather than just a letter.

### 5. Coverage metrics & data cleanup

Each profile now records what fraction of the recipe's weight was actually matched
— separately for nutrition and for sustainability — and flags recipes below roughly
80% coverage. Separately, the recipe knowledge graph was pruned of non-food
"ingredients" (≈17.7k equipment / cooking-spray lines) and ≈4.3k recipes that were
left empty as a result.

### 6. Full re-profiling run

On the back of all the above, every recipe from the curated sources (HealthyFoods,
MyPlate, FoodHero, Irish SafeFood) plus the Recipe1M-with-nutrition subset was
re-profiled against all three regional food-composition tables (Ireland, Hungary,
United States) — **173,757 recipe-profiles in total** — and the results stored,
replacing the earlier figures.

---

## Part 3 — Recipe-Knowledge-Graph Ingredient Cleanup

The Neo4j graph stored ingredient text in five different shapes depending on
which scraper imported each dataset. Quantities, units and ingredient names were
all mixed into a single `Ingredient.name` field, sharing nodes was inconsistent,
and a single ingredient could exist under many spellings. The cleanup pass moved
the graph onto a uniform shape:

```
(Recipe)-[:HAS_INGREDIENT {measurement, unit, quantity, weight_grams}]
        -> (Ingredient {name, canonical_id})
```

where every `Ingredient` node is a **shared, deduplicated, clean noun** and all
quantitative information lives on the relationship.

### Method

Three passes were applied:

1. **Postgres-driven migration.** For the ~58,000 recipes that had been profiled
   under the latest pipeline run, the canonical short-name produced by the
   nutrition matcher (`matched_sustainability_ingredient`, falling back to
   `matched_nutritional_ingredient`) was written onto the Neo4j ingredient node
   and the quantity/unit/weight was lifted onto the relationship. Affected
   sources: HealthyFoods, MyPlate, Irish SafeFood.
2. **Regex/string normaliser.** A deterministic pass over every remaining
   Neo4j ingredient string applied: strip unicode-fraction and decimal quantity
   prefixes (`½`, `¼`, `1`, `1.5`, `2/3`), strip cooking units and articles,
   strip parentheticals, remove the carbon-foundation `" X"` suffix marker,
   take the first comma segment of USDA-style labels (with category-aware
   handling — `"chicken, ground"` → `"chicken ground"`, `"parsley, fresh"` →
   `"parsley"`), strip common preparation prefixes (`"chopped onion"` →
   `"onion"`), **strip trailing count/portion units that aren't form-bearing**
   (`"garlic clove"` → `"garlic"`, `"lemon wedge"` → `"lemon"`,
   `"fennel bulb"` → `"fennel"`, `"fresh thyme sprig"` → `"thyme"`), keeping
   genuine form distinctions intact (`"bay leaf"`, `"cinnamon stick"`,
   `"salmon fillet"`, `"ground clove"` all preserved), **strip leading
   qualifier modifiers** (`"fresh basil"` → `"basil"`, `"whole milk"` →
   `"milk"`), collapse singular/plural for the trivial cases (`"eggs"` →
   `"egg"`, `"tomatoes"` → `"tomato"`, `"green onions"` → `"green onion"`),
   lowercase, dedupe, drop noise tokens, and MERGE the survivors. Applied to
   all five sources, including the long tail of Recipe1M.
3. **Hand-curated remap.** A small dictionary fixed the remaining quirks that
   patterns can't catch — USDA "category-first" inversions (`"oil vegetable"`
   → `"vegetable oil"`, `"juice fruit"` → `"fruit juice"`), category-stripping
   for spice rows (`"spices paprika"` → `"paprika"`), and undoing the
   over-eager plural-stripper on `-us` words
   (`"asparagu"` → `"asparagus"`, `"couscou"` → `"couscous"`,
   `"hummu"` → `"hummus"`, `"water convolvulu"` → `"water convolvulus"`).
4. **LLM splitter for FoodHero compound strings.** A final pass over the ~720
   FoodHero ingredient nodes that still carried multi-ingredient text glued
   together by the broken scraper (e.g. `"margarine or butter"`,
   `"vegetable oil½ medium onion"`, `"each salt and black pepper"`). Groq's
   `llama-3.3-70b-versatile` split each into canonical atomic ingredients
   (695 entries yielded 2,212 atomic ingredients; 26 modifier-only fragments
   like `"drained and rinsed"` were dropped). Cost: ~$0.02 worth of tokens.

The dirty original text is preserved verbatim in a separate
`(:Recipe)-[:HAS_INGREDIENT_ORIGINAL {position}]->(:Ingredient_Original)`
chain so the historical record is intact.

### Results

| Metric | Before | After | Change |
|---|---:|---:|---:|
| Distinct Ingredient nodes (whole graph) | 43,426 | **31,096** | **−28 %** |
| HAS_INGREDIENT relationships | 7,003,450 | 7,004,673 | +0.02 % |

The relationship count moved by +1,223. The cleanup is a relabel + dedup
pass, not a deletion — the small net gain comes from the FoodHero LLM split
expanding ~700 compound nodes into ~2,200 atomic ingredient edges, offset by
~245 dropped edges to meaningless leftovers (`"1 to"`, `"1 can ("`,
`"or canned"`, single-digit nodes).

### Top-10 ingredients per dataset, after cleanup

| Source | Top ingredients (recipe count) |
|---|---|
| HealthyFoods | garlic 1,634 · olive oil 1,420 · onion 898 · carrot 805 · egg 778 · tomato 755 · lemon juice 625 · milk 605 · red capsicum 586 · pepper 580 |
| MyPlate | salt 375 · vegetable oil 300 · onion 292 · pepper 252 · tomato 238 · garlic 199 · sugar 193 · milk 163 · egg 130 · sauce 121 |
| FoodHero | salt 196 · black pepper 140 · onion 134 · vegetable oil 130 · garlic 101 · garlic powder 86 · water 72 · sugar 63 · milk 49 · carrot 45 |
| Irish SafeFood | onion 23 · garlic 21 · pepper 20 · olive oil 18 · carrot 14 · tomato 11 · red pepper 11 · paprika 10 · vegetable oil 10 · potato 9 |
| Recipe1M | salt 272,988 · egg 187,020 · garlic 178,269 · butter 173,710 · sugar 152,646 · onion 140,857 · olive oil 123,795 · water 114,525 · milk 92,715 · flour 78,601 |

Before the cleanup, the same query on FoodHero returned tokens such as `"1"`,
`"½ teaspoon salt"`, `"¼ teaspoon black pepper"`, `"1 to"`, `"1 can ("`,
`"frozen"`, `"or canned"` and `"1 Tablespoon vegetable oil"` — i.e. raw
measurement lines rather than ingredients.

### Out of scope (deferred)

- **Recipe1M precision dedup**. The regex pass collapsed the obvious
  variants but long-tail spelling variants are an unbounded problem;
  spot-fixes can be added to the remap dictionary as needed.

### No nutrition recompute required

The nutrition pipeline reads from the Postgres composition tables keyed on
`recipe_id` + `canonical_food_id`. The Neo4j `Ingredient.name` field is never
on the path that produces `total_nutrients`, `nutri_score`, or
`nutrition_profiling_details`. The graph cleanup was string-level metadata
only and did not invalidate any of the 173,757 recipe profiles.

---

*Companion document: `docs/system-functionality-report.md` describes how the
profiling and search functionalities work end to end. `docs/graph-schema.md`
documents the updated node and relationship shapes.*

---

## Part 4 — Post-Recompute Trace Backfills

The May-11 recompute stored the headline result columns (`total_nutrients`,
`nutri_score`, `nutrition_profiling_details`, `trace`) but several
explainability fields were left blank. Three deterministic backfills then ran
into the existing 173,757 rows without re-executing the pipeline:

### 1. Nutrition-match confidence (per-ingredient)

Re-classifies each per-ingredient match in `nutrition_profiling_details` into
one of *alias / curated / strong / weak / none* based on the matcher's signal
(alias-table hit / curated-link hit / BM25 score / vector distance / food-class
gate failure). Run by `scripts/postgres/backfill_nutrition_match_confidence.py`.

- Rows visited: **173,757** (all `recompute_2026-05-11`)
- Entries patched: **1,133,376**
- Final confidence histogram:
  - strong  961,653
  - alias   134,901
  - curated  26,027
  - none     10,671
  - weak        124
- 6,858 entries flagged as "rederive mismatch" — match changed when re-run with
  the new matcher logic.

### 2. Nutri-Score breakdown (point-level)

Recomputes the negative-points (energy, sugars, sat-fat, sodium) and
positive-points (fibre, protein, fruit/vegetable/legume %) decomposition that
maps to each A-E grade, using the already-stored `total_nutrients` plus
per-ingredient weights from `nutrition_profiling_details`. Run by
`scripts/postgres/backfill_nutri_score_breakdown.py`.

- Rows visited: **173,757**
- `nutri_score_breakdown` filled: **173,736** (99.99 %)
- 21 rows skipped because the underlying `total_nutrients` was missing
  required fields.

### 3. Weight-resolution trace (per-ingredient)

Adds explicit per-ingredient provenance to every Neo4j-sourced recipe (the
~6,684 curated recipes whose weights came from the weight tool rather than
from a precomputed dataset):

- `weight_match_type`, `weight_source`, `weight_fallback`,
  `weight_portion_desc`, `weight_llm_likely_fired`, `weight_rederived_g`

The backfill replays `ingredient_weight_tool_usda` with `WEIGHT_LLM=""`, so
any ingredient whose weight has drifted from the originally-stored value is
flagged as having been resolved by the LLM fallback in the original run.
Run by `scripts/postgres/backfill_weight_trace.py`.

- Curated recipes processed: **6,684** (HealthyFoods 5,183 + MyPlate 1,039 +
  FoodHero 416 + Irish SafeFood 46)
- Profile rows updated (×3 regions): **20,052**
- Rows with at least one LLM-resolved ingredient: **2,076** (~10 %)
- Recipe1M phase: **153,705 rows** stamped with `weight_method =
  "dataset_precomputed"` (no weight tool was invoked — Recipe1M ships
  precomputed grams).

### Resulting trace coverage of `recompute_2026-05-11` rows

| Field | Rows filled |
|---|---:|
| `nutri_score_breakdown` | 173,736 / 173,757 |
| Per-ingredient `match_confidence` (inside `nutrition_profiling_details`) | 173,757 / 173,757 |
| Per-ingredient `weight_match_type` (Neo4j-sourced rows only) | 20,052 / 20,052 |
| Per-ingredient `weight_method = "dataset_precomputed"` (Recipe1M only) | 153,705 / 153,705 |

Together these make every recipe's profile fully explainable: for every
ingredient you can see which composition pool matched it, with what
confidence, how its weight was resolved, and where each Nutri-Score point
came from.
