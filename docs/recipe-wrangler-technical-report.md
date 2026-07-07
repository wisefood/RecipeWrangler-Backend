# RecipeWrangler — Data, Profiling and Evaluation (Technical Report)

This document is a self-contained reference for the RecipeWrangler nutrition-profiling
system. It describes the input datasets and what each one contains, the reference
data the system depends on, how a recipe is turned into a complete nutritional
profile, the structure of the output, and how the pipeline has been evaluated. It
is written for a technical report and contains no source code.

---

## 1. Overview

RecipeWrangler ingests recipes from several heterogeneous sources, normalises them
to a common shape, and computes for each recipe — from the ingredients up — a full
nutritional profile: per-100 g and per-serving macronutrients, a Nutri-Score grade
(A–E) with a point-level breakdown, and a carbon-footprint estimate. Because food
composition tables are regional, the system computes a separate profile per recipe
per regional reference table, making inter-regional variation explicit rather than
hiding it behind a single value. Every computed value is stored together with its
provenance (which reference entry matched, with what confidence, how each weight was
resolved, and how each Nutri-Score point was earned), so the output is auditable.

The system rests on four data stores: a **Neo4j** knowledge graph (recipes,
ingredients, allergens, dietary and dish-type tags), a **PostgreSQL** database (the
food-composition reference tables, the curated static pipeline data, and the
computed profile records), a **Chroma** vector store (ingredient-name embeddings for
fuzzy matching), and **Elasticsearch** (title autocomplete and a fallback recipe
list). Language models are used only in three bounded, fallback-gated roles
(ingredient parsing, last-resort weight estimation, and natural-language query
interpretation); all scoring and matching decisions are deterministic.

---

## 2. Input datasets

### 2.1 Sources and provenance

Five recipe sources are currently supported and tested, plus one auxiliary dataset
(HUMMUS) used to enrich Recipe1M.

| Source | Origin | Region | Delivery format |
|---|---|---|---|
| **Recipe1M** | Large public research corpus aggregated from many cooking sites | International (US-leaning) | Multi-layer dataset JSON |
| **HealthyFoods** | Healthy Food Guide | Australia / New Zealand | Scraped, cleaned JSON |
| **MyPlate** | USDA MyPlate | United States | Scraped, cleaned JSON |
| **FoodHero** | Oregon State University food-security resource | United States | Scraped, cleaned JSON |
| **SafeFood** | Irish SafeFood authority (provided via RCSI) | Ireland | Web-scraped JSON (operational) + a small RCSI laboratory spreadsheet (reference) |
| **HUMMUS** (auxiliary) | Curated Food.com corpus | — | CSV, joined to Recipe1M by URL |

**SafeFood has two forms.** A structured nutrition spreadsheet of ~47 laboratory-
measured recipes was provided directly by RCSI; this is the highest-grade nutrition
reference in the project and is retained for evaluation only. The operational
SafeFood collection is a larger web scrape of **334 recipes** from the SafeFood site,
and this is the version actually ingested.

### 2.2 Canonical input schema

Every source is normalised to a single canonical input record before ingestion.
Fields absent in a given source are left empty at this stage and either enriched from
a secondary source or computed downstream.

| Field | Type | Required | Notes |
|---|---|---|---|
| title | string | Yes | Recipe display name |
| ingredients | list[string] | Yes | Free-text ingredient lines (quantity + unit + name embedded) |
| instructions | list[string] | No | Cooking steps |
| duration | integer | Recommended | Total time in minutes |
| serves | integer | Recommended | Number of servings |
| image_url | string | No | URL to recipe image |
| source | string | Yes | Origin dataset identifier |
| url | string | No | Original source URL |

### 2.3 What each source actually provides

The sources differ sharply in completeness. Critically, **none ships a structured
per-ingredient nutritional value or a Nutri-Score**; the deepest a source goes is a
recipe-level nutrition panel (HealthyFoods, SafeFood), and most provide no nutrition
at all. (✓ present, ~ partial/derived, ✗ absent.)

