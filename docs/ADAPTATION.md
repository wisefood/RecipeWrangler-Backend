# Recipe adaptation

**Purpose:** what adaptation does today, how each mode works, the API, the rules, the limits and where the code is. Dated progress is in [STATUS.md](STATUS.md), open tasks in [TODO.md](TODO.md), numbers and example recipes in [evaluation/adaptation](../evaluation/adaptation/README.md), scoring methods in [METHODS.md](../evaluation/METHODS.md). Matching, cost prices and analysis are in their own guides ([MATCHING.md](MATCHING.md), [COST.md](COST.md), [ANALYSIS.md](ANALYSIS.md)).

State: 23 November 2026. All four modes answer through one route, `GET /api/v1/recipes/{id}/adaptations?mode=&region=`, with optional `allergens` and `diet`. Development set: the 537 Living Lab recipes (Curated Irish, Hungarian, Slovenian), each in its native regional profile (IE, HU, SI) plus EU. Draft PR #13 (`adaptation-and-matching-fixes`) is open, not merged or deployed.

## The story in one page

Adaptation means: take a recipe, find the one change that helps most on one facet, check that the change is safe for this user, and answer with the changed recipe and a measured benefit. The four facets differ only in what they measure and what they change.

| Facet | What it asks | What changes in the recipe | How the benefit is measured |
|---|---|---|---|
| Nutrition | Which ingredient carries the Nutri-Score penalty? | Cut an amount, swap a food, add a vegetable | Nutri-Score letter and points per serving |
| Sustainability | Which ingredient carries most CO2e? | Swap a food | kg CO2e per serving (AGRIBALYSE) |
| Cost | Which ingredient carries most of the price? | Swap a food for a cheaper one of the same kind | EUR per serving (reference prices) |
| Time | Which step waits on a passive step (simmer, boil, bake)? | Start an independent prep step during the wait | Minutes saved, a model estimate |

What every mode shares:
1. **One answer shape.** `adaptation_row.py` returns a `recipe_adaptations` row (`AdaptationResponse`): `status` (`adapted`, `no_suggestion`, `not_scored`, `not_suitable_for_user`), `reason_code` plus a sentence, `original`, `adapted`, `swap`, `benefit`, `provenance`. Only the engine behind it differs (`MODES` in `adaptation_row.py`).
2. **Stored row first, live engine second.** Nutrition and sustainability have stored rows (Postgres `recipe_adaptations`); cost and time have none and always run live. A restricted request recomputes live for sustainability, cost and time; for nutrition it checks the stored swap, because the live nutrition engine takes 18 to 60 s.
3. **Restrictions.** A recipe that already breaks the user's allergens or diet gives `not_suitable_for_user`. A swap that adds an allergen or breaks the diet is refused (`alternatives_add_allergen`).
4. **Code decides, a model only words.** Every candidate, gate and number is code. A model, when switched on, only chooses between candidates the code already approved (nutrition, cost) or estimates a duration that code then clips (time), and code validates what it writes.

| Facet | Stored rows | Live engine (current time) | Model step | Switch |
|---|---|---|---|---|
| Nutrition | 537 (IE 308, HU 138, SI 91) | 18 to 60 s | choice and rewrite (stored rows) | none in the live route |
| Sustainability | 2,148 (537 by 4 regions) | a few seconds, code-written text | none | none |
| Cost | none | 0.3 to 2 s | choice and rewrite | `COST_ADAPT_LLM=true` |
| Time | none | under 1 s, code only | minutes-saved estimate | `TIME_ADAPT_LLM=true` |

Model provider for cost and time: `ADAPT_LLM_SOURCE` (`openrouter` in production, `vllm` locally; model and address `ADAPT_LLM_MODEL`, `ADAPT_LLM_BASE_URL`). With the switch off the row is complete without a model.

## What changed (for someone who saw the first PR description)

