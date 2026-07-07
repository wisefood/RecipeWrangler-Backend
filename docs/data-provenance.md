# Data Provenance Report

This report documents every dataset used in the RecipeWrangler system: where it
comes from, the fields it arrives with, what we change during ingestion, and the
fields a record ends up with in our own data stores. It also explains how the
USDA household-measurement table converts recipe quantities into grams, and how
the HUMMUS dataset is used to enrich Recipe1M. It is written for a technical
report and describes the data only — not the software.

The system keeps two kinds of data:

- **Recipe collections** — the recipes themselves (title, ingredients, method,
  timing, servings, images), gathered from public cooking sites and one large
  research corpus.
- **Reference data** — food-composition tables (per-100 g nutrient values),
  a household-measurement-to-grams table, a carbon-footprint table, and several
  ingredient-mapping / knowledge resources used to match a free-text recipe
  ingredient to the right reference record.

Every recipe is parsed into ingredients, each ingredient is converted to a weight
in grams, and each ingredient is matched to a composition record to produce
nutrition and sustainability figures. Those results are stored per recipe and per
region (US/USDA, Irish, Hungarian, EU).

---

## 1. Recipe collections

For each collection below: **Input** = the fields the source provides;
**Edited** = what we normalise, derive, or drop; **Final** = the fields the
recipe carries in our system after ingestion and profiling.

All collections converge on a common final recipe record:

> `recipe_id`, `title`, `source`, `source_id`, `expert_recipe` (true for the
> curated/lab collections), `status`, `duration` (minutes), `serves`,
> `image_url`, `instructions`, `url`, plus an attached set of parsed ingredients
> (each with its original text, a cleaned canonical name, the original
> measurement, and a resolved weight in grams), detected allergens, and — once
> profiled — per-region nutrition totals, a Nutri-Score grade, and a carbon
> footprint.

### 1.1 MyPlate (~1,072 recipes)

US federal nutrition resource (originally myplate.gov, now myplate.food).

- **Input:** `title`, `url`, `servings`, `description`, `ingredients` (raw lines
  such as "1 tablespoon vegetable oil"), `directions`, `notes`, `image_url`,
  `duration`, `recipe_id`/`id`.
- **Edited:** every site URL rewritten from the old `myplate.gov` domain to the
  new `myplate.food` domain (applied across the export and all stored copies);
  `duration` normalised to float minutes (ranges averaged, "1 hour 30 minutes"
  resolved, unicode fractions handled); `servings` parsed to a number; each
  ingredient line parsed into a cleaned name + measurement, weighed in grams, and
  nutrition-matched.
- **Final:** the common recipe record with `url` on the new domain, parsed
  ingredients with gram weights, and per-region nutrition + Nutri-Score.

### 1.2 HealthyFoods (~5,300 recipes)

Australian Healthy Food Guide. Ships recipes plus a **separate per-serving
nutrition reference** file used as ground truth for evaluation.

- **Input (recipe):** `link`, `title`, `description`, `time_minutes`, `serves`
  (string, e.g. "4", "4-6"), `image_url`, `ingredients`, `instructions`,
  `variations`, `tips`, `badge_tags` (dietary/category tags such as "Dairy free",
  "Vegetarian", "Ready in 20 minutes"), `source`.
- **Input (nutrition reference):** keyed by URL, a `nutrition_per_serve` block
  with `Calories`, `Kilojoules`, `Protein`, `Total fat`, `Saturated fat`,
  `Carbohydrates`, `Sugar`, `Dietary fibre`, `Sodium`, `Calcium`, `Iron`
  (coverage is partial — many values are blank).
- **Edited:** stable recipe id derived from the URL/title; `time_minutes` → float
  minutes; `serves` string → number; source-name typos normalised; `tips` and
  `variations` consolidated into notes; ingredients parsed/weighed/matched. The
  reference nutrition is attached per recipe and, where present, used to compute a
  reference Nutri-Score grade as evaluation ground truth.
- **Final:** common recipe record + dietary tags + (where available) a reference
  per-serving nutrition row used only for evaluation.

### 1.3 FoodHero (~520 recipes)

US food-security/nutrition resource (foodhero.org). Carries categories and
allergen-style tags but no structured nutrition (only a nutrition-label image).