| Field | Recipe1M | MyPlate | FoodHero | HealthyFoods | SafeFood |
|---|:--:|:--:|:--:|:--:|:--:|
| Title | ✓ | ✓ | ✓ | ✓ | ✓ |
| Free-text ingredients | ✓ | ✓ | ✓ | ✓ | ✓ |
| Instructions | ✓ | ✓ | ✓ | ✓ | ✓ |
| Servings | ✗ → HUMMUS | ✓ | ~ (yield text) | ✓ | ✓ (~96%) |
| Duration | ✗ → HUMMUS | ✓ (combined) | ~ (prep+cook; cook 67%) | ✓ (combined) | ~ (prep only) |
| Meal category | ✗ → HUMMUS | ✗ | ✓ | ~ (badge tags) | ✓ |
| Image | ✓ (separate layer) | ✓ | ✓ | ✓ | ✓ (remote) |
| Recipe-level nutrition | ~ (51k subset only) | ✗ | ✗ (label image only) | ✓ (11 fields) | ~ (4 of 6 NS nutrients) |
| Per-ingredient nutrition | ✗ | ✗ | ✗ | ✗ | ✗ |
| Native Nutri-Score | ✗ | ✗ | ✗ | ✗ | ✗ |

Three observations drive the pipeline design:

- **Ingredients are always free text.** No source separates quantity, unit and name,
  so every source requires ingredient parsing.
- **Nutrition is the most uneven field.** Only HealthyFoods supplies a complete
  recipe-level panel. The SafeFood web scrape supplies energy, total fat, saturated
  fat, sugars and salt but **omits fibre and protein**; Recipe1M supplies a panel for
  only its ~51k nutrition subset and **omits fibre**; MyPlate and FoodHero supply none
  (FoodHero only links a pre-rendered nutrition-label image). Source-supplied
  nutrition is therefore never sufficient on its own, and the pipeline recomputes
  nutrition for every recipe so all sources are scored on a single, consistent basis.
- **Servings, duration and category are missing from Recipe1M**, which carries none
  natively; these are recovered from the HUMMUS join.

### 2.4 Dataset statistics

**Original record counts and post-cleaning sizes.**

| Source | Original records | Operational (post-QC) | Notes |
|---|---:|---:|---|
| Recipe1M | ~1,029,720 (corpus) | ~826,773 | 51,235 of the corpus ship a nutrition panel |
| HealthyFoods | 5,314 | ~5,181 | 96.5% carry source nutrition |
| MyPlate | 1,072 | ~1,038 | no native nutrition |
| FoodHero | 523 | ~412 | nutrition only as label image |
| SafeFood (web) | 334 | 334 | + 47 lab recipes retained as reference |

**Recipe1M image and nutrition coverage.** ~1,024,496 of the corpus (99.5%) have an
image; only 51,235 (≈5%) carry a nutrition panel, and that panel itself omits fibre.

**HUMMUS enrichment of Recipe1M.** HUMMUS contains 591,485 raw recipes (507,335 after
preprocessing), all from Food.com. Recipe1M records are matched to HUMMUS by
normalised URL; against the operational Recipe1M set (827,782 records), the match
rates are:

| Field copied from HUMMUS | Records | % of operational Recipe1M |
|---|---:|---:|
| Any match | 409,075 | 49.42% |
| Cooking duration | 403,662 | 48.76% |
| Servings | 348,977 | 42.16% |
| Dish-type tags | 190,114 | 22.97% |

Only duration, servings and dish-type tags are imported from HUMMUS; its own nutrition
facts and pre-computed diet-quality scores are deliberately not loaded, so that
nutrition and Nutri-Score are recomputed consistently for every recipe.

**Nutri-Score data availability.** No source ships a native Nutri-Score grade. The
six nutrients required to compute one (energy, sugars, saturated fat, sodium, fibre,
protein) are available from source nutrition only as follows:

| Source | Required nutrients present | Gradeable from own data |
|---|:--:|:--:|
| HealthyFoods | 6 / 6 | Yes |
| Recipe1M (nutrition subset) | 5 / 6 (no fibre) | No (one short) |
| SafeFood (web) | 4 / 6 (no fibre, protein) | No |
| MyPlate | 0 / 6 | No |
| FoodHero | 0 / 6 | No |