| Before | Now |
|---|---|
| Separate routes per use: `POST /adapt/small-gain` (nutrition), `/adapt/suggestions`, `/adapt/simulate`, `/adapt/enhance` | One public adaptation route, `GET /{id}/adaptations?mode=&region=`. `/adapt/suggestions`, `/adapt/simulate` and `/adapt/small-gain` are no longer routes; `/adapt/enhance` stays. The old engines stay as internal code. |
| Sustainability served stored rows only; cost came only through `/adapt/enhance` (0 results); time not exposed | Sustainability, cost and time each have a live engine (`sustainability_live.py`, `cost_live.py`, `time_live.py`) in the same row shape |
| No user restrictions | Optional `allergens` and `diet` on every mode: the recipe's own lines are checked first, then the swap, with the graph's stored allergen and diet tags as a safety net |
| Live nutrition took 190 to 235 s | 18 to 60 s: a loop evaluated an expensive call once per metric (fixed), and `matching_compositions_identity` is a new index (alembic `20261120_0012`) |
| Sustainability live check failed on 6 of 537 recipes (rule labels, parser version) | Fixed in `comparison_store._without_rules`; 537 of 537 load |
| Graph allergen and diet tags were mixed old versions | Re-run on the live graph on 20 Nov (backup in `backups/graph_tags_20261120/`) |

## Connecting the stored-rows table (read this before deploying)

**Which modes have stored rows.** Nutrition and sustainability. Cost and time have no stored rows and always run live. For nutrition the stored rows are what keeps the answer fast: without one the live engine takes 18 to 60 s per recipe, so a user without allergens or diet gets the stored row in about 0.1 s. Sustainability also has stored rows (2,148), but its live engine answers in a few seconds, so a missing sustainability row is only slower.

**Where.** Table `recipe_adaptations` in the PostgreSQL database `nutrients`, the same database as `nutrients-recipe-profiles` and `matching_compositions`. The API connects with `NUTRITION_HOST`, `NUTRITION_PORT`, `NUTRITION_DB`, `NUTRITION_USER`, `NUTRITION_PASSWORD` (and optionally `NUTRITION_SCHEMA`, default `public`), through `utils/nutrition_postgres.get_engine`. No new environment variable is needed. Locally: container `wisefood-postgres`, host port 5435.

**How to get the table onto the platform database (either way).**
1. `alembic upgrade head`: creates `recipe_adaptations` (0010), the generated column `is_adapted` (0011) and the `matching_compositions_identity` index (0012). Then load the rows with `scripts/nutrition/load_adaptations.py --apply` and `scripts/sustainability/load_adaptations.py` (dry run by default; they upsert in one transaction and can be repeated).
2. Or restore `dumps/sent/2026-11-20/nutrients.dump` (27 tables, alembic head `20261120_0012`): it already contains the table, the rows and the index.

**Table.** Primary key `(recipe_id, mode, region)`. Columns: `recipe_id`, `mode` (`nutrition`, `sustainability`, `cost`), `region` (`IE`, `HU`, `SI`, `EU`), `version`, `status` (`adapted`, `no_suggestion`, `not_scored`), `is_adapted` (generated from `status`), `reason_code`, and jsonb `original`, `adapted`, `swap`, `benefit`, `provenance`. `mode` is limited to those three values by a check constraint, which is fine because time is never stored; if time rows are ever stored, the constraint needs a migration.

**What is in it.**

| Mode | Rows | Regions |
|---|---|---|
| nutrition | 537 (488 adapted, 49 `no_suggestion`) | the recipe's native region only: IE 308, HU 138, SI 91. `region=EU` has no nutrition row |
| sustainability | 2,148 | all 537 recipes in IE, HU, SI and EU |
| cost, time | none | live engine on every call |

**How the API uses it** (`adaptation_row.py`). Look up `(recipe_id, mode, region)`.
- No `allergens` and no `diet`: return the stored row as it is (`source` is the stored version). When there is none, run the mode's live engine (nutrition 18 to 60 s; sustainability, cost and time a few seconds or less). A `no_suggestion` row returns its sentence as `reason`.
- With `allergens` or `diet`: nutrition checks the stored swap against the restriction (about 0.2 s, no live search); sustainability, cost and time recompute live. A stored row is never returned unchecked to a restricted user.

**Check after loading.** `select mode, region, status, count(*) from recipe_adaptations group by 1,2,3 order by 1,2,3;` should show nutrition IE 308, HU 138, SI 91 in total and sustainability 537 per region.

