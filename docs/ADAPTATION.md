# Recipe adaptation

**Purpose:** what adaptation does today, how each mode works, the API, the rules, the limits and where the code is. Dated progress is in [STATUS.md](STATUS.md), open tasks in [TODO.md](TODO.md), numbers and example recipes in [evaluation/adaptation](../evaluation/adaptation/README.md), scoring methods in [METHODS.md](../evaluation/METHODS.md). Matching, cost prices and analysis are in their own guides ([MATCHING.md](MATCHING.md), [COST.md](COST.md), [ANALYSIS.md](ANALYSIS.md)).

State: 19 November 2026. All results are unsaved drafts; nothing adapted is deployed. Development set: the 537 Living Lab recipes (Curated Irish, Hungarian, Slovenian), each in its native regional profile (IE, HU, SI) plus EU.

## Modes

| Facet | Engine | Public route | State |
|---|---|---|---|
| Nutrition | Rule-based: amount cuts, curated food swaps, extra vegetables or served-with additions (`nutrition_adaptation.py`, `nutrition_swap_rules.py`), then an LLM stage (`nutrition_adaptation_llm.py`) | `POST /api/v1/recipes/{id}/adapt/small-gain` (deterministic part, no LLM) | Frozen 19 Nov: 293 of 537 adapted, 198 enriched, 46 with nothing |
| Sustainability (CO2e) | Same mechanisms on CO2e (`sustainability_adaptation.py`, `sustainability_swap_rules.py`) | Stored results via `GET /api/v1/recipes/{id}/adaptations` | About 105 adapted; reviewed AGRIBALYSE mapping, section B (fish) not approved |
| Cost | Controlled pipeline with the cost objective (`service.py`, `impact_objectives.py`) | `POST /api/v1/recipes/{id}/adapt/enhance` with `mode: cost` | 48 recipes with a suggestion |
| Time | Discovery of passive-step overlaps (`time_understanding.py`, `time_planning.py`, `time_schedule.py`) | None | 49 recipes with a candidate; review required |

`EnhancementRequest.mode` accepts `nutrition`, `sustainability` and `cost`. Time is not exposed.

## API

All routes are in `services/adaptation/router.py`, mounted in `api/main.py`.

| Route | Use |
|---|---|
| `GET /{id}/adaptations?mode=&region=` | Stored adaptation for one mode and region, or its reason code and sentence when there is none (404 when no row). Reads the `recipe_adaptations` table. |
| `POST /{id}/adapt/enhance` | Controlled pipeline (below): unsaved draft or explicit evidence gaps. Body: `region`, `mode`, `goal_nutrients`, `constraints`. |
| (removed 20 Nov) | `POST /{id}/adapt/suggestions` and `POST /{id}/adapt/simulate` are no longer routes; the legacy engine in `service.py` stays internal for the cost pipeline and its tests. |

**One call, one shape.** `GET /{id}/adaptations?mode=&region=` is the only public adaptation route besides `/adapt/enhance`. `adaptation_row.py` returns a `recipe_adaptations`-shaped row (`AdaptationResponse`) for every mode: the stored row, or the mode's engine when nothing is stored (nutrition: the live engine, about 3 minutes, code-written card, `source: live`; sustainability: the live engine, a few seconds; cost: none yet, 404). Only the engines differ per mode.

**User restrictions (allergens and diet).** One call for every mode: `GET /{id}/adaptations?mode=&region=` with optional `allergens` (repeated) and `diet` (`vegan`, `vegetarian`, `pescatarian`). Nutrition: the stored row is checked, no live search (about 0.2 s). The recipe's own lines are checked first (`not_suitable_for_user`, `reason_code` `recipe_breaks_user_restriction`, `offending`), then the swapped-in foods with the same gate as the live engine (`restriction_problems`: lexical allergen and diet detection plus the graph's stored ingredient tags); a swap that fails gives `no_suggestion` with `alternatives_add_allergen` and the offending foods, no alternative is searched (the live search takes minutes). A stored `no_suggestion` row has no saved lines, so it is returned as it is with `applied_restrictions`. Sustainability with restrictions is computed live (`sustainability_live.py`, `source: "live"`, title and steps not rewritten). Cost returns 422. The former `POST /adapt/small-gain` is no longer a route: `router.nutrition_adaptation` and `nutrition_adaptation.adapt_active_recipe` remain as the internal live engine (2 to 4 minutes per recipe, 1,500 composition queries) used by the tests and for any future live fallback.

