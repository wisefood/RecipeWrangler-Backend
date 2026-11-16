# Recipe adaptation

**12 November (afternoon) — safety gate, rule fixes, LLM stage, end-to-end check (heldfix worktree, uncommitted, not deployed):**
537 recipes: **441 adapted** (Irish 262, Hungarian 100, Slovenian 79), 96 no sensible adaptation; B and below 149/189, 70 up a
letter. New: a hard allergen/diet gate (`restriction_problems`: no added allergen, no vegan/vegetarian/pescatarian break, "possible"
counts as added); no same-weight swap from bone-in cuts; minced food stays minced; the wholemeal-bread target excludes flour mixes
and crispbread. LLM stage (`small_gain_llm.py`, hosted qwen/qwen3-32b only): picks a candidate, then copies every step word for word
except the changed foods (word-level diff validator), keeps database-style ingredient lines as the database name (the profiler
matches them exactly), and notes are written in code. 46 recipes have an LLM rewrite (r8); full run not started. End-to-end check
(`analysis/.../e2e_check.py`, `e2e_report.py`): original and final text both through the production profiling chain; 41 of 73
sampled recipes measured before the OpenRouter key ran out of credit: 30 pass every check (Hungarian 13/13, Slovenian 8/8,
Irish 9/20); 6 Irish pass the adaptation checks but the chain does not reproduce the saved baseline from the original text;
4 are pipeline noise (an unchanged line re-parsed differently between the two runs); 1 real fail (skimmed milk gain half of
claimed). Known text-to-profile gap: "half white, half wholemeal flour" (13 adapted recipes) is not profiled as written. Live endpoint on a temporary
127.0.0.1 port returns exactly the offline result for 9 recipes. 53 tests pass.

**12 November — first nutrition adaptation version (overnight, owner-directed):**
Results, method and limits.
537 screening-ready Living Lab recipes in native profiles: **445 adapted, 92 no sensible
healthier adaptation, 0 failed**; B and below 150/189, 72 recipes up a whole letter. One card
per adapted recipe (`cards.md`). Changes: amount reductions, curated swaps to other foods,
a loose similar-food search (any part of the same animal, skinless, poultry ham), lean-protein
swaps for fatty meats (flagged as changing the dish), extra vegetables (fallback), production
product/part swaps; the strongest single change wins, up to three combined only for a better
letter. Tiers per owner direction ("even a small change counts"): policy floors 174, better
letter 9, B-and-below ≥10% gain 47, small ≥5% gain 215; no grade drop. In code (heldfix
worktree, uncommitted, not deployed): `services/adaptation/small_gain.py` + `small_gain_rules.py`,
endpoint `POST /api/v1/recipes/{id}/adapt/small-gain` (`{region}`), deterministic, unsaved, no
LLM; 14 tests; live runs match the offline results. `/adapt/enhance` and its gates are
unchanged. No writes, $0. Next: LLM culinary choice between candidates (shown their
before/after tables) and an instruction rewrite, pending a cost cap. Baseline findings:
onion FVL undercount, 74 matcher-pitfall lines (`baseline-repair-list.json`), and baselines
that match the wrong part (e.g. "loin of lamb" matched to leg).

**Current phase priority (user clarification, 10 November):** Adaptation testing
focuses on Curated Irish Recipes, Curated Hungarian Recipes and Curated Slovenian
Recipes from the Living Labs, using the corresponding regional nutrition profile.
Iterate on deterministic mechanisms directly; use ordinary HTTP to verify supported
complete paths. The existing successful chicken casserole belongs to the Irish
Living Lab collection. The eight-case hosted ledger remains unchanged; selecting
new Living Lab diagnostics does not authorize new paid cases.