- **Input:** `source_url`/`canonical_url`/`url`, `language`, `title`,
  `description`, `image_url`, `prep_time`, `cook_time`, `makes`/`recipe_yield`
  (e.g. "about 4 cups"), `ingredients`, `directions`, `notes`,
  `nutrition_label_url` (image only), `categories`, `date_published`.
- **Edited:** servings extracted from the yield/"makes" text; duration combined
  from prep + cook times; canonical URL chosen; ingredients
  parsed/weighed/matched. No structured nutrition is taken from the source — it is
  computed by our pipeline.
- **Final:** common recipe record + category tags; nutrition is entirely
  pipeline-computed.

### 1.4 Irish SafeFood — laboratory set (~46–47 recipes)

Lab-measured recipes from the Irish SafeFood authority. This is the highest-grade
nutrition ground truth in the project and is retained for evaluation.

- **Input (per recipe row):** source title (e.g. "AP_Chicken Soup_12.01.23"),
  raw/cooked batch weights, yield factor, `servings`, serving weight (e.g.
  "542 g"), `prep_time`, `cook_time`, cost, newline-delimited ingredients and
  method, and a **full lab nutrient panel** both per-100 g and per-serving:
  energy (kJ and kcal), fat, saturated fat, carbohydrate, sugars, **fibre**,
  **protein**, salt.
- **Edited:** title cleaned (source prefix and date stripped); duration summed
  from prep + cook; serving weight extracted to grams; deterministic recipe id;
  ingredients parsed once and profiled across regions; allergens detected. The lab
  per-serving panel is stored separately as the **`safefood` ground-truth** record
  (salt converted to sodium at ≈400 mg per gram).
- **Final:** common recipe record (expert flag true, serving weight in grams) +
  the lab `safefood` ground-truth nutrition used as the evaluation reference.

> Because the lab set is the only SafeFood data with protein/carbohydrate/fibre,
> it remains the evaluation ground truth even after the operational dataset is
> replaced by the larger web set (below).

### 1.5 Irish SafeFood — web set (334 recipes)

Scraped from safefood.net across five meal categories (breakfast 20, lunch 67,
dinner 186, snacks 25, desserts 36). Becomes the operational SafeFood dataset.

- **Input (per recipe):** `name`, `url`, `category`, `description`, `prep_time`,
  `cook_time`, `total_time`, `serves`, `ingredients`, `method`, `image_url`, and a
  **partial nutrition block** that the website publishes per serving only:
  `energy_kj`, `energy_kcal`, `fat_g`, `saturates_g`, `sugars_g`, `salt_g`, plus a
  "5-a-day" note. **No protein, carbohydrate, or fibre is published.**
- **Edited:** deterministic recipe id from the title; serves and duration parsed
  from text; ingredients parsed once and profiled across all four regions
  (US/IE/HU/EU); allergens detected; the real recipe URL and the real site image
  are used directly. The published per-serving values are stored under a separate
  **`safefood_web`** label (salt converted to sodium at 393.4 mg per gram) so they
  can never overwrite the laboratory `safefood` ground truth.
- **Final:** common recipe record + four per-region nutrition profiles + a
  partial `safefood_web` published-nutrition record.

### 1.6 Slovenian OPKP (10 recipes)

Small, high-detail set from the Slovenian national food platform, encoded with
standardised EuroFIR component codes.

- **Input:** bilingual recipe name (Slovenian + English), consumed amount and unit
  (grams), free-text procedure, a weight yield factor; per-ingredient bilingual
  names with amounts in grams; and a nutrient table where each row is a EuroFIR
  component (code, name, selected value, unit, value type, acquisition type).
- **Edited:** English name used as the canonical title; ingredient amounts kept in
  grams; yield factor applied to the consumed amount; per-100 g component values
  scaled to recipe totals via the consumed amount.
- **Final:** common recipe record + a EuroFIR-standardised reference nutrition
  profile.

### 1.7 Recipe1M + HUMMUS enrichment (~1.0 M base; 51,235 with nutrition)

**Recipe1M** is a large public recipe corpus aggregated from many cooking sites.
Each base record carries: `id`, `title`, `url`, `partition` (train/val/test),
`ingredients` (free text), and `instructions` (free text). A second layer
contributes recipe images.

**Recipe1M+ nutrition subset (51,235 recipes).** A subset ships with
pre-computed ingredient data. Per record it adds, alongside the base fields:

- `quantity` and `unit` (parallel lists, e.g. "8" / "ounce"),
- **`weight_per_ingr`** — a pre-computed per-ingredient weight in grams,
- `nutr_values_per100g` — recipe nutrition per 100 g (energy, fat, protein, salt,
  saturates, sugars),
- `nutr_per_ingredient` — per-ingredient nutrient totals (energy, fat, protein,
  saturates, sodium, sugars),
- `fsa_lights_per100g` — UK FSA traffic-light bands (green/amber/red) for fat,
  salt, saturates, sugars.

> **What `weight_per_ingr` is.** It is each ingredient's quantity multiplied by
> the USDA standard portion for that unit — an *algorithmic* reference weight, not
> a laboratory measurement. For this subset our pipeline consumes these weights
> directly instead of recomputing them, which is why this subset is also used to
> *validate* our own weight tool: the tool is re-run independently and compared
> against `weight_per_ingr` as a held-out reference.

**HUMMUS** is a curated, preprocessed dataset of ~507,000 Food.com recipes. Per
recipe it provides: `recipe_url` (the join key), `title`, `duration`, `serves`,
dish-type `tags` (e.g. "breakfast", "main-dish", "desserts", "snacks",
"beverages", "easy"), per-serving Food.com nutrition facts, and three pre-computed
diet-quality scores (`who_score`, `fsa_score`, `nutri_score`).

**How HUMMUS enriches Recipe1M.** Recipe1M records are matched to HUMMUS by
**normalised recipe URL** (lower-cased, scheme/host canonicalised). On a match, we
take from HUMMUS only the metadata that Recipe1M lacks and that needs no
recomputation:

- **`duration`** (cooking time in minutes),
- **`serves`** (servings count),
- **dish-type `tags`** (attached to the recipe).

The HUMMUS diet-quality scores (`who_score`, `fsa_score`, `nutri_score`) and its
Food.com nutrition facts are deliberately **not** loaded. Instead, nutrition and
Nutri-Score are recomputed by our own pipeline from matched composition records,
so that every recipe in the system is scored consistently on the same basis.

- **Final (Recipe1M):** base record (id, title, url, instructions) + HUMMUS
  duration, serves, and dish-type tags + pipeline-computed per-region nutrition,
  Nutri-Score, and (where computed) carbon footprint. For the 51,235-recipe
  subset, ingredient weights come from the supplied `weight_per_ingr`.

---

## 2. Food-composition reference tables

Four per-100 g nutrient tables back the nutrition calculation. Each ingredient is
matched into the appropriate table(s) for the region being profiled. The four
tables differ sharply in size, nutrient breadth, and — importantly — naming style,
which materially affects how often each one wins a match (see §2.5).

### 2.1 USDA (~7,793 foods)

US FoodData Central (SR Legacy). The broadest table by far. Each food has a
5-digit USDA identifier, an official description, and a very wide per-100 g
nutrient panel: water, protein, fat, carbohydrate, fibre, ash, sugars (total and
individual sugars), energy (kJ and kcal), the full mineral set (sodium,
potassium, calcium, magnesium, phosphorus, iron, zinc, copper, manganese,
selenium, and more), the full vitamin set (A/retinol/carotenoids, D, E
tocopherols, K, the B-complex, folate, C), cholesterol and the full fatty-acid
breakdown (total saturated/mono/poly/trans plus individual chains), amino acids,
and phytosterols. In total it carries the complete USDA nutrient set (well over a
hundred fields per food).

### 2.2 Irish CoFID (~1,307 foods)

Irish Composition of Foods Database, derived from the UK McCance & Widdowson
tables. Foods use **long, qualified laboratory names** — e.g. "Milk,
semi-skimmed, pasteurised, summer and autumn" or "Apples, cooking, baked with
sugar, flesh only, weighed with skin". Columns: a food id, `Food Name`,
`Description` (sampling basis), food `Group`, and **34 per-100 g nutrients**:
water, total nitrogen, protein, fat, carbohydrate, energy (kcal and kJ), starch,
total sugars, glucose, fructose, saturated/mono/poly fatty acids, sodium,
potassium, calcium, magnesium, phosphorus, iron, copper, zinc, chloride,
manganese, selenium, iodine, retinol, carotene, retinol equivalent, vitamin D,
vitamin E, vitamin B6, vitamin B12, vitamin C. No fatty-acid chains, amino acids,
or phytosterols.