Consequently, a **ground-truth Nutri-Score** (a reference grade computed from a
dataset's own nutrition) exists for only two datasets: Recipe1M (computed from its own
supplied panel for the nutrition subset) and the SafeFood **laboratory** set. The
SafeFood web set cannot be graded from its own nutrition.

### 2.5 Data preparation and quality filtering

Two preparation stages run before profiling: source-level preprocessing and
nutritional outlier removal.

**Source preprocessing.** All records undergo deduplication by title and ingredient
fingerprint, removal of records lacking both a title and an ingredient list, and
Unicode normalisation of fraction characters (½, ¼, ¾, …) to decimals. Ingredient
names are canonicalised (lower-cased, trailing punctuation stripped) before linking
to the knowledge graph.

**Ingredient-graph normalisation.** The graph originally stored ingredient text in
several inconsistent shapes, with quantities, units and names mixed into one field. A
multi-step pass moved every recipe onto a uniform shape — a shared, deduplicated,
clean ingredient noun, with all quantitative information held on the recipe-to-
ingredient relationship. The pass stripped quantity/unit/preparation tokens, collapsed
singular/plural, applied a hand-curated remap correcting inverted labels and
over-eager stemming, and used a language model to split compound ingredient strings
produced by the FoodHero scraper. This reduced distinct ingredient nodes from
**43,426 to 31,096 (−28%)** while leaving the ~7.0 million recipe-ingredient
relationships essentially unchanged. The original dirty text is preserved separately
so the historical record is intact.

**Nutritional outlier removal.** Recipe1M's supplied per-serving nutrition (computed
from crowd-sourced quantities) is highly unreliable: the median is ~600 kcal/serving
but the upper tail runs into the millions (e.g. a "World's Largest Apple Pie" entry at
~3.4 million kcal, and even a non-food "Hard Wood Floor Cleaner" entry inside the set).
Two passes were applied, evaluated over the recipes that carried a parseable
per-serving energy figure:

- **Pass 1 (statistical):** a per-dataset inter-quartile-range upper bound. Removed
  **5,709** Recipe1M recipes (≈11.2% of the 51,183 calorie-evaluable Recipe1M
  recipes), plus a handful from MyPlate (~31–38) and HealthyFoods (2).
- **Pass 2 (strict):** any recipe above **1,000 kcal per serving**. This is the cap
  used to produce the cleaned datasets the profiler reads. It removed **19,788**
  Recipe1M recipes (≈38.7% of the same evaluable subset, leaving 31,395); the two
  passes are not additive — the Pass-1 set is largely contained within Pass 2.

The curated collections needed only a handful of outlier removals each; their larger
overall reductions (e.g. HealthyFoods and FoodHero) come from the deduplication and
ingredient-normalisation stage, not from the calorie outlier passes.

---

## 3. Reference data

### 3.1 Food-composition tables

Four per-100 g nutrient tables back the nutrition calculation. Each ingredient is
matched into the appropriate table(s) for the region being profiled. The tables differ
sharply in size, nutrient breadth and naming style.

| Table | Region | Foods | Nutrient fields | Naming style |
|---|---|---:|---:|---|
| **USDA** (FoodData Central, SR Legacy) | US | 7,793 | 148 | Official descriptions; full panel |
| **EU composite** | Pan-European (global pool) | 8,148 | 112 | Harmonised to USDA names |
| **Irish CoFID** | Ireland | 1,307 | 34 | Long laboratory-qualified names |
| **Hungarian** | Hungary | 500 | 11 | Short generic English staples |

**Nutrient coverage relevant to Nutri-Score.** The Nutri-Score needs six nutrients.
USDA and the EU composite carry all six; the Irish table lacks fibre; the Hungarian
table lacks sugars, saturated fat and fibre. This is the main reason the matcher pools
each regional table together with the EU global pool (next section) — to recover
nutrients the smaller regional tables omit.

| Nutri-Score input | USDA | EU | Irish | Hungarian |
|---|:--:|:--:|:--:|:--:|
| Energy | ✓ | ✓ | ✓ | ✓ |
| Sugars | ✓ | ✓ | ✓ | ✗ |
| Saturated fat | ✓ | ✓ | ✓ | ✗ |
| Sodium | ✓ | ✓ | ✓ | ✓ |
| Fibre | ✓ | ✓ | ✗ | ✗ |
| Protein | ✓ | ✓ | ✓ | ✓ |