**10 November repair continuation (supersedes earlier prototype gap descriptions):**
The meat-fat conjunction parser is corrected with conflict-preserving frozen-label
recovery. Candidate fallback now follows eligibility, tries ten targets, and expands
canonical graph identities through three bounded taxonomy depths (sixty proposals).
Search truncation remains explicit; comparison/restriction safeguards are retained.
The connected offline suite passes 1,344 tests with zero network attempts. Publisher
HTML establishes the dairy/beef extraction duplicates; dairy source and 150 g ricotta
baseline repairs are applied with rollback evidence. A streamed quantity-impact audit
found nine explicit-mass discrepancies in seven recipes / 28 regional profiles.
The repaired eight-request HTTP run still demonstrates only one distinct successful
recipe (chicken, three cached responses); several real-recipe successes remain pending.
Beef composition/source-text repair, the pork lean baseline conflict, and broader
corpus work remain unresolved. `--fresh` uses the existing approved eight-case ledger.
See the current prototype report for receipts and coverage limits.


All seven explicit-mass repair packages are now active: **28 live profile digests
verified**, with unchanged food selections, servings, unrelated mass estimates and
validated edible/drainage adjustments. Dairy source rollback and reapplication were
exercised; current backup: `source-review/dairy-applied-verified.json`.


Updated 10 November 2026. This is the single adaptation guide. [The working plan](PLAN.md) owns priorities and progress; [METHODS.md](../evaluation/METHODS.md) defines scoring.

## Product direction agreed with the user

The four facets are **nutrition, sustainability, monetary cost, and time**. Meaningful nutrient improvements within a Nutri-Score letter grade are supported by the local enhancement default, `meaningful_nutrient_v1`. A better letter is no longer required; meaningful target reductions and the eight-nutrient nonregression guards still apply. Thresholds are provisional engineering criteria; calibration and deployment remain pending.

Feasibility participants will use the overall platform and filters. They will not independently annotate ingredient matches or allergen detections. Internal evaluations must therefore check those stages and the complete user workflow before the study. No external expert-annotation campaign is a dependency.

## Current status and API boundary

**10 November — runnable first nutrition prototype:**
Run it locally.
The real chicken endpoint returned a complete card with three fresh LLM responses.
The bounded launcher passes a real HTTP check of all eight approved requests:
automatic/explicit chicken requests return that recipe's complete card using exact
genuine response reuse, with zero fresh calls and unchanged source/profile data.
Six other requests retain explicit refusals. Final connected offline suite: 1,330
passed with no network attempts. Provider failures and source duplication now have
specific reasons and plain-language explanations. The prototype disables fresh
generation and accepts only eight approved requests. It is not deployed or broad
coverage validation. The corpus refresh is separate from trying the working recipe.

**10 November — overnight prototype work:** Current evidence.
The general per-serving quantity bug is fixed. An exact database checkpoint
confirms 1,236 refreshed recipes / 4,944 regional profiles, zero mismatches and
unchanged reference panels. The connected offline suite passes 1,322 tests.
The eight-case fresh HTTP pilot has a separately approved $0.50 / 24-attempt
allowance, mandatory durable spending guards and an isolated approved-request
app; global LLM defaults and shared search infrastructure remain unchanged.
Graph projection is separately incomplete (197 pending at this checkpoint).

**Latest 9 November — cooking-adjusted recipe card demonstrated:**
Actual current-code result. The real chicken API now returns a complete
unsaved, review-required card: all 11 ingredients, unchanged masses/servings,
updated instructions and complete eight-nutrient input estimates. Two genuine
culinary responses were replayed against identical current inputs; a fresh
genuine editor call changed the long cooking step from 1 hour to 45 minutes.
The mechanism handles supported existing duration/temperature adjustments for
any eligible food; mocked full-flow checks cover chicken, duck and pork. Exact
patches and all required parameter changes are independently checked at editing
and card assembly. Extra ingredients/quantities/methods/media remain blocked.
The model's time proposal is not a cooking trial or safety approval; saved mass
and finished-dish nutrient uncertainty remain explicit. No deployment or recipe
save. 1,315 offline tests pass with zero network attempts. All four approved
hosted attempts are consumed; known successful cost $0.00376656.

The separate bulk refresh reached 1,067 recipes in activation receipts, with
1,235 packages prepared, then stopped at a raw-weight preservation guard. It is
incomplete and needs the per-serving amount case resolved before resumption.
The earlier hosted failure/checkpoints below predate the cooking-parameter fix.

