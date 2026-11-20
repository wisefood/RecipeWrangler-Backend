# Status

Daily record of work done, newest first; every entry carries its date. Open tasks are in [TODO.md](TODO.md); how to run and deploy is in [OPERATIONS.md](OPERATIONS.md).

## 2026-11-20 (evening) — Nutrition adaptation: first working version; live engine 3x faster; sustainability live check fixed

**State.** Nutrition adaptation works through `GET /adaptations?mode=nutrition&region=` (stored row, restrictions, live engine when nothing is stored). Checked through a real local API (127.0.0.1:8002): stored row 0.08 s; `allergens=nuts` 0.95 s and `diet=vegetarian` 0.33 s keep the wholewheat pasta swap; `allergens=gluten` returns `not_suitable_for_user`; HU adapted and `no_suggestion` rows, `diet=keto` (422) and `mode=time&diet=vegan` (422). With no `allergens` or `diet` the API reads table `recipe_adaptations` in the PostgreSQL database `nutrients` (`NUTRITION_*` env, container `wisefood-postgres`, host port 5435): 537 nutrition rows (IE 308, HU 138, SI 91). The platform database needs the same rows loaded.

**Live engine (recipe has no stored row), current time.** Before: 193 to 235 s (192 s on `0000656901`). After the two changes below: 61 s on the pasta recipe `e9e84e5c` (engine called directly), 42 s on `0000656901` through the API on a cold process, 18 s on an immediate repeat of the same call (part of that is caches, so a new recipe will sit between 18 and 42 s). Results are identical: pasta recipe byte for byte against the pre-change output with a fixed `PYTHONHASHSEED`; `0000656901` API response identical to the 20 Nov response. This is the speed users would see for recipes outside the 537 until rows are precomputed for them; no swap routes were removed to get it.
1. Bug in `screen_production_swaps`: `dict.setdefault(position, expensive_call())` evaluated the call every time, so the graph and food-part lookups ran once per metric (12 graph lookups for 6 distinct ingredients). Now `_memo` runs each once per position (175 s to 121 s). Test `tests/test_screen_production_swaps_lookups.py` fails on the old code.
2. **Database change: index `matching_compositions_identity`** on `public.matching_compositions (release_id, collection, COALESCE(annotation->'text_annotation'->'facets'->>'base_identity', annotation->'text_annotation'->>'main_ingredient'))`, migration `20261120_0012` (alembic head; downgrade drops it). `identity_composition_records` filtered on that expression with no index, a full scan of 46,668 rows at about 0.32 s per search (296 searches, 46 distinct, 134 s of one call). With the index a search takes 2.8 ms (1.3 s in total per recipe). **The platform database needs this index** (`alembic upgrade head`, or the dump the owner sends). Applied to the local `nutrients` database on 5435 only.

**What is left of the live time (about 40 s of 61):** `_candidate_profile` matches about 140 distinct candidate names (0.2 to 0.3 s each, Elasticsearch plus candidate gate). Options not done: parallel matching (needs a thread-safety check), cheap class filters before matching, a cross-request cache, skipping the graph route (would lose swaps; owner does not want that), precomputed rows for all recipes. Known tie-break, not new: with a random hash seed one `alternatives` entry can come from either of two duplicate CIQUAL pasta records (3 kcal apart); the adaptation itself does not change.

**Sustainability live check.** `saved_selection_source_mismatch` came from anatomy rule labels (`scoped_anatomy_v1` vs `v2`) in the saved source facts, not from nutrition content. `comparison_store._without_rules` drops those labels from the digest comparison (real content differences still block). 40-recipe Living Lab sample (IE): failures 7 to 1, live and stored status agree on 39 of 39 compared; the remaining one (`1c7fcc55`, pork chops, `cofid:18-523`) is real drift (saved profile parser v9, release row v8) and needs a re-profile (write, owner approval, backup). Test `tests/test_comparison_store_rule_label.py`.

**Tests.** 1,572 adaptation, nutrition, comparison and matching-store tests pass.

**Next.** Fix the `1c7fcc55` mismatch (re-profile); sustainability through the API with `diet=` and an allergen that changes the swap; cost behind the same GET; platform steps (nutrition rows, index, `recipes_v7`, manifest keys, importer, image rebuild); PR comment correcting the removed routes.

## 2026-11-20 — User allergens and diet in the adaptation API (nutrition, sustainability); diet and allergen tags re-run on the live Neo4j

**Goal.** The platform partner passes the user's allergens and diet to the adaptation API; our side makes the API respect them. Stored adaptation rows stay as they are and are served only when no restriction is sent.

**API contract (built).**
- Nutrition: `POST /api/v1/recipes/{id}/adapt/small-gain` takes optional `allergens` (list) and `diet` (`vegan`, `vegetarian` or `pescatarian`). Empty input behaves as before (the route was always live; nutrition has no stored rows in `recipe_adaptations` yet).
- Sustainability: `GET /api/v1/recipes/{id}/adaptations?mode=sustainability&region=` takes `allergens` (repeated parameter) and `diet`. None: the stored row, unchanged. Any: never the stored row; runs live (`sustainability_live.py`) and returns a stored-row-shaped answer with `source: "live"` and `applied_restrictions`. Other modes with these parameters return 422.
- Outcomes with restrictions: the usual ones, plus `not_suitable_for_user` (nutrition `outcome`, sustainability `status`) with `offending` lines when a recipe line already breaks the restriction (no swap offered; sustainability `reason_code` `recipe_breaks_user_restriction`); `no_suggestion` with `alternatives_add_allergen` when every option fails the gate.