### 2.3 Hungarian (~500 foods)

Hungarian food-composition table. Foods use **short, generic English staple
names** — e.g. "Bread", "brown rice", "olive oil", "wheat flour". Columns: a food
id, `Food Name`, `Category`, and **10 per-100 g nutrients**: energy (kJ and kcal),
protein, fat, carbohydrate, sodium, potassium, calcium, magnesium, retinol
equivalent, vitamin E. The smallest and narrowest table.

### 2.4 EU composite (~8,148 foods)

A union of three European national tables, used as the **global fallback** for the
regional profiles and as the basis of the EU region itself:

- **Ciqual** (France, ANSES),
- **CoFID** (UK, McCance & Widdowson),
- **NEVO** (Netherlands, RIVM).

Each food keeps a prefixed identifier showing its origin (e.g. `ciqual:32014`,
`cofid:…`, `nevo:…`), an English food name, the source, the country, and a food
group. The three sources are **merged by union, not deduplicated** — the same food
may appear up to three times with different ids and slightly different values.
Nutrient names are harmonised to the USDA naming scheme where a clean mapping
exists; EU-specific nutrients with no USDA equivalent (iodine, salt, chloride,
organic acids, polyols, non-starch polysaccharide, free sugars, plant/animal
protein, haem/non-haem iron) are retained under their own names. Composite "dish"
foods (prepared meals, soups, ice creams) are dropped from the French and Dutch
sources so the table stays ingredient-level. A handful of cross-source definitional
differences (e.g. two different vitamin-A retinol-equivalent formulas; available
vs by-difference carbohydrate) are flagged during the build.

### 2.5 Why naming style drives coverage (Hungarian > Irish)

