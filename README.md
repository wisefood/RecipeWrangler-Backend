# RecipeWrangler Backend

FastAPI backend for recipe search, parsing, nutrition profiling, and sustainability scoring. Built with LangGraph pipelines, a Neo4j recipe graph, and multi-source nutrition data.

---

## Architecture Overview

```
Client
  │
  ▼
FastAPI (src/recipe_wrangler/api/)
  │
  ├── GET   /health
  ├── GET   /api/v1/recipes/autocomplete                  → Elasticsearch
  ├── GET   /api/v1/recipes/{recipe_id}                   → Neo4j + PostgreSQL
  ├── POST  /api/v1/recipes/search                        → Neo4j + Groq LLM + Elasticsearch (fallback)
  ├── POST  /api/v1/recipes/param_search                  → Neo4j
  ├── POST  /api/v1/recipes/profile                       → Groq LLM + Chroma + PostgreSQL
  ├── POST  /api/v1/recipes/create                        → Neo4j + PostgreSQL (profiling pipeline)
  ├── POST  /api/v1/recipes/{recipe_id}/substitute        → Neo4j + Chroma + PostgreSQL
  └── PATCH /api/v1/recipes/{recipe_id}                   → Neo4j + Elasticsearch

Services
  ├── Neo4j         – Recipe knowledge graph (recipes, ingredients, allergens, diet tags)
  ├── Elasticsearch – Recipe title search index
  ├── Chroma        – Vector store for nutrition and sustainability matching
  ├── PostgreSQL    – Structured nutrition data (USDA + Irish) and profiling traces
  └── Groq LLM      – Recipe parsing, natural language → Cypher, weight estimation
```

---

## Endpoints

### GET /health
Simple health check. Returns `{"status": "ok"}`.

---

### GET /api/v1/recipes/autocomplete

**Purpose:** Real-time recipe title suggestions for a search box.

**Parameters:** `q` (2–120 chars), `limit` (1–20, default 8)

**Flow:**
```
q="chick"
  └── Elasticsearch bool_prefix query on title field
        └── Deduplicate (case-fold)
              └── Return top N suggestions
```

**Databases:** Elasticsearch only.

**Example:**
```bash
curl "http://localhost:8001/api/v1/recipes/autocomplete?q=chick&limit=5"
```

---

### GET /api/v1/recipes/{recipe_id}

**Purpose:** Fetch full recipe details including ingredients, instructions, and nutrition breakdown.

**Flow:**
```
recipe_id
  ├── Neo4j      → title, ingredients, instructions, tags, duration, serves
  └── PostgreSQL → stored nutrition totals + profiling trace
        └── Compute Nutri-Score breakdown
              └── Return RecipeDetailResponse
```

**Databases:** Neo4j, PostgreSQL.

**Returns:** Title, ingredients with quantities, step-by-step instructions, diet tags, Nutri-Score (A–E), macro/micronutrients per serving, carbon footprint.

When Postgres has source-provided nutrition for the recipe, the response also includes:

- `has_ground_truth_nutrition`
- `ground_truth_nutrition_source`
- `ground_truth_nutrition.nutrients_per_serving`

Those fields expose original/source per-serving nutrition separately from computed regional profiles. SafeFood rows use `nutrition_source="safefood"`, Recipe1M original nutrition uses `nutrition_source="recipe1m_original"`, and HealthyFoods source nutrition uses `nutrition_source="healthyfoods_original"` when available. Computed profiles such as `usda`, `irish`, and `hungarian` are not marked as ground truth.

---

### POST /api/v1/recipes/search

**Purpose:** Natural language recipe search. Translates a question into a graph query.

**Input:**
```json
{
  "question": "chicken and rice under 30 minutes",
  "exclude_allergens": ["peanut"]
}
```

**Flow:**
```
question
  │
  ├── Empty question? → random MyPlate recipes from Elasticsearch
  │
  └── LangGraph pipeline (text2cypher_v2):
        1. Extract_Constraints (Groq LLM)
           → {preferred_ingredients, excluded_ingredients, allergens,
              diet_tags, title_keywords, max_duration, limit}
        2. Map_Constraints_to_Cypher
           → parameterised Cypher (no freeform generation)
        3. Execute on Neo4j
        4. Fallback: param_search → Elasticsearch random (if pipeline fails)
  │
  └── Normalise results
        ├── Attach Nutri-Score colours (PostgreSQL)
        ├── Attach recipe scores
        └── Return recipe cards
```

**Databases:** Neo4j (primary), Elasticsearch (fallback).
**LLM:** Groq (constraint extraction only – Cypher is template-based, not free-form).

---

### POST /api/v1/recipes/param_search

**Purpose:** Deterministic, parameter-driven recipe filtering. No LLM involved.

**Input:**
```json
{
  "include_ingredients": ["chicken", "rice"],
  "exclude_ingredients": ["cream"],
  "exclude_allergens": ["gluten"],
  "diet_tags": ["dairy-free"],
  "dish_types": ["breakfast"],
  "max_duration_minutes": 45,
  "limit": 10,
  "offset": 0
}
```

**Flow:**
```
filters
  └── Build parameterised Cypher
        ├── ALL(ingredient IN include_ingredients WHERE EXISTS in graph)
        ├── NOT EXISTS excluded_ingredients
        ├── NOT EXISTS allergens via HAS_ALLERGEN
        ├── EXISTS diet_tags via HAS_TAG
        ├── EXISTS dish_types via HAS_TAG to Tag category=dish-type
        └── duration <= max_duration_minutes
  └── Execute on Neo4j → enriched recipe cards
        └── Attach image_url, duration, serves, nutri_score from PostgreSQL
```

Results are ordered deterministically for pagination: expert recipes first, then preferred catalog sources (`FoodHero` and `HealthyFoods`), then `has_profile=true` recipes, then recipes with duration and serving metadata, then stable recipe identity fields. Empty filter payloads return a stable profiled-recipe catalog page using `limit` and `offset`.

**Databases:** Neo4j, PostgreSQL.

---

### POST /api/v1/recipes/profile

**Purpose:** Parse raw recipe text and return full nutrition + sustainability profile.