**Why naming style drives coverage.** Although the Irish table (1,307 foods) is larger
than the Hungarian (500), a *smaller* share of Irish-region ingredients is sourced
from the Irish table (~20.9%) than the Hungarian share from its own (~39.4%). The
matcher pools the regional and global tables and the best-scoring candidate wins, with
no preference for the local table; long Irish qualifier tails ("…, pasteurised, summer
and autumn") score poorly against a plain term like "milk", so a cleaner European entry
often wins, whereas Hungarian staples ("olive oil", "garlic") match plain terms almost
verbatim. Coverage therefore measures name-match quality on common staples, not the
nutritional completeness of the table.

### 3.2 The EU composite global database

The EU composite table is a purpose-built **global fallback pool** behind every
regional profile, and a region in its own right. It is a union of three European
national tables — **Ciqual** (France, ANSES), **CoFID** (UK, McCance & Widdowson) and
**NEVO** (Netherlands, RIVM) — harmonised into a single USDA-shaped schema so the
matcher can swap tables transparently. In building it:

- the three sources were **merged by union, not deduplicated** (the same food may
  appear up to three times with source-prefixed identifiers — `ciqual:…`, `cofid:…`,
  `nevo:…` — and slightly different values);
- nutrient names were **mapped onto the USDA naming scheme** wherever a clean
  equivalence exists; EU-specific nutrients with no USDA counterpart (iodine, salt,
  chloride, organic acids, polyols, free sugars, haem/non-haem iron, plant/animal
  protein, and others) are retained under their own names;
- composite **"dish" foods** (prepared meals, soups, ice creams) were dropped from the
  French and Dutch sources to keep the table ingredient-level.

The result holds 8,148 foods with 112 nutrient keys. The USDA table was also evaluated
as the global pool and gives closely comparable coverage; the EU composite was adopted
as the global fallback to keep it consistent with the European regional tables it sits
behind, so regional and fallback nutrition are expressed on the same compositional
basis.

### 3.3 Portion / weight references

Converting a recipe line such as "1 cup flour" into grams requires a food-specific
portion weight. A USDA household-measurement table provides, for ~7,500 foods, a set of
household portions (cup, tbsp, tsp, slice, clove, …) each with a gram weight. Counted
and ambiguous units ("1 medium onion", "2 eggs") are resolved from a curated
unit-to-grams reference and standard volume conversions. An offline reference dataset
of observed `(ingredient, unit) → grams` signatures is maintained so the runtime does
not depend on live lookups; only deterministically-accepted entries and high-confidence
model-rebuilt entries are used.

### 3.4 Sustainability reference

Per-ingredient environmental impact comes from **SustainableFooDB**, an open-access
life-cycle database providing ~7,400 ingredients, each with a food category and a
carbon-footprint value in **kg CO₂-equivalent per kg of food** (cradle-to-retail).
Values range from ~0.1 kg CO₂e/kg for most fruits and vegetables to ~18–20 kg CO₂e/kg
for beef and lamb.

---

## 4. How the profiling tool works

Given a canonical input recipe, the profiling tool produces — **per regional
composition table** — gram weights per ingredient, recipe and per-serving nutrition, a
Nutri-Score grade with breakdown, and a carbon-footprint estimate. Deterministic
components always take precedence; any model-resolved value is flagged in the
provenance trace.

### Step 1 — Parse (only when the input is raw text)

When a recipe arrives as unstructured text, a language model extracts the structured
fields (title, ingredient list, per-ingredient measurements, servings, total time).
When the recipe is supplied already structured (as from an imported dataset), this step
is skipped and no model is called.

### Step 2 — Weight resolution (measurement → grams)

Each "quantity + unit" string is converted to a gram weight through a deterministic
cascade, in priority order:

1. **Curated offline reference tables** (hand-verified and machine-built
   unit-to-gram datasets, including USDA portion weights and standard unit references),
   read from PostgreSQL.
2. **Vector similarity** — if no direct hit, the ingredient name is embedded and matched
   against ingredient vectors in Chroma to borrow a comparable portion weight.
3. **Language-model fallback** — only if everything above misses, a model proposes a
   plausible weight, which is accepted only after a plausibility verifier passes it.

**Accuracy guards.** Servings are sanitised (an implausible value is replaced by an
estimate derived from total recipe weight), and any single ingredient whose weight is
implausibly large relative to the recipe is capped, so parsing artefacts cannot poison
the totals. Each ingredient records *how* its weight was resolved. For datasets that
ship precomputed gram weights (the Recipe1M nutrition subset), this step is skipped and
the supplied weights are used directly.

### Step 3 — Nutrition matching and scaling

For each ingredient, the name is matched to a row in the chosen composition table,
using, in order: a curated alias table, a machine-built curated link index, a
cleaned-text lexical/BM25 match, and a vector match — all behind a **food-class
compatibility gate** (so a meat ingredient cannot match a vegetable row), with a
cooking-state/brand penalty and an ontology disambiguation round-trip on the top
candidates. The regional table and the EU global pool compete on one scoreboard with no
preference for the local table. Each match carries a confidence label (alias / curated /
strong / weak / none). The matched per-100 g nutrients are scaled by the ingredient's
gram weight and summed to **recipe totals**, then divided by servings to give
**per-serving** values. A **nutrition coverage** figure (the fraction of recipe weight
actually matched) is recorded, and recipes below ~80% coverage are flagged.

### Step 4 — Nutri-Score grade

From the per-100 g values of the six required nutrients plus the fruit/vegetable/
legume share (derived from the ingredient list, not from a nutrition panel), a
deterministic calculation produces an A–E grade, a numeric score and a colour,
following the 2023 algorithm. The grade is accompanied by a full point breakdown: the
four negative components (energy, sugars, saturated fat, sodium) and the three positive
components (fibre, protein, fruit/vegetable/legume %), including the official rule that
withholds protein points when the negative total is high and fruit content is low. No
language model is involved.

### Step 5 — Sustainability (carbon footprint)

Each ingredient is matched against the SustainableFooDB entries (cleaned-name lookup →
verified alias map → vector match with a hard food-class gate and lexical re-ranking,
plus hand-coded overrides for known bad source entries). Incompatible or unmatched
ingredients contribute zero rather than a wrong figure. Per-ingredient footprints are
summed to a recipe total, a per-serving figure and a per-kilogram figure, each with a
confidence label and a coverage figure.

### Regional profiles

Steps 3–5 are run independently per regional composition table, producing a separate
profile record per recipe per region. This makes inter-regional nutritional variation
explicit and queryable rather than collapsing it into one value.

---

## 5. Output: the generated recipe profile

### 5.1 Storage

A profile is stored across three complementary stores:

- **Neo4j** — the recipe graph entity with its ingredient, allergen and dietary/dish-
  type tag relationships, plus summary fields (Nutri-Score, per-serving sustainability,
  a "has profile" flag) used to order search results.
- **PostgreSQL** — the full structured profiling trace (one row per recipe per region).
- **Elasticsearch** — a flattened search/autocomplete document.

Because the profile is regional, the PostgreSQL composite key is `recipe_id +
nutrition_source` (the region/table used).

### 5.2 PostgreSQL profile schema

| Field group | Fields | Notes |
|---|---|---|
| Identity | recipe_id, title, source, nutrition_source (region), source_id, pipeline_version, computed_at, updated_at | Composite key: recipe_id + nutrition_source |
| Nutritional totals | total_nutrients: {energy_kcal, protein_g, carbohydrate_g, fat_g, saturated_fat_g, sugar_g, fibre_g, sodium_mg} | Full recipe totals |
| Per-serving nutrition | total_nutrients_per_serving: same fields | Totals ÷ servings |
| Nutri-Score | nutri_score: {nutri_score (A–E), score, color}; nutri_score_breakdown: negative + positive point decomposition | 2023 algorithm |
| Sustainability | total_sustainability, total_sustainability_per_serving, sustainability_per_kg, sustainability_profiling_details | From SustainableFooDB |
| Ingredient detail | nutrition_profiling_details[]: per ingredient — name, measurement, weight_g, matched entry, canonical_food_id, source table, match distance, contribution, per-100 g and scaled nutrients | One record per ingredient |
| Provenance | nutrition_profiling_debug, trace: full pipeline trace including model calls, weight method and matching decisions | For auditability |

Tags and allergens are **not** part of the PostgreSQL row; they are stored as
`HAS_TAG` and `HAS_ALLERGEN` relationships on the recipe node in Neo4j and joined into
the response at query time.

### 5.3 Worked example

A real profile for "Honey Ginger Strawberry Ice Cream" (Recipe1M), under the EU region:

Input (canonical):

```json
{
  "title": "Honey Ginger Strawberry Ice Cream",
  "ingredients": ["1 1/4 cups fresh strawberries", "1/2 cup honey",
    "1 teaspoon ground ginger", "1/2 teaspoon ground cinnamon",
    "1 tablespoon vanilla extract", "3 cups 1% low-fat milk"],
  "serves": 3, "source": "recipe1m"
}
```

Output (per-serving extract):

```json
{
  "recipe_id": "a446b062b6", "nutrition_source": "eu",
  "total_nutrients_per_serving": {
    "energy_kcal": 334.6, "protein_g": 9.65, "carbohydrate_g": 64.62,
    "fat_g": 3.06, "saturated_fat_g": 1.51, "sugar_g": 63.71,
    "fibre_g": 3.96, "sodium_mg": 111.62 },
  "nutri_score": { "nutri_score": "Nutriscore_B", "score": 2, "color": "green" },
  "total_sustainability": 4.47, "total_sustainability_per_serving": 1.49
}
```

The input had only free text; the pipeline resolved each line to grams (total 1,223 g),
matched each to the EU composite table, scaled and summed to totals and per-serving
values, then computed the grade (negative 4 minus positive 2 → score 2 → grade B). Each
ingredient's matched entry, source table and match distance are recorded for audit.

### 5.4 Provenance and explainability

Every profile is fully explainable. Each ingredient match is classified into one of
*alias / curated / strong / weak / none*; across the full profile set the confidence
mix is overwhelmingly strong (≈85% strong, ≈12% alias/curated), with under 1% weak.
Each Nutri-Score is stored with its point-level breakdown, and each weight with its
resolution method (offline reference, portion table, vector match, or model fallback —
or "dataset precomputed" for the Recipe1M subset). For every ingredient one can see
which composition pool matched it, with what confidence, how its weight was resolved,
and where each Nutri-Score point came from.

---

## 6. Computed profile statistics

The current profile store holds **95,599 region-profiles**.

**By recipe source:**

| Source | Region-profiles |
|---|---:|
| Recipe1M | 66,742 |
| HealthyFoods | 20,747 |
| MyPlate | 4,156 |
| FoodHero | 2,068 |
| SafeFood | 1,872 |

**By region / nutrition source:** EU 39,500 · Recipe1M-own-nutrition 31,447 ·
Irish 8,094 · Hungarian 8,089 · USDA 8,088 · SafeFood (web) 334 · SafeFood (lab) 47.

**Computed Nutri-Score distribution by source** (EU reference, row %):

| Source | A | B | C | D | E |
|---|--:|--:|--:|--:|--:|
| Recipe1M | 26% | 20% | 25% | 22% | 7% |
| HealthyFoods | 59% | 16% | 16% | 9% | 1% |
| MyPlate | 46% | 23% | 20% | 9% | 2% |
| FoodHero | 46% | 26% | 18% | 8% | 2% |
| SafeFood | 68% | 13% | 11% | 7% | 0% |

The grade ordering matches expectations: the curated, deliberately-healthy sets
(SafeFood, HealthyFoods) skew strongly to A, while the general Recipe1M corpus is
spread evenly across A–D with the most E. The grade is also stable across composition
tables — choosing a different regional table moves the verdict by only a few points.

**Ground-truth Nutri-Score distribution** (computed from each dataset's *own* supplied
nutrition; only two datasets have one):

| Dataset | A | B | C | D | E | n |
|---|--:|--:|--:|--:|--:|--:|
| Recipe1M (own nutrition) | 2,831 | 7,265 | 7,753 | 8,555 | 5,043 | 31,447 |
| SafeFood (lab) | 44 | 1 | 2 | — | — | 47 |

The SafeFood laboratory set grades **94% A**, a useful fidelity check; Recipe1M's own
nutrition skews toward D/E, consistent with a general corpus.

---

## 7. Evaluation

### 7.1 Matcher coverage — EU global pool vs USDA

The ingredient matcher was audited over the full Neo4j ingredient vocabulary, with an
ingredient counted as matched when its confidence label is alias, curated or strong.
Two views are reported: per-unique-ingredient (each distinct name counts once — a test
of reach into the long tail) and recipe-occurrence-weighted (each name weighted by how
often it appears — closer to user-facing impact).

| View | USDA matched | EU matched |
|---|--:|--:|
| Per-unique vocabulary | 87.6% | **89.8%** |
| Recipe-occurrence-weighted | 98.4% | **99.3%** |

The EU global pool edges USDA on both views. The hand-curated alias table is
source-agnostic and ties; USDA wins slightly on curated links (it has a dedicated
Recipe1M→USDA link table), and the EU pool recovers the difference through stronger raw
vector + lexical matches.

### 7.2 Pipeline overhaul — what shifted

The profiling pipeline was reworked for accuracy and explainability: outlier-
contaminated recipes were removed, weight estimation became a deterministic cascade with
plausibility guards, nutrition and sustainability matching moved to a single
food-class-gated scoreboard with confidence labels, and the Nutri-Score gained a
point-level breakdown. The main measured effects:

- **Hungarian inflation removed.** A previous region-sticky matcher with unchecked
  weight amplification inflated the Hungarian slice (median energy ~3,509 kcal, protein
  ~72 g). After the overhaul the three regions agree to within a few percent (Hungarian
  median energy ~1,109 kcal vs USDA ~1,102 kcal).
- **Macro medians dropped to realistic levels**, driven mostly by outlier removal
  (38.7% of the evaluable Recipe1M recipes were above the strict 1,000 kcal/serving cap)
  plus the single-ingredient weight cap and serves sanitisation.
- **HealthyFoods accuracy improved.** Median energy moved from ~495 kcal (overestimated)
  to ~390 kcal, within ~3% of the scraped reference, after the weight cascade and
  food-class gate removed amplification and bad matches.
- **SafeFood under-grading corrected.** The lab set (93.6% grade A by its own reference)
  was previously under-graded (44.7% A); after the overhaul it recovers to 73.9% A on the
  USDA slice, with median energy and fat roughly halved toward the reference.
- **Grades became more honest, not more lenient.** With class-gated matching, protein is
  no longer over-assigned, so grades shift slightly toward D/E where the corrected
  nutrition warrants it.

### 7.3 Regional pool-fallback calibration

After the overhaul, the Irish region draws ~80% of its ingredient matches from the
global pool rather than the Irish table (up from ~40%). This is a routing change, not a
regression: the Irish table has only 1,307 rows against USDA's 7,793 and the EU pool's
8,148, so for most queries the highest-fidelity candidate objectively lives in the
larger pool. The old "try-Irish-first" gate masked this and accepted class-mismatched or
cooked/processed Irish rows for raw queries; the new food-class gate and cooking-state
penalty reject those. Measured low-confidence (weak) match rates at the ingredient level
are now very low (HealthyFoods 4.25%, MyPlate 1.05%, Recipe1M 0.07%). The only honest
ways to push more matches into the Irish pool are to grow the Irish table or add an
Irish-side alias index — tightening the scoring would not help, because the candidates
are not there.

---

## 8. Summary of which engine does what

| Stage | Engine / method |
|---|---|
| Parse raw recipe text | Language model (skipped for structured input) |
| Measurement → grams | Curated PostgreSQL references → Chroma vector match → model fallback (plausibility-checked) |
| Nutrition lookup | PostgreSQL composition tables (USDA / Irish / Hungarian / EU) + curated alias table + Chroma vector match, food-class gated |
| Nutri-Score | Deterministic calculation (no model) |
| Sustainability | SustainableFooDB via Chroma + curated alias/override maps |
| Persist profile | PostgreSQL profile table; summary fields on the Neo4j recipe node |

All matching and scoring decisions are deterministic; language models are confined to
parsing and last-resort weight estimation, and any model-resolved value is flagged in
the provenance trace.