**How the check works.** `nutrition_adaptation.restriction_problems(parts, line_texts, allergens, diet)` (shared by both modes): the old recipe-relative gate (no new allergen, no break of a diet the recipe respects), plus the user's restrictions on the swapped-in food (`user_restriction_problems`: keyword detection, allergen names resolved with `allergen_filter_groups`, so lactose is milk and gluten is wheat), plus the graph's stored ingredient tags (`tag_problems`: `HAS_ALLERGEN` or `SUITABILITY_FOR = not_suitable`, looked up by the record label's name, most specific name first; unknown or missing tags decide nothing; graph errors are ignored; the graph is read only when restrictions are sent). Finding: once a recipe passes the "recipe breaks the user's restriction" precheck, the old gate already blocks targets that add an allergen or break a diet the recipe respects; the per-candidate user check and the tags are a safety net. Live sustainability returns no model text: the changed line reads `original line  →  use X`, `benefit.tip` is the code-written CO2e sentence, title and steps are the original ones (local generative LLMs are prohibited).

**Sustainability live path.** Factor functions moved from `analysis/sustainability-adaptation-2026-11-17/run.py` into `sustainability_live.py` (same CIQUAL code join, then the reviewed name mapping). CIQUAL table shipped as `src/recipe_wrangler/tools/data/agribalyse_ciqual_codes.json` (1,632 codes from the catalogue sqlite) beside `agribalyse_keys.json`, so no Postgres change. `sustainability_adaptation.adapt` got `allergens` and `diet` arguments. Parity: on the 537 captured Living Lab inputs the live code reproduces the stored results exactly (105 adapted with the same swap, 392 no suggestion, 40 not scored; offline).

**Pushed to PR #13** (`adaptation-and-matching-fixes`, no AI attribution): `930ccfa` nutrition route fields and gate; `51473d4` graph ingredient tags; `64b9f81` live sustainability route. Two PR comments with the API description: https://github.com/wisefood/RecipeWrangler-Backend/pull/13#issuecomment-7023283015 and https://github.com/wisefood/RecipeWrangler-Backend/pull/13#issuecomment-7023947854. Tests: 8 new in `tests/test_nutrition_adaptation_restrictions.py` plus 5 for tags, 13 in `tests/test_sustainability_live.py` (all mocked; tests switch the graph lookup off with `USE_TAGS`); 218 adaptation tests pass. The live routes were not exercised through the API on a real recipe. This STATUS entry, `docs/TODO.md` and the cost files are not committed.

**Live graph update (owner approved 20 Nov, API not in use).** Before: live had mixed old diet tags (v1/v10, pescatarian v8: 33 vegan, 90 vegetarian, 75 pescatarian recipes suitable) and fewer allergen edges (gluten on 222 ingredients, no sulphites). Backup first: `backups/graph_tags_20261120/` (`suitability_for` 41,928 edges, `has_allergen_ingredient` 2,958, `allergen_declarations` 3,067 nodes, `diet_support_nodes` 18; gzipped JSON lines, no restore script written). Then in tmux `graph_tags` (no model, no GPU, `nice`): `tag_allergens.py --replace` (deleted 2,958 edges and 2,958 inferred declarations; 56 s) and `classify_vegan_vegetarian.py --apply` (01:05 to 01:20, exit 0). Log `analysis/graph-tags-2026-11-20/run.log`.

**Result, live Neo4j (identical to the rehearsal on `neo4j-scratch`).**

| | suitable | not suitable | unknown |
|---|---|---|---|
| Recipes, vegan (v11) | 1,314 | 6,335 | 94 |
| Recipes, vegetarian (v11) | 3,723 | 3,931 | 89 |
| Recipes, pescatarian (v9) | 5,019 | 2,688 | 36 |
| Ingredients, vegan | 4,161 | 2,910 | 5,053 |
| Ingredients, vegetarian | 4,910 | 1,834 | 5,380 |
| Ingredients, pescatarian | 5,131 | 1,328 | 5,665 |

Ingredient allergen edges now: gluten 937, wheat 856, milk 870, sulphites 17. Nothing new listens on `0.0.0.0` (7687 and 7474 are `127.0.0.1`).

**Nutrition API check, same day (local API on 127.0.0.1:8002, no vLLM needed: the stored text comes from the 17-19 Nov LLM runs and the live route is deterministic).** Recipe `e9e84e5c` (pasta with courgette, IE): no restriction, `diet=vegetarian` and `allergens=nuts` all return wholewheat pasta (fibre 3.9 to 12.4 g per serving, A stays A); `allergens=gluten` returns `not_suitable_for_user` with the gluten and wheat lines. New `scripts/nutrition/load_adaptations.py` loaded 537 nutrition rows into `recipe_adaptations` on the local Postgres (dry run first; `GET /adaptations?mode=nutrition&region=IE` returns the row, a `no_suggestion` row returns `reason_code` and `reason`). Finding: a live call takes 2 to 3 minutes on a cold API (repeated Neo4j reads during profiling); the stored read is instant. The platform database needs the same load before deploying.

**Elasticsearch facets re-indexed (20 Nov).** The builder exists (`scripts/catalog/build_recipes.py`; the earlier note that none was found was wrong). Added `--no-swap` so an index can be built and diffed before the alias moves. Dry run (7,743 assembled, 0 skipped, carry-over of annotations for all 7,743), then `--apply --new-index recipes_v7 --no-swap` in tmux (30 s, no model). Diff gate `analysis/es-reindex-2026-11-20/diff_gate.py recipes_v6 recipes_v7`: same 7,743 ids; changed fields are the diet and allergen ones (`consumer_suitability` 7,718 docs, `allergen_evidence` 6,881, `diet_tags` 5,844, `suitable_for` 4,932, `allergens` 3,889) plus timestamps, `content_digest` (derived) and profile list order (compared by source and region: 0 real profile differences); one doc gained the claims `high_protein`, `healthy_and_nutritious`. Facet counts: vegan 34 to 1,117, vegetarian 97 to 3,723, pescatarian 88 to 5,019; allergens now include sulphites (202), gluten 1,091 to 3,281. Vegan is 1,117 in ES against 1,314 in the graph: ES marks a recipe `unknown` instead of suitable when allergen evidence says "possible presence" (9 of 9 sampled gaps), a stricter rule than the graph's. Alias `recipes` switched to `recipes_v7` (atomic); `recipes_v6` kept for rollback (`POST _aliases` back). The platform's ES needs the same build.

**API simplified (20 Nov, late).** `POST /adapt/small-gain` is no longer a public route (404, absent from OpenAPI); it was the prototype's name for the live engine. Nutrition now uses `GET /adaptations?mode=nutrition&region=` like the other modes, with optional `allergens` and `diet` checked against the stored row (`nutrition_adaptation.restrict_stored_row`): measured 0.01 s unrestricted, 0.2 to 0.3 s restricted on `e9e84e5c`; gluten gives `not_suitable_for_user`, nuts or vegetarian keep the wholewheat pasta swap. Profile of one live call (235 s): 217 s in `screen_production_swaps`, 150 s of it waiting on 1,574 Postgres composition queries (`identity_composition_records` 296 calls, 134 s), the rest in matching `resolve`; no LLM, no Neo4j. Limits: a blocked stored swap gets no alternative; a stored `no_suggestion` row cannot be checked against the recipe's lines. Earlier PR comments describing `/adapt/small-gain` are outdated: post a correction.

**One adaptation route (20 Nov, late).** `GET /adaptations` is one handler for every mode (`adaptation_row.py`, response model `AdaptationResponse`): stored row first; with no stored row the mode's engine runs (nutrition live 192 s on `0000656901`, a prawn and mango salad outside the Living Lab set; sustainability 6 s); restricted nutrition checks the stored swap, restricted sustainability recomputes and falls back to the stored row when the live engine cannot load the recipe (`saved_selection_source_mismatch` seen on `2df7ddbc`; cause not investigated). Cost has no engine behind this route (404). `suggestions`, `simulate`, `small-gain` and `/substitute` are no longer routes. Tests: 54 in the endpoint, live and restriction files, 1,323 in the adaptation subset. Not done: cost behind the same route; speed-up of the live nutrition engine (1,574 Postgres composition queries); PR comment correcting the earlier API description.

**Handoff to the sustainability work (20 Nov, end of session).**
Done today (pushed to PR #13 up to `079e506`): nutrition rows loaded into `recipe_adaptations` (537, native region, local Postgres only); one `GET /adaptations` for every mode with one response model (`adaptation_row.py`, `AdaptationResponse`); `/adapt/small-gain`, `/adapt/suggestions`, `/adapt/simulate` and `/substitute` removed as routes (engines kept internal); Elasticsearch `recipes` alias now on `recipes_v7` (diet and allergen facets match the graph, `recipes_v6` kept); nutrition checked through the API on a pasta recipe: stored row, `allergens=nuts`, `diet=vegetarian`, `allergens=gluten` (not suitable), plus one live call on a recipe outside the Living Lab set (192 s). Not committed: this STATUS entry, `docs/TODO.md`.
Waiting for, before sustainability counts as complete:
1. **Live engine failures.** `sustainability_live` returned `saved_selection_source_mismatch` for `2df7ddbc` (IE) although a stored row exists; cause not investigated. A 40-recipe sample of the Living Lab set (IE, live vs stored status) was started in tmux `sus_sample`, output `/tmp/claude-1001/-home-karvanitis-RecipeWrangler-Backend/00eb9d30-8f7b-483f-98b7-9bc993fde8a5/scratchpad/sus_sample.out` (script `sus_sample.py` beside it; may be gone after a reboot, rerun by sampling 40 ids from `nutrition_results.json` and calling `adapt_active_recipe(id, 'IE')`). Read it first: if many recipes fail, the restricted sustainability path depends on the stored-row fallback in `adaptation_row.py` and the cause must be found.
2. **Owner sign-off:** mapping section B (fish) not approved; UI wording; `/adapt/enhance` wiring for sustainability; commit of the remaining sustainability files.
3. **Not yet tested through the API for sustainability:** `diet=` on a stored recipe and an allergen case that changes the swap (only fish/nuts on `2df7ddbc` were tried).
4. **Platform side:** the nutrition rows and the `recipes_v7` Elasticsearch build must be repeated on the platform; the partner is being told by the owner; the earlier PR comments (small-gain, suggestions, simulate) are outdated and need a correcting comment.
5. **Later:** cost behind the same GET (no engine yet, 404); speed-up of the live nutrition engine (1,574 Postgres composition queries per call, owner said leave for now).

**Open / next.**
1. ~~Elasticsearch re-index~~ DONE 20 Nov, see the entry below.
2. Tell the platform partner: visible diet counts changed a lot (vegan recipes 33 to 1,314, vegetarian 90 to 3,723, pescatarian 75 to 5,019), allergen edges were rebuilt, and the new `allergens` / `diet` parameters exist (two PR comments above).
3. Cost route has no restriction input (its changes are still local and uncommitted); the cost nutrition guards, price rules and stored-data steps are in the cost entry below.
4. Halal and kosher are not covered (gluten-free works as `allergens=gluten`). Live sustainability text is not rewritten.
5. Exercise both live routes through the running API on a real recipe (not done here).
6. Rollback of the graph step, if needed: re-import the edges from `backups/graph_tags_20261120/` (suitability and allergen edges, declarations); no tested script.
7. Commit this STATUS entry when the owner says; from the 19 Nov entries: platform manifest keys, vLLM `vllm_shared` still running, cost changes uncommitted.

## 2026-11-19 (cost) — Cost adaptation reviewed and fixed locally; 61 of 537 Living Lab recipes; nothing committed, stored data not updated

**State found.** Two cost routes. Old route `service._generate_cost_suggestions` (`/adapt/suggestions`, mode=cost) prices saved (name, weight) pairs through `CostCatalogue.resolve` and takes substitutes from graph edges. Production route `/adapt/enhance` (`enhancement_screening`, `impact_objectives`, `objective_approval`) uses the new matching engine and returned 0 cost results: its strict price rule rejected alias and base-product matches (salmon fillet matches by alias at base scope), `price_unknown` blocks 446 recipes (complete pricing of every ingredient required) and 74% of records lack a stated raw preparation state. The old route cannot re-match at present (matching asset paths moved, see the manifest note in the entry below); it was run on saved names and weights instead.

**Decisions (owner).** Cost adaptation is cost-only; nutrition and sustainability are separate mechanisms to be combined later. Price rule option B: alias and base-product matches are accepted and labelled as estimates (FoodOn group median stays excluded). Nutrition guards off for cost for now, kept in code for the combined mode. Fish base prices from plain fresh forms only. Fresh fish is not compared with a preserved-only price.

**Code (local, uncommitted).**
- `impact_objectives.py`: cost branch accepts `detailed_product` and `base_product`; evidence names the product, match method and price scope.
- `objective_approval.py`: `COST_NUTRITION_GUARDS = False`; cost mode skips the grade and per-nutrient guards but still reports nutrient deltas; the complete-comparable-nutrients requirement still applies.
- `pricing/build_lookup.py`: fish base price is the median of plain forms only (excludes smoked, prepared, preserved, canned, breaded, battered, cooked, peeled, sticks, slices, rings, sauce, oil; tiers: fresh fillets, other fresh not whole or gutted, any plain fresh, plain frozen, else all rows).
- `pricing/cost_ingredient_aliases.json`: "X fillet(s)" for salmon, trout, cod, plaice, pike-perch maps to the fresh-fillet detail price instead of the base price.
- `pricing/cost_calculator.py`: `CostCatalogue.same_price_form`; `service._evaluate_cost_candidate` rejects a fish swap when only one side has a fresh price.
- Tests: production-integration (proxy accepted, cost ignores nutrition regression while guards are off, nutrition regression tests run with the flag on), pricing pipeline (4 fish cases), cost calculator (same-form), cost adaptation fake catalogue. 95 passed across the four files.

**Result on the 537 Living Lab recipes (old route, no LLM, guards off):** current catalogue 54; new fish prices without the same-form rule 64; with it **61** (Irish 48 of 308, Hungarian 6 of 138, Slovenian 7 of 91). Swaps: chicken to pork 33, beef to pork sausage 13, fish to trout 12, rice to pearl barley 3. Median saving 0.37 EUR per serving. Salmon to trout is about 14% (IE 25.92 vs 22.23 EUR/kg) instead of the inflated 52% caused by smoked salmon in the base price. Production route: 0.

**Findings on the data.** The 322 catalogue products (246 detail, 76 base; EU, IE, HU, SI) are economic reference prices from mixed market stages (wholesale, farm-gate), not shop prices; only EUMOFA fish is retail. Base price is the median of the selected detail rows. One price per animal (chicken breast and thigh share 6.82, beef steak and mince share 9.16, IE).

**Allergens and diets (answered, not built).** Old route has no allergen or diet input; it only blocks a swap that adds an allergen the recipe lacks or breaks a vegan, vegetarian or pescatarian recipe (keyword detection). A user restriction the recipe already breaks, and halal, kosher or gluten-free, are not covered (chicken to pork would be offered to a halal user). `/adapt/enhance` accepts `constraints` (`forbidden_allergens`, `required_consumer_groups` vegan or vegetarian, no pescatarian) but yields no cost swaps. Fits the agreed allergen/diet contract in the entry below.

**Open.**
- Stored data not updated: pricing pipeline, Postgres re-import (`cost_products`, `cost_prices`, `cost_aliases`, `cost_recipe_calibrations`) and cost-category backfill. Base-price changes shift the Q33/Q67 category boundaries and the Elasticsearch cost facets; needs owner approval and a backup.
- Production route yields 0: decide whether cost mode prices only the target ingredient and its replacement (flagging unpriced lines) and drops the raw-state requirement; same-form rule not yet in that route.
- Review the 61 swaps for culinary fit (generic chicken to pork, beef to pork sausage questionable); thresholds are provisional; no LLM text for cost.
- Coverage: 22 recipes with no price, 451 with no swap clearing 10%; mixed forms in other foods (tomato).
- Allergen and diet input for the cost route (add `constraints` to the suggestions request, extend groups with pescatarian, test that vegan or nut-free requests drop pork and similar swaps).
- Re-combine with `COST_NUTRITION_GUARDS = True` when nutrition and cost are merged (TODO item 9 in `docs/TODO.md`).
- `docs/COST.md` not yet updated with the fresh-only and same-form rules; `docs/ADAPTATION.md` cost row is updated.

## 2026-11-19 (evening) — Nutrition adaptation finalized, pushed to PR #13; diet tagging pushed; allergen/diet contract agreed

**Supersedes the "(late)" entry below** (its "uncommitted, awaiting owner go" is done: pushed in the commits listed here).

**Ham swaps blocked.** The production profiler returns the same ham record for any ham wording, so a ham swap cannot be verified. Removed the `ham_to_lean_ham` rule and blocked every swap on a line that names ham in all three swap paths (production swaps, curated rules, descriptor route) plus the loose search (`HAM` in `nutrition_adaptation.py`, test added). Effect: `80f22057` and `9f958127` became enrichments, `ESSRG_80` has no suggestion, `ea4cd06b` keeps only its crème fraîche swap. A trial that required a number in the line for descriptor swaps dropped 5 legitimate recipes (hummus lines whose amount comes from metadata) and was reverted.

**Rewrite fixes** (`nutrition_adaptation_llm.py`, tests added): a dropped imperial amount is no longer a quantity change; the record name "pork ham roasted w/o gravy frozen product" shows as "cooked ham" ("roast ham" matched no record and cost a recipe its Nutri-Score); the validator now rejects a rewrite that drops "per person" from an amount (it quadrupled the rice weight in `ed9b8c65` and showed as a protein worsening). Rewrites re-run on the local vLLM for `ed9b8c65`, `ea4cd06b`, `a9d5e020`, `72322097`; e2e re-run for the changed recipes.

**Matching release manifest.** After the `data/` restructure the matcher loaded its five assets from new paths while `matching_releases.manifest.input_sha256` (release `2026-10-27-v11`) kept the old ones, so the engine refused to load ("Matching asset does not match published release: dictionary") and three LoSalt recipes failed retrieval. Added the new path keys next to the old ones (hashes verified identical), backup in `backups/matching_releases_2026-10-27-v11_20261119.json`. The platform database needs the same keys before deploying.

**Final numbers** (offline review, tables, `freeze_adaptation.py`, `finalize_docs.py` patched for the deleted `docs/PLAN.md`): 537 Living Lab recipes: 284 adapted (83 up a letter), 204 enriched, 49 no suggestion. E2e of the final text: 220 of 284 pass; flags gain_not_confirmed 51, worsening 23, grade_drop 1 (`60883d08`, one of the 32 stale baselines left unmarked); 73 of 83 claimed letter gains confirmed. The 13 flagged changed recipes are profiler record choices (chickpea wording, brown rice record, an unquantified crème fraîche line) or small claims, not rule bugs. Golden test counts updated to 284/204/49.

**Tests.** Full suite 5423 passed, 1 failed (the golden test, then updated); with `test_consumer_suitability.py` run separately the diet files pass (185). In a clean worktree 11 nutrition tests failed because they read the gitignored `analysis/` capture; they now skip without it (`needs_data`). `test_catalog_es_schema.py` (2) fails on the branch before this work; a few portion tests fail without a database.

**Pushed to PR #13** (`adaptation-and-matching-fixes`, no AI attribution, no history rewritten): `02d47e4` scorer, rules, diagnosis (+ router/service import renames); `740ae7c` LLM stage; `1a85ff5` preparation state from the record label; `ce3175f` matching fixes (LoSalt alias, chicken breast, cream sour); `ebed1a3` docs and frozen results; `ef84458` test skips; `3d2c31f` diet tagging code. PR comments cover API (endpoint unchanged; `outcome` now `adapted`/`enriched`/`no_sensible_adaptation`/`failed`, new `enrichment`, `diagnosis`), schemas (none, no migration) and code, and the diet correction. Kept out of every commit: cost files (`pricing/*`, `impact_objectives.py`, `objective_approval.py`, the cost hunks in `service.py`), `scripts/*`, `.gitignore`, `Makefile`, docker files, `analysis/README.md`, `docs/TODO.md`, the `.docx`, the other session's staged deletions. The three held-back tests (`test_profiling_accuracy.py`, `test_parser_live_route.py`, `test_ingredient_coverage_fixes.py`) were not asked about again.

**Diet tagging (`3d2c31f`).** Commit `352ceab` ("restore vegetarian, vegan and pescatarian tagging") contained only `reviewed_diet_ingredients.json` while its message described rule and classifier changes. The code is now in `3d2c31f` and a PR comment corrects the reading; the old commit was not rewritten (11 pushed commits including a merge, hashes already given to colleagues). Changes: versions vegan/vegetarian v10 to v11 and pescatarian v8 to v9 (stored v10 counts as stale until the classifier re-runs); pescatarian blocked only by meat and fish (`no_meat_evidence`); rennet cheeses and pesto block vegetarian, halloumi is vegetarian only, wine, oil, margarine, vegetable stock and plain plant staples suitable; "may contain" no longer decides suitability; names cleaned before positive evidence; `_apply_reviewed` fills unknowns from the reviewed table; `classify_vegan_vegetarian.py --apply` writes the table and six `DietaryRuleNote` nodes. `--apply` has not been run against the graph by this work.

**Decision, not built: allergens and diet type as API input** (nutrition, cost, sustainability, same adapt endpoints, no separate API). None given: return the stored row from `recipe_adaptations`. Any given: run the pipeline live and drop candidates that violate them, never serve the stored row. Today the stored rows are checked only against the recipe's own restrictions, and `/adapt/small-gain` takes only `region`. Sustainability needs a live wrapper around `sustainability_adaptation.adapt` with the factor functions moved from `analysis/sustainability-adaptation-2026-11-17/run.py` into `src`; the factor mapping stays flagged as awaiting sign-off. Spec in memory `project_adaptation_stored_rows_policy`.

**Open:** build the allergen/diet contract (tests first, mocked, no paid calls); cost changes still local (base-product prices accepted, nutrition guards off for cost); platform manifest keys; run the diet classifier with `--apply` on the target graph; this STATUS entry is uncommitted; vLLM `vllm_shared` still running (ask before stopping); unused `llm-runs/reapplied-2026-11-19k-stale` kept on disk.

## 2026-11-19 — Repository tidy-up, data/ restructure, API reads reference data from Postgres

- **data/ restructure (local, gitignored):** `data/` is now `recipes/`, `nutrition/` (`composition_tables/`, `reference_recipes/`, `matching/`), `portions/`, `sustainability/`, `cost/`, `images/`, `retired/`. Run and experiment outputs moved to the root `analysis/`. About 5 GB of old runs from `analysis/` and 3.8 GB from `data/` were archived (`backups/archives/*.tar.zst`, verified) and removed; the empty `deployment/` folder, the old `handoff` dump and the incident logs were deleted; `dumps/` keeps the two latest sent exports. Hardcoded paths in scripts, `src/`, tests and docs follow.
- **API / database:** the API reads `reviewed_portion_foods`, `structured_external_portions` (11,467 rows) and `healthyfoods_nutrition` from `pipeline_static_data` instead of files, through `utils/pipeline_data_pg.load_pipeline_data_or_file` (Postgres first, warning plus file fallback). Importer: `scripts/postgres/import_pipeline_static_data.py --only ...`. The five matcher files are still read from disk, now under `data/nutrition/matching/`. Deployment steps: [OPERATIONS.md](OPERATIONS.md#runtime-reference-data-in-postgres).
- **Docker:** the `Dockerfile` COPY lines and the `recipe-api-docker` images mount follow the new layout. The running `recipe-wrangler-api` container (baked image `matching-2026-11-04-v31`) was not rebuilt or restarted.
- **Git:** commits `61e0464`, `ee0cc70`, `fd31362`, `cb896ac` pushed to PR #13 (`adaptation-and-matching-fixes`) after merging 9 upstream commits; local branch fast-forwarded; PR comment with the API notes posted.
- **Docs:** every guide now has one purpose and a state date: [OPERATIONS.md](OPERATIONS.md) (run, deploy, rollback), [DATA.md](DATA.md) (reference data and stored tables), [ANALYSIS.md](ANALYSIS.md) (recipe profile pipeline and contracts), [MATCHING.md](MATCHING.md) (ingredient matching), [COST.md](COST.md) (price pipeline and cost categories), [ADAPTATION.md](ADAPTATION.md) (all four adaptation modes, rewritten from the code; cost adaptation rules moved there from COST). Previous texts are kept locally in `analysis/docs-history-2026-11-19/`. `PLAN.md` split into this file and [TODO.md](TODO.md); dated deployment records moved here from `OPERATIONS.md`; `docs/README.md` removed; `docs/specs/` (generated Elasticsearch mapping copies, untracked and unused outside one test) removed together with `scripts/catalog/dump_mappings.py` and the test that compared the copies; `catalog/es_schema.py` stays the single source for the index mappings.

## 2026-11-19 (late) — Nutrition adaptation frozen, awaiting owner go for commits

**Dataset:** 537 Living Lab recipes: 284 adapted (83 up a letter), 204 enriched, 49 nothing; 283 LLM rewrites, 1 code card (`llm-runs/withdrawn.json`). Frozen in `evaluation/adaptation/results/` (`nutrition_results.json`, `nutrition_llm_rewrites.json`, `living_lab_adaptations.md/.csv`, `review_table.*`, `nutrition_e2e_check.json`, `summary.json`); README updated; golden test `test_packaged_module_reproduces_the_recorded_results` now reads the frozen results.

**End-to-end check (all 284, local parser, no hosted calls):** 220 pass; flags gain_not_confirmed 51, worsening 23, grade_drop 1; labels stale_baseline 32, sodium_gain_unverifiable 1; 73 of 83 claimed letter gains confirmed. No recipe is left without a Nutri-Score. Report `analysis/nutrition-adaptation-2026-11-11/e2e-final-2026-11-19/report.json` (`e2e_final_report.py`).

**Changes today (all uncommitted; ham swaps blocked as unverifiable, "per person" kept on rewritten amounts, matching release manifest got the new asset paths):** rules (`food_roles.py`, `nutrition_adaptation.py`: lentil variety, implausible-fibre descriptor swaps, nut butter stays a butter, vague average fish, nicer fractions), LLM stage (`nutrition_adaptation_llm.py`: tin/bone-in/count weights, wording cleanup, tips, cut-word validator), matching/data (`hybrid.py`, `occurrence_facts.py`, `reviewed_aliases.py`, `reviewed_nutrition.json`: live profiling only), Postgres row fill for chickpea saturated fat 0.28 g/100 g (backup `backups/matching_compositions_chickpea_004005_20261119.json`), tests. Full suite: 5,403 passed, 20 deselected (`tests/test_consumer_suitability.py`, fails for a change outside this work).

**Open:** commits (owner go; 4 themed + optional 5th for matching/data); hosted-parser check of the LoSalt wording (cents, not run); 33 stale baselines left unmarked; unused `llm-runs/reapplied-2026-11-19*` dirs on disk; shared vLLM still up (owner to say when to stop).

## 2026-11-19 (night) — Nutrition adaptation: report relabelled, rewrite-line bugs fixed, 27-recipe re-check

**Step 1, report recount** (`e2e_recount.py`, rebuilds `e2e-local-2026-11-18/report.json`): 157 pass, 138 flagged (gain_not_confirmed 110, worsening 41, pipeline_no_score 14, grade_drop 4). Labels, not failures: `stale_baseline` 32 (21 also flagged), `sodium_gain_unverifiable` (salt line <= 1 g) 29. Four further relaxations (noise correction, claims < 5% skipped, protein/fibre trade-off, LoSalt > 1 g unverifiable) exist only as the `EXT` diagnostic in `e2e_report.py` (would give 77 flagged); not agreed criteria, not applied.

**Step 2, causes:**
- Rewrite text, fixed in `display_line` (12 new tests): tin forms (plural "tins", "400g tin", "1 tin" without weight, "2 x 400g cans") now state the drained grams (60%, not for "drained weight"); a swapped meat line that lost its count states the weight; "1 X, around N kg" states the weight as a decimal ("1.5 kg", the profiler reads "1½ kg" as 2 kg). 19 stored rewrites re-applied without an LLM call into `llm-runs/reapplied-2026-11-19c` (earlier `-19`, `-19b` dirs unused, left on disk).
- Pipeline, not fixed: unchanged line "Cream sour" is matched to "Cream ice cream" in the rewrite run (herb soup, mashed potatoes, chicken paprikash, beef in horseradish; sugar jumps). Reproduces on re-run, so it is matcher sensitivity, not random noise.
- Baseline-side parse: "¼ chicken stock cube" reads 0.25 g (one pot roast chicken, sodium 156->484); "⅔ stock cube ... dissolved in" reads 0.7 g in 3 recipes (weight LLM).
- Trade-offs that equal the claim: banana peanut butter, red lentil pottage (bad candidate, red -> green lentils), beef in horseradish (Parmesan -> cottage cheese). Fish cakes and peanut butter toast not diagnosed.

**Step 3, no Nutri-Score:** "boiled chickpeas" is matched to `Chickpeas, dried, boiled without salt, drained` when the parser drops "boiled" from the name; that record has no saturated fat (data gap, EU table). When the name keeps "boiled" it matches `Chick pea, boiled/cooked in water` and scores. Chicken fried/stew: the unchanged line "Chicken meat, breast without skin, boneless" is unresolved (`property.food_part`) in the local run, before and after. Butter bean, béchamel, cranberry sauce records lack sugar. Decision needed: fill the missing nutrient or prefer the complete record (engine change).

**Re-check** (27 recipes, local vLLM, duty 0.4, 0 failures): casserole, drumsticks, thai butternut curry, provencal now score or pass; chickpea recipes still unscored by the record gap above; lamb and roast chicken to be re-run with the decimal weight (tmux `e2e_recheck2`).

**Step 4, baselines:** second local run of the 32: 31 reproduce the first run exactly (differences -35% to +120% vs stored, so deterministic, not random noise), 1 varies (sweet cheese strukli). Caveat: both runs use the local parser/weight LLM; stored profiles may come from the hosted parser, so part of the gap can be parser difference, not staleness. A re-profile write should use the production parser; needs approval, backup first. Dry-run list: `baseline-reprofile-dryrun-2026-11-19.json`. No Postgres write done.

**Lamb / roast chicken re-run with decimal weight** (`e2e-local-2026-11-19c`): lamb worsening gone (energy gain 0.37 claimed, -0.05 measured); roast chicken still energy 0.41 vs 0.19 and sodium 278->332 (whole chicken with bone 1600 g -> "meat only" 2000 g: bad rewrite text/weight).

**Update (later 19 Nov):**
- Cream sour fixed in `matching/occurrence_facts.py` (`source_description_name` restores a comma-free released label); 4 recipes re-run, 3 now pass, beef in horseradish keeps its real protein trade-off.
- Chickpea `saturated_fat_g` 0.28 g/100 g filled by hand (owner, 2026-11-19) in `matching_compositions` release `2026-10-27-v11`, record CREA 004005 (three jsonb fields); row backup `backups/matching_compositions_chickpea_004005_20261119.json`.
- Stale baselines checked with the production models (llama-3.3-70b parse, gpt-4o-mini weights, originals only, 32 recipes, measured $0.1434 on the shared key): 8 reproduce the stored baseline (Idrija, barbecue chicken and others were local-model errors), 24 still differ from stored and match the local run (real difference between stored profile and today's pipeline). Table: `baseline-hosted-check-2026-11-19.json`. No Postgres write.

Nothing committed. Hosted calls: only the 32-original baseline check above ($0.1434).

## 2026-11-19 — Nutrition adaptation: local end-to-end check done, 149 of 295 flagged, fixes pending

**Result:** all 295 adapted Living Lab recipes re-profiled (original and rewritten text) through the real parser, matcher and Nutri-Score, 0 failures. 146 pass, 149 flagged. Of 86 claimed letter gains, 60 confirmed, 26 not. **Not ready to freeze.** Nothing committed or deployed.

Flags: gain_not_confirmed 110, worsening 41, baseline_issue 32, pipeline_no_score 14, grade_drop 4 (a recipe can carry several).

**Where:** report `analysis/nutrition-adaptation-2026-11-11/e2e-local-2026-11-18/report.json` (built with `e2e_report.check`, per-recipe JSON beside it). Run script `e2e_check.py` (`--resume --duty 0.4`), local parse via shared vLLM (Qwen3-32B-AWQ, 127.0.0.1:8005, 0.50 GPU, 2 seqs, tmux `vllm_shared`, still up; shut down when the sustainability chat is done). No hosted calls.

### Done this session
- Final rewrite text per recipe = latest ok rewrite across all LLM run dirs (`merged_llm`); offline audit `check_candidates.py`: 295/295 have a current candidate and clean lines (3 database-style: ESSRG_146, 66, 94).
- Fixes in `nutrition_adaptation_llm.py`: cookbook flour halves stated as two lines (was halving the flour), tin swapped for cooked pulses states the drained weight, recipe pasta shape kept, count rule relaxed for tin lines. `nutrition_adaptation.py`: canned chickpeas -> `boiled chickpeas` (the old name matched a record with no sugar, so no Nutri-Score). Tests added; suite 5,368 passed with `tests/test_consumer_suitability.py` deselected (its new CHICKEN EGG test fails, changed outside this session).
- `e2e_report.py`: sodium gains from salt lines of 1 g or less are labelled unverifiable (pipeline reads them as plain salt, policy `salt_pepper_1g_per_recipe_v1`). Not yet applied to the local report.

### Open (to fix next)
1. Apply the <=1 g salt label and a stale-baseline label to the local report, then re-count the real failures.
2. Real failures seen, cause not yet read: falafel burgers (energy 475->809, sodium 285->877), one pot roast chicken (sodium 156->484), honey roast ham (protein 16.9->0.2), herb soup (sugar 1.0->4.2), mashed potatoes (sugar 4.4->8.2), chicken paprikash (sugar 6.3->10.5), thai chilli chicken (sat fat 2.0->2.5), plus protein drops on peanut butter toast, banana peanut butter, red lentil pottage, beef in horseradish, fish cakes. Decide per recipe: parse/match error on the rewrite, bad rewrite text, or bad candidate.
3. 14 `pipeline_no_score` (e.g. chicken fried, chicken stew, thai butternut curry, vegetables provencal): no Nutri-Score for original or rewrite.
4. 32 `baseline_issue`: stored profile differs from today's pipeline (drained tins 210 g vs about 126 g, idrija dumplings 1121 vs 510 kcal, oven chips, chicken goujons, others). Engine requires baseline reproduction (537/537), so the fix is re-profiling stored baselines (Postgres write, needs owner approval).
5. 26 letter gains not confirmed: separate noise and local-parser LoSalt reading from real.
6. Then: owner reviews `review_table.md` and about 15 final texts; freeze results, review table and merged LLM outputs into `evaluation/adaptation/results/`; update `docs/PLAN.md` and README numbers (still 296 adapted / 402 LLM texts); fix golden test `test_packaged_module_reproduces_the_recorded_results`; 4 themed commits only when the owner says.

### Limits
No paid hosted run (owner). AGENTS.md exception for the shared 0.50 vLLM was not recorded (edit blocked); owner adds it if wanted.

## 2026-11-18 — Diet tagging (vegetarian, vegan, pescatarian) fixed and pushed

**Result:** on a scratch copy of the graph (7,743 recipes) suitable counts went vegetarian 90→3,723, vegan 33→1,314, pescatarian 75→5,019. Unknown: 89 / 94 / 36. **Live Neo4j and ES are not updated yet.**

**Pushed:** commit `352ceab` on PR #13 (`adaptation-and-matching-fixes`), with a PR comment holding the explanation and numbers. Pushed from a clean checkout of origin (the local tree was 8 commits behind); only the tagging files went. Dumps not exported.

### Cause
Almost every ingredient stayed `unknown`, and the recipe roll-up turns any unknown ingredient into an unknown recipe.

### What changed
- **Pescatarian:** only meat and fish block. Suitable when no meat/fish and no unknown stock, broth, gravy, gelatine, marshmallow or kofta (`no_meat_evidence`). `possible_allergens` no longer decides suitability.
- **Name cleaning:** quantity/prep words and synonyms stripped before positive evidence; stored names unchanged.
- **Reviewed table:** `utils/data/reviewed_diet_ingredients.json`, 1,965 names, fills only still-unknown ingredients. Used by the batch classifier and `refresh_recipe_suitability`, so new imports reuse it. Written to the graph as `ReviewedDietIngredient`. Reviewer "strict default" rows (262 diet entries) kept `unknown`.
- **Owner rules:** Parmesan, Pecorino, Grana Padano, Gruyère, Manchego, pesto, gelatine, Worcestershire block vegetarian; Emmental conditional; halloumi vegetarian only; wine, plain oil, margarine, vegetable stock, tortillas, custard powder, miso suitable; jelly not vegan/vegetarian. Six `DietaryRuleNote` nodes written on `--apply`.
- **Titles:** "mac 'n' cheese" and "nice cream" no longer block vegan. Titles are never altered by the pipeline.
- Versions unchanged: `vegan-vegetarian-v11`, `pescatarian-v9`.

### Checks
- 150-recipe audit: 90/90 suitable side correct, 52/60 blocked side. 8 false blocks traced to strict-default rows; 7 of 8 now clear.
- Scratch run on `neo4j-scratch` (127.0.0.1:7690): 5395 tests passed. On the PR branch 7 failures also fail without this commit (`test_catalog_es_schema`, portion tests); `test_recipe_instruction_scrapers.py` fails at collection (missing `backfill_healthyfoods_instructions`).

### Next
- Go-ahead for live: back up `SUITABILITY_FOR` to `backups/`, `tag_allergens.py --replace`, classifier `--apply`, ES re-index, v5-vs-v6 diff gate before any alias switch.
- Tomorrow: send the dumps.
- Parser fix at source for new imports (prep words to `note`).
- Remove `neo4j-scratch` container and `neo4j-scratch-data` volume when done; `stash@{0}` left alone.

## 2026-11-18 — Sustainability (CO2e) adaptation finished and pushed

**Result:** 105 of 537 Living Lab recipes (20%) adapted with lower-CO2e swaps. 392 have no valid suggestion, 40 are not scored. Same 105 in all four regions (IE, HU, SI, EU).

**Pushed:** commit `fc034bd` on PR #13 (`adaptation-and-matching-fixes`, base `main`). Nothing went to main.

### What changed
- **Swap mechanism** (`services/adaptation/sustainability_adaptation.py`, `sustainability_swap_rules.py`): hotspot ingredients (≥20% of recipe CO2e) are swapped by curated rules (red meat→poultry/mince, half meat→lentils, butter→margarine, cheese→mozzarella, tuna→sardines etc.) plus related-food search. Protected foods are never swapped. Allergen/diet gate kept. Lines without a stated amount are not swap sources.
- **CO2 factors:** pipeline now uses AGRIBALYSE 3.2 via a reviewed name mapping (`tools/sustainability_factors.py`). CONCITO and role averages only complete totals. The legacy FooDB factor path was removed from the calculator.
- **Parser fix:** "N containers … approx. Xg" now gives N×X grams.
- **LLM rewrites:** local Qwen3-32B (shared vLLM :8005) wrote the adapted text; 3 recipes went through capped OpenRouter. 105/105 ok. The note on each swap is one code-written CO2e sentence.
- **Checks:** end-to-end original vs adapted run through the production profiling pipeline, 29/29 ok.

### Storage and API
- New Postgres table `recipe_adaptations` (PK `recipe_id, mode, region`; `status` adapted|no_suggestion|not_scored; generated `is_adapted` boolean). Alembic 0010 and 0011. 2148 rows loaded via `scripts/sustainability/load_adaptations.py`.
- New endpoint `GET /api/v1/recipes/{recipe_id}/adaptations?mode=sustainability&region=IE|HU|SI|EU`. 404 if no row. Non-adapted rows carry a `reason` sentence.

### Frontend hookup
- Branch on `is_adapted`.
- Show `adapted.title/lines/steps`, highlight `changed_positions`.
- `benefit.tip` is the only text; `benefit.metric` gives before/after/change.
- Flags: `provenance.flags.changes_dish_identity`, `provenance.flags.indicative`, `swap.half_lentils`.

### Known limits / next
- `/adapt/enhance` still has its own flow and a second AGRIBALYSE path (`sustainability_references`); unify onto the single mapping.
- `scripts/sustainability/publish_release.py` is stale: update or retire.
- White-fish swaps (→hake) are indicative only.
- FooDB data kept (legacy).
- Swap-search test skips until the nutrition modules (`nutrition_adaptation`, `food_roles` additions) land on the branch.
- Nutrition rows are not in `recipe_adaptations` yet.
- Local working tree still holds the same changes uncommitted and is behind origin; after pulling, reset these files to the pushed version.

## 2026-11-17 — Nutrition LLM run (local vLLM under a one-off owner exception, last 22 on hosted qwen3-32b for $0.02)

283 adapted recipes through choice + rewrite: 270 ok (95%), 6 the model judged not worth it, 7 withdrawn or failed and keep the code card (flour lines or validator limits); a bug that halved flour amounts (the rescaled-amount logic read the half-wholemeal swap as a cut) was found and fixed, all affected rewrites regenerated; local vLLM was stopped twice by its guard when free VRAM on the shared GPU suddenly fell to 1.5 GB (cause not identified), so further local runs wait for a quiet GPU; 207 enriched recipes get a code-written "serve with" card (no LLM). Outputs `analysis/nutrition-adaptation-2026-11-11/llm-runs/local-2026-11-17-full/`. Two validator gaps found and fixed the same day: a replaced cheese or cream no longer survives as a generic word in steps ("grated cheese" after cheese -> yoghurt), and code now removes the old amount of a cut ingredient from steps ("20ml of olive oil" -> "the olive oil"); the 10 affected rewrites were regenerated, 0 of 267 remain affected. The 10 failed rewrites keep the code card (the model keeps naming the swapped food, adds extra ingredient lines or mishandles a rescaled quantity). Next is the end-to-end re-profile of the rewritten texts. Files renamed: `small_gain*` -> `nutrition_adaptation.py`, `nutrition_adaptation_llm.py`, `nutrition_swap_rules.py` (route `/adapt/small-gain` unchanged).

## 2026-11-17 — Sustainability rework, Living Lab dev set

CO2e adaptation rebuilt on the nutrition mechanisms (`sustainability_adaptation.py`, `sustainability_swap_rules.py`): 108 adapted, no nutrition judging (cod to hake indicative, bone-in meat with edible-yield factor, tuna to sardines, prawns to mussels, half-lentil swaps in stews and curries, poultry ham added), 497 scorable, CIQUAL code join first then reviewed name rules (audited: 1.4% of CO2e questionable), 95% of the saving from red meat to poultry; AGRIBALYSE first, CONCITO fallback for totals only; diagnosis by >= 20% CO2e share, curated table plus related-food search, strict nutrition guard, protected foods. Mapping bugs fixed in the local copy (bell pepper as black pepper, stock as meat, "plain"/"cow's milk" record names). Details: [evaluation/sustainability](../evaluation/sustainability/README.md#adaptation-rework-17-november). Mapping sections A, C, D approved by the owner 18 Nov (B, fish, not approved). LLM choice + rewrite run 18 Nov (local vLLM, owner OK): full read of the rewrites found and fixed junk targets, half-lentil wording, wrong notes, database wording, leftover old foods; 106 adapted, 103 rewrites ok, 3 keep the code card. End-to-end check (30 recipes, 0 failures, 0 new allergens): 21 ok, 6 smaller gain, 3 not lower; meat, margarine, lentil swaps confirmed, fish/dairy/tin-count lines disagree because the pipeline uses other sustainability factors and misparses '2 tins'; AGRIBALYSE mapping applied to the pipeline in the working tree (uncommitted), parser fix for '2 tins of X, approx. Yg', red-to-red swaps allowed, no-amount lines skipped; final e2e 29 of 29 ok; all 105 adapted recipes have an LLM rewrite (last 3 via capped OpenRouter). UI wording, /adapt/enhance wiring and commit wait for the owner. Uncommitted.

## 2026-11-16 — Nutrition rework, Living Lab dev set

283 adapted, 79 up a letter, 207 enriched (served-with addition), 47 with nothing; candidates ranked by Nutri-Score points removed; full review table in the local analysis folder (was 363 / 63 / - / 174); swaps only on the ingredients carrying the negative points, descriptor search route, regional table first then EU. Same-family swap rules, protected foods, curated rows (low-sodium salt, canned to cooked pulses, skinless poultry, tuna in water, poultry sausages, lean ham, reduced-sugar ketchup/jam, feta to cottage cheese, pastry to filo), oil/butter quarter cuts, Nutri-Score diagnosis (`diagnosis.py`), additions judged per serving. Details and limits: [evaluation/adaptation](../evaluation/adaptation/README.md#nutrition-rework-16-november). Uncommitted.

## 2026-11-13 — All tests green

all 4,959 tests pass. Root causes: tests inherited the developer `.env` (`NUTRITION_MATCHER_MODE=new`; now pinned to legacy in `tests/conftest.py`); stale fixtures (catalogue rows without `annotation`, unversioned suitability claims) and stale expectations (1 g salt/pepper policy, gluten-free as allergen-absence filter, parser `max_tokens`). Two real matcher fixes in `contextual_parser.py`: stock cubes/powder/paste and scoped stock phrases no longer get the liquid `broth_or_stock` facet; a suppressed cut word ("breast fillet") is no longer flagged as unexplained text. Both change live matching only for newly computed profiles.

## 2026-11-13 — Scripts kept for the API image

scripts are not in the API image (`.dockerignore`); kept only data-pipeline/operational tools and evaluation scorers. 46 internal check/audit/pilot scripts and the tests that only tested them archived under `analysis/archived-*-2026-11-13/`; evaluation dataset CSVs now tracked (matching gold stays local, partner data); cost figures tracked.

## 2026-11-13 — Push audit

dead experimental matching chain (`matcher`, `foodon`, `foodon_policy`, `identity_retrieval`, `compounds`, `interfaces`, `wisefood/`) and 7 tests of gitignored analysis runners archived under `analysis/archived-*-2026-11-13/`; evaluation trimmed to one README + data/results per area (adaptation rewritten for the four modes, sustainability reports combined, nutrition repairs folded into its README); root `Fineli_Rel20` zip moved to `data/`. 50 tests still fail (pre-existing).

## 2026-11-13 — Repo cleanup

`matching/experimental/` (the live `new` matcher) merged into `matching/` and the folder removed; `matching/identity.py` shim and `facet_retrieval.py`, `adaptation/input_basis.py`, `restriction_evidence.py` folded into their users; 38 unreferenced one-off scripts moved to gitignored `analysis/archived-scripts-2026-11-13/`; `test_experimental_*` renamed `test_matching_*`. Backup `backups/pre_cleanup_20261113_125948.tar.gz`. Nothing committed.

## 2026-11-13 — UI benefit facet

every LLM-stage result now carries a separate `benefit` block (facet, free-text `tip`, code-measured `metric`, `swap`, `notes`) for nutrition, cost, sustainability and time; the model writes the sentence with a `{benefit}`/`{minutes}` placeholder and no digits, code fills the measured number; without a model tip a code sentence is used. First time example with one hosted call: Roast pork with ratatouille, 5-8 min saved. Existing nutrition/cost/sustainability results need the LLM re-run to get model tips.

## 2026-11-13 — Time adaptation

overlap discovery now runs code-only (`deterministic_understanding`) with any passive anchor (simmer/boil/rise/..., not just bake) and an opt-in start-only draft with no minutes claim: 49 Living Lab recipes with a candidate (Irish 43, Hungarian 6), word-overlap independence still noisy, review still required. Prepared-rice pilot removed as too specific (module, rice fields in time extraction, rice scripts; backup `backups/adaptation_untracked_20261113_112926.tar.gz`); time-extraction prompt now `source_fragments_v6`. Cleanup: five adaptation `*_models.py` merged into `services/adaptation/models.py`; unused `matching_variants.py`, the nutrition prototype launcher and four one-off pilot scripts deleted (backup `backups/adaptation_untracked_20261113_115126.tar.gz`).

## 2026-11-13 — Cost mode repaired, sustainability example

existing cost mode repaired (profile-format break) and tightened with the shared role filter and visible-saving floors instead of a parallel engine: 48 Living Lab recipes with a cost suggestion; end-to-end example Grilled salmon cutlets -> trout (EUR 3.80 -> 1.91/serving). Sustainability end-to-end example: Roast lamb -> roast turkey (pipeline CO2 1.82 -> 0.23 kg/serving). Next: fold sustainability into the existing sustainability mode the same way, then the single /adapt/enhance merge.

## 2026-11-13 — Night (unattended): graph route and role filter

graph + FoodOn route added to the nutrition screen; shared culinary-role filter (`food_roles.py`: same job in the dish, no prepared dishes, drinks or baby food as targets, no produce-for-produce, compound records excluded) -> nutrition 363 adapted (Irish 218, Hungarian 84, Slovenian 61), 63 up a letter, 81 graph-route swaps (cream -> Greek yogurt, couscous/rice -> quinoa/barley, feta -> mozzarella); baseline recalculation extracted as `make_evaluator` for every facet. Generic facet engine (`analysis/facet_candidates.py`): sustainability 433 scored / 150 with a sensible candidate (reviewed AGRIBALYSE mapping, legacy CO2 factors were wrong for courgette, milk, stock, water); cost 286 scored / 73 with a candidate (catalogue prices only 152 base foods); time coverage measured (no model). Still pending: nutrition LLM re-run on local vLLM (owner go-ahead), then time understanding on local vLLM, mapping sign-off, repo inventory.

## 2026-11-12 — Night: no invisible cuts

Owner: halving 1 g of salt or a few grams of oil is not a suggestion a user would accept. Amount cuts of salt, oil/fat, cheese, cream and cured meat removed (lighter-version swaps stay; sugar, sauce, jam, chocolate, nut butter and stock-cube cuts stay). 336 of 537 adapted (Irish 207, Hungarian 80, Slovenian 49), 44 up a letter; the LLM accepted 246 (Irish 155, Hungarian 64, Slovenian 27) and judged 88 not worth making, mostly the extra-vegetable fallback. Open: cheese swaps are rare (no reduced-fat feta etc. in the data), owner to decide on a different cheese type; baselines miss cooking water/stock and do not count mushrooms/onions as vegetables (inflates B-and-below).

## 2026-11-12 — Evening: full LLM run

on local vLLM (owner exception, see AGENTS.md): 402 of 441 adapted recipes have the LLM title/steps/lines (Irish 233, Hungarian 93, Slovenian 76) after the 16 validation failures were retried with fixed checks; 39 the model rejected every candidate and keep the deterministic card. GPU peaked at 79 C. Next: file inventory, merge into one `/adapt/enhance` endpoint, themed PRs, then sustainability.

## 2026-11-12 — Afternoon: safety gate, LLM stage, end-to-end check

allergen/diet safety gate, bone-in and mince rule fixes: 441 adapted. LLM stage (hosted qwen3-32b) validated on 46 recipes. End-to-end profiling check of original vs final text: 30 of 41 measured pass, 6 baseline-reproduction only, 4 pipeline noise, 1 fail. OpenRouter key near its $20 limit; full LLM run and the rest of the end-to-end check wait for credit. Next: fix the half-wholemeal-flour line, owner decisions on named dishes and filo, then commit on owner's go-ahead.

## 2026-11-12 — Nutrition adaptation v1

537 screening-ready recipes, 445 adapted, 92 with no sensible healthier change, 0 failed. Deterministic endpoint `POST /api/v1/recipes/{id}/adapt/small-gain`, no LLM. In the `held-baseline-repair` worktree, uncommitted. `/adapt/enhance` unchanged.

## 2026-11-11 — Baseline repairs

10-11 Nov repairs raised complete profiles from 357 to 458 of 576; 109 recipes still held. See [baseline repairs](../evaluation/nutrition/README.md#baseline-repairs).

## 2026-11-04 — Matching release 2

**State 4 Nov 18:00 UTC:** v31 image deployed; promotion `matching-release-2-2026-11` active (30,972 profiles, 15:07 UTC; reports in
`analysis/matching-release-2/`: prepare/rehearsal/promote JSON). Projection worker draining 7,743 recipes to Neo4j/ES/cache (~12/min,
done ~02:00 UTC 5 Nov). Rollback: `promote_matching_production rollback --promotion-id matching-release-2-2026-11` (restores rows,
re-queues projection). Next steps: see [TODO.md](TODO.md).

API code: image `wisefood/recipe-wrangler:matching-2026-11-04-v30` deployed 4 Nov (user-run compose update); v31
(`matching-2026-11-04-v31`) built and pending deployment. Each image is the 29 Oct image (pinned by digest
`sha256:ef7228…`) plus ten matching source files copied into `site-packages` and `/app/src`; no package operations.
Overlay equality was verified against the workspace (711 ingredient resolutions, 40 recipe calculations, identical).
Rollback of the code: set the compose image back to `wisefood/recipe-wrangler:matching-2026-10-29` and `up -d`.
The compose file before the change is saved in `backups/pre_v30_backfill_20261104/`.

Profiles: v31 backfill writes inactive runs (`scripts/composition/backfill_matching_profiles.py --shard i --shards 4`,
2 CPUs / 8 GB each, watchdog on host memory and live search latency). Served profiles change only through
`promote_matching_production.py prepare --mechanism-version … / promote [--rehearse]`, which now allows a second
promotion within the same release, backs up every replaced row, and queues projection for every affected recipe in the
same transaction; `rollback` restores the backups and queues projection again. Pre-backfill dumps:
`backups/pre_v30_backfill_20261104/` (profiles table 32,680 rows verified, active pointers, deployment, promotions).

## 2026-10-29 — Matching deployment recorded

The saved deployment uses release `2026-10-27-v11`, database mode `new`, API mechanism `occurrence_matching_v13_quality_fixes`, and catalogue alias `recipes -> recipes_matching_20261029`. The prior `recipes_v5`, legacy matching code, immutable matching runs and predecessor serving rows remain for rollback. The saved audit records 7,743 recipes, 30,972 regional profiles and 309,536 current graph occurrences.

The API container is `recipe-wrangler-api`, port 8001, restart policy `unless-stopped`. Pinned model/parser assets were packaged for offline startup; `.env` is mounted, not built into the image. Generated-image storage is the only public data mount in that recorded deployment.

Deployment commands preserved from the activation run:

```bash
make build IMGTAG=wisefood/recipe-wrangler:matching-2026-10-29
docker compose --env-file .env -f recipe-api-docker/docker-compose.yml up -d
curl -fsS http://127.0.0.1:8001/health/matching
```

Before rerunning, verify the current release, Docker configuration, build constraints, pinned-model cache and database access. The recorded build uses the released PostgreSQL assets and cached model rather than sending operational data/secrets into the build context.

Saved validation includes 125 focused regressions, offline model selection checks, a served recipe GET, an incremental temporary-recipe projection check followed by cleanup, and a full projection audit. Reference-only rows retained fingerprint `b413545a71a4db8a910e50845c8e4262`. These checks do not establish a full-suite pass, current database state, high nutrient completeness, or adaptation readiness.

Evidence is under `analysis/matching_production_2026-10-29/`: `activation.json`, `final_verification.json`, `incremental_smoke.json`, `rollback_rehearsal.json`, and related deployment/preflight records. New reports go to `analysis/<run-id>/`; do not relocate these historical paths while scripts and manifests still depend on them.

## Decisions

| Date | Decision |
|---|---|
| 30 Oct | Four adaptation facets: nutrition, sustainability, cost, time |
| 30 Oct | Curated Irish, Hungarian and Slovenian sets are the primary nutrition references |
| 30 Oct | No external expert annotation campaign; internal and public evaluation only. Study participants judge complete platform tasks |
| 30 Oct | Unresolved allergen status: keep the recipe and flag the uncertainty, including in filtered results. Known conflicts stay excluded; never claim confirmed absence |
| 30 Oct | Nutrition adaptation may count meaningful improvements within the same Nutri-Score letter |
| 30 Oct | Plan is dependency-led, no artificial deadline |
| 3 Nov | Allergen reference is EU 1169/2011 Annex II (rules in [METHODS](../evaluation/METHODS.md)) |
| 3 Nov | Profile diet and allergens are hard search constraints; preferred ingredients are soft |
| 3 Nov | Gold-table restriction in matching is an evaluation run only; production keeps regional source policy |
| 4 Nov | 165 matching-backfill gram changes approved |
| 10 Nov | Adaptation testing focuses on the Living Lab recipes in their native region |
| 11 Nov | "Even a small change counts" for nutrition adaptation tiers; no grade drop allowed |
| 16 Nov | Living Lab recipes are the adaptation dev set; fix there first, then roll out |
| 16 Nov | A recipe staying A needs a 10% gain; oil and butter may be cut by a quarter, cheese not; salt gets low-sodium salt plus a quarter less |
| 16 Nov | Never swap or cut eggs, whole fruit, oily fish, whole nuts and seeds, avocado; poultry swaps with meat only; no olive-oil advice |
| 16 Nov | Additions (served-with) are judged per serving, not by Nutri-Score, and carry no grade |
| 17-18 Nov | Sustainability adaptation: protected foods apply; change only ingredients with >= 20% of recipe CO2e; nutrition is not judged at all (18 Nov: separate facet), allergen and diet gate stays; butter to margarine only; no grain swaps, no feta to cottage cheese; title-food swaps allowed and flagged; cod to hake on hold; no red meat to lentils; cheese-type swaps allowed |
| Standing | Resource limits in the local `AGENTS.md` (not in Git); local generative LLMs prohibited; ask before paid batches and package changes |