**Known staleness.** The stored rows were built before the 23 Nov Nutri-Score update. 6 Living Lab nutrition rows (Irish) changed letter and need regenerating (`analysis/nutri_fill_2026-11-21/living_lab_letter_changes.json`, local). The platform's database needs the rows reloaded after that regeneration.

## Modes

| Facet | Engine | Public route | State |
|---|---|---|---|
| Nutrition | Rule-based: amount cuts, curated food swaps, extra vegetables or served-with additions (`nutrition_adaptation.py`, `nutrition_swap_rules.py`), then an LLM stage (`nutrition_adaptation_llm.py`) | `GET /adaptations?mode=nutrition` (stored row, restrictions, live engine) | Frozen 19 Nov: 293 of 537 adapted, 198 enriched, 46 with nothing; 488 stored rows 20 Nov |
| Sustainability (CO2e) | Same mechanisms on CO2e (`sustainability_adaptation.py`, `sustainability_swap_rules.py`, live: `sustainability_live.py`) | `GET /adaptations?mode=sustainability` | 105 adapted of 537; reviewed AGRIBALYSE mapping, section B (fish) not approved |
| Cost | Top cost contributor, same-category cheaper substitute from the graph, no nutrition re-match (`cost_live.py`, `cost_llm.py`, `service.py`) | `GET /adaptations?mode=cost` | 13 of 100 Irish recipes adapted; swap quality unreviewed |
| Time | Passive-step overlap found by code, start-only rewrite (`time_live.py`, `time_llm.py`, on `time_understanding.py` and `time_planning.py`) | `GET /adaptations?mode=time` | 7 of 537 adapted; first working version, rows unreviewed |

`EnhancementRequest.mode` (the older `/adapt/enhance` pipeline) accepts `nutrition`, `sustainability` and `cost`; time is not part of it.

## API

All routes are in `services/adaptation/router.py`, mounted in `api/main.py`.

| Route | Use |
|---|---|
| `GET /{id}/adaptations?mode=&region=` | Stored adaptation for one mode and region, or its reason code and sentence when there is none (404 when no row). Reads the `recipe_adaptations` table. |
| `POST /{id}/adapt/enhance` | Controlled pipeline (below): unsaved draft or explicit evidence gaps. Body: `region`, `mode`, `goal_nutrients`, `constraints`. |
| (removed 20 Nov) | `POST /{id}/adapt/suggestions` and `POST /{id}/adapt/simulate` are no longer routes; the legacy engine in `service.py` stays internal for the cost pipeline and its tests. |

**One call, one shape.** `GET /{id}/adaptations?mode=&region=` is the only public adaptation route besides `/adapt/enhance`. `adaptation_row.py` returns a `recipe_adaptations`-shaped row (`AdaptationResponse`) for every mode: the stored row, or the mode's engine when nothing is stored (nutrition: the live engine, about 3 minutes, code-written card, `source: live`; sustainability: the live engine, a few seconds; cost and time: the live engine only, no stored rows). Only the engines differ per mode.

**User restrictions (allergens and diet).** One call for every mode: `GET /{id}/adaptations?mode=&region=` with optional `allergens` (repeated) and `diet` (`vegan`, `vegetarian`, `pescatarian`). Nutrition: the stored row is checked, no live search (about 0.2 s). The recipe's own lines are checked first (`not_suitable_for_user`, `reason_code` `recipe_breaks_user_restriction`, `offending`), then the swapped-in foods with the same gate as the live engine (`restriction_problems`: lexical allergen and diet detection plus the graph's stored ingredient tags); a swap that fails gives `no_suggestion` with `alternatives_add_allergen` and the offending foods, no alternative is searched (the live search takes minutes). A stored `no_suggestion` row has no saved lines, so it is returned as it is with `applied_restrictions`. Sustainability with restrictions is computed live (`sustainability_live.py`, `source: "live"`, title and steps not rewritten). Cost returns 422. The former `POST /adapt/small-gain` is no longer a route: `router.nutrition_adaptation` and `nutrition_adaptation.adapt_active_recipe` remain as the internal live engine (2 to 4 minutes per recipe, 1,500 composition queries) used by the tests and for any future live fallback.

