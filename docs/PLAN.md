# Working plan

Last updated 12 November 2026. Update this file and the relevant topic guide after each working session; do not add dated handoff files. The full plan history up to 12 November is kept locally in `analysis/docs-history-2026-11-12/`.

## Objective

Make analysis and search reliable and measurable, then extend recipe adaptation across nutrition, sustainability, cost and time. Evaluate individual stages and complete user workflows, keeping results explainable and coverage visible. The feasibility study uses the complete platform.

| Stage | Done when | Status |
|---|---|---|
| Baseline | Reproducible initial measurements with known limits | Done |
| Strengthen analysis (parsing, weights, matching, nutrition, allergens) | Serious known failures fixed; accuracy and coverage remeasured | Development evaluation done ([evaluation/](../evaluation/README.md)); stored data behind code |
| Strengthen search | Restrictions preserved, relevant results, acceptable speed | Metrics agreed; not run |
| Strengthen adaptation | Real nutrition, cost, sustainability and time changes evaluated on real recipes | Nutrition v1 on Living Lab recipes; others pending |
| Platform check | Feedback and user scenarios replayed on the release candidate | Not started |

## Current focus

Nutrition adaptation on the Living Lab recipes (Curated Irish, Hungarian, Slovenian), each in its native regional profile.

- **12 Nov, nutrition adaptation v1:** 537 screening-ready recipes, 445 adapted, 92 with no sensible healthier change, 0 failed. Deterministic endpoint `POST /api/v1/recipes/{id}/adapt/small-gain`, no LLM. In the `held-baseline-repair` worktree, uncommitted. `/adapt/enhance` unchanged.
- **12 Nov (afternoon):** allergen/diet safety gate, bone-in and mince rule fixes: 441 adapted. LLM stage (hosted qwen3-32b) validated on 46 recipes. End-to-end profiling check of original vs final text: 30 of 41 measured pass, 6 baseline-reproduction only, 4 pipeline noise, 1 fail. OpenRouter key near its $20 limit; full LLM run and the rest of the end-to-end check wait for credit. Next: fix the half-wholemeal-flour line, owner decisions on named dishes and filo, then commit on owner's go-ahead.
- **12 Nov (evening): full LLM run** on local vLLM (owner exception, see AGENTS.md): 402 of 441 adapted recipes have the LLM title/steps/lines (Irish 233, Hungarian 93, Slovenian 76) after the 16 validation failures were retried with fixed checks; 39 the model rejected every candidate and keep the deterministic card. GPU peaked at 79 C. Next: file inventory, merge into one `/adapt/enhance` endpoint, themed PRs, then sustainability.
- **12 Nov (night): no invisible cuts.** Owner: halving 1 g of salt or a few grams of oil is not a suggestion a user would accept. Amount cuts of salt, oil/fat, cheese, cream and cured meat removed (lighter-version swaps stay; sugar, sauce, jam, chocolate, nut butter and stock-cube cuts stay). 336 of 537 adapted (Irish 207, Hungarian 80, Slovenian 49), 44 up a letter; the LLM accepted 246 (Irish 155, Hungarian 64, Slovenian 27) and judged 88 not worth making, mostly the extra-vegetable fallback. Open: cheese swaps are rare (no reduced-fat feta etc. in the data), owner to decide on a different cheese type; baselines miss cooking water/stock and do not count mushrooms/onions as vegetables (inflates B-and-below).
- **13 Nov (night, unattended):** graph + FoodOn route added to the nutrition screen; shared culinary-role filter (`food_roles.py`: same job in the dish, no prepared dishes, drinks or baby food as targets, no produce-for-produce, compound records excluded) -> nutrition 363 adapted (Irish 218, Hungarian 84, Slovenian 61), 63 up a letter, 81 graph-route swaps (cream -> Greek yogurt, couscous/rice -> quinoa/barley, feta -> mozzarella); baseline recalculation extracted as `make_evaluator` for every facet. Generic facet engine (`analysis/facet_candidates.py`): sustainability 433 scored / 150 with a sensible candidate (reviewed AGRIBALYSE mapping, legacy CO2 factors were wrong for courgette, milk, stock, water); cost 286 scored / 73 with a candidate (catalogue prices only 152 base foods); time coverage measured (no model). Still pending: nutrition LLM re-run on local vLLM (owner go-ahead), then time understanding on local vLLM, mapping sign-off, repo inventory.
- **13 Nov (day):** existing cost mode repaired (profile-format break) and tightened with the shared role filter and visible-saving floors instead of a parallel engine: 48 Living Lab recipes with a cost suggestion; end-to-end example Grilled salmon cutlets -> trout (EUR 3.80 -> 1.91/serving). Sustainability end-to-end example: Roast lamb -> roast turkey (pipeline CO2 1.82 -> 0.23 kg/serving). Next: fold sustainability into the existing sustainability mode the same way, then the single /adapt/enhance merge.
- **13 Nov (time):** overlap discovery now runs code-only (`deterministic_understanding`) with any passive anchor (simmer/boil/rise/..., not just bake) and an opt-in start-only draft with no minutes claim: 49 Living Lab recipes with a candidate (Irish 43, Hungarian 6), word-overlap independence still noisy, review still required. Prepared-rice pilot removed as too specific (module, rice fields in time extraction, rice scripts; backup `backups/adaptation_untracked_20261113_112926.tar.gz`); time-extraction prompt now `source_fragments_v6`. Cleanup: five adaptation `*_models.py` merged into `services/adaptation/models.py`; unused `matching_variants.py`, the nutrition prototype launcher and four one-off pilot scripts deleted (backup `backups/adaptation_untracked_20261113_115126.tar.gz`).
- **13 Nov (UI benefit facet):** every LLM-stage result now carries a separate `benefit` block (facet, free-text `tip`, code-measured `metric`, `swap`, `notes`) for nutrition, cost, sustainability and time; the model writes the sentence with a `{benefit}`/`{minutes}` placeholder and no digits, code fills the measured number; without a model tip a code sentence is used. First time example with one hosted call: Roast pork with ratatouille, 5-8 min saved. Existing nutrition/cost/sustainability results need the LLM re-run to get model tips.
- **13 Nov (repo cleanup):** `matching/experimental/` (the live `new` matcher) merged into `matching/` and the folder removed; `matching/identity.py` shim and `facet_retrieval.py`, `adaptation/input_basis.py`, `restriction_evidence.py` folded into their users; 38 unreferenced one-off scripts moved to gitignored `analysis/archived-scripts-2026-11-13/`; `test_experimental_*` renamed `test_matching_*`. Backup `backups/pre_cleanup_20261113_125948.tar.gz`. Nothing committed.
- **13 Nov (push audit):** dead experimental matching chain (`matcher`, `foodon`, `foodon_policy`, `identity_retrieval`, `compounds`, `interfaces`, `wisefood/`) and 7 tests of gitignored analysis runners archived under `analysis/archived-*-2026-11-13/`; evaluation trimmed to one README + data/results per area (adaptation rewritten for the four modes, sustainability reports combined, nutrition repairs folded into its README); root `Fineli_Rel20` zip moved to `data/`. 50 tests still fail (pre-existing).
- **13 Nov (scripts):** scripts are not in the API image (`.dockerignore`); kept only data-pipeline/operational tools and evaluation scorers. 46 internal check/audit/pilot scripts and the tests that only tested them archived under `analysis/archived-*-2026-11-13/`; evaluation dataset CSVs now tracked (matching gold stays local, partner data); cost figures tracked.
- **13 Nov (tests green):** all 4,959 tests pass. Root causes: tests inherited the developer `.env` (`NUTRITION_MATCHER_MODE=new`; now pinned to legacy in `tests/conftest.py`); stale fixtures (catalogue rows without `annotation`, unversioned suitability claims) and stale expectations (1 g salt/pepper policy, gluten-free as allergen-absence filter, parser `max_tokens`). Two real matcher fixes in `contextual_parser.py`: stock cubes/powder/paste and scoped stock phrases no longer get the liquid `broth_or_stock` facet; a suppressed cut word ("breast fillet") is no longer flagged as unexplained text. Both change live matching only for newly computed profiles.
- **17 Nov (sustainability rework, Living Lab dev set):** CO2e adaptation rebuilt on the nutrition mechanisms (`sustainability_adaptation.py`, `sustainability_swap_rules.py`): 108 adapted, no nutrition judging (cod to hake indicative, bone-in meat with edible-yield factor, tuna to sardines, prawns to mussels, half-lentil swaps in stews and curries, poultry ham added), 497 scorable, CIQUAL code join first then reviewed name rules (audited: 1.4% of CO2e questionable), 95% of the saving from red meat to poultry; AGRIBALYSE first, CONCITO fallback for totals only; diagnosis by >= 20% CO2e share, curated table plus related-food search, strict nutrition guard, protected foods. Mapping bugs fixed in the local copy (bell pepper as black pepper, stock as meat, "plain"/"cow's milk" record names). Details: [evaluation/sustainability](../evaluation/sustainability/README.md#adaptation-rework-17-november). Mapping sections A, C, D approved by the owner 18 Nov (B, fish, not approved). LLM choice + rewrite run 18 Nov (local vLLM, owner OK): full read of the rewrites found and fixed junk targets, half-lentil wording, wrong notes, database wording, leftover old foods; 106 adapted, 103 rewrites ok, 3 keep the code card. End-to-end check (30 recipes, 0 failures, 0 new allergens): 21 ok, 6 smaller gain, 3 not lower; meat, margarine, lentil swaps confirmed, fish/dairy/tin-count lines disagree because the pipeline uses other sustainability factors and misparses '2 tins'; AGRIBALYSE mapping applied to the pipeline in the working tree (uncommitted), parser fix for '2 tins of X, approx. Yg', red-to-red swaps allowed, no-amount lines skipped; final e2e 29 of 29 ok; all 105 adapted recipes have an LLM rewrite (last 3 via capped OpenRouter). UI wording, /adapt/enhance wiring and commit wait for the owner. Uncommitted.
- **Baselines:** 10-11 Nov repairs raised complete profiles from 357 to 458 of 576; 109 recipes still held. See [baseline repairs](../evaluation/nutrition/README.md#baseline-repairs).
- **Next:** LLM culinary choice between candidates and instruction rewrite (needs a cost cap); fix the baseline matcher pitfalls found (wrong part, e.g. "loin of lamb" -> leg; onion vegetable-share undercount); finish the held 109.

## Open before release

1. Nothing is committed or deployed since 5 October. The served API reports matching release `2026-10-27-v11`; confirm the served version before any release step.
2. Stored data is behind the code: nutrition re-profile of 30,972 profiles (~11 h), allergen/diet refresh, FoodHero graph re-import, duplicated MyPlate lines.
3. Fresh raw-text section extraction and native-language Hungarian/Slovenian input are untested.
4. The frontend sends region `US` for non-Irish users, which the backend treats as Ireland.

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
| Standing | Resource limits in the local `AGENTS.md` (not in Git); local generative LLMs prohibited; ask before paid batches and package changes |
| 17-18 Nov | Sustainability adaptation: protected foods apply; change only ingredients with >= 20% of recipe CO2e; nutrition is not judged at all (18 Nov: separate facet), allergen and diet gate stays; butter to margarine only; no grain swaps, no feta to cottage cheese; title-food swaps allowed and flagged; cod to hake on hold; no red meat to lentils; cheese-type swaps allowed |

## Discussion points

1. Persistent vegan/vegetarian profile selections as hard restrictions: confirm through the platform API contract.
2. Meaningful nutrition benefit: per-objective absolute/relative floors from the baseline, not one percentage.
3. Language scope: measure actual Irish, Hungarian and Slovenian query/source support.
4. Inference budget: present cases, calls and cost before any substantial paid batch.
5. Nutrient tolerances for matching and the 2% missing-mass rule are provisional.

## Feedback coverage

All 75 entries are in the [feedback register](feedback/RecipeWrangler_All_Feedback.md). "Resolved" in the source is not a current regression pass.

| Primary work | Feedback IDs |
|---|---|
| Search/filter/relevance/profile contract | R1-01, R1-10, R1-11, R1-14, R1-15, R1-19, R1-23; R2-04, R2-08, R2-24; PF-01 |
| Allergen safety | R1-17 |
| Quantities, parsing, matching, nutrition | R1-12, R1-13, R1-16, R1-18, R1-25; R2-02, R2-05, R2-07; PF-04, PF-06 to PF-10, PF-12 to PF-17 |
| Regional consistency | R1-04; R2-01, R2-27 |
| Corpus tags/instructions | R1-06; R2-03, R2-06, R2-16, R2-19, R2-25; PF-02, PF-03, PF-11 |
| Adaptation/cost | R1-26, R1-29; R2-14, R2-15, R2-18 |
| Transparency/display/servings | R1-09, R1-20, R1-21, R1-22, R1-24; R2-09, R2-10, R2-12; PF-05 |
| Input/import/availability | R1-27; R2-20, R2-21, R2-22, R2-23 |
| Localization and adjacent platform content | R1-02, R1-03, R1-05, R1-28, R1-30, R1-31; R2-26 |
| Positive/non-actionable observations | R1-07, R1-08; R2-11, R2-13, R2-17 |

Frontend localization, favourites/copy/scaling, composition-table browsing, spider-chart explanation and education content need explicit platform ownership outside this backend.

## Working rules

- Every repair gets a focused regression test and a before/after evaluation on the affected cases.
- Report coverage with accuracy: full denominators, unknowns, abstentions, regressions. Methods in [METHODS](../evaluation/METHODS.md).
- New run outputs go to local `analysis/<task>-<date>/`; results worth sharing are copied into `evaluation/<step>/`.
- Before deleting generated output, check code and doc references. Never delete databases, source files, benchmark labels or before/after nutrition evidence as cleanup without approval.