**Input:**
```json
{
  "raw_recipe": "Chicken Tikka Masala\n2 chicken breasts...",
  "region": "IE",
  "persist_trace": false
}
```

**Flow:**
```
raw_recipe
  │
  └── LangGraph pipeline (recipe_profiling_chain):
        │
        ├── 1. Parse (Groq LLM)
        │      → title, ingredients[], measurements[], directions[], serves, total_time
        │
        ├── 2. Weigh (USDA weight tool)
        │      → grams per ingredient
        │      Fallback chain (in order):
        │        a) USDA canonical name match
        │        b) Hybrid USDA match (Chroma vector + BM25 lexical + FoodOn gate) → Postgres lookup
        │        c) Recipe1M LLM weight CSV
        │        d) FDA/LLM unit-grams CSV
        │        e) Live Groq/vLLM-compatible LLM (last resort)
        │        f) Herb/spice default (0.3g per pinch)
        │
        ├── 3. Nutrition (Chroma + PostgreSQL)
        │      → Query Chroma for best ingredient match
        │      → Fetch per-100g values from Postgres
        │      → Scale by weight, sum totals + per-serving
        │      → Source: Irish table (region=IE), USDA (region=US), or Hungarian table (region=HU)
        │
        ├── 4. Sustainability (Chroma)
        │      → Match ingredient to carbon footprint DB
        │      → kg CO2e per ingredient → total + per-serving
        │
        └── 5. Nutri-Score (A–E)
               → Computed from macros (energy, saturated fat,
                 sugar, sodium, protein, fibre, fruit/veg)
               → Optional: persist trace to PostgreSQL
```

**Databases:** Chroma (nutrition + sustainability matching), PostgreSQL (nutrient data + trace storage).
**LLM:** Groq for parsing + weight fallback by default; currently switched to local vLLM (`ingredient-tagger`, Llama 3.1 8B on :8008) for the bulk Recipe1M retag — switch back to Groq afterwards (`WEIGHT_LLM_SOURCE`).

---

## Ingredient matching strategy (weight & nutrition)

The hard part of profiling is mapping a free-text recipe ingredient ("3 cloves garlic, finely chopped", "1 lb boneless skinless chicken breast", "313 cups flour") onto (a) a gram weight and (b) a row in a composition table (USDA / Irish / Hungarian). Both are now **deterministic-first cascades** with cheap reliable steps tried before expensive fuzzy ones, hard guards against the obvious cross-category errors, and a confidence label on every result so weak matches are *visible* rather than silently wrong. The LLM is a last resort, and its output is verified before it's trusted.

### 1. Weight estimation — `tools/ingredient_weight_tool.ingredient_weight_tool_usda`

Per ingredient line, first hit wins:

1. **Repair the input first.** Recipe1M strips slashes from fractions (`34 cup` → `3/4 cup`, `12 lb` → `1/2 lb`), fuses the measurement into the name (`name="12 lbs lean hamburger", measurement="1"`), leaves a bare unit abbreviation in the name (`name="c. grated parmesan", measurement="1/3"` → `1/3 cup …`), and writes ranges (`1-2 tbsp`). All of that is normalised before anything else; the same repairs run in the recipe-line splitter.
2. **`28 oz can` is one can, not a 28× multiplier** — a `<N> oz/lb/g/ml <can/jar/tin/…>` measurement is parsed as a single container (the explicit-package-size path then reads `28 oz` → ~794 g) instead of multiplying by 28 → 22 kg.
3. **Verified offline reference table** (`ingredient_unit_reference_dataset` in Postgres) — for the recurring `(ingredient, normalized_unit)` signatures Neo4j actually contains, short-circuit the live cascade with a frozen, audited per-unit weight. Only `accepted_deterministic` and LLM-rebuilt rows with confidence ≥ 0.7 are used.
4. **USDA name match** — direct canonical lookup → hybrid embedding + BM25 lexical match → weight-name fallback, with FoodOn / food-class compatibility checks that reject implausible links (the matched record's FoodOn branch must be compatible with the ingredient).
5. **Deterministic fallbacks** — explicit package size in the text, common-unit reference tables (cup/tbsp/slice grams for known ingredients), liquid density (water-like vs other liquids), curated FDA/LLM unit-grams CSVs by name then by USDA id.
6. **USDA portion data** — the matched record's portion table (`1 cup chopped → X g`), scaled by quantity, with spice/powder hints.
7. **Recipe1M LLM CSVs** — the old 6,733-row batch is **gated off by default** (`RECIPE1M_LLM_FALLBACK_ENABLED=false`) — it was generated by an 8B with no verifier and is largely fabricated.
8. **Live LLM, last resort** — only when the cascade still produced nothing (`live_llm_missing_*`) or when the result's confidence is below `LIVE_LLM_CONFIDENCE_THRESHOLD` (default 0.45). The 8B prompt ships ~15 culinary reference anchors and is told to estimate one unit then multiply. **Its output is verified** (`_llm_portion_plausibility_error`): rejects tiny units that came back too heavy (`clove > 25 g`, `leaf > 5 g`, `pinch/dash > 5 g`, `teaspoon > 25 g`, `tablespoon > 60 g`), anything `> 5 kg` per unit, and oz/lb/L unit-conversion constants leaking onto non-mass units (`28.35` on a "clove", `1000` on a "can"). On rejection it keeps the deterministic value or records an error — it never poisons the recipe total.
9. **Confidence scoring** — every result gets 0–1: direct USDA ≈ 0.9, offline reference 0.90 / 0.70–0.88, deterministic fallbacks mid-range, `live_llm_*` low, minus penalties for inferred quantity/unit.

The verified offline reference is rebuilt by the pipeline in `docs/weight_reference_rebuild.md` (export signatures from Neo4j → deterministic candidates → LLM judge + verifier → materialise → import to Postgres).

### 2. Nutrition / composition-table match — `tools/nutrition_match.best_nutrition_match`

Used by `nutritional_calculator.nutritional_tool_chroma`. Per ingredient, in order:

0. **Hand-curated alias table** (`ingredient_nutrition_aliases`, ~190 ingredients / ~680 alias rows; `scripts/build_nutrition_aliases.py`) — checked first on the *raw* name then the cleaned name. Pins the high-frequency raw proteins / produce / staples to the canonical raw/plain USDA records: `chicken breast → Chicken, …, breast, skinless, boneless, meat only, raw`, `ground beef → Beef, ground, 85% lean / 15% fat, raw`, `red onion → Onions, raw`, `unsalted butter → Butter, without salt`, `low-fat yoghurt → Yogurt, plain, low fat`, `dark chocolate → Chocolate, dark, 60-69% cacao`, `red wine → Alcoholic beverage, wine, table, red`, etc. — including the low-fat/nonfat/light dairy variants, the chocolate/cocoa family and common alcohols. Hand-verified → `confidence="alias"`, wins outright. *(Raw-name-first matters: `clean_query` strips qualifiers like "unsalted"/"low-fat", so without it "unsalted butter" → "butter" → the salted-butter alias would win.)*
1. **Clean the query** — strip parentheticals, prep clauses (`finely chopped`, `to brush`, `for dusting`), qualifier words, leading quantities; normalise UK/US spellings (`yoghurt`→`yogurt`, `rocket`→`arugula`, `courgette`→`zucchini`) and singular/plural — *before* the embedding. (`raw`/`cooked`/`canned`/`light`/`dark` are kept — they're part of the food identity.)
2. **Gather candidates** from the Irish + USDA Chroma pools, **plus the `recipe1m-usda-links-canonical` link as one more candidate** scored on its own `embedding_similarity` — that table is itself a machine embedding-match (median sim ~0.77, *not* human-verified — it maps `all-purpose flour → "Potato flour"`, `garlic cloves → "garlic pear"`), so it competes with a small prior bonus, it never overrides; a branded/cooked label gets no bonus at all.
3. **Rerank** by `0.6·similarity + 0.4·BM25 + 0.15·token-overlap` — a zero-token-overlap "attractor" candidate (e.g. the `Italian Arborio risotto` record) is heavily demoted unless cosine ≥ 0.90; a `recipe1m → USDA` candidate that survives the guards gets a small prior; a candidate carrying a cooking-state / branded / dried marker the recipe didn't ask for is demoted (so `chicken breast` beats "Oscar Mayer … honey glazed" / "casseroled, meat and skin", `buttermilk` beats "… dried"), and a state-less query gets a small nudge toward the `…, raw` record.
4. **Food-class guard (hard gate)** — coarse classes (animal protein / dairy / plant-milk / egg / grain / legume / nut-seed / leafy-green / vegetable / fruit / spice-herb / oil-fat / sweetener / alcohol / …) with a conservative hard-incompatible set: rejects dairy↔plant-milk (`low-fat yoghurt ↛ Tofu yogurt`), animal↔non-animal (`chuck ↛ Cheese, colby`), alcohol↔grain (`chianti wine ↛ Arborio rice`), egg↔vegetable (`egg ↛ eggplant`), spice↔leafy-green (`arugula ↛ fenugreek`), vegetable↔grain (`red onion ↛ red rice`), spice↔grain (`ground cinnamon ↛ cinnamon bread`), …
5. **FoodOn ontology nudge (soft)** — on the top-3 only (a Neo4j round-trip per candidate, too costly for all), reuses the weight tool's `_foodon_class_ids_for_ingredient` / `_foodon_classes_have_common_ancestor` with form-word/synonym normalisation; only `±penalty` into the score, never a hard reject, because the local FoodOn graph is sparse and `is False` is often a false signal.
6. **Confidence label on every detail row** — `match_confidence` ∈ `{alias, curated, strong, weak, none}` + `match_reason` (incl. `:foodon_incompat`). A `weak` or `none` ingredient is visible in the profiling trace instead of being silently zeroed or silently accepted.

### 3. Recipe-level guards & quality signals — `tools/recipe_profiling_tool.Recipe_Profiling_Node`

- **`serves` sanitised** — a parsed/source `serves` in `[1, 50]` is kept (so "makes 24 cookies" survives); otherwise estimated from total recipe weight (~450 g/serving, clamped to `[1, 16]`); a wildly-large total weight is *not* trusted for the estimate (falls back to 4 — the weight cap below trims it). Recorded in `serves_source` ∈ `{given, estimated}`.
- **Weight sanity cap** — if total weight > 2.5 kg/serving, the dominant ingredient is trimmed (the `"313 cups flour" → 39 kg` parse-artefact class) toward ~700 g/serving, or the whole recipe is scaled down to the ceiling. `weights_capped` flag recorded; the sanitised totals are what feed nutrition + Nutri-Score.
- **Nutrition coverage** — `nutrition_coverage` = fraction of recipe weight that got a real nutrition match; `nutrition_low_coverage` flagged below ~0.8. Recipes with zeroed-out ingredients are now visible, not silently low.
- **Sustainability match** (`sustainability_calculator.best_sustainability_match`) — the same strategy, scaled down: clean the query → exact lookup in the cf-value index (`sustainability_ingredients` Chroma collection, ~7.4k entries) on the cleaned + singularised name → a small `recipe-name → DB-entry` alias map (`ground beef → beef`, `unsalted butter → butter`, `chicken breast → chicken`, `red onion → onion`, `cherry tomatoes → tomato`, `all-purpose flour → flour`, …, all targets verified to exist) → else the vector path with a **food-class hard gate**, a BM25 + token-overlap rerank and the same cooking-state/processed-marker demotion. Confidence label `sustainability_match_confidence` ∈ `{exact, alias, strong, weak, none}` on every ingredient. An incompatible / no match → no cf_val → that ingredient contributes 0 CO₂e (rather than an unrelated figure). `sustainability_coverage` (fraction of recipe weight with a CF match) + `sustainability_low_coverage` are recorded alongside `nutrition_coverage`. *(NB: the DB's cf_val numbers are category-quantised — all beef ≈ the same value etc. — and a few are off in absolute terms; getting the right category is what matters.)*
- All of the above + `match_confidence` land in `state.profiling_quality`, `full_profile.profiling_quality`, and `pipeline_trace.profiling.quality`. `scripts/validate_profiling.py` is the post-retag check (per-serving-kcal distribution, the `serves_source` / `weights_capped` / `coverage` / `match_confidence` mix, and divergence vs source-provided "ground truth" nutrition where present).

### Where the LLM is (and isn't) used

LLM (Groq / vLLM): recipe parsing, the last-resort weight estimate (verified), and the offline-reference rebuild's judge (also verified). **Not** the LLM: nutrition per-100 g lookup (deterministic Postgres join), Nutri-Score (a fixed formula in `utils/nutri_score.py`), and — the matchers themselves are deterministic ranking over embedding/lexical/ontology signals, not LLM calls.

---

## Weight Tool Progress

### 2026-05-11 reliability pass + offline reference + graph cleanup + nutrition matcher

Parser / cascade fixes:

- Slashless mass-fraction repair (`12 lb` -> `1/2 lb`, `34 lb` -> `3/4 lb`) and slashless range repair (`14-1 cup` -> `1/4-1 cup`, `14-13 cup` -> `1/4-1/3 cup`) in both the ingredient splitter and the weight parser.
- Lift a measurement fused into the *start* of the ingredient name (`name="12 lbs lean hamburger", measurement="1"` -> `1/2 lb lean hamburger`).
- Recombine a bare unit abbreviation fused into the name with the quantity left in the measurement field (`name="c. grated Parmesan cheese", measurement="1/3"` -> `1/3 cup grated Parmesan cheese`; also `tbsp.`, `tsp.`, `dash`, `pkg.`, `oz`, `lb`, `clove`, `slice`, `can`, …). Guarded against `candy`/`canned`/`cinnamon`-type false splits.
- Fixed the `"28 oz can"` bug: the leading number of a `<N> oz/lb/g/ml <can/jar/tin/…>` measurement was being read as an `N×` multiplier (so `28 oz can` of diced tomatoes -> 22 kg). It now parses as one container; the explicit-package-size path re-reads `28 oz` -> ~794 g.

LLM fallback hardening:

- Gated both Recipe1M LLM weight CSVs behind `RECIPE1M_LLM_FALLBACK_ENABLED` (default off) — that 6.7k-row batch was generated by an 8B with no verifier and is mostly fabricated (blanket 0.8 confidence, 1 L = 1 kg, qty arithmetic errors).
- Rewrote the live-LLM weight prompt (`ingredient_weight_llm_tool.py`) with ~15 culinary reference anchors (clove ≈ 3 g, cup flour ≈ 125 g, 28 oz can ≈ 795 g, …) and "estimate one unit, then multiply".
- Added an LLM-portion verifier to `_llm_portion_plausibility_error` (used by every live/cached LLM portion path): rejects tiny units that come back too heavy (`clove > 25 g`, `leaf > 5 g`, `pinch/dash > 5 g`, `teaspoon > 25 g`, `tablespoon > 60 g`), anything `> 5 kg` per unit, and oz/lb/L unit-conversion constants leaking onto non-mass units (e.g. `28.35` on a "clove", `1000` on a "can"). Rejections become an `error` instead of poisoning the recipe total.
- `_apply_low_confidence_live_llm` now runs that verifier on the LLM estimate and, on rejection, keeps the (low-confidence but plausible) deterministic value instead of swapping in the bad LLM number.
- `WEIGHT_LLM_SOURCE` / `WEIGHT_LLM` / `PARSE_LLM` switched to local vLLM (`ingredient-tagger`, Llama 3.1 8B on :8008) for the bulk retag; switch back to Groq names afterwards.

Offline weight-reference dataset (now live):

- Ran the full rebuild pipeline (`docs/weight_reference_rebuild.md`): 45,910 `(ingredient, unit)` signatures from Neo4j -> deterministic candidates -> 1,794 `needs_llm_rebuild` rows judged by the 8B + verifier (1,792 accepted, 2 rejected) -> materialized `data/processed/weight_reference/ingredient_unit_reference_dataset.csv`.
- Wired it into the runtime cascade: `_lookup_offline_reference` short-circuits before the live USDA/embedding lookup with `match_type="offline_reference_dataset"`; only `accepted_deterministic` and `llm_rebuilt`-with-confidence ≥ 0.7 rows are used. Confidence: 0.90 deterministic, 0.70–0.88 LLM-rebuilt.
- Imported the dataset (and re-imported the other 8 static files) into the `pipeline_static_data` Postgres table — the runtime reads static data from Postgres, not disk, so a fresh CSV is inert until `scripts/postgres/import_pipeline_static_data.py` is re-run.
- Added a verifier to the LLM judge (`preprocessing/one_off/judge_weight_reference_candidates_vllm.py`): `verifier_status` / `verifier_reason` columns; rejects suspicious-default weights, `> 5 kg`/unit, deterministic verdicts with no parseable candidate, etc.
- Fixed the range-parse bug in `preprocessing/one_off/export_weight_reference_signatures.py` (`1-2 tablespoons` -> `("1","tablespoon")` instead of `("1","-2")`).

Profiling-pipeline accuracy guards (`recipe_profiling_tool.Recipe_Profiling_Node`):

- **`serves` sanity-check + estimator** (`_sanitize_serves`) — a parsed/source `serves` in `[1, 50]` is kept (so "makes 24 cookies" survives); otherwise estimated from total recipe weight (~450 g/serving, clamped to `[1, 16]`); if the total weight is itself absurd, fall back to 4 (so the weight cap below trims it instead of `serves` ballooning). `state.serves` is overwritten with the sanitised value; `serves_source` ∈ `{given, estimated}` is recorded.
- **Per-recipe weight sanity cap** (`_cap_recipe_weights`) — if total weight > 2.5 kg/serving, trim the dominant ingredient (the `"313 cups flour" → 39 kg` parse-artefact class) down toward ~700 g/serving, or scale the whole recipe down to the ceiling if no single ingredient dominates. `weights_capped` flag recorded; the (sanitised) totals are what feed nutrition + Nutri-Score. *(e.g. a broken "313 cups flour" recipe: 39.3 kg → 2.8 kg, kcal/serving 8,939 → 2,500, flagged.)*
- **Coverage metrics** — `nutrition_coverage` = fraction of recipe weight that got a real nutrition match (`nutrition_low_coverage` flagged below ~0.8), and likewise `sustainability_coverage` / `sustainability_low_coverage`. Recipes with zeroed-out ingredients are now *visible*, not silently low.
- All of the above + `match_confidence` (nutrition) and `sustainability_match_confidence` land in `state.profiling_quality`, `full_profile.profiling_quality`, and `pipeline_trace.profiling.quality`, plus declared top-level `RecipeState` fields.
- **Sustainability ingredient match reworked** (`sustainability_calculator.best_sustainability_match` → `(cf_val, matched_name, confidence∈{exact,alias,override,strong,weak,none})`) — same strategy as the nutrition matcher: clean query → exact lookup in the cf-value index (the `sustainability_ingredients` Chroma collection's `name → cf_val`, ~7.4k entries) on the cleaned + singularised name → a `recipe-name → DB-entry` alias map (`ground beef → beef`, `unsalted butter → butter`, `chicken breast → chicken`, `red onion → onion`, `cherry tomatoes → tomato`, `all-purpose flour → flour`, …, ~60 verified targets) → else the vector path with a **food-class hard gate** + BM25/token-overlap rerank + cooking-state/processed-marker demotion. `_SUST_CF_OVERRIDE` hand-corrects obvious DB errors — the DB tags `beef stock` with the *solid* beef CF (≈19.5 kg CO₂e/kg) → override 2.0; `chicken broth` → 1.5; `vegetable stock` → 0.4; `salt` → 0.02. Incompatible / no match → `cf_val=None` → 0 CO₂e for that ingredient (not an unrelated figure). The `beef stock` override alone halved the beef-stew sample's footprint: 36.8 → 20.0 kg CO₂e (6.14 → 3.33 /serving). Replaces the old naive `query_sustainability_db(name)[0]`.
- **`scripts/validate_profiling.py`** — post-retag sanity check: reads a JSONL of profiled-recipe dicts and reports the per-serving-kcal distribution (flags the `<50` / `>1200` tails), the `serves_source` / `weights_capped` / `nutrition_coverage` / `match_confidence` mix, and — where a recipe carries source-provided ("ground truth") nutrition — the divergence vs the recomputed values (flags >25 %).

Neo4j graph cleanup:

- `scripts/cleanup_non_food_ingredients.py --apply`: removed 17,769 non-food `HAS_INGREDIENT` edges (cooking-spray family, foil, skewers, baking sheets, rolling pins, toothpicks, spatulas, graters, …), 144 orphaned `Ingredient` nodes, and 4,331 recipes left with `< 2` ingredients (≈132 from the cleanup, ≈4,200 pre-existing 0–1-ingredient shells). Graph now ≈811 k recipes / ≈43.4 k ingredients / ≈7.00 M edges.

Nutrition matcher (recipe ingredient -> composition-table record):

- New `tools/nutrition_match.py` (`best_nutrition_match`) replaces the naive top-1 Chroma vector pick used by `nutritional_calculator.nutritional_tool_chroma`. It: (0) checks a **hand-curated alias table** first — checked on the *raw* name then the cleaned name (`scripts/build_nutrition_aliases.py` → `data/processed/fallbacks/ingredient_nutrition_aliases.csv` → `pipeline_static_data.ingredient_nutrition_aliases`, ~680 alias rows / ~190 ingredients) — `chicken breast` → `Chicken, …, breast, skinless, boneless, meat only, raw`, `ground beef` → `Beef, ground, 85% lean / 15% fat, raw`, `red onion` → `Onions, raw`, `buttermilk` → `Milk, buttermilk, fluid`, `unsalted butter` → `Butter, without salt`, `low-fat yoghurt` → `Yogurt, plain, low fat`, `dark chocolate` → `Chocolate, dark, 60-69% cacao`, `red wine` → `Alcoholic beverage, wine, table, red`, etc. — including the low-fat/nonfat/light dairy variants, the chocolate/cocoa family, and common alcohols. `confidence="alias"`, hand-verified, wins outright. (Raw-name-first matters because `clean_query` strips qualifiers like "unsalted"/"low-fat"; and `clean_query` no longer strips `light`/`dark` so `dark chocolate`/`dark soy sauce` keep that token.) (1) **cleans the query** — strips parentheticals, prep clauses, qualifier words, leading quantities, normalises UK/US spellings + singular/plural, before the embedding; (2) gathers candidates from the Irish + USDA Chroma pools **plus the `recipe1m-usda-links-canonical` entry as one more candidate** scored on its own `embedding_similarity` (this table is itself a machine embedding-match, median sim ~0.77 — *not* human-verified — so it competes with a small prior bonus, it never overrides; e.g. its `all-purpose flour → Potato flour` and `garlic cloves → garlic pear` entries are correctly beaten by the vector candidates); (3) **reranks** by similarity + BM25 + token overlap, with a zero-overlap "attractor" candidate (e.g. the `Italian Arborio risotto` record) heavily demoted unless cosine ≥ 0.90; (4) applies a **food-class compatibility guard** (hard gate) — coarse classes (animal protein / dairy / plant-milk / egg / grain / legume / nut-seed / leafy-green / vegetable / fruit / spice-herb / oil-fat / sweetener / alcohol / …) with a conservative hard-incompatible set that rejects dairy↔plant-milk (`low-fat yoghurt`↛`Tofu yogurt`), animal↔non-animal (`chuck`↛`Cheese, colby`), alcohol↔grain (`chianti wine`↛`Arborio rice`), egg↔vegetable (`egg`↛`eggplant`), spice↔leafy-green (`arugula`↛`fenugreek`), vegetable↔grain (`red onion`↛`red rice`), … — **plus a soft FoodOn ontology nudge** on the top-3 candidates (reuses the weight tool's Neo4j-backed `_foodon_class_ids_for_ingredient` / `_foodon_classes_have_common_ancestor`, with form-word/synonym normalisation; only `±penalty` into the score, never a hard reject — the local FoodOn graph is sparse — and only on the top-3 to keep the Neo4j round-trips bounded); (5) a **threshold split** — results carry `match_confidence` (`alias`|`curated`|`strong`|`weak`|`none`) and `match_reason` (incl. `:foodon_incompat`) on every detail row, so weak/no-match ingredients are visible in the trace instead of silently zeroed or silently accepted. Tuning: `_STRONG_SCORE` / `_WEAK_SCORE` / `_CURATED_BONUS` / `_HARD_INCOMPATIBLE` in `nutrition_match.py`. `nutrition_match.py` calls `load_runtime_env()` at import (the curated-link / FoodOn lookups need `NUTRITION_*` env vars).
- Match-quality audit (`scripts/audit_nutrition_matches.py --matcher new|old`, 400 sampled Neo4j ingredients, seed 13): loose-flag rate ~42–43% — but that number is the audit's *deliberately loose* heuristic and ~half of its flags are its own false positives (plurals/spellings/low-but-fine similarities); filtering those, the realistic read is **~88–92% correct (incl. `alias`/`curated`/`strong`), ~3–4% honest `none`/`weak` (visible in the trace, not silently wrong), ~5–8% genuinely-wrong-but-accepted** (close-but-imperfect: `green capsicum → Peppers, sweet, red, raw`, `cherry chips → Cherries, sweet, raw`, …) — vs the pre-rework ~10–20% *silent*-wrong, with the catastrophic cross-category cases (yoghurt↔tofu, beef↔cheese, wine↔rice, arugula↔fenugreek, all-purpose-flour↔potato-flour) and the cooked/deli-protein class (chicken breast↔deli-roll) eliminated. Sample-recipe spot-check (`Recipe_Profiling_Chain_Structured`, 3 recipes / 24 ingredients): all 24 correct — `chicken breast → Chicken, …, breast, skinless, boneless, meat only, raw`, `buttermilk → Milk, buttermilk, fluid, cultured, lowfat`, `red onion → Onions, raw`, `all-purpose flour → Wheat flour, white, all-purpose, enriched`, garlic/broccoli/carrots/ginger/eggs/sesame-oil/olive-oil/feta/arugula/cherry-tomatoes/soy-sauce/white-rice/… all to their raw/plain records. The chicken-breast fix alone moved the stir-fry sample from ~2,167 mg sodium/serving (Nutri-Score C) to ~579 mg (Nutri-Score A). Residual long tail (bge-small): specialty sauces collapsing to `Sauce, barbecue`, mushroom varieties, "english cucumber"↔"english walnut", within-meat (`chicken mince`↔`lamb mince`) — extend the alias table (`scripts/build_nutrition_aliases.py`) for the worst recurring offenders, or switch to `bge-base` / a cross-encoder rerank; diminishing returns.

Audit (1,000-recipe sample, seed 13): flagged rows 1,586 -> 1,210; `error:missing_unit` 540 -> 336; the LLM-portion verifier caught 31 bad outputs and turned them into errors. Catastrophic deterministic outliers (`> 3 kg` ingredient on inputs like `"313 cups flour"`) are unchanged and would need a per-recipe sanity cap (not done).

Validation run:

- `cd tests && PYTHONPATH=../src uv run python -m unittest test_ingredient_weight_confidence test_ingredient_line_splitter test_cleanup_non_food test_nutrition_match test_profiling_accuracy` (89 tests, all green)
- `uv run python -m py_compile src/recipe_wrangler/tools/ingredient_weight_tool.py src/recipe_wrangler/tools/ingredient_weight_llm_tool.py src/recipe_wrangler/tools/recipe_profiling_chain.py src/recipe_wrangler/tools/recipe_profiling_tool.py src/recipe_wrangler/tools/nutrition_match.py src/recipe_wrangler/tools/nutritional_calculator.py src/recipe_wrangler/tools/sustainability_calculator.py scripts/cleanup_non_food_ingredients.py scripts/audit_nutrition_matches.py scripts/build_nutrition_aliases.py scripts/validate_profiling.py`

### 2026-05-07 accuracy pass

- Added hybrid USDA ingredient linking: Chroma vector candidates plus BM25 lexical candidates are deduplicated, reranked, and exposed as `match_source="hybrid"`.
- Added FoodOn compatibility checks for hybrid matches. When both the raw ingredient and candidate have FoodOn classes, incompatible taxonomy branches are rejected; missing FoodOn data falls back neutrally to lexical/vector scoring.
- Added deterministic guards for known bad category matches, large bare-number quantities, ambiguous USDA portion descriptions, packaged/count units, and common unit references.
- Added conservative LLM-derived unit-weight filtering and merged accepted rows into `data/processed/recipe1m/food_weights_updated.csv`.
- Added `preprocessing/one_off/audit_weight_tool_outliers.py` to sample Recipe1M recipes and flag suspicious weights such as huge ingredients, heavy volume units, live LLM fallbacks, missing units, and low confidence rows.
- Fixed Recipe1M slashless fraction artifacts in the ingredient splitter and weight parser, e.g. `34 cup` -> `3/4 cup`, `12 cup` -> `1/2 cup`, `1 12 teaspoons` -> `1 1/2 teaspoons`.

Validation run:

- `uv run python tests/test_ingredient_weight_confidence.py`
- `uv run python tests/test_ingredient_line_splitter.py`
- `uv run python -m py_compile src/recipe_wrangler/tools/ingredient_weight_tool.py src/recipe_wrangler/tools/recipe_profiling_chain.py preprocessing/one_off/audit_weight_tool_outliers.py`

---

### POST /api/v1/recipes/

**Purpose:** Create a new recipe and immediately compute its nutrition profile and Nutri-Score.

**Input:**
```json
{
  "title": "Pasta Carbonara",
  "ingredients": ["100g spaghetti", "2 eggs", "50g bacon", "30g parmesan"],
  "instructions": ["Cook pasta", "Fry bacon", "Mix and serve"],
  "serves": 2,
  "duration": 20,
  "region": "IE",
  "image_url": null,
  "tags": ["gluten-free"],
  "allergens": ["egg", "milk"]
}
```

`region` must be `IE`, `US`, or `HU`. `instructions`, `image_url`, `tags`, `allergens` are optional.

**Flow:**
```
ingredients + title
  └── Profiling pipeline (same as /profile)
        └── Nutri-Score computed
              └── Write recipe node to Neo4j
                    └── Write nutrition trace to PostgreSQL
                          └── Return {recipe_id, message}
```

**Databases:** Neo4j (recipe node), PostgreSQL (nutrition trace).
**LLM:** Groq (weight estimation fallback).

**Example:**
```bash
curl -X POST http://localhost:8001/api/v1/recipes/ \
  -H "Content-Type: application/json" \
  -d '{"title":"Omelette","ingredients":["2 eggs","10g butter"],"serves":1,"duration":5}'
```

---

### POST /api/v1/recipes/{recipe_id}/substitute

**Purpose:** Find the best substitute for a given ingredient in a recipe and try to re-profile the modified recipe.

**Input:**
```json
{
  "ingredient": "butter",
  "region": "IE"
}
```

`region` must be `IE`, `US`, or `HU`. Defaults to `IE`.

**Flow:**
```
recipe_id + ingredient
  │
  ├── Neo4j → fetch recipe (title, ingredients, measurements, serves)
  │
  ├── Substitution lookup (in order):
  │     1. HAS_SUBSTITUTION edges (MISKG-curated) — exact name match
  │     2. HAS_SUBSTITUTION edges (MISKG-curated) — single-word-qualifier variants
  │          e.g. "salted butter", "clarified butter" for query "butter"
  │     3. FoodOn taxonomy — progressive depth (1 hop, then 2, then 3)
  │          stops at the tightest ancestor that yields results
  │
  ├── 404 if no substitutes found
  │
  ├── Swap ingredient name in recipe (keep original measurement)
  │
  └── Re-profile with Recipe_Profiling_Chain_Structured (no LLM parse)
        → Chroma + PostgreSQL nutrition lookup
        → Nutri-Score recomputed
        → Return original_ingredient, substitute, candidates, modified nutrition profile
```

**Databases:** Neo4j (recipe + substitution graph), Chroma (nutrition matching), PostgreSQL (nutrient data).

**Returns:** `original_ingredient`, `substitute` (best pick), `substitution_source` (`graph_direct` or `foodon_taxonomy`), `candidates` (all found), `modified_recipe_profile`.

`modified_recipe_profile` has two modes:
- Normal path: full profiling output with recalculated nutrition.
- Fallback path: `status="profiling_unavailable"` plus modified ingredient list and measurements. This keeps API call usable even when downstream profiling stack is unavailable or too slow.

**Example:**
```bash
curl -X POST http://localhost:8001/api/v1/recipes/dfe70383db/substitute \
  -H "Content-Type: application/json" \
  -d '{"ingredient": "butter", "region": "IE"}'
```

---

## Experimental Adaptation API

Tracked in repo as standalone service:
- `POST /api/v1/recipes/{recipe_id}/adapt/suggestions`
- `POST /api/v1/recipes/{recipe_id}/adapt/simulate`

Local run:
```bash
PYTHONPATH=src uvicorn recipe_wrangler.services.adaptation.app:app --reload --port 8101
```

Swagger UI:
`http://127.0.0.1:8101/docs`

### POST /api/v1/recipes/{recipe_id}/adapt/suggestions

**Purpose:** Recommend what to change in recipe.

**Input:**
```json
{
  "region": "IE",
  "mode": "nutrition",
  "max_swaps": 1,
  "use_llm": false
}
```

**Mechanism:**
```
recipe_id
  ├── PostgreSQL → load stored profiling trace
  ├── Find worst offender
  │     nutrition mode       → worst negative Nutri-Score contributor
  │     sustainability mode  → highest CO2e contributor
  │     reduce_quantity mode → worst nutrient contributor for portion cut
  ├── Neo4j → find substitute candidates
  │     1. MISKG substitutions
  │     2. FoodOn taxonomy siblings
  ├── Guard candidates by food-class compatibility
  ├── Re-simulate candidates with nutrition/sustainability calculators
  └── Return ranked suggestions
```

**Returns:** offending ingredient, contribution percentage, ranked suggestions, simulated improvement metrics. If recipe is already good enough for chosen mode, endpoint returns a validation-style error instead of fake suggestions.

### POST /api/v1/recipes/{recipe_id}/adapt/simulate

**Purpose:** Simulate exact swap chosen by client.

**Input:**
```json
{
  "region": "IE",
  "swap": {
    "original_ingredient": "cinnamon",
    "substitute_ingredient": "vanilla"
  }
}
```

**Mechanism:**
```
recipe_id + explicit swap
  ├── PostgreSQL → load stored profile
  ├── Resolve original ingredient weight/details
  ├── Match substitute nutrition + sustainability
  ├── Recompute totals, per-serving, per-100g
  ├── Recompute Nutri-Score breakdown
  └── Return before/after deltas
```

**Returns:** original/simulated Nutri-Score, nutrient deltas, original/simulated per-serving and per-100g totals, and CO2e deltas when available.

---

### PATCH /api/v1/recipes/{recipe_id}

**Purpose:** Update mutable fields on an existing recipe.

**Input:**
```json
{
  "instructions": ["Updated step 1", "Updated step 2"],
  "image_url": "https://example.com/image.jpg"
}
```

Both fields are optional. At least one must be provided.

**Flow:**
```
recipe_id + fields
  ├── Neo4j  → update instructions / image_url on Recipe node
  └── Elasticsearch → reindex image_url if changed
        └── Return {recipe_id, updated_fields, message}
```

Returns 404 if the recipe does not exist in Neo4j.

**Databases:** Neo4j, Elasticsearch.

**Example:**
```bash
curl -X PATCH http://localhost:8001/api/v1/recipes/3758007968 \
  -H "Content-Type: application/json" \
  -d '{"instructions": ["Step 1", "Step 2", "Serve hot"]}'
```

---

## Services & Data Stores

| Service | Role | Docker |
|---|---|---|
| **Neo4j** | Recipe graph: recipes, ingredients, allergens, diet tags | `neo4j-docker/docker-compose.yml` |
| **Elasticsearch** | Recipe title autocomplete and fallback search | `elasticsearch-docker/docker-compose.yml` |
| **Chroma** | Vector similarity for nutrition & sustainability matching | `chromadb-docker/docker-compose.yml` |
| **PostgreSQL** | Nutrition data (USDA + Irish) and profiling traces | `postgresql-docker/docker-compose.yml` |

---

## Running Locally

### 1. Start services

```bash
docker compose -f neo4j-docker/docker-compose.yml up -d
docker compose -f postgresql-docker/docker-compose.yml up -d
docker compose -f chromadb-docker/docker-compose.yml up -d
docker compose -f elasticsearch-docker/docker-compose.yml up -d
```

### 2. Install dependencies

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
```

### 3. Configure environment

```bash
cp .env.example .env  # edit with your values
```

Required:
```
NEO4J_URI=bolt://localhost:7687
GROQ_API_KEY=gsk-...
ELASTIC_URL=http://localhost:9200
ELASTIC_INDEX=recipes
CHROMA_HOST=localhost
CHROMA_PORT=8000
NUTRITION_HOST=localhost
NUTRITION_PORT=5432
NUTRITION_DB=...
NUTRITION_USER=...
NUTRITION_PASSWORD=...
```

### 4. Run migrations

```bash
PYTHONPATH=src alembic upgrade head
```

### 5. Start API

```bash
PYTHONPATH=src uvicorn recipe_wrangler.api.main:app --reload --port 8001
```

Swagger UI: http://localhost:8001/docs

---

## Repository Structure

```
src/recipe_wrangler/
├── api/               # FastAPI app, routers, config, dependencies
├── tools/             # LangGraph pipelines and tool implementations
├── repositories/      # Data access layer (Neo4j, Postgres, Chroma)
├── utils/             # DB clients, embedding helpers, scoring utilities
└── schemas/           # Pydantic models

preprocessing/         # One-time data import scripts (not part of the API)
notebooks/             # End-to-end tool tests
*-docker/              # Docker Compose files per service
alembic/               # Database migrations
```

---

## Smoke Test

```bash
BASE=http://localhost:8001

curl -sS "$BASE/health"

curl -sS "$BASE/api/v1/recipes/autocomplete?q=pasta"

curl -sS -X POST "$BASE/api/v1/recipes/search" \
  -H "Content-Type: application/json" \
  -d '{"question": "quick chicken pasta", "exclude_allergens": []}'

curl -sS -X POST "$BASE/api/v1/recipes/param_search" \
  -H "Content-Type: application/json" \
  -d '{"include_ingredients": ["chicken"], "max_duration_minutes": 30}'

curl -sS -X POST "$BASE/api/v1/recipes/profile" \
  -H "Content-Type: application/json" \
  -d '{"raw_recipe": "Pasta\n200g spaghetti\n100g tomatoes\nBoil pasta. Add sauce.", "region": "IE"}'

curl -sS -X POST "$BASE/api/v1/recipes/" \
  -H "Content-Type: application/json" \
  -d '{"title": "Omelette", "ingredients": ["2 eggs", "10g butter"], "serves": 1, "duration": 5, "region": "IE"}'

# Use the recipe_id returned by create above
curl -sS -X PATCH "$BASE/api/v1/recipes/{recipe_id}" \
  -H "Content-Type: application/json" \
  -d '{"instructions": ["Beat eggs", "Cook in butter", "Fold and serve"]}'

curl -sS -X POST "$BASE/api/v1/recipes/{recipe_id}/substitute" \
  -H "Content-Type: application/json" \
  -d '{"ingredient": "butter", "region": "IE"}'
```

---

## Code Cleanup Recommendations

The codebase has grown organically and has several areas worth tidying before sharing with the team.

### High priority

**1. Delete `tools/text2cypher.py` (v1)**
`text2cypher_v2.py` is in production. v1 uses a different LLM provider and a different architecture. It is not called anywhere in the active API. Remove it to avoid confusion.

**2. Delete `utils/nutrition_postgres.py` (v1)**
This version uses `subprocess` to call `psql` directly. `nutrition_postgres_v2.py` uses SQLAlchemy and is what the API actually uses. Remove v1.

**3. Rename files to drop version suffixes**
Once v1 files are removed, rename `text2cypher_v2.py` → `text2cypher.py` and `nutrition_postgres_v2.py` → `nutrition_postgres.py` and update all imports accordingly.

**4. Add `.env.example`**
There is no `.env.example` in the repo. Add one with all required and optional keys (values blanked out). This is essential for onboarding.

**5. Verify `.gitignore` covers data files**
Confirm `chroma_db/`, `*.dump`, `*.sqlite`, `data/`, `qdrant_storage/`, `*.pkl`, `*.csv` (in data dirs) are all ignored. A `nutrients_postgres_2026-02-16.dump` and `tmp_recipe_images.sqlite` are currently untracked — do not commit these.

### Medium priority

**6. Move `preprocessing/` scripts out of the main repo (or into a subdir)**
These are one-time data pipeline scripts, not part of the running service. Consider moving them to a separate repo or a clearly labelled `scripts/data-prep/` folder with its own README explaining when each script runs and in what order.

**7. Split `routers/recipes.py` (960 lines)**
This file mixes routing, business logic, and normalisation. Extract:
- Normalisation helpers → `utils/normalisation.py`
- Fallback search logic → a service layer function
- Endpoint handlers should only call service functions and return responses

**8. Remove `utils/user_preferences.py` if it is a stub**
If this file only contains placeholder code, delete it. Stub files add noise when reading the codebase.

**9. Clean up `preprocessing/` `__init__.py` filename**
`__.init.__.py` is a typo — it is not a valid Python package init file. Either rename it to `__init__.py` or delete it.

### Low priority / nice to have

**10. Add a `CONTRIBUTING.md`** with setup steps, branch naming, and PR expectations.

**11. Pin the Groq model name in config** rather than using `openai/gpt-oss-20b` as the default, which is confusing (it is routed through Groq's OpenAI-compatible API but reads like an OpenAI model).

**12. Add type hints to repository functions** — `repositories/` is the newest layer and is the right place to enforce this going forward.