**Stored results.** With no `allergens` or `diet` sent, `GET /adaptations` reads this table in the PostgreSQL database `nutrients` (the same one as `nutrients-recipe-profiles`; env `NUTRITION_HOST`, `NUTRITION_PORT`, `NUTRITION_DB`; local container `wisefood-postgres`, host port 5435). Table `recipe_adaptations`, primary key `(recipe_id, mode, region)`, `status` `adapted`, `no_suggestion` or `not_scored`, a `reason_code`, and a generated `is_adapted` flag (alembic 0010 and 0011). Loaded by `scripts/sustainability/load_adaptations.py`. Currently 2,148 sustainability rows (537 recipes by 4 regions). Nutrition rows (537, native region only: IE 308, HU 138, SI 91; 488 adapted incl. 205 code-card, 49 `no_suggestion`) are loaded by `scripts/nutrition/load_adaptations.py` from the frozen results; no EU row exists for nutrition, so `region=EU` returns 404. For `no_suggestion` rows the sentence is in `provenance.reason`, returned as `reason`.

## Nutrition

1. **Diagnosis** (`diagnosis.py`): which Nutri-Score components carry the negative points and what the recipe lacks. Report only.
2. **Candidates:** swaps and cuts only on the ingredients that carry those points. Curated rows first (low-sodium salt, canned to cooked pulses, skinless poultry, tuna in water, poultry sausages, lean ham, reduced-sugar ketchup and jam, feta to cottage cheese, pastry to filo), then a descriptor search over released composition records, the regional table first and then EU.
3. **Role filter** (`food_roles.py`, shared by every facet): the replacement does the same job in the dish. No prepared dishes, drinks or baby food as targets, no produce for produce, no compound records.
4. **Gates:** no added allergen, no vegan, vegetarian or pescatarian break ("possible" counts as added); no grade drop; a recipe that stays at A needs a 10% gain; no same-weight swap from bone-in cuts; minced food stays minced.
5. **Amount cuts:** oil and butter may be cut by a quarter; salt gets low-sodium salt plus a quarter less; no invisible cuts (salt, oil, cheese, cream and cured meat are not trimmed by small amounts).
6. **Protected foods:** never swapped or cut: eggs, whole fruit, oily fish, whole nuts and seeds, avocado. Poultry swaps only with meat. No olive-oil advice.
7. **Additions** (a served-with item) are judged per serving, carry no Nutri-Score grade and are never counted as a grade gain.
8. **LLM stage** (`nutrition_adaptation_llm.py`, hosted qwen3-32b): chooses between the surviving candidates, then writes one structured rewrite of title, steps and ingredient lines. A word-level diff validator allows changes only on the changed foods; code writes the notes and the `benefit` block (facet, tip, measured metric). Failed or withdrawn rewrites keep the code-written card. `hosted_budget.py` makes paid transport fail closed. Local generative LLMs (vLLM) are allowed since 20 Nov within the AGENTS.md resource limits (GPU at most 40% of compute and VRAM, preflight first); earlier one-off exceptions are recorded in STATUS.

The scorer is the original Nutri-Score algorithm (sodium, saturated fat in grams, protein rule at 11 points, no cheese exception), not the 2023 update.

## Sustainability

Baseline and candidates use CO2e: AGRIBALYSE factors first, CONCITO only as a fallback for totals; the CIQUAL code join first, then reviewed name rules (audited: 1.4% of CO2e questionable). An ingredient is a target only when it carries at least 20% of the recipe's CO2e. Candidates: the curated table (`sustainability_swap_rules.py`) plus related-food search. Nutrition is not judged (it is a separate facet); the allergen and diet gate and the protected-foods list stay. Owner rules: butter to margarine only, no grain swaps, no red meat to lentils, no feta to cottage cheese, cod to hake on hold, title-food swaps allowed and flagged, cheese-type swaps allowed. A recipe with no suggestion gets a reason code and sentence (`sustainability_reasons.py`).

## Cost