Although the Irish table is larger (1,307 foods) than the Hungarian (500), a
*smaller* share of Irish-region ingredients is sourced from the Irish table
(~20.9%) than the Hungarian share from its own table (~39.4%). This is a
naming-style effect, not a size effect. The matcher pools the regional table and
the EU table together and the best-scoring candidate wins, with no preference for
the local table. The Irish lab names carry long qualifier tails ("…, pasteurised,
summer and autumn") that lower the match score against a plain recipe term like
"milk", so the cleaner EU entry often wins even when the Irish table contains the
food. The Hungarian staples ("olive oil", "garlic", "sugar") match plain recipe
terms almost verbatim and win whenever present. Coverage therefore measures
name-match quality on common staples, not nutritional completeness of the table.
(Detailed analysis and worked examples are in the EU-vs-USDA coverage report.)

---

## 3. USDA household-measurement table (quantities → grams)

Converting a recipe line such as "1 cup flour" into grams requires a
food-specific portion weight, because a cup of flour and a cup of butter weigh
very differently. The **USDA portions table** provides this. It covers ~7,500 of
the USDA foods, and for each food lists a set of household portions, each with:

- a portion description (the household unit — "cup", "tbsp", "tsp", "slice",
  "clove", "stick", "pat", "piece", etc.),
- an amount,
- the corresponding weight in grams.

**How it is used.** For a given ingredient and measurement, the unit in the recipe
("cup") is matched against that food's portion descriptions, and the listed gram
weight is scaled by the recipe quantity. For example "1 cup butter" resolves to
~227 g, "1 tablespoon butter" to ~14 g — different weights for the same unit
because the table is per-food. Counted items and ambiguous units ("1 medium
onion", "2 eggs") are resolved from a curated unit-to-grams reference and standard
US-customary volume conversions (cup ≈ 237 ml, tablespoon ≈ 15 ml, teaspoon ≈ 5
ml, and so on). When a measurement cannot be resolved deterministically, a
language-model fallback estimates the grams. Each resolved ingredient records the
weight in grams and which path produced it (portion table, unit reference, or
language-model estimate). For the Recipe1M nutrition subset (§1.7) the supplied
`weight_per_ingr` is used directly in place of this conversion.

---

## 4. Sustainability — carbon footprint table

Per-ingredient environmental impact comes from **SustainableFooDB**, an
open-access food life-cycle database. It provides ~7,373 ingredients, each with a
food category and a **carbon-footprint value in kg CO₂-equivalent per kg of food**
(cradle-to-retail). Categories span meat, dairy, fish/seafood, fruit, vegetable,
cereal, bakery, legume, nuts/seeds, spice/herb, beverage (including alcoholic and
caffeinated), and prepared dishes. Values range from roughly 0.1 kg CO₂e/kg for
most fruits and vegetables to ~18–20 kg CO₂e/kg for beef and lamb. During
profiling each recipe ingredient is matched to its nearest entry, the per-kg value
is scaled by the ingredient's weight, and the contributions are summed to a recipe
total and a per-serving figure. The match is guarded so that an ingredient is only
paired with an entry of a compatible food class.

---

## 5. Reference & mapping resources (ingredient → record matching)

These resources exist to connect a free-text recipe ingredient to the correct
reference record. They contribute candidates and guards to the match; the final
choice is always decided by scoring, not taken blindly.

### 5.1 Curated alias table (~690 aliases, ~190 ingredient families)

A hand-built list pinning common ingredients and their variants to a specific
canonical food record — e.g. every form of "boneless skinless chicken breast" to a
single raw chicken-breast record, "all-purpose flour" to the white enriched flour
record, "butter" to salted butter. It also fixes traps where stripping descriptors
would mislead (e.g. "unsalted butter", "low-fat yoghurt"). When an ingredient
matches an alias it is accepted immediately with the highest confidence,
short-circuiting the rest of the match. (Aliases point at USDA records and are used
only for the USDA region; the EU and regional profiles do not use them.) Two
companion references give counted-item gram weights (e.g. "1 medium bell pepper" ≈
119 g) and exact US-customary volume-to-millilitre conversions.

### 5.2 Recipe1M → USDA and → FoodOn links (~16,000 each)

Pre-computed associations from canonical Recipe1M ingredient names to USDA food
ids (with a similarity score) and to FoodOn ontology classes. These were generated
by embedding similarity and are **not** human-verified, so they are used as
*candidate hints* that compete in the match — they never override scoring.

### 5.3 MISKG — canonical ingredient identities

A normalisation layer built from Recipe1M that maps the many surface forms of an
ingredient name to a single canonical identity (name + id), plus tables of
original vs processed ingredient names and a list of Recipe1M ingredients that have
no nutrition mapping. It supplies the canonical ingredient names used throughout
matching and deduplication.

### 5.4 FoodOn — food ontology

The FoodOn food ontology provides a hierarchical classification of foods. It is
used as a **food-class compatibility guard**: when matching an ingredient to a
composition or sustainability record, the candidate's food class is checked
against the ingredient's class in the ontology hierarchy, so that semantically
incompatible matches (e.g. a dairy item matched to an oil) are penalised or
rejected.

### 5.5 FlavorDB — flavour-compound similarity

FlavorDB models each ingredient's volatile flavour compounds. It provides
ingredient nodes and ingredient-to-ingredient flavour-overlap scores. In the
recipe-adaptation feature it supplies a **flavour-similarity** signal (overlap of
shared compounds between two foods), used as a tie-breaker when proposing
whole-food ingredient swaps (e.g. broccoli ↔ cauliflower scoring as similar). It is
only consulted when both ingredients have enough compound data to be reliable.

### 5.6 Embeddings

Ingredient names are matched semantically using a sentence-embedding model
(configurable; currently Qwen3-Embedding) backing a vector index. The embedding
similarity between a recipe ingredient and a candidate record is one component of
the overall match score, combined with lexical (term-overlap) scoring and the
ontology food-class guard.

---

## 6. Where the final data lives

For every recipe, two stores hold the result:

- **Recipe graph** — the recipe and its ingredients: the recipe's metadata
  (title, source, url, image, duration, serves, instructions, allergens, dish-type
  tags) and its ingredients, each linked with its original text, cleaned canonical
  name, original measurement, and resolved gram weight.
- **Nutrition store** — one profile per recipe **per region** (US/USDA, Irish,
  Hungarian, EU), each holding total and per-serving nutrient values, a Nutri-Score
  grade, a per-ingredient breakdown (the matched composition record, the weight in
  grams, the match distance, and the carbon footprint), and — for the reference
  collections — a separate ground-truth nutrition record (lab `safefood`,
  `safefood_web`, Recipe1M original, HealthyFoods reference, or the EuroFIR-coded
  OPKP values) used for evaluation.

Each per-ingredient breakdown records **which table supplied the nutrition**
(regional table or EU composite), which is exactly what the regional-coverage
figures count.