**Earlier 9 November hosted result before cooking support:**
Current refresh and real-model evidence.
415 recipes / 1,660 regional profiles are refreshed and active; exact run IDs,
mirrors, totals, score and reference fingerprint verify without mismatches
(`checkpoint415.json`). The approved two-worker run is continuing inside a shared
2-core / 12-GiB / no-swap CPU-only cgroup, with the original two numerical threads
per pinned core. Four chicken regional calculations reproduce exactly. A general
refresh safeguard now preserves saved estimates where a new mass guess lacks
source evidence; source-backed quantities and edible-mass estimates stay explicit.
The full 7,743-recipe refresh remains incomplete. Real chicken screening now
passes all eight nutrient guards. Genuine hosted role/candidate assessment
returns both breast changes as `suitable_with_changes`, requesting cooking-time/
method adjustments. Those operations are not executable through the current raw-
part editor, so the API correctly returns `needs_evidence` with no final card.
This is now a culinary-plan limitation, distinct from the repaired matching and
native-component evidence. Two successful calls cost $0.00257712; the earlier
rate-limited attempt is retained, with no known charge. Three total attempts;
the approved cap is four / $0.05, and no further calls were made. Negative controls
still block/abstain. Graph projection has 145 pending jobs (44 currently with errors), including
missing exact compositions for off-catalogue estimates; database refresh and graph completion remain
separate. Earlier checkpoints below retain their historical scope.

**9 November refreshed-baseline/live-index checkpoint:**
Evidence and current bulk progress.
Owner-approved isolated recovery is complete. Thirteen recipes / 52 regional
profiles are refreshed and active on matcher v36; the full 7,743-recipe / 30,972-
profile refresh is still running in bounded, resumable chunks. All 12 original
smoke profiles reproduce exactly with the bounded cache. The real chicken API
now screens two same-table breast substitutions, with complete eight-nutrient
conditional before/after totals: the leading candidate changes saturated fat
6.5073 to 1.1073 g/serving, energy 443.6453 to 338.6453 kcal/serving, and protein
51.7311 to 63.4811 g/serving. This is a conditional saved-input calculation;
the 1,000 g edible-mass assumption remains unverified. No-change and vegan
controls are blocked; strict unverified-input control remains needs_evidence.
The owner approved a maximum $0.05/three-call hosted test. Its first role-assessment
request was rejected with a rate-limit error, with no retry or fallback. No
culinary approval, edited instructions or complete real card is claimed. Actual
charge is unavailable; the failed attempt retains its maximum-cost reservation.
Native EFSA carbohydrate component codes now support same-dataset/release
comparisons without asserting the wrapper's by-difference definition. Unknown
provenance remains unknown. Current production code passes 1,282 connected offline
tests; seven additional controller/call-budget tests pass. No deployment or
reference-label changes. Earlier checkpoints below retain their historical scope.


**Latest 9 November implementation — general food-part adaptation:**
Implementation, checks and saved-recipe boundary.
Enhancement now searches all different supported raw parts of the same product/
biological source in the pinned PostgreSQL release. Discovery, cost/emissions
lookup, comparison requirements and exact editing have no chicken dispatch branch.
Singular/plural/multiword parts, count/volume-to-saved-mass display and separate
derived stock products use the same generic path. Internal edit identities are
explicit; no food/cut defaults are inferred. Required evidence and numerical
guards remain. New regressions cover eight animal/plant families and a non-chicken
HTTP card with mocked LLMs; **914 connected tests pass**. The saved chicken still needs composition evidence;
no generated real draft, persistent refresh or deployment is claimed. Cooking
method changes remain outside the executable raw-part operation.

**Latest 9 November generalization investigation:** code, saved-data and synthetic probes.
General part/method graph helpers existed in `matching_variants.py` (removed 13 Nov, unused) but were
not connected to enhancement. Six animal and two plant synthetic cases pass the
generic part comparison; singular cases and chicken plurals assemble mocked-model
cards, while seven non-chicken plurals expose the generic editor limitation.
The 99 saved recipes contain only 9/293 selected records with stated text part
facts; wider catalogue labels offer leads, not validated coverage. Replace the
chicken discovery/editor/verified-policy special cases with one scoped food-part
operation. Method changes need a separate yield/retention/media and step-edit
operation; raw/cooked composition swaps cannot supply that calculation. This is
an investigation, with no application changes, models, live reads or deployment.