**Live route (20 Nov, `cost_live.py`).** `GET /adaptations?mode=cost` runs the older service engine (`service._generate_cost_suggestions`: top cost contributor, same-category cheaper substitute from the graph) on the stored (name, weight) pairs, with no nutrition re-match and no nutri-guard. The changed line reads `original line  ->  use X`; the tip is one code-written sentence with the saving per serving (reference prices, `provenance.flags.indicative`). With `COST_ADAPT_LLM=true` (`cost_llm.py`) a model first chooses the best of up to 3 candidates (a processed product or a dish-changing swap is rejected) and then rewrites the line, title and steps under the nutrition validators; any failure keeps the code card and records `provenance.llm.status`. Reason codes: `no_prices`, `no_substitute_found`, `no_cheaper_option`, `alternatives_add_allergen`, `recipe_breaks_user_restriction`, `no_active_profile`. The thresholds below apply to both routes. The old `/adapt/enhance` cost mode (every ingredient priced, exact matches) still yields 0 results.

Cost uses the controlled pipeline with price evidence from [COST.md](COST.md). A suggestion needs a relative price improvement of at least 10% on the swapped ingredient (`COST_MIN_REDUCTION_PCT`), an ingredient that carries at least 10% of the recipe cost (`COST_MIN_OFFENDER_SHARE`), a saving of at least 5% of the recipe cost (`COST_MIN_RECIPE_SAVING_SHARE`) and at least EUR 0.05 per serving (`COST_MIN_SAVING_PER_SERVING_EUR`), with the shared role filter (all in `service.py`). Price evidence is detailed or base-product; alias and base matches are accepted as labelled estimates, FoodOn group medians are excluded, and both sides must resolve to different products. Two cuts that map to the same base-price proxy cannot show a cut-specific saving. Since 19 November the nutrition guards (grade regression, per-nutrient regression) are switched off for cost (`COST_NUTRITION_GUARDS = False` in `objective_approval.py`) so the facets can be measured separately; nutrient deltas are still reported. Set the flag back to `True` when nutrition and cost are combined.

## Time

**Idea.** Many recipes wait on a passive step (simmer, boil, bake, rise, rest) and then do prep work that could have started during the wait. The adaptation tells the cook to start one such independent step during the wait. It changes the order of work, not the ingredients, so allergens and diet cannot change the result (a recipe that already breaks them is still `not_suitable_for_user`).

**Steps** (`time_live.py`, code only unless the model switch is on):
1. **Understand the steps** (`time_understanding.py:deterministic_understanding`): split the instructions into fragments, label each passive or active from verbs and "N minutes" text, and link each fragment to the earlier ones it depends on (shared food words, reference words, same step). Over-linking only removes candidates.
2. **Find overlaps** (`time_planning.py:discover_overlaps`): an anchor is a passive fragment with a stated duration (simmer, boil, bake, roast, rise, soak, rest). A later step is a candidate if nothing in it depends on the anchor. Recipes that already say "while" or "meanwhile" are skipped (`existing_parallelism_requires_baseline_analysis`), as are recipes over 30 steps.
3. **Tighten candidates** (`time_live.py`): the moved step must start with a prep verb (cut, peel, slice, mix, combine, whisk, sieve and similar), must not refer to cooked output, the oven or the pan (`DEPENDS`), must not be a serving, storing, cooling or note step (`NOT_WORK`), must not itself be passive, must not be the last step, and the wait must be at least 5 minutes. The two word lists came from reading the 48 first-pass candidates of 23 Nov; they will miss other recipes.
4. **Write the row:** the moved step gets the prefix "Once the preceding roasting stage has started, begin this step: ". Nothing else in the recipe changes. The earlier food-naming clause ("While the potatoes roast") was dropped because it guessed the food wrongly.
5. **Minutes saved** (`time_llm.py`, only with `TIME_ADAPT_LLM=true`): the recipe text rarely states how long the moved step takes (0 of 48 candidates had it), so code cannot measure the saving. One model call returns a range for the moved step; code accepts it only if 0 < low <= high <= 120 and clips both ends to the wait window's lower bound (the work cannot save more than the wait it runs inside). Without the model, or when it fails, the row stays start-only with no minutes.

**What the row says.** `provenance.flags`: `unreviewed` and `start_only` always, `indicative` when minutes are shown; `provenance.timing` holds the wait window and the work estimate; `provenance.llm.status` is `ok` or `estimate_failed`. The tip says no saving is verified, or "it saves about N to M min (model estimate, indicative)".

