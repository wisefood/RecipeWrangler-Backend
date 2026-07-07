# services/

Service modules that sit alongside the main API (`src/recipe_wrangler/api/`).
They are tracked and shipped with the backend.

Each subdirectory is one service. Currently:

| Service | Status | Mounted on |
|---|---|---|
| [`adaptation/`](#adaptation) | in-test (v5) — nutrition / sustainability / reduce-quantity | Standalone FastAPI app on port 8101 |

---

## adaptation

Suggests how to improve a recipe along two axes — Nutri-Score or carbon footprint — by **swapping** an ingredient for a better one or **reducing** the worst offender's quantity. Candidate swaps come from the recipe knowledge graph (MISKG substitution edges + FoodOn taxonomy siblings); the food-composition tables (Irish / USDA / Hungarian) provide nutritional comparison; FlavorDB provides a flavor-similarity tiebreak; the sustainability table provides per-ingredient CO2e.

### Status

**v5 — tracked in repo, standalone API, no UI.** Built up in layers, each traceable:

- **v1** deterministic swap pipeline (target worst negative nutrient → rank offenders → graph substitutes → simulate + re-score).
- **v2** token-based FCT→graph name resolver + strict grade-preservation gate.
- **v3** optional LLM judge (filter + rerank culinary nonsense, opt-in via `use_llm`).
- **v4** sustainability mode (target highest-CO2e ingredient; rank by CO2e reduction).
- **v5** four additions, documented below: **food-class guard** (always-on, kills cross-category nonsense deterministically), **FlavorDB tiebreak** (flavor-aware ranking), **reduce-quantity mode** (use less when no swap helps), **nutri-guard on sustainability** (a CO2e swap may not worsen the grade).

Current canonical run path is standalone FastAPI app (`app.py`) on port 8101.

### Endpoints

Standalone service exposes both under `POST /api/v1/recipes/{recipe_id}/adapt/`.

#### `POST /adapt/suggestions`
Returns the top-N substitute candidates for the recipe's worst-scoring negative nutrient.

Request:
```json
{ "region": "IE", "mode": "nutrition", "max_swaps": 3, "use_llm": true }
```
- `region`: `IE` / `US` / `HU` — selects the composition table.
- `mode`:
  - `nutrition` — swap to improve the worst Nutri-Score negative nutrient.
  - `sustainability` — swap to cut the highest-CO2e ingredient (with a nutri-guard so the grade can't worsen).
  - `reduce_quantity` — when no swap helps, recommend *using less* of the worst nutrient contributor (the smallest cut that still improves the grade). No LLM, no graph substitutes needed.
- `max_swaps`: 1–3, number of ranked suggestions to return after the LLM filter (if enabled).
- `use_llm` (default `false`): when `true`, an LLM judge runs over the top-10 deterministic candidates, rejects culinary-nonsense swaps (e.g. swapping butter for salt in a butter sauce, or beef mince for offal in a burger), and reranks the survivors by recipe-aware sense. See *LLM judge* below. Ignored in `reduce_quantity` mode.

Each suggestion carries an `action` field: `"swap"` (replace with `substitute_name`) or `"reduce"` (keep the ingredient, see the `reduced_*` fields).

Response — see `schemas.SuggestionsResponse`. The `mode` field on the response tells the client which axis was optimised.

**Nutrition-mode fields:** `current_nutri_score`, `target_nutrient`, `target_nutrient_label`, `target_nutrient_points`, plus per suggestion: `simulated_nutri_score`, `nutri_score_points_saved`, `target_nutrient_per_100g`, `original_per_100g`, `nutrient_delta_per_serving`.

**Sustainability-mode fields:** `current_co2e_per_serving_kg`, `current_co2e_total_kg`, plus per suggestion: `simulated_co2e_per_serving_kg`, `co2e_reduction_per_serving_kg`, `co2e_reduction_pct`, `original_cf_kg_co2e_per_kg`, `candidate_cf_kg_co2e_per_kg`.

**Reduce-quantity-mode fields:** same context block as nutrition mode (`current_nutri_score`, `target_nutrient*`), plus per suggestion (`action: "reduce"`): `simulated_nutri_score`, `nutri_score_points_saved`, `reduced_from_weight_g`, `reduced_to_weight_g`, `reduction_pct`. `substitute_name` / `source` / `category_distance` are null.

**Shared fields (all modes):** `offending_ingredient`, `offending_ingredient_contribution_pct` (% of the relevant total — nutrient amount or CO2e), `suggestions[]` with `rank`, `action`, `original_ingredient`, `substitute_name`, `source`, `category_distance`, `flavor_similarity` (FlavorDB Jaccard in [0,1], or null when coverage is too sparse), `introduces_allergen`, `new_allergens`, `explanation`, optional `llm_justification`. When `use_llm=true`: also `llm_used`, `llm_model`, `llm_source`, `llm_rejected[]`.

#### `POST /adapt/simulate`
Apply a specific swap and return what the recipe's nutrient totals and Nutri-Score would become.

Request:
```json
{
  "region": "IE",
  "swap": {
    "original_ingredient": "cream, whipped, cream topping, pressurized",
    "substitute_ingredient": "nonfat sour cream",
    "weight_g": 120.0
  }
}
```
- `weight_g` is optional; defaults to the original ingredient's weight in the profile.
- Substitutes are not pre-validated against Neo4j — the service just tries to match them in the food-composition table. 422 on no match.

Response — see `schemas.SimulateResponse`. Returns `original_*` and `simulated_*` totals (per 100g and per serving), `nutrient_delta`, the full simulated `nutri_score_breakdown`, plus `original_co2e_per_serving_kg`, `simulated_co2e_per_serving_kg`, `co2e_reduction_per_serving_kg` (so frontends can show both nutrition AND CO2e impact of any swap).

### How it works

Pipeline lives in `service.py` and follows the 5-step plan:

1. **Target nutrient** (`_identify_target_nutrient`): from the persisted `nutri_score_breakdown`, pick the highest-scoring negative nutrient — one of `energy`, `sugar`, `saturated_fats`, `sodium`. Requires ≥ 3 points (lower → no adaptation needed).

2. **Offender** (`_rank_offender_candidates` + walk-down in `generate_suggestions`): rank recipe ingredients by their absolute contribution to the target nutrient. Walk down the list until one ingredient yields a viable suggestion. Each candidate must:
   - Have a Neo4j substitution path (MISKG `HAS_SUBSTITUTION` edge OR a `FoodOnClass` with siblings).
   - Be resolved from its FCT canonical name (e.g. `"soy sauce made from soy (tamari)"`) to the matching Neo4j Ingredient node name (e.g. `"soy sauce"`) via `resolve_graph_name()`.

3. **Substitutes** (`find_substitute_candidates`): query Neo4j for both MISKG substitutes and FoodOn siblings (1→2→3-hop fallback), tagged with `source` (`miskg` / `foodon`) and `category_distance` (`low` / `medium` / `high`). Deduped by lowercase name; MISKG wins ties.

4. **Filter + simulate** (`_evaluate_candidate`): for each candidate, in order —
   - **Food-class guard** (`_food_class_compatible`): reject up front if the candidate's `food_class` is incompatible with the original's. Same class passes; a small `_CLASS_COMPATIBILITY` map also allows `dairy ↔ oil_fat` (butter↔margarine↔oil). Lenient when either side can't be classified. This is the deterministic wall against cross-category nonsense (sugar→oil, butter→chocolate chips) — fires always, no LLM.
   - Run `nutritional_tool_chroma` at 100g for the candidate's per-100g profile. Discard if no FCT match or `target_per_100g ≥ original_per_100g`.
   - Simulate the swap (re-sum whole-recipe per-100g), re-score with `compute_nutri_score_breakdown_from_values`, and drop unless the target's negative points decrease **and** the overall letter grade strictly improves (grade-preservation gate).

5. **Rank + explain** (sort by `points_saved`, then `relative_improvement`, then **`flavor_similarity`** as a tiebreak): `_build_explanation` builds a templated headline + reason. Flag new allergens (`HAS_ALLERGEN` edges on candidate not on original).

   **FlavorDB tiebreak** (`flavor_similarity` in `neo4j_queries.py`): Jaccard overlap of the two ingredients' FlavorDB flavor-compound sets (best-mapped `FlavorDBIngredient` per side via `FLAVORDB_EQUIVALENT`). Used **only as a tiebreak** over the already culinarily-sane MISKG/FoodOn candidate set — never as a generator (pointed at FlavorDB directly it would call tea a butter substitute). **Sparsity-gated**: returns null unless both ingredients have ≥15 mapped compounds, so sparse/unmapped ingredients (sugar, margarine) simply don't influence ranking. In practice it engages mostly on whole-food swaps (broccoli→cauliflower ≈ 0.85, beef→pork ≈ 0.23), not additives.

#### reduce-quantity mode (`_generate_reduce_quantity_suggestions`)

When no swap improves a recipe (common for energy/fat-heavy dishes with no healthier substitute), this mode recommends *using less* instead. It targets the same worst negative nutrient, ranks offenders by contribution (no substitution-path requirement — any ingredient can be reduced), and for the top offender tries retained fractions `0.7 / 0.5 / 0.3` (smallest cut first), recommending the **first** reduction that strictly improves the grade. `0.3` (keep 30%) is the floor. Suggestions carry `action: "reduce"` with `reduced_from_weight_g` / `reduced_to_weight_g` / `reduction_pct`. No LLM, no FlavorDB.

#### sustainability nutri-guard

In `_evaluate_sustainability_candidate`, after confirming a CO2e reduction, the swap is simulated nutritionally and **rejected if it worsens the Nutri-Score grade** — so a low-carbon swap can't silently tank health. If the candidate has no composition match (nutrition can't be judged), it's kept and the LLM judge remains the backstop.

#### Candidate-quality layering

The three quality layers stack, cheapest first:
1. **Food-class guard** — always-on, deterministic; kills cross-category nonsense from both MISKG and FoodOn.
2. **FlavorDB tiebreak** — orders the in-class survivors by flavor closeness.
3. **LLM judge** (`use_llm=true`) — catches residual *within-class* oddities the guard can't separate (e.g. `bananas → vanilla extract`, `bicarbonate of soda → saltine crackers`), at ~7–15s latency.

### Data shape workaround

The dominant `recompute_2026-05-11` pipeline persists `nutrition_profiling_details` with only `fat_g` / `carbs_g` / `protein_g` per ingredient — not `saturated_fat_g` / `sugar_g` / `sodium_mg` / `fibre_g` / `energy_kcal`. To get the breakdown-relevant per-ingredient contributions without re-profiling 57k rows:

- `_recompute_ingredient_details()` calls `nutritional_tool_chroma` once per request using the stored `(name, weight_g)` pairs.
- Adds ~1–2 s on first request per recipe (Chroma loads its embedding model on first use). Subsequent requests are fast.

The persisted `name` field is the FCT canonical row name (long form), but Neo4j Ingredient nodes use everyday names. `resolve_graph_name()` in `neo4j_queries.py` bridges this via, in order:
1. Verbatim case-insensitive match against the FCT name and any hints.
2. Token-based scoring: among Ingredient nodes whose every token appears in the FCT token set (with plural↔singular crossover) AND which actually have substitution paths, prefer the most-specific (most tokens), then tightest cluster in the FCT name, then shortest name.

### LLM judge

Sits on top of the deterministic candidate set when `use_llm=true`. The judge **cannot invent substitutes** — it only filters and reranks from the candidates `service.py` already produced. Any candidate name in the LLM response that doesn't exactly match the input set is silently dropped so the simulation math is never poisoned by hallucinated ingredients.

Provider-agnostic via the OpenAI-compatible Chat Completions API — works against vLLM, Groq, or any OpenAI-compatible inference server unchanged.

Config (read at call time, env vars):

| Var | Default (vllm) | Default (groq) |
|---|---|---|
| `ADAPT_LLM_SOURCE` | `vllm` | `groq` |
| `ADAPT_LLM_BASE_URL` | `http://localhost:8005/v1` | `https://api.groq.com/openai/v1` |
| `ADAPT_LLM_MODEL` | `qwen3-32b` | `llama-3.1-8b-instant` |
| `ADAPT_LLM_API_KEY` | `none` | `$GROQ_API_KEY` |
| `ADAPT_LLM_TIMEOUT` | `90` (seconds) | `90` |

Fail-open: any LLM error (network timeout, malformed JSON, empty ranking) → caller falls back to the deterministic ranking and the endpoint never 422s a request that the deterministic pipeline could have answered.

Qwen3-specific: the judge passes `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` so Qwen3 doesn't waste the token budget on `<think>` blocks before the JSON. Harmless for non-Qwen models that ignore the flag.

Observed behaviour on the audit recipes (qwen3-32b on local vLLM):
- **PB Fudge Frosting**: rejected `valencia oranges` and `peanut flour` (wheat allergen); kept `peanut powder` with a recipe-aware justification. ~7s.
- **Lemon Butter for Steak**: rejected `seasoning salt` (excessive sodium, lacks creamy texture); surfaced `diced tomatoes with sweet onions` instead. ~12s.
- **Sweet & Sour Dressing**: rejected `skim milk` and `natural yoghurt` as sugar substitutes ("doesn't function as a sugar substitute in a dressing"); kept `frozen blueberries`. ~10s.

### Layout

```
adaptation/
├── __init__.py
├── app.py              # Standalone FastAPI app entry (port 8101, dev fallback)
├── router.py           # APIRouter mounting the two endpoints
├── schemas.py          # Pydantic request/response models
├── service.py          # Steps 1–5 orchestration + optional LLM-judge integration
├── llm_judge.py        # OpenAI-compatible LLM judge (vLLM / Groq) — filter + rerank
└── neo4j_queries.py    # MISKG/FoodOn/allergen Cypher + resolve_graph_name()
```

### Running locally

Run standalone service on port 8101:

```bash
PYTHONPATH=src uvicorn recipe_wrangler.services.adaptation.app:app --port 8101
```

Swagger UI: `http://localhost:8101/docs`

Example curl:
```bash
curl -s -w "\nHTTP %{http_code}\n" -X POST \
  http://127.0.0.1:8101/api/v1/recipes/f0e8ca9199/adapt/suggestions \
  -H "Content-Type: application/json" \
  -d '{"region":"IE","mode":"nutrition","max_swaps":3}'
```

### Known limitations

24-recipe audit (region=IE, balanced across all four negative nutrients). Two iterations recorded so the impact of each fix is traceable:

| Metric | v1 (initial) | v2 (resolver + grade gate) |
|---|---|---|
| Returned suggestions | 13/24 (54%) | **14/24 (58%)** |
| Top suggestion improved grade | 7 | **14 (gate enforces it)** |
| Top suggestion left grade unchanged | 5 | **0** |
| Top suggestion made grade worse | 1 | **0** |
| Top suggestion via curated MISKG | 11 | 5 |
| Top suggestion via FoodOn sibling | 2 | 9 |
| Sodium-targeted recipes succeeded | 0/6 | **5/6** |

#### Fixed in v2

- **Token-based resolver** (`resolve_graph_name`). FCT names now map to the most-specific Neo4j Ingredient whose tokens all appear in the FCT name and which actually has substitution paths. Includes plural↔singular handling so `"sugars, powdered"` lands on `powdered sugar`, not `powdered`. Killed the `oil → "oil" → sea salt` and `butter, without salt → "butter" → chocolate chips` failure modes by anchoring on the right node up front.
- **Strict grade-preservation gate** in `_evaluate_candidate`. A candidate is dropped unless `simulated_grade < current_grade` (strictly better letter). Killed all the "saved 10 points but grade unchanged" cases.
- **Sodium adaptation now works.** `table salt`'s FoodOn class has salt-free siblings (`McCormick's salt-free all-purpose` and similar), so once the resolver lands on `table salt` instead of failing to resolve, useful suggestions surface.

#### What v3 caught (LLM judge, opt-in via `use_llm=true`)

The grade gate didn't stop culinary-nonsense swaps that happened to improve the nutri-score (`peanut butter → valencia oranges`, `butter → seasoning salt`, `granulated sugar → skim milk`). The v3 LLM judge rejects these explicitly:
- `valencia oranges` → "would drastically alter the flavor and texture, changing the dish's identity"
- `seasoning salt` → "adds excessive sodium and lacks the creamy texture and flavor of butter"
- `skim milk` → "doesn't function as a sugar substitute in a dressing"

Adds ~7–15 s latency per request on `qwen3-32b` via local vLLM; sub-second on Groq with a smaller model.

#### What's still off

- **MISKG/FoodOn cross-category nonsense is now caught deterministically** by the v5 food-class guard (`oil → buttermilk`, `butter → chocolate chips`, `sugar → anise oil` all dropped without an LLM). What remains is **within-class oddity** (`bananas → vanilla extract`, `bicarbonate of soda → saltine crackers`) — same food_class, so the guard can't separate them; `use_llm=true` is still needed for that nuance. A full MISKG/FoodOn edge audit would fix it at the data layer but is deferred.
- **First request is slow**: ~5–10 s while Chroma loads. Subsequent ~1–2 s for `use_llm=false`, ~10–15 s for `use_llm=true`. We recompute per-ingredient nutrition every call because the persisted detail rows don't include the breakdown nutrients; LLM cache would amortise this.
- **`max_swaps > 1` returns parallel top-N suggestions**, not sequentially-applied chains. Sequential adaptation deferred.
- **FlavorDB coverage is uneven** — the tiebreak only engages on whole-food swaps with rich compound mappings; sparse/unmapped ingredients (additives, seasonings, many branded names) yield `flavor_similarity: null` and fall back to category distance.
- **Sustainability mode requires a 10% minimum CF reduction** for a candidate to be considered (`SUSTAINABILITY_MIN_REDUCTION_PCT` in `service.py`) — filters out trivial within-category swaps (e.g. olive→canola oil) that wouldn't be worth recommending.

#### Roadmap

Done in v2:
- ✅ Token-based resolver
- ✅ Grade-preservation gate

Done in v3:
- ✅ LLM-judge layer (vLLM/Groq, opt-in via `use_llm`)

Done in v4:
- ✅ Sustainability mode (`mode: "sustainability"`) — targets the highest-CO2e ingredient, ranks substitutes by absolute CO2e reduction per serving; LLM judge handles culinary fit. `/simulate` now also reports CO2e impact regardless of mode.

Done in v5:
- ✅ **Food-class guard** (`_food_class_compatible`) — always-on deterministic filter; drops cross-category swaps from both MISKG and FoodOn. Closes the energy-target 422 gap together with reduce-quantity.
- ✅ **FlavorDB tiebreak** (`flavor_similarity`) — sparsity-gated flavor-compound Jaccard; tiebreak-only over the sane candidate set, never a generator.
- ✅ **Reduce-quantity mode** (`mode: "reduce_quantity"`) — recommends the smallest weight cut (keep 70/50/30%) of the worst offender that improves the grade, for recipes with no useful substitute.
- ✅ **Sustainability nutri-guard** — a CO2e-cutting swap is rejected if it worsens the Nutri-Score grade.

Next:
1. **Cache the LLM judge** by `(recipe_id, region, candidate_set_hash)` — same inputs → no second call. The candidate set is deterministic given the graph state.
2. **Multi-nutrient penalty in the deterministic ranking** — penalise candidates that drag any non-target nutrient backwards. Refines what gets handed to the LLM.
3. **MISKG / FoodOn data audit** — flag and drop bad curated edges and over-broad class assignments, to fix the residual within-class oddities without needing `use_llm=true`.
4. **Companion swaps** — only if the LLM doesn't naturally surface combos.
5. **Time/budget constraints** — the GA's outstanding D3.2 requirement; not yet started.

### Reuses from main codebase (existing helpers, not modified)

- `utils.nutrition_postgres.fetch_recipe_profiling_trace_by_id` — profile fetch
- `tools.nutritional_calculator.nutritional_tool_chroma` — per-ingredient nutrient lookup
- `tools.sustainability_calculator.best_sustainability_match` — per-ingredient CO2e lookup
- `utils.nutri_score.compute_nutri_score_breakdown_from_values` — Nutri-Score re-scoring
- `utils.neo4j_utils.run_query` — Cypher executor

### Repo-level changes to support this service

The only tracked changes are:
- `/src/recipe_wrangler/services/` added to `.gitignore`.
- `api/main.py` mounts the tracked adaptation router directly, so fresh clones and CI expose the same API surface.

`routers/recipes.py`, `repositories/`, and `tools/` are untouched.