**Latest 9 November selected-branch follow-up:** implementation and chicken replay.
Adaptation now scopes complete first-listed foods to their original source prefix,
retains full provenance/quote spans and removes the unused alternative through the
exact editor allowlist. Edit intent can use stated occurrence cut facts while
comparison still requires independent table support. Separate chicken stock no
longer counts as another meat occurrence. Counted chicken-cut replacements display
the saved gram mass, marked approximate for proxies. **503 connected tests pass**,
including a complete synthetic card with mocked LLMs. The saved chicken now clears
identity/instruction preflight but returns `needs_evidence` on composition cut/
skin/bone facts; no real generated draft, model call or deployment is claimed.

**Latest 9 November matcher follow-up:** first-listed selection fixed locally.
Nutrition matching and its candidate gate now select the same first option,
preserve its own qualifiers and abstain instead of silently selecting a later
food. Runtime label v33; no stored refresh or deployment. Adaptation still needs
explicit branch propagation into source/instruction evidence. The chicken's saved
baseline already selected thighs, so this change does not reclassify its old
quantity estimate or validate the complete draft.

**Latest 9 November follow-up — within-grade enhancement is now the local default.**
The owner authorized removing strict letter improvement as a requirement.
`meaningful_nutrient_v1` reuses the existing experimental floors: at least 10%
AND 50 kcal / 5 g sugar / 1 g saturated fat / 100 mg sodium reduction per serving,
no worsening grade, and all eight comparable nutrients without adverse changes
beyond rounding tolerances (0.1 kcal, 0.1 mg sodium, 0.001 g otherwise).
Explicit goals can target zero-point nutrients; an ordinary request with no goal
can discover a positive target without the old three-point threshold. Unsupported
or unavailable requested goals cannot silently select another nutrient.
The former strict policy and explicit-goal experiment remain internal comparison
versions. Legacy suggestions/simulation retain their separate behavior. The HTTP
request still cannot choose a policy. This code change does not resolve source,
quantity, restriction or analytical-definition gaps in the real pilot, and does
not establish a generated real-recipe success. Validation and limitations.

9 November saved-real-input orchestration pilot:
five real recipes, 30 actual HTTP-handler/orchestration requests with saved service,
candidate and source-recovery boundaries; all correctly return unsaved abstentions
without model calls. Chicken's experimental numerical pass does not clear source/
part evidence or ambiguous instruction edits. Stock also fails source/product
compatibility and its protein guard. Reviewed fills and parser-normalized fractions
no longer cause false source-mismatch blocks; 563 connected tests pass, including
15 new regressions. Native carbohydrate definitions remain unsupported for these
rows, so the experimental eight-nutrient final gate still cannot approve a fully
comparable real draft. No complete real edited draft, genuine generated end-to-end
success, paid call, persistent refresh or deployment is claimed.

9 November within-grade experiment:
the internal `within_grade_pilot_v1` candidate requires a 10% target reduction plus
an absolute per-serving minimum (50 kcal, 5 g sugar, 1 g saturated fat or 100 mg
sodium), no grade worsening and no nutrient regressions beyond explicit rounding
tolerances. An explicit supported nutrient goal is required. The candidate flows
through deterministic and final gates but is not enabled by default, exposed as
a public request option or deployed. Thresholds are provisional, not health
claims. Saved-case numerical evidence and synthetic HTTP draft checks remain
separate from the pending full real-recipe adaptation evaluation.

`POST /api/v1/recipes/{recipe_id}/adapt/enhance` exists in application code with modes `nutrition`, `cost`, `sustainability` and regions `EU`, `IE`, `HU`, `SI`. Request constraints can narrow the operation. Authoritative recipe contents and the active matching profile supply quantities and nutrition; public callers cannot attest trusted replacement nutrients or quantity evidence.

The enhancement path has complete synthetic flows with mocked model responses. Its latest consolidated status reports **no successful adapted card demonstrated on a real existing recipe**. Real requests reach discovery/comparison and abstain on evidence or candidate quality. Matching deployment does not prove this endpoint is deployed or ready to replace legacy suggestions/simulation routes.