**Result (23 Nov).** 537 Living Lab recipes: 7 adapted, 447 no overlap, 69 skipped for existing parallelism, 14 over the size bound. Examples: steaming sprouts for 20 min, so cut the bacon; roasting for 20 min, so slice the chicken. A local-model run on the 48 first-pass candidates gave 48 valid ranges in 41 s (Qwen3-32B-AWQ, one-off exception in AGENTS.md); the filters were tightened afterwards and the estimates were not re-run on the final 7.

**Not built.** `time_schedule.py` (a bounded scheduler for several dishes in one meal) and the reviewed-overlap rewrite (`rewrite_reviewed_overlap`, needs a person's resource and independence review) exist and are tested but are not behind the route. A person's review of the 7 rows is the next step, and the filters are the first thing to loosen after it.

## Controlled enhancement pipeline (`/adapt/enhance`)

1. Pin the active recipe, profile, run and composition release; bind occurrence IDs and the recipe text revision.
2. Pick one objective and rank the ingredient contributors; a whole-recipe Nutri-Score factor is not an ingredient-owned number of points.
3. Try up to three ingredient targets: same-identity part variants, graph substitutions, same-product nutritional variants.
4. Resolve real released compositions and check identity, facets, state and basis, quantities, nutrient definitions, completeness and dietary and allergen constraints.
5. Apply the numerical policy (`objective_approval.py`: meaningful per-serving reduction within the same Nutri-Score letter, eight non-regression nutrient guards, rounding-level tolerance); keep at most ten candidates for the contextual step.
6. Use the model for ingredient roles and batch culinary suitability, retaining inferred status and reasons.
7. Recompute the objective deterministically and select an eligible candidate.
8. Allow bounded instruction patches, validate, recheck the baseline, and assemble an unsaved card with a decision audit.

A successful run uses at most three model requests (roles, candidate assessment, instruction editing); a screening failure uses none. The model cannot invent composition IDs, nutrient values, equivalent grams, yields or arbitrary instructions.

## Evidence and limits

- 74% of composition records state no preparation state. An unstated state is missing evidence except for "as sold" families (dairy, oils, sweeteners, and since 16 Nov produce, nuts, herbs, condiments as a test). Grains, pulses and meat need a stated state, so many search candidates stay `needs_evidence`. The fix is annotating the records, a release-level change.
- With the curated table hidden, search finds the same target in 64 of 130 recipes (49%). The table stays for cross-type swaps (butter to oil, pastry to filo), state changes and amount factors.
- 33 saved baselines differ from today's pipeline and are left unmarked; baselines miss cooking water and stock and do not count mushrooms and onions as vegetables.
- Cooking-method edits and quantity changes by cooking (yield, retention, cooking media) are not executable in the enhancement pipeline.
- Thresholds are provisional engineering criteria; calibration and deployment require the platform check.
- Product variants must keep identity and product form; no-added-sugar is not zero sugar.

## Code map

All modules are under `src/recipe_wrangler/services/adaptation/`.

- **API and models:** `router.py`, `schemas.py`, `models.py`, `app.py` (standalone dev app), `service.py` (legacy routes and shared cost and sustainability policy), `recipe_response.py`, `enhancement_response.py`, `preview.py`.
- **Nutrition:** `nutrition_adaptation.py`, `nutrition_swap_rules.py`, `nutrition_adaptation_llm.py`, `nutrition_benefit.py`, `nutrition_reasons.py`, `diagnosis.py`, `food_roles.py`.
- **Sustainability:** `sustainability_adaptation.py`, `sustainability_swap_rules.py`, `sustainability_reasons.py`.
- **Time:** `time_understanding.py`, `time_planning.py`, `time_schedule.py`, `execution_facts.py`, `cooking_parameters.py`.
- **Controlled pipeline:** `enhancement.py`, `enhancement_screening.py`, `enhancement_pipeline.py`, `candidate_selection.py`, `production_candidates.py`, `nutritional_variants.py`, `comparison.py`, `comparison_store.py`, `quantity_declarations.py`, `occurrence_evidence.py`, `contextual_assessment.py`, `instruction_editor.py`, `objective_approval.py`, `impact_objectives.py`.
- **Support:** `neo4j_queries.py` (graph candidates), `llm_judge.py` (legacy substitution sanity check), `hosted_budget.py` (paid-call guard).
- **Stored results:** `repositories/recipe_adaptations.py`.
