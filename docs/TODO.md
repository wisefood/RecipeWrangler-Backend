# TODO

Open tasks, decisions waiting for the owner and standing rules. Work already done, with dates, is in [STATUS.md](STATUS.md).

## Objective

Make analysis and search reliable and measurable, then extend recipe adaptation across nutrition, sustainability, cost and time. Evaluate individual stages and complete user workflows, keeping results explainable and coverage visible. The feasibility study uses the complete platform.

| Stage | Done when | Status |
|---|---|---|
| Baseline | Reproducible initial measurements with known limits | Done |
| Strengthen analysis (parsing, weights, matching, nutrition, allergens) | Serious known failures fixed; accuracy and coverage remeasured | Development evaluation done ([evaluation/](../evaluation/README.md)); stored data behind code |
| Strengthen search | Restrictions preserved, relevant results, acceptable speed | Metrics agreed; not run |
| Strengthen adaptation | Real nutrition, cost, sustainability and time changes evaluated on real recipes | Living Lab dev set: nutrition frozen (19 Nov), sustainability done (18 Nov); cost and time in progress; rollout to all recipes pending |
| Platform check | Feedback and user scenarios replayed on the release candidate | Not started |

## Next

- LLM culinary choice between candidates and instruction rewrite (needs a cost cap); fix the baseline matcher pitfalls found (wrong part, e.g. "loin of lamb" -> leg; onion vegetable-share undercount); finish the held 109.
- Nutrition adaptation (frozen 19 Nov): commits (owner go; 4 themed plus an optional fifth for matching/data); hosted-parser check of the LoSalt wording (cents, not run); 33 stale baselines left unmarked; remove unused `llm-runs/reapplied-2026-11-19*` directories; stop the shared vLLM when the owner says.
- Sustainability: mapping section B (fish) not approved; UI wording, `/adapt/enhance` wiring and commit wait for the owner. Live engine now loads all 537 Living Lab recipes and diet/allergen cases are checked through the API ([STATUS.md](STATUS.md), 20 Nov night); no case found where an allergen changes the swap (margarine with soy is not flagged, check gap).
- Adaptation API: cost is live behind the single `GET /adaptations` (20 Nov, local, uncommitted; commit with the cost pricing files; cost-table re-import needs owner approval); live nutrition engine is 18 to 61 s per recipe without a stored row (was 192 s; remaining time is candidate name matching, see [STATUS.md](STATUS.md) 20 Nov evening); precompute rows for all recipes; PR comment correcting the removed routes; on the platform: nutrition row load, the `matching_compositions_identity` index (alembic `20261120_0012`) and the `recipes_v7` build. Saved profiles of 6 recipes still carry parser v8 against the v9 release annotations (comparison ignores the label); a release-wide reprofile refreshes them.
- Time adaptation: review of the 49 Living Lab candidates; word-overlap independence still noisy.

## Open before release

1. PR #13 (`adaptation-and-matching-fixes`) is open and not merged or deployed. The running API container still uses the baked image `matching-2026-11-04-v31` and reports matching release `2026-10-27-v11`; confirm the served version before any release step. Before deploying, run the `pipeline_static_data` importer for the three new entries (see [OPERATIONS.md](OPERATIONS.md#runtime-reference-data-in-postgres)).
2. Stored data is behind the code: nutrition re-profile of 30,972 profiles (~11 h), allergen/diet refresh, FoodHero graph re-import, duplicated MyPlate lines.
3. Fresh raw-text section extraction and native-language Hungarian/Slovenian input are untested.
4. The frontend sends region `US` for non-Irish users, which the backend treats as Ireland.

5. **Known limit, adaptation evidence:** 74% of composition records state no preparation state. The comparison treats an unstated state as missing evidence except for "as sold" families (dairy, oils, sweeteners; on 16 Nov extended to produce, nuts, herbs and condiments as a test). Grains, pulses and meat still need a stated state, so many search candidates stay `needs_evidence`. The fix is annotating the records, a release-level change.
6. **Known limit, curated table:** with the table hidden, search finds the same target in 64 of 130 recipes (49%) and another swap on the line in 11 more; the table stays for cross-type swaps (butter to oil, pastry to filo), state changes (canned to cooked) and amount factors.
7. **Known limit, scorer:** original Nutri-Score algorithm (sodium, saturated fat in g, protein rule at 11 points, no cheese exception), not the 2023 update.

8. **Composition release task, deduplication:** some national databases are imported twice under two names (`ciqual` / Anses CIQUAL 2025, `fineli` / Fineli 2025, `cofid` / CoFID); keep the newest version. Within the EU collection (33 databases) one food can appear in many tables (chicken breast in 13). Owner, 16 Nov: keep the Irish, Hungarian and Slovenian tables, deduplicate the general EU collection; matching uses these records, so it needs a new release and validation. Adaptation already searches the regional table first and falls back to EU (EU mode uses EU only).
9. **TODO, combined adaptation:** cost mode runs without the nutrition guards (grade regression and per-nutrient regression) since 19 Nov, so the facets can be built and measured separately. Its nutrient deltas are still reported. When nutrition and cost enhancement are combined, set `COST_NUTRITION_GUARDS = True` in `objective_approval.py` and decide the trade-off rule. Cost still needs complete comparable score nutrients to be approved.

## Prepared locally, not applied or deployed

- Pending local allergen-contract rollout
- Pending source-join validation
- Pending filter/planner rollout
- Pending recipe allergen edit contract
- Pending allergen classification v2 — 31 October 2026
- Pending FoodOn link policy v2 — 31 October 2026
- Read-only reconciliation and propagation prerequisites — 31 October 2026
- Verified prerequisites and cache retry — local, 31 October 2026
- Prepared composition/reconciliation rollout — 1 November 2026
- Source-complete nutrition reprofile package
- Transactional source reprofile primitives
- Read-only source reprofile operator preflight

Details and gates are in the local history (`analysis/docs-history-2026-11-12/docs/OPERATIONS.md`).

## Corpus audit (planned)

Work from a versioned read-only snapshot with source and release hashes. Count active and disabled recipes, duplicate IDs and formulations, empty names and instructions, ingredient and measurement length mismatches, unresolved source alignment, missing servings and duration, instruction-only foods and missing source URLs, by source and region. For each nutrient, separate missing source value, unresolved match, unsupported unit or basis, zero or invalid weight, known source anomaly and unsupported total; rank missing contributions by occurrence frequency and nutrient impact; audit composition units, nutrient definitions, trace markers and duplicate releases. Outliers are review candidates: keep source and pre-fix values, exclude only with a documented reason, and show sensitivity with and without flagged references. Do not delete a recipe or clip calories because of an arbitrary per-serving cutoff.

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
- Update [STATUS.md](STATUS.md) after each working session with a dated entry, and this file when a task is added or finished; do not add dated handoff files.