Successful output is an unsaved, review-required recipe draft with provenance and `recipe_ready=false`. Official finished-dish Nutri-Score remains null on conditional input-basis estimates; a separately labelled input score estimate may be returned. On failure there is no selected candidate/card; diagnostics explain why.

The backend card contains title, servings, the full updated ingredient list and
mass references, instructions and recalculated nutrition. The response includes
the before/after comparison and change audit. The LLM editor confirms exact
code-allowed edits or abstains; it does not freely rewrite methods, times or
temperatures. Extra required cooking changes prevent acceptance. A nutrition
draft does not automatically refresh cost, timing or diet/allergen tags.

The prepared-rice pilot (raw rice -> ready-rice pouch, rice-only yield/product evidence,
rice fields in time extraction) was removed on 13 November 2026 as too specific; a backup is in
`backups/adaptation_untracked_20261113_112926.tar.gz`. A general ready-form swap is not built.
The public endpoint still does not expose a time mode.

A private hosted temporal-understanding and reviewed-overlap editor prototype
has now been tested with Qwen3-32B: six-call report.
It reuses the existing exact-patch and unsaved instruction-draft models, refuses
local model fallback, and validates quoted durations/step IDs/dependency structure.
One controlled Focaccia wording change succeeded using a separately source-reviewed
plan; model extraction remained unreliable on the four corrected real-recipe
inputs. It is not an autonomous scheduler, public API mode or validated elapsed
time improvement. No rejected inference is promoted to an approved rewrite.

A subsequent 235B-total/22B-active comparison
was inconclusive: shared-host safeguards interrupted three of four submissions;
the only received response copied the schema instead of extracting data.
The prompt's “Return the supplied JSON schema only” wording is ambiguous and
needs an explicit schema-instance contract before another comparison. No larger-
model rewrite ran, and this supplies no evidence for changing production models.
Reported received cost was $0.00060586; three interrupted bills remain unknown.

The user then requested prompt work and the existing **Llama 3.3 70B** baseline.
Prompt-v2/Llama report:
the extraction and editor now explicitly request schema-conforming **field values**,
with clearer mixed-timer/state/offset rules. Five calls completed with usage,
reported $0.00203612. Two of four extractions passed structure and narrow rice
checks; source review still found attention/dependency errors. Two were rejected
for mixed timers. The reviewed editor repeated patches and hit its token limit;
no new draft was accepted. **579 connected regressions pass**, including schema-
echo rejection and interrupted-call reservation/no-retry guards. These results
support using the existing hosted Llama family as a development baseline, not
an autonomous scheduler or production model change; prompt and model both changed.

The subsequent source-fragment and renderer fix
now gives Llama fixed source fragments to label, derives durations in code and
keeps mixed/unknown phases untimed. Focaccia and curry passed the revised hosted
extraction checks. The final rice schema removes model-written durations entirely;
that reduced schema is verified offline, not yet with another hosted call.
Reviewed overlap wording is now rendered deterministically with no model call,
retaining review/resource/dependency gates. A real accepted Focaccia extraction
plus its separate reviewed overlap now yields one exact unsaved draft; no numeric
saving or full scheduling approval. **594 connected regressions pass**. Exactly
five paid calls across revisions cost $0.00169230, including rejected attempts;
the final renderer did not promote the prior short-selector refusal to approval.

The next overlap discovery and fit-check enhancement
automatically finds candidate baking/prep overlaps, then requires a review bound
to the exact recipe, inferred graph and candidate. One-cook resource/attention,
readiness and complete timing scope must be confirmed. Only complete unqualified
prep ranges fitting the shortest reviewed baking window yield a draft; unknown
times, conflicting resources and existing parallelism block that path. Window
slack is not a claimed meal saving. The renderer now handles nonadjacent work
with an explicit baking-step reference and revalidates review/resource facts.
**635 connected regressions pass**, including 72 temporal cases. Four cached
recipes and three synthetic controls were checked with no model calls. Focaccia's
candidate is discovered, but its strict finish-inside-window claim correctly
remains `needs_evidence`; the earlier reviewed start-only draft is narrower.