**Stored results.** Table `recipe_adaptations`, primary key `(recipe_id, mode, region)`, `status` `adapted`, `no_suggestion` or `not_scored`, a `reason_code`, and a generated `is_adapted` flag (alembic 0010 and 0011). Loaded by `scripts/sustainability/load_adaptations.py`. Currently 2,148 sustainability rows (537 recipes by 4 regions). Nutrition rows (537, native region only: IE 308, HU 138, SI 91; 488 adapted incl. 205 code-card, 49 `no_suggestion`) are loaded by `scripts/nutrition/load_adaptations.py` from the frozen results; no EU row exists for nutrition, so `region=EU` returns 404. For `no_suggestion` rows the sentence is in `provenance.reason`, returned as `reason`.

## Nutrition

1. **Diagnosis** (`diagnosis.py`): which Nutri-Score components carry the negative points and what the recipe lacks. Report only.
2. **Candidates:** swaps and cuts only on the ingredients that carry those points. Curated rows first (low-sodium salt, canned to cooked pulses, skinless poultry, tuna in water, poultry sausages, lean ham, reduced-sugar ketchup and jam, feta to cottage cheese, pastry to filo), then a descriptor search over released composition records, the regional table first and then EU.
3. **Role filter** (`food_roles.py`, shared by every facet): the replacement does the same job in the dish. No prepared dishes, drinks or baby food as targets, no produce for produce, no compound records.
4. **Gates:** no added allergen, no vegan, vegetarian or pescatarian break ("possible" counts as added); no grade drop; a recipe that stays at A needs a 10% gain; no same-weight swap from bone-in cuts; minced food stays minced.
5. **Amount cuts:** oil and butter may be cut by a quarter; salt gets low-sodium salt plus a quarter less; no invisible cuts (salt, oil, cheese, cream and cured meat are not trimmed by small amounts).
6. **Protected foods:** never swapped or cut: eggs, whole fruit, oily fish, whole nuts and seeds, avocado. Poultry swaps only with meat. No olive-oil advice.
7. **Additions** (a served-with item) are judged per serving, carry no Nutri-Score grade and are never counted as a grade gain.
8. **LLM stage** (`nutrition_adaptation_llm.py`, hosted qwen3-32b): chooses between the surviving candidates, then writes one structured rewrite of title, steps and ingredient lines. A word-level diff validator allows changes only on the changed foods; code writes the notes and the `benefit` block (facet, tip, measured metric). Failed or withdrawn rewrites keep the code-written card. `hosted_budget.py` makes paid transport fail closed. Local generative LLMs are prohibited by project policy; one-off owner exceptions are recorded in STATUS.

The scorer is the original Nutri-Score algorithm (sodium, saturated fat in grams, protein rule at 11 points, no cheese exception), not the 2023 update.

## Sustainability

Baseline and candidates use CO2e: AGRIBALYSE factors first, CONCITO only as a fallback for totals; the CIQUAL code join first, then reviewed name rules (audited: 1.4% of CO2e questionable). An ingredient is a target only when it carries at least 20% of the recipe's CO2e. Candidates: the curated table (`sustainability_swap_rules.py`) plus related-food search. Nutrition is not judged (it is a separate facet); the allergen and diet gate and the protected-foods list stay. Owner rules: butter to margarine only, no grain swaps, no red meat to lentils, no feta to cottage cheese, cod to hake on hold, title-food swaps allowed and flagged, cheese-type swaps allowed. A recipe with no suggestion gets a reason code and sentence (`sustainability_reasons.py`).

## Cost

Cost uses the controlled pipeline with price evidence from [COST.md](COST.md). A suggestion needs a relative price improvement of at least 10% on the swapped ingredient (`COST_MIN_REDUCTION_PCT`), an ingredient that carries at least 10% of the recipe cost (`COST_MIN_OFFENDER_SHARE`), a saving of at least 5% of the recipe cost (`COST_MIN_RECIPE_SAVING_SHARE`) and at least EUR 0.05 per serving (`COST_MIN_SAVING_PER_SERVING_EUR`), with the shared role filter (all in `service.py`). Price evidence is detailed or base-product; alias and base matches are accepted as labelled estimates, FoodOn group medians are excluded, and both sides must resolve to different products. Two cuts that map to the same base-price proxy cannot show a cut-specific saving. Since 19 November the nutrition guards (grade regression, per-nutrient regression) are switched off for cost (`COST_NUTRITION_GUARDS = False` in `objective_approval.py`) so the facets can be measured separately; nutrient deltas are still reported. Set the flag back to `True` when nutrition and cost are combined.

## Time

`time_understanding.py` extracts hosted temporal inferences from the steps; `time_planning.py` discovers passive-step overlaps in code only (any passive anchor: simmer, boil, rise, bake) and checks one reviewed window; `time_schedule.py` is a bounded, non-preemptive scheduler under reviewed facts. No minutes are claimed without review; a start-only draft is opt-in. Prep time must not become total time, and summing step durations is not elapsed time. Instruction extraction research (RecipeBERT probe) is in [evaluation/instructions](../evaluation/instructions/README.md).

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