The other proposed time modality is scheduling independent actions. It preserves
ingredients, cooking methods and required durations, but can overlap unattended
cooking with independent preparation. Recipe2Plan's annotated planning benchmark
is a useful starting resource; it does not extract validated dependencies from our
recipe prose. A first pilot would use reviewed offline action records, one cook,
known equipment and a deterministic feasibility check. Existing “meanwhile” work
must be part of the baseline; no saving is inferred by first treating the recipe
as fully serial. Unknown timings, attention or dependencies cause abstention.
The subsequent reviewed whole-meal prototype and fresh evaluation
are implemented privately, without API integration or a new graph.
`execution_facts.py` prefills quote-linked equipment/state/window proposals and
an unconfirmed review template. `time_schedule.py` schedules exact, completely
reviewed action times with one cook, unit equipment, state dependencies and safe
passive completion/handoffs. Existing overlap and exact finish offsets must be
preserved; a missing baseline never yields a saving claim. Greedy scheduling is
not an optimality proof. Unknown/ranged/qualified times cause abstention.

Five fresh, frozen hosted cases used Llama 3.3 70B for **$0.00153474** total.
Original strict validation passed 1/5; four handling/passive conflicts were
rejected. Conservative offline replay now retains those conflicting phases as
unknown and untimed, with recorded uncertainty; all five remain unapproved and
return `needs_evidence` for whole-meal scheduling. The source audit also fixed
storage deadlines being treated as action durations, false state objects and
an explicit cooked-rice contradiction flag. This is not five semantic successes
or a real-recipe time-saving demonstration. Synthetic reviewed controls establish
conditional 42→30 and 42→35 minute schedules, shared-resource no-improvement
outcomes and missing-time abstention. **675 connected regressions pass**,
including 40 new execution-fact/scheduling tests. One validation attempt stopped
on host swap activity; after stable measurements, the final run passed under
1 CPU/512 MiB/no-swap limits. All outputs remain `recipe_ready=false`.
Source-fact preparation reduces transcription, but semantic review and complete
source timing are still necessary; no automatic approval or generic method
replacement is claimed. Private extraction, candidate discovery, single-window
fit checks and reviewed rendering remain available under their narrower gates.

The accepted checkpoint and final suggestion format are logged in
[evaluation/adaptation/README.md](../evaluation/adaptation/README.md):
an actionable suggestion, revised steps and a short explanation, with original
steps available for comparison. Display estimated minutes saved only when the
complete baseline/proposed schedule supports them. Whole-meal output is currently
a timeline; its prose renderer remains pending. The reviewed overlap branch uses
deterministic wording. No LLM whole-meal verbalizer or public integration was
enabled by this presentation agreement.

Cooking-appliance changes were subsequently investigated, not enabled:
oven→air-fryer evidence and design.
The proposed separate operation preserves ingredient input masses and matches
product/state/cut, batch load, preparation and appliance before allowing any
temperature/time patch. Manufacturer timings cannot be transferred from frozen
products to fresh ingredients, and a component saving does not establish an
elapsed-meal improvement.

## First readiness fixes — 5 November 2026

The audit follow-up (archived locally) records the first implementation batch. Approval now uses `nutrition_objective_approval_v2` with `conservative_no_regression_v1`: energy, sugar, saturated fat and sodium cannot increase per serving, and protein/fibre cannot decrease, except for floating-point tolerance. This is a conservative development policy, not a calibrated trade-off threshold. Strict target-point and letter improvement still applies in nutrition mode.

Released composition loading now retains exact Open Food Facts ingredient diet properties with source hashes and property roots. Lexical allergen findings remain inferred; they do not establish absence or cross-contact safety. Unchanged source ingredient text and final title/ingredients/instructions are checked for restriction conflicts. Allergy-restricted requests still need genuine absence evidence and may abstain.

Same-product nutrition edits remove supported parenthetical/comma claims and abstain on unhandled or conflicting qualifiers. Sustainability targeting and approval use the source-pinned AGRIBALYSE reference policy, retaining full records, market, scope and source hashes; missing/incompatible evidence causes abstention with no legacy factor fallback in enhancement. Its current supported environmental scope is the FR reference scenario, not a localized footprint for each requested region.

The final offline connected selection passed **778 tests** under monitored limits. These changes have not been demonstrated on fresh live recipes or deployed; they do not complete the formal evaluation, judge calibration, candidate fallback or within-grade policy work.

## Candidate search fixes — 5 November 2026

The search follow-up (archived locally) adds deterministic final-policy and text-edit preflights before the ten-candidate limit. Known nutrient/grade failures and unsupported exact edits cannot occupy the shortlist. Screening can move to the next ranked ingredient under the existing three-target limit, without inference. Final approval and text checks still repeat after culinary assessment.

The user retained the hard **three model calls per request** budget. Dispatch attempts are counted before the transport call, including failed attempts. Local editor refusals can fall through to an already assessed candidate without an extra call; after a paid editor refusal consumes the third call, no retry occurs. A fresh ingredient's role/culinary assessment plus editing needs three calls, so it cannot start after the first target has spent calls. Requests expose that budget limitation instead of claiming infeasibility. Provider/schema failures stop the request, and stale-baseline checks remain mandatory.

Responses retain per-candidate objective preflight, final objective and instruction decisions, plus `model_calls`, `model_call_limit=3` and `search_policy_version`. These are dispatch diagnostics, not complete provider/cost receipts. The connected offline selection passed **784 tests**; live yield has not been remeasured.

## Existing controlled pipeline

1. Pin the active recipe/profile/run and composition release; bind occurrence IDs and recipe text revision.
2. Select one objective and rank ingredient contributors. A whole-recipe Nutri-Score factor is not an ingredient-owned number of points.
3. Try up to three ingredient targets; discover same-identity part variants, graph substitutions, and same-product nutritional variants.
4. Resolve real released compositions and check identity, known facets, source/state/basis compatibility, quantities, nutrient definitions, completeness and dietary/allergen constraints.
5. Check final numerical policy and supported exact patches when their trusted contexts are available; rank eligible candidates and retain at most ten for contextual evaluation.
6. Use the model for ingredient roles and batch culinary suitability, retaining inferred status and reasons. Additional unvalidated operations cannot execute automatically.
7. Recompute the complete objective deterministically and select an eligible candidate.
8. Permit bounded instruction patches, validate them, recheck the baseline, and assemble an unsaved card and decision audit.

The successful path currently uses at most three model requests: roles, candidate assessment and instruction editing. Screening failures use none. The model cannot invent composition IDs, nutrient values, equivalent grams, yields or arbitrary recipe instructions.

## Supported operations and limitations

| Area | Existing behavior | Required extension |
|---|---|---|
| Same-food parts / raw substitutes | General source-pinned raw-part discovery, comparison and exact editing across supported animal/plant sources | More supported real cases, preserved property evidence and grounded source identities |
| Product nutritional variants | Low/reduced fat, skimmed, low/reduced sodium, no-added-salt, sugar qualifiers; numeric benefit required | Audit actual product coverage; a claim dictionary cannot supply a missing nutrient record |
| Nutrition approval | Meaningful per-serving nutrient reduction; same or better grade, all eight nutrient guards; no negative-point minimum | Threshold calibration, supported real-source definitions and broader objective/ingredient fallback |
| Cost | Replacement-specific price evidence; current minimum 10% reduction, same-category/nutrition guards | Validate sensitivity to price scope, missing contributions, and shared base-price proxies |
| Sustainability | Replacement-specific emissions evidence; current minimum 10% reduction and nutrition guards | Comparable factor scope/boundary, uncertainty, and whole-recipe completeness |
| Instructions | Generic ingredient/product/part text patches, plural/multiword parts, saved-mass display and selected-alternative removal | Broader occurrence-to-step links; required time/method/quantity edits as executable operations with dependencies |
| Cooking and quantities | Not executable in the new enhancement pipeline | Sourced edible yields, retention, cooking media, mass balance and instruction validation |
| Time | No enhancement mode | Source time model, parallel/serial actions, required cooking constraints and bounded method edits |

Product variants must preserve compatible identity and product form; removing a supported claim span cannot erase powder, concentrate, flavour, brand or processing distinctions. No-added-sugar is not zero sugar. The conditional estimate path can retain unknown optional qualifiers but must block known conflicts and missing required evidence.

## Existing real-data lessons

- Teriyaki salmon exhausts the selected sodium target. The active catalogue has ordinary teriyaki sauce but no supported low-sodium variant in the examined evidence. Older USDA-linked display strings are not released low-sodium compositions.
- Ingredient fallback reached salmon, but reviewed alternatives failed improvement/source/state checks. Current deterministic screening also falls through on known objective/text-edit failures. Fresh target assessment after paid context/editing remains constrained by the retained three-call budget.
- A lower-sodium sweet-soy catalogue pair exists, but the examined recipe's observed `kecap manis` identity remains unresolved. Its A grade blocked the former strict rule; the current default permits meaningful A→A benefit, while observed grounding still needs evidence.
- Scoped source-context repairs were activated for curry and soup across four regions. That is eight profiles and 124 verified canonical occurrences at that checkpoint, not a corpus-wide repair.
- The latest status records 631 focused tests passing. This is historical focused validation, not a new test run, full-suite result or real-recipe success rate.

Detailed evidence remains under `artifacts/adaptation/`. Superseded dated narratives are recoverable through the documentation inventory.

## Next design decisions

Calibrate the default within-grade policy's provisional benefit floors against supported baseline distributions. A candidate must improve a comparable nutrient objective by a meaningful amount, preserve hard dietary constraints, and expose changes in other nutrients, cost, emissions and time. The implemented absolute and relative floors exclude rounding-size savings; a lower Nutri-Score point total is optional supporting evidence. The current version permits only rounding-level adverse nutrient changes; material trade-off allowances remain separate future work.

Search several plausible objectives and ingredient targets under a bounded deterministic and inference budget. Preserve every rejection reason, deduplicate candidates, detect repeated failures and stale baselines, and stop when no supported improvement exists. Report success, valid abstention, avoidable abstention and unknown separately.

Keep equal stated grams only when the two inputs have a compatible declared basis. For method changes, choose either a comparable cooked-food route or a raw-input plus supported yield/retention route. Account for added/consumed oil, drained liquid, edible fractions and water; avoid double application of cooking adjustments. A method change must alter the relevant steps and dependent times, not just an ingredient label.

For the first real demonstrations, select numerically feasible cases across the curated sources before paying for contextual calls. Validate nutrition first, then cost and sustainability, then time. Expand coverage by evidence and source-backed aliases rather than by weakening contradiction checks. Retain the legacy routes until complete user scenarios pass the agreed evaluation gates.

## Instruction extraction for future time adaptation

The [RecipeBERT development probe](../evaluation/instructions/README.md) downloaded
the traditional ReciFine checkpoint and processed 68 inputs, including 36 steps from
six local recipes, on CPU without package changes. It recovered 20 of 21 selected
diagnostic mentions; these incomplete positive checks are not an accuracy estimate.
Food/action/tool/state/duration extraction is promising, but temperature boundaries,
storage-time interpretation, repetition and action dependencies need additional work.
Exact reproduction of published predictions remains unresolved (242/283 token labels
agree; 4/20 sequences identical); a casing follow-up stopped on the host-swapping
safeguard. No production integration or automatic recipe editing was enabled.
The checkpoint declares CC BY-NC 4.0. Next establish independent span/relation
evaluation and reproducible preprocessing before connecting extraction to scheduling.

## Code map

All modules below are under `src/recipe_wrangler/services/adaptation/`:

- `enhancement.py`, `enhancement_screening.py`, `enhancement_pipeline.py`: targeting, discovery and orchestration.
- `candidate_selection.py`, `production_candidates.py`, `nutritional_variants.py`: released candidate discovery and screening.
- `comparison.py` (incl. input basis and restriction evidence), `comparison_store.py`, `quantity_declarations.py`: snapshots, evidence and numerical comparison.
- `contextual_assessment.py`, `instruction_editor.py`: constrained model steps.
- `objective_approval.py`, `impact_objectives.py`: final deterministic approval.
- `recipe_response.py`, `enhancement_response.py`, `router.py`, `schemas.py`: card, audit and API contract.
- `service.py`: legacy production policy and routes' supporting behavior.
