# Handoff — Ingredient Parsing + Nutrition Matching (2026-09-21)

Continuation notes for next session. Written mid-session because the current
Claude Code session hit its monthly spend cap (resets 19:20 Athens time).

## 1. Parsing loop (per-dataset measurement/quantity parsing)

Standing method for every dataset: **parse (LLM) → scan (deterministic-quantity
cross-check, real code not fuzzy diff) → fix (prompt first, code/regex only if
prompt can't work) → targeted retest on fixed/failed cases → full sweep → 0
disagreements before calling a dataset done.**

Core file: `src/recipe_wrangler/tools/parse_recipe_tool.py`
- `parse_ingredient_lines()` — LLM parser, model `meta-llama/llama-3.3-70b-instruct`
  (OpenRouter or local vLLM).
- `_apply_deterministic_overrides()` / `_deterministic_quantity()` — self-contained
  deterministic re-check that catches/corrects simple-line arithmetic mistakes.
  Guarded by exclusion regexes (`_HAS_ALTERNATIVE_WITH_QTY_RE`, `_HAS_PAREN_WITH_DIGIT_RE`,
  `_PACKAGE_DASH_RANGE_RE`, `_MULTIPLY_COUNT_RE`, `_TRAILING_WEIGHT_RE`, `_HAS_PLUS_WORD_RE`)
  so the override never stomps a correct LLM answer.
- `_fold_accents()` / `_normalize_stacked_fractions()` / `_strip_control_chars()` —
  defenses against a real, reproducible LLM-level unicode corruption failure mode
  (accented letters, stacked superscript fractions) under longer multi-line context.
- `max_tokens=2000` added to all three `_parser_llm` branches (vllm/openrouter/groq)
  as a general robustness guard against runaway generations. Confirmed this does
  **not** actually bound generation time on the local vLLM tool-call path — root
  cause unresolved, see §4.

### Datasets processed this loop, status
| Dataset | Status | Notes |
|---|---|---|
| SuperValu | done, verified clean | |
| Irish Heart | done, verified clean | |
| SlovenianKitchen | done, verified clean | |
| SafeFood | done, verified clean | |
| MyPlate | done, verified clean | ran partly local vLLM, partly OpenRouter after budget hits |
| ESSRG | fixed directly (not a parser bug) | see §3 |
| HealthyFoods | **in progress**, see §5 | |

### Where parsed results currently live — IMPORTANT
All `*_parsed.json` outputs are sitting in the **session scratchpad**, not in
the repo or a durable data directory:
```
/tmp/claude-1001/-home-karvanitis-RecipeWrangler-Backend/ecc6192b-efa8-4370-82da-17f699b8b423/scratchpad/
```
Scratchpad is session-scoped and **not guaranteed to survive** past this
session. Naming convention was `{dataset}_v{N}_parsed.json` with a
`{dataset}_final_parsed.json` marking the verified-clean version once found
(e.g. `myplate_final_parsed.json`, `foodhero_final_parsed.json`,
`best_of_hungary_final_parsed.json`). **Not confirmed in this session whether
these final files were already written back into Neo4j** (the actual source
of truth) or only ever lived as scratch JSON — check this first before
assuming any dataset's fixes are actually live in the graph. If they were
already committed to Neo4j, the scratchpad copies are just an audit trail and
can be ignored/lost with no harm.

## 2. Nutrition matching investigation (this session's main work)

File: `src/recipe_wrangler/tools/nutrition_match.py`
(uncommitted — see §6)

`best_nutrition_match(name, source)` pipeline: curated-alias short-circuit →
`clean_query()` → ES hybrid (vector+BM25) retrieval via `_candidate_pools(source)`
(region-specific + EU pool compete together) → hard pre-filter
(`classes_compatible()` + `animal_kinds_compatible()` + `ingredient_forms_compatible()`)
→ weighted scoring (`_base_score`: 60% similarity + 30% BM25 + 10% retrieval-rank
+ up to 15% token-overlap + penalties/bonuses) → confidence label
(`curated`/`strong`/`weak`/`none`).

`food_class()` is **pure keyword/regex**, not ML — ordered list of patterns
(`_CLASS_PATTERNS`), first match wins, `"other"` fallback.
`classes_compatible()` is permissive by default: only ~60 explicit pairs in
`_HARD_INCOMPATIBLE` are rejected; `"other"` and `"condiment_sauce"` are
unconditionally compatible with everything.

### What was checked / how
Diff-based verification, repeated after every change:
1. Save a full-corpus baseline (`test_all_matches.py`, all 2343 unique
   ingredient names from the 8 refined datasets, `source='eu'`).
2. Make a code change.
3. Rerun, diff for (a) confidence-level regressions (strong/curated → weak/none)
   and (b) `none → strong` transitions specifically (a rescue heuristic
   creating a *new* confidently-wrong match doesn't show up as a level
   regression — advisor flagged this gap after it let `vegeta` silently match
   a biscuit).
4. Regional pools (irish/hungarian/slovenian) checked the same way via
   `test_regional_matches.py`.
5. `tests/test_nutrition_match.py` run after every batch (51 passed / 10
   subtests as of now).

Scratchpad harnesses: `test_all_matches.py`, `test_regional_matches.py`,
result files `nutrition_match_results.json`,
`nutrition_match_results_baseline.json`, `{region}_match_results.json`, and
the name lists `all_refined_ingredient_names.json` /
`{region}_ingredient_names.json` — all in the same scratchpad dir as §1.

### What was found and fixed
- **Root structural finding**: ~19% (1546/8148, later 1477/8148) of the EU
  composition table classifies as `"other"` — no keyword recognizes it.
  Roughly half genuine vocabulary gaps (fish species, herbs, leafy greens),
  half correctly-ambiguous composite products (drinks, baby formula, candy).
  Advisor's call: extend vocabulary, don't touch the permissive compatibility
  rule (`"other"` staying always-compatible is correct — narrowing it would
  create more harm than the gap it closes).
- Extended `_ANIMAL_KIND_PATTERNS` (bass, catfish, flounder, halibut, herring,
  mahi, pollock, snapper, sole, swordfish, tilapia, frog, hake, pike, pomfret,
  ling, coley, dentex, bloater, winkle, plaice, mullet, whiting, quail,
  pheasant, rabbit, venison, bison, + widened chicken/pork/beef/lamb/shellfish
  patterns) and the matching `animal_protein` class pattern.
- Extended `spice_herb`, `oil_fat`, `nut_seed`, `legume`, `grain_cereal`,
  `vegetable`, `leafy_green`, `dairy` class patterns with a frequency-ranked
  list of head-nouns pulled from the actual `"other"`-classified EU records
  (not guessed).
- Added `_HARD_INCOMPATIBLE` pairs `(dairy, legume)` and `(legume, grain_cereal)`.
  Tried `(dairy, grain_cereal)` too — reverted, it broke macaroni/oatmeal
  matches (comment left in code explaining why).
- Added a single-word prefix/suffix token-overlap rescue in `_base_score()`,
  gated on `q_class == cname_class and q_class != "other"` (an ungated version
  created false positives: pepperoni→"Chili pepper", vegeta→a biscuit).
- Added synonyms: scallion(s)→onion, jalapeno(s)→chilli.
- Added a cold-water/cold-milk form guard mirroring the existing hot-water one.
- Added two permanent coverage tests to `tests/test_nutrition_match.py`:
  every `animal_protein`-classifying word must map to an `animal_kind`
  (except an explicit `deliberately_ambiguous` set), and the reverse.

### Two regressions found + fixed this session (order-collision bug class)
Both caused by a word being added to one class's pattern while also being a
common prefix inside a *different* class's compound term, with the wrong
class checked earlier in `_CLASS_PATTERNS`' fixed iteration order:
1. **Bare `pepper`** (tried, reverted) — misclassified `"Pepper, cayenne,
   ground"` as vegetable instead of spice_herb (modifier comes *after* the
   word in that composition-table naming convention, so a before-only
   negative lookbehind guard couldn't catch it; vegetable checked before
   spice_herb). Fixed by using the narrower exact compound
   `chil[il]?\s*pepper` instead of bare `pepper`.
2. **Bare `soy`/`soya`** (found + fixed this session) — misclassified
   `"soy sauce"` as `legume` instead of `condiment_sauce` (legume checked
   before condiment_sauce), breaking the match for `"fish sauce or soy
   sauce"` (dropped strong→none). Fixed with a negative lookahead:
   `soy(?!\s*sauce)|soya(?!\s*sauce)`. Verified fixed directly with
   `food_class()`.

### Open at cutoff — root-caused, not yet fixed
`european blend salad greens` (was strong→`"Salad, green"`, now weak→`"European
sprat, raw"`). Root cause found:
- Retrieval actually ranks `"Salad, green"` best by embedding distance (0.173
  vs sprat's 0.327) — the correct candidate isn't lost at retrieval.
- `food_class("European sprat, raw")` → `"other"` (sprat isn't in the fish
  vocabulary — a real gap). `"other"` is unconditionally class-compatible
  with everything, so the gate doesn't block it.
- Query and candidate both contain the literal word `"european"`. BM25 gives
  sprat a large lexical boost off that single shared token (`lexical_score=8.1`),
  enough to overturn the 0.15 embedding-distance gap and win final scoring.

So this is **not** a classifier collision like pepper/soy — it's the lexical
(BM25) term of `_base_score()` over-crediting a geographic/descriptor word
("european") as if it were a meaningful head-noun match. Two independent
fixes possible: add `sprat` to the fish vocabulary (closes this one instance,
same whack-a-mole pattern as the rest of tonight), or down-weight/cap BM25
credit from tokens that aren't head nouns (fixes the general case). See §2b
below — advisor's recommendation is to not patch this one instance until the
`food_group` metadata question is settled, since a vocabulary-based fix here
would likely become dead code.

Full re-sweep done (tmux `eu_sweep_rerun`, log at `{scratchpad}/sweep_rerun.log`).
Confirms the soy/soya fix worked with 0 new fallout: down to **1 regression**
(`european blend salad greens`, the one above), **0 `none→strong`**
transitions. `nutrition_match_results.json` in scratchpad is now the
up-to-date result set — update `nutrition_match_results_baseline.json` to
match it once the salad-greens case is resolved.

## 2b. Architecture review — is the class-gate approach even right?

User's hypothesis: the regex class gate might be net noise, since dense
embeddings could already capture semantic class similarity, and a name-only
regex genuinely can't always infer class correctly. Asked `advisor` to
research the literature and review the pipeline design. Findings:

**This is a studied problem** — food composition table linking / ingredient
grounding. Relevant prior art:
- **FoodEx2** (EFSA) — the standardized EU food classification; CIQUAL
  (source of most `nutritional_ingredients_eu` records, `eu_id: "ciqual:..."`)
  has FoodEx2 mappings.
- **LanguaL** — faceted food description standard, older CIQUAL tables coded
  against it.
- **FoodOn** — was previously wired into this pipeline via Neo4j and removed
  for latency / fail-open reasons (see code comment at
  `nutrition_match.py:838-840`). That removal was correct for a
  request-time dependency; doesn't rule out using FoodOn/FoodEx2 **offline**
  to precompute a class per corpus record once, at build time.

**Key structural finding — the corpus already carries a curated category,
and the pipeline ignores it.** Every EU record has `metadata.food_group`
(checked: **100% coverage**, 0 missing across all 8148 records — e.g. sprat's
record has `food_group: "meat, egg and fish"`). Instead of reading this,
`food_class()` re-derives a class from the display *name* via regex — the
exact bug class behind pepper/soy/sprat.

Caveat: `food_group` is **not one clean taxonomy** — 155 distinct values,
clearly multiple source vocabularies mixed together (readable CIQUAL-style
labels like `"meat, egg and fish"` alongside cryptic 2-4 letter codes like
`DR`, `MAE`, `MCA`, `JA` from other source systems within the same
collection). Using it needs a small hand-written `food_group → internal
class` mapping **per source system**, not one dict. Still far cheaper and
more reliable than regex-over-name — this becomes a build-time job over the
corpus instead of a request-time regex, and the regex would then only need
to run on the free-text **query** side (recipe ingredient lines), where no
metadata exists and it's genuinely unavoidable.

**Ablation run to test the user's noise-vs-signal hypothesis directly**
(scratchpad `ablation.py`, tmux `ablation`, full 2343-name sweep, 3 variants
vs baseline — `ablation_baseline_current.json`, `ablation_no_class_gate.json`,
`ablation_no_animal_kind_gate.json`, `ablation_no_gates_at_all.json`):
- **`animal_kinds_compatible` gate removed**: 0 changes across all 2343
  names. Either fully redundant with `classes_compatible`, or this
  particular test set never exercises it — inconclusive, not proof it's
  dead weight. Needs testing with different/harder queries before touching it.
- **`classes_compatible` (food-class gate) removed**: 11 regressions, 12
  confidence-level "improvements". But manually checking the "improvements"
  shows most are confidently-wrong matches getting mislabeled strong, not
  real wins — e.g. `dry red beans` → `"Dry sausage"` (strong, wrong),
  `lemon pepper seasoning` → `"Pepper, capsicum, yellow, raw"` (strong,
  wrong), `great northern beans` → `"Northern pike, raw"` (strong, wrong —
  a fish). The 11 real regressions (`crushed tomatoes`, `great northern
  beans`, `shell pasta`, etc.) are genuine losses.

**Conclusion**: the class gate is doing real, net-positive work — the
noise-vs-signal hypothesis doesn't hold as-is. But the *mechanism* (regex
over candidate name) is the wrong implementation of a sound idea — the
`food_group` metadata already contains that signal in curated form on the
candidate side, so the gate's classification logic should move to a build-
time `food_group` mapping and stop trying to infer class from candidate text
at all. Query-side classification (recipe ingredient text, no metadata
available) still needs the regex approach.

**Scope call from advisor, given the spend cap**: don't start the
`food_group`-mapping refactor this session — it's a real architecture change
(new build-time step, per-source mapping tables, re-running the gate logic
end to end), not a quick patch. Treat tonight's ablation + `food_group`
coverage check as *deciding the right direction cheaply*, not as unblocking
work. Also: none of the matching refinement (tonight's fixes or this
refactor) is actually usable downstream until ingredient curation moves off
its current ~2% of corpus mass — profile recompute is deferred until then
(see `data/analysis/nutrition_curation/PLAN.md`).

## 3. ESSRG fixes (already done, verified)
Both issues were confirmed source-spreadsheet gaps, not parser bugs (checked
directly against `data/ESSRG/PLANEAT T442 MEAL DB LL ESSRG.xlsx` via
`pandas.read_excel`):
- Deleted empty record `ESSRG_126` ("Potato and white bean salad").
- Backfilled `quantity`/`weight_g`/`measurement` for fried-egg and
  minestrone-soup entries in `ESSRG_43`/`ESSRG_105`, recomputed `nutrition`
  blocks using the pipeline's own `calculate_nutrition()` from
  `scripts/prepare_essrg.py` (not reimplemented).
- File: `data/ESSRG/ESSRG_recipes_clean.json`. Backup:
  `ESSRG_recipes_clean.json.bak-preessrgfix-20260920_232404`.

## 4. Local vLLM setup (working, but unstable under load)
- Model: `casperhansen/llama-3.3-70b-instruct-awq`, served via
  `/home/karvanitis/vllm-serve/.venv/bin/vllm serve`.
- tmux session name used this session: `vllm_llama70b`, port 8005.
- Flags: `--gpu-memory-utilization 0.96 --max-model-len 12288 --max-num-seqs 1
  --enable-auto-tool-choice --tool-call-parser llama3_json --quantization awq_marlin`.
  `--tool-call-parser` **must** be `llama3_json`, not `hermes` (hermes is for
  Qwen — causes a parser error on Llama). `--max-num-seqs 1` forced by a
  KV-cache-size error at 2 (weights already use ~40GB of the 48GB card).
- Validated: output identical in quality to OpenRouter-hosted version (14/14
  regression matches).
- **Unresolved bug**: reproducible stuck-generation cascades — server pins at
  `Running: 1 reqs` indefinitely, no stop token, every subsequent queued
  request then times out client-side with zero server-side error. `max_tokens`
  cap did not fix this for the local tool-call path specifically (confirmed
  empirically). Recurred across ~4 restart cycles. Root cause not found
  (suspected LangChain/vLLM guided-decoding interaction under this specific
  AWQ quantization). Pragmatic workaround used: switch the affected dataset
  chunk to OpenRouter API when it recurs, switch back to local when
  budget-conscious.

## 5. HealthyFoods — COMPLETE

tmux session `healthyfoods_local5` finished on its own (2026-09-22, exact
time not logged) — **2189/2189, 2178 ok, 11 fail**, no crash, no cascade
stall, session exited cleanly. Failure breakdown unchanged from the
mid-run check: 8 genuinely empty ingredient sections in the source data
(not a parser bug), 3 local-vLLM timeouts (known issue, §4). Nothing
systemic, nothing new at the tail end of the run.

Final output: `{scratchpad}/healthyfoods_local5_progress.jsonl` (2189
lines, one JSON object per recipe — same scratchpad-is-ephemeral caveat as
every other dataset's `_parsed.json`, see §1's "IMPORTANT" note on where
parsed results live and the open question of whether they've been written
back to Neo4j yet).

**Not yet done, next steps per the standard loop** (§1):
1. Run the production bug-scan (deterministic quantity cross-check, not
   just the manual spot-check below) on all 2178 successful recipes.
2. Fix anything found via the standard prompt-first loop.
3. Verify clean via targeted retest + full resweep.
4. Then run it through the matching investigation like the other 7
   refined datasets (build ingredient name list, run against `source='eu'`
   + `source='healthyfoods'`-if-applicable, diff-check).
None of this has started yet — the spot-check below is a sanity check, not
a substitute for the real bug-scan.

**Spot-checked ~7 recipes / ~85 ingredient lines mid-run (not the full
production bug-scan, which needs completion first)** — parser quality looks
solid: fractions convert correctly (`1 ¾ cups`→1.75, `½-1 teaspoon`→0.75
midpoint), multi-pack totals correct (4×500g→2000g, 2×400g→800g), no-quantity
lines ("to taste") correctly left blank rather than inventing a number, no
unicode corruption seen. Two minor things found, neither urgent:
- **Redundant/duplicated ingredient names**: e.g. `"chickpea garbanzo
  garbanzos"` — looks like multiple name variants got concatenated instead
  of one being picked. Would hurt matching (garbled/duplicate tokens).
- **Inconsistent unit-string spelling** on otherwise-correct plain
  (non-compound) lines: same dataset mixes `"2 tablespoons olive oil"` →
  `"30.0 ml"` (converted) in one recipe against `"1 tablespoon parmesan"` →
  `"1.0 tablespoon"` (kept as-is) in another — same LLM, same rule
  ("preserve the original unit" per the prompt, `parse_recipe_tool.py`
  ~line 423), applied inconsistently. Not a wrong number either way
  (`ingredient_weight_tool.py` handles both `ml` and `tbsp` downstream
  fine, see below), just an instruction-following inconsistency, same
  category as the other documented LLM quirks (§1).

**Clarified during this spot-check, not a bug, just noting where the logic
actually lives**: `parse_recipe_tool.py` deliberately does NOT convert
tsp/tbsp/cup to grams itself (its prompt explicitly says to preserve the
original unit, except for compound `"X + Y"` lines that must be summed).
The real volume→weight conversion happens later, in
`ingredient_weight_tool.py`, which has both a flat ml table
(`teaspoon: 5.0`, `tablespoon: 15.0`, `cup: 240.0`) and ingredient-aware
density overrides on top of it (e.g. `"loose mixed salad cup" → 40g`,
`"shredded hard cheese cup" → 113g`, `"ice cubes cup" → 140g`) — more
accurate than a flat ml-to-g conversion at parse time would be.

Once complete: run the same production bug-scan used for every other
dataset, fix via the standard loop, verify clean, then run it through the
matching investigation like the other 7 refined datasets.

### 5a. Production bug-scan — done, one real systemic bug found and fixed

Ran the standard deterministic quantity cross-check (`full_numeric_audit2.py`
pattern from prior datasets, adapted as `{scratchpad}/healthyfoods_audit.py`)
against the raw source (`data/HealthyFoods/HealthyFood_recipes.json`) for all
2178 successfully-parsed recipes.

**Result: 22,601 ingredient lines checked, 496 flagged (2.2%).**

- **94 flags (19%) — a real, systemic bug, root-caused and fixed.** Unicode
  fraction-slash characters (`⁄`, U+2044) combined with superscript digits,
  in the **bare form with no leading whole number** (`"¹⁄³ cup ricotta"`,
  `"²⁄³ cup milk"` — distinct from the plain-ASCII `"¹/³"` form, and
  distinct from the mixed-number form `"1¹⁄³"` that was already handled),
  were consistently misread by the LLM: it dropped the denominator and kept
  only the numerator, overstating quantity **3×** (`¹⁄³ cup` →
  `"1.0 cup"` instead of `0.33`; `²⁄³ cup` → `"2.0 cup"` instead of `0.67`).
  Confirmed root cause in code: `_normalize_stacked_fractions()`
  (`parse_recipe_tool.py` ~line 615) only matched this Unicode shape when a
  whole-number digit preceded it (`_STACKED_FRACTION_RE` requires leading
  `\d+`); the bare form fell through unhandled straight to the model.
  **Fixed**: added `_BARE_STACKED_FRACTION_RE` (negative lookbehind for a
  preceding digit, run after the existing pattern so the whole-number case
  is consumed first) — converts `"¹⁄³ cup ricotta"` → `"1/3 cup ricotta"`
  before the model ever sees it. Verified directly on all 4 shapes (bare,
  mixed-number, already-working ASCII-slash variant, unaffected control).
  Full parser test suite still passes (`pytest tests/ -k parse`: 14 passed,
  1 pre-existing unrelated failure — a stale mock assertion about
  `max_tokens=2000`, not caused by this change).
  **This fix only affects future parses** — the already-parsed HealthyFoods
  data in `healthyfoods_local5_progress.jsonl` still has the old wrong
  values for these ~94 lines; a targeted re-parse of just the flagged
  recipes (not a full rerun) would be the next step, same "targeted retest"
  pattern used for every other dataset's fixes.
- **152 flags — audit-tool false positives**, not real bugs: lines like
  `"spray oil"` where the audit script doesn't recognize "spray" as a valid
  count-of-1 unit. Parsed correctly, flagged only by an audit-tool gap.
- **~250 remaining — mostly benign** (single-count items like `"5cm piece
  ginger"` → `1.0 piece`, correctly parsed, audit-tool false positive
  again), plus a handful of genuinely ambiguous container-multiply cases
  worth a human glance but not clearly wrong, e.g. `"4 x white fish
  fillets (600g)"` → `2.4 kg` (assumes 600g *per* fillet; source text
  doesn't clearly rule out 600g *total* for all 4) — a source-text
  ambiguity, not a parser logic bug, not chased further this session.

**Net: 97.8% clean, one real systemic bug found and fixed at the source.**
Full flag list preserved at `{scratchpad}/healthyfoods_audit_flags.json`
if the remaining ~250 need a closer look later.

### 5b. Targeted re-parse of the 72 affected recipes — done, verified

The fraction-slash bug affected **72 distinct recipes (3.3% of the 2178
parsed)**, 94 ingredient lines total. Re-parsed just those 72 (not a full
rerun) using the fixed parser, local vLLM
(`llama-3.3-70b-instruct` on `localhost:8005`), script
`{scratchpad}/healthyfoods_reparse_frac.py`, output
`{scratchpad}/healthyfoods_frac_reparse_progress.jsonl` — **72/72 succeeded,
0 failures**, ~29 min runtime.

Merged into `{scratchpad}/healthyfoods_final_parsed_fixed.json` (the 72
recipes' `ingredients` replaced in the full 2178-recipe set, everything
else unchanged).

**Verification**: re-running the standard audit tool against the fixed set
still showed 94 "flags" on these exact lines — but that's a **false alarm
in the audit tool itself**, not a real leftover bug: the audit's own
`extract_numbers()` (`full_numeric_audit2.py`) can't parse `"¹⁄³"` either
(`extract_numbers("¹⁄³ cup macadamias...")` → `[]`), so it can't confirm a
correct match against that raw text — same blind spot that caused the
original bug, just now on the checking side. Confirmed the fix is actually
clean by direct inspection instead of trusting the audit tool: checked all
780 ingredient lines across the 72 recipes for any leftover raw superscript
or fraction-slash character in the parsed `measurement` field — **0
found**. Also spot-checked one recipe by hand (`"Nutty toasted muesli"`):
6 ingredients that were all wrongly `"1.0 cup"` are now correctly
`"0.33 cup"`.

**Status: this specific bug is fully closed** — code fixed, verified at the
regex level, re-parse run, re-parse verified clean by direct data
inspection. `healthyfoods_final_parsed_fixed.json` in scratchpad is the
now-correct full dataset — **still not written anywhere durable** (same
scratchpad-is-ephemeral caveat as everywhere else in this doc, §1).

**Not done this session**: writing `healthyfoods_final_parsed_fixed.json`
anywhere durable (Neo4j or otherwise); the remaining ~250 non-fraction
audit flags from §5a (mostly benign, a few worth a human glance, not
chased further); running this dataset through the matching investigation
(still queued).

### 5c. Is this bug present in OTHER datasets too? Checked — mixed answer, not fully closed

The §5a/§5b fix only covers HealthyFoods. Before assuming any other
dataset is affected or safe, grepped every dataset's **raw source** JSON
for the fraction-slash character (`⁄`, U+2044) directly, rather than
guessing:

- **Not affected at all (0 occurrences)**: SuperValu, SlovenianKitchen,
  MyPlate, BestOfHungary, TheHungarySoul, ESSRG. These datasets never
  contained this character in the first place — nothing to fix, nothing to
  re-verify.
- **Affected, fixed, re-parsed, verified clean**: HealthyFoods (§5a/§5b,
  94 lines / 72 recipes).
- **Affected, only partially addressed, NOT re-parsed or re-verified**:
  **Irish Heart** (~6 raw lines, e.g. `"120g /41⁄2 oz feta cheese"`,
  `"1⁄2 teaspoon each ground cumin, turmeric and paprika"`,
  `"11⁄2 tablespoons white-wine vinegar"`) and **FoodHero** (~2 raw lines,
  e.g. `"3⁄4 cup puréed peach"`, `"1⁄4 medium onion"`). Both datasets were
  marked "done, verified clean" earlier in this session (§1's per-dataset
  status table) — but that verification predates this bug class's
  discovery, so it could not have caught this.

**Key difference from the HealthyFoods case**: these use the same
fraction-slash character but with **plain ASCII digits**, not superscript
(`"1⁄2"` not `"¹⁄²"`). The original fix (`_STACKED_FRACTION_RE` /
`_BARE_STACKED_FRACTION_RE`) only targets the superscript form and did
nothing for these. Added a blanket fallback in `_normalize_stacked_fractions()`
(`parse_recipe_tool.py`): after the two superscript-specific patterns run,
any remaining `⁄` character is replaced with a plain `/` (safe — the
fraction-slash exists only to mean division, unambiguous to convert).
Verified: `"1⁄2 teaspoon..."` → `"1/2 teaspoon..."` (clean fix).

**One sub-case remains genuinely unresolved**: a whole number glued
directly to the fraction with no space, e.g. `"41⁄2 oz"` (meant as "4 1/2
oz") → now becomes `"41/2 oz"` — still ambiguous, could be misread as the
literal fraction 41⁄2 rather than 4 and a half. The superscript version of
this same shape could be fixed unambiguously because superscript digits
are an unambiguous "this is the fraction part" signal; plain digits carry
no such signal, and there are only ~6-8 total instances across both
datasets to generalize a safe rule from — not enough to justify a riskier
regex. Left as a known, narrow, low-volume gap rather than force a fix.

**Not done, real open item**: Irish Heart and FoodHero have NOT been
re-audited or re-parsed for this bug — unlike HealthyFoods, it's not yet
confirmed whether these ~8 raw lines actually produced a wrong parsed
value in the existing data, only that the raw character was present and
previously unhandled. Small scope (a handful of lines across 2 datasets) —
worth a quick targeted audit + reparse pass next session, same pattern as
§5a/§5b, before either dataset can be called verified-clean against this
specific bug class.

## 6. Next-session plan — reranker first (user's explicit call)

Researched FoodOntoRAG (arXiv 2603.09758) + its GitHub repo
(`jan3657/onto_rag`, `Restructured` branch) + its Zenodo eval dataset
(record 17347785) as a real-world comparable to this matching pipeline. Full
findings in §2b above. Two candidate improvements came out of it:

1. **Add a CrossEncoder reranking stage.** Their architecture: dense (FAISS)
   + lexical (Whoosh) retrieval → **CrossEncoder rerank** → LLM final pick.
   Model: `cross-encoder/ms-marco-MiniLM-L-6-v2` — small, off-the-shelf,
   general-purpose (not food-specific, not fine-tuned). A CrossEncoder scores
   each `(query, candidate_text)` pair jointly through one model, unlike our
   current approach (`_base_score()` in `nutrition_match.py`) which scores
   embedding-similarity and BM25 *independently* then fuses them with fixed
   hand-picked weights (60/30/10 + bonuses). Joint scoring should be more
   robust to exactly tonight's bug class (one shared literal token
   dominating an otherwise weak match), since the model sees the full
   query-candidate pair together instead of two separately-computed numbers
   getting added.
   - **Decision: user pushed back on doing enrichment first — start with the
     reranker instead.** Rationale (my read, not yet independently
     re-verified with advisor): the reranker is a bigger architectural lever
     that could subsume or reduce the need for both the BM25-dilution trick
     in item 2 *and* eventually the hand-written `classes_compatible()`
     regex gate itself, rather than adding another layer next to them.
   - Open question for next session, unresolved: FoodOntoRAG's own reranker
     still produced the "yellow 5 lake" → "florida lake perch" bug (see §2b),
     so a CrossEncoder is not a guaranteed fix for this exact failure class —
     needs testing on our own data before trusting it, same diff-based
     verification method as the rest of tonight (baseline sweep → add
     reranker → resweep → check regressions + `none→strong`).
   - Dependency check needed first: is `sentence-transformers`'s
     `CrossEncoder` already available (the embedding model likely already
     uses `sentence-transformers`, so probably yes, not yet confirmed) or
     does it need adding.
   - Where it'd slot in: after candidate retrieval + hard gates
     (`classes_compatible`/`animal_kinds_compatible`/`ingredient_forms_compatible`),
     rerank the surviving candidate set, before/instead of the current
     `_base_score()` fusion — exact insertion point not yet decided, needs
     reading `_base_score()` and the scoring loop closely first.

2. **Enrich candidate text before scoring** (deprioritized behind the
   reranker per the user's call above, not abandoned — cheaper, smaller
   blast radius, could still be worth doing in parallel or as a fallback if
   the reranker doesn't pan out). Their approach: concatenate
   `label + definition + synonyms + parent-class labels + relations` into
   one text blob per ontology term before indexing. Our equivalent (no
   ontology graph, but unused metadata already on every EU record):
   ```python
   enriched_text = f"{name}. Food group: {food_group}. Country: {country}."
   ```
   Worked-through example for tonight's actual bug (query
   `"european blend salad greens"`):
   - **Today**: candidate text is just `"European sprat, raw"` → tokens
     `{european, sprat, raw}` → shares `{european}` with the query → enough
     BM25 signal to win over the correct candidate.
   - **Enriched**: `"European sprat, raw. Food group: meat, egg and fish.
     Country: FR."` → tokens `{european, sprat, raw, food, group, meat, egg,
     fish, country, fr}` → still only shares `{european}`, but that one
     match is now diluted across a longer, food-domain-irrelevant document,
     lowering (not raising) BM25 score under standard length normalization.
     The correct candidate (`"Salad, green"` + its own food_group text)
     isn't diluted the same way since its added tokens are also
     food-domain-neutral relative to the query. Net: correct candidate
     should win without touching any regex or gate.
   - Implementation location: `_candidate_name(c)` is used for
     tokenizing/BM25 (`nutrition_match.py` around line 857-858,
     `corpus = [_tokens(n) for n in names]`) — would need a parallel
     `_candidate_enriched_text(c)` that appends `metadata.food_group` (and
     maybe `country`) before tokenizing, used for BM25 only (not for display
     name or for `food_class()` — that stays name-only unless/until the
     bigger `food_group`-mapping refactor from §2b happens).

Both are real code changes — do not start either without running the diff
sweep before/after and checking with `advisor` first if the reranker's
insertion point turns out to interact with the existing gates in a way
that's not obvious from this plan.

### 6a. Reranker spike, done — result: not a clean win as-is

Ran a real experiment, not just theorizing: `cross-encoder/ms-marco-MiniLM-L-6-v2`
(off-the-shelf, `sentence_transformers.CrossEncoder`, CPU — GPU was full,
local vLLM HealthyFoods batch using 46GB) scored against the exact same
gated candidate pool the current pipeline uses (retrieval + hard gates
identical, only the final ranking step swapped — bare candidate name only,
no enrichment, isolating just the reranker variable). Script:
`{scratchpad}/reranker_experiment.py`.

**Dataset 1 — Slovenian** (100 recipes, 341 unique ingredient names,
smallest already-reparsed dataset): 135/341 agree with current pipeline,
206/341 disagree. Manually judged a random sample of 20 disagreements
(2 excluded — kitchen equipment like "drinking glasses", not food, not a
fair test either way):
- **Current pipeline clearly better: 7** — `semolina`, `egg and egg white`,
  `dark chocolate`, `raspberries`, `artichokes`, `arborio rice` all picked
  more precise/correct current-pipeline matches; reranker over-matched to
  processed/wrong variants. Worst case: **`plum tomatoes` → reranker picked
  `"Plum"`** (the fruit) — same literal-token-collision bug class as the
  sprat case, reproduced on our own data with a cross-encoder instead of
  BM25.
- **Reranker clearly better: 4** — most notably **`crème fraîche` → current
  pipeline returned `"Cheese, Cheddar, English"` at confidence `none`
  (a real, existing bug); reranker correctly found `"Creme fraiche"`**, an
  exact match sitting in the candidate pool that the current fusion missed.
  Also fixed `butter`, `mineral water`, `bourbon vanilla sugar` (avoided
  unwanted attributes like cream/sweetener the current scoring picked up).
- **Toss-up, both mediocre: ~7** — `vanilla paste`, `farmer's cheese`,
  `cheese`, `vanilla extract`, `bread rolls`, `borlotti beans`.

**Verdict**: current pipeline wins more often in this sample (7 vs 4), but
the reranker fixes at least one real bug (`crème fraîche`) and isn't
uniformly worse. It is **not** a drop-in upgrade on raw candidate names —
it reproduces the exact literal-token-collision failure mode we've been
chasing all session (`plum tomatoes`→`Plum`), same as FoodOntoRAG's own
"yellow 5 lake" bug (§2b). Off-the-shelf MS-MARCO is a general web-search
relevance model, not food-tuned, and scoring bare names gives it the same
thin signal BM25 has. The two things that would plausibly move this from
"toss-up" to "clear win", not yet tried: (a) rerank against the **enriched**
candidate text (§6, item 2 — `name + food_group + country`), not bare
names — the reranker experiment run so far deliberately isolated the
reranker as the only variable, so this hasn't been tested in combination
yet; (b) a food-domain-tuned or few-shot-prompted reranker instead of raw
MS-MARCO.

**Dataset 2 — Hungarian** (149 recipes, 369 unique ingredient names,
`{scratchpad}/reranker_experiment_hungarian.py`,
`{scratchpad}/reranker_experiment_hungarian_results.json`): 168/369 agree,
201/369 disagree (~45%, similar rate to Slovenian). Manually judged another
random-20 sample:
- **Current pipeline clearly better (~9 incl. leaning cases)**: `bell
  peppers` (current: `"green peppers"` correct / reranker: `"chilli,
  green"` — wrong food), `aubergine` (exact / reranker picked a prepared
  dish), `cream cheese` (exact / reranker picked a sweetened fortified
  variant), `asparagus tips`. **`brown onion` → reranker picked `"Brown
  meagre, raw"` — a fish**, matched purely on the literal word "brown".
  Same literal-token-collision bug class as Slovenian's `plum tomatoes`→
  `"Plum"`, different word, same mechanism, **third dataset now** (sprat,
  plum, brown) — this is a repeatable failure class, not one-off noise.
- **Reranker clearly better (2)**: **`crème fraîche` → same exact bug and
  same exact fix as the Slovenian run** (current still returns `"Cheese,
  Cheddar, English"` at confidence `none`; reranker correctly finds `"Creme
  fraiche"`) — confirms this specific current-pipeline bug is real and
  repeatable, not sample noise. Also `vegetable stock pot` → current
  matched literal `"cooking pot"` (a kitchen utensil, nonsense); reranker
  picked `"Soup, vegetable, canned"`, far more sensible.
- **Toss-up (~8)**: mostly untranslatable regional terms (`piros arany` — a
  Hungarian paprika-paste brand name, `csipetke` — a dumpling type) where
  neither pipeline has real signal to work with.

**Consistent finding across both runs**: current pipeline wins more often
overall, but the *same two patterns* repeat on both datasets rather than
looking like sample noise — (1) reranker reliably rescues cases where the
current fusion buries a correct candidate under a wrong one
(`crème fraîche` twice, `vegetable stock pot` once), and (2) reranker
reliably reproduces the literal-token-collision bug on fresh words each
time (`plum`, `brown`) that the class gate currently blocks structurally in
most cases but the reranker has no equivalent protection against.

### 6b. Correction, found after re-investigating with advisor — the reranker's apparent "losses" were mostly test-harness artifacts, not real bugs

Asked advisor to weigh in again with the two-dataset reranker result.
Advisor's read: don't chase candidate-text enrichment next — every bug found
tonight (sprat, plum, brown-onion) is a candidate that reached scoring with
`food_class() == "other"`, which is unconditionally gate-compatible; that's
the single common cause, and the standard entity-linking literature term for
our `classes_compatible` gate is **"Type Checking"** (confirmed via a survey
paper, arXiv 2109.12520) — a recognized, load-bearing technique, not
something to route around. Advisor's proposed decisive test: check whether
`metadata.food_group` would have caught sprat/plum/brown-onion if used as a
hard gate input instead of derived `food_class()`.

**Ran that check. Result is more specific than the blanket theory — 2 of 3
have one root cause, the third is a completely different, unrelated bug**:

- **`european blend salad greens` → `"European sprat, raw"`**: confirmed
  candidate-side `"other"`-hole. `food_group="meat, egg and fish"` for
  sprat — a `food_group`-based gate *would* have caught this. Advisor
  correct here.
- **`plum tomatoes` → `"Plum"` (the reranker experiment's result)**: **NOT**
  an `"other"`-hole. `food_class(clean_query("plum tomatoes"))` returns
  `"fruit"` — the query itself misclassifies, because "plum" is checked
  before "tomato" in `_CLASS_PATTERNS`' fixed order (same order-collision
  bug class as pepper/soy, §2). The plum candidates are correctly classed
  `"fruit"` too, so the gate does exactly what it's told — query-side, not
  candidate-side, and `food_group` metadata (candidate-only) **can't fix a
  query-side bug**. **More importantly: checked current production
  (`best_nutrition_match('plum tomatoes', source='hungarian')`) — it
  returns `"Tomatoes, cherry, raw"`, correctly, at `strong`.** The reranker
  experiment's `Plum` result was an artifact of the experiment script (bare
  CrossEncoder scoring bypasses the token-overlap logic below), **not a
  live bug.**
- **`brown onion` → `"Brown meagre, raw"` (a fish, reranker experiment's
  result)**: also **not live in production**. `_STOP` (nutrition_match.py,
  around line ~110) already excludes colour words (`brown`, `red`, `green`,
  etc.) from the token-overlap term specifically because of **this exact
  historical case** — there's a comment in the code citing "brown onion" →
  "Brown meagre, raw" as the reason the stopword was added. Checked current
  production directly: returns `"red onion"` (wrong colour, but a real
  onion, not a fish). **The reranker experiment reproduced a bug that was
  already fixed in the current pipeline**, because feeding bare candidate
  names straight into an off-the-shelf CrossEncoder bypasses that
  stopword-aware overlap logic entirely — the protection lives in
  `_base_score()`, which the experiment deliberately skipped.

**So the reranker's tally needs correcting**: 2 of the disagreements I
reported as "reranker worse" (`plum tomatoes`, `brown onion`) are not real
regressions — the current pipeline already handles both correctly, and the
reranker only looked bad because the experiment stripped away protections
that exist elsewhere in the pipeline. The reranker's genuine wins stand:
`crème fraîche` (real, confirmed below) and — checked directly — **`vegetable
stock pot` → `"cooking pot"` is also a real, live, confirmed bug** (current
production returns `"cooking pot"`, a kitchen utensil, at `strong`). Same
`"other"`-hole mechanism as sprat: `food_class("cooking pot")` → `"other"`,
gate lets it through against `vegetable`.

**`crème fraîche` — real bug, but the root cause is neither the gate nor
scoring fusion.** Debugged directly: `clean_query("crème fraîche")` returns
`"cr me fra che"` — the accented characters (`è`, `î`) are being stripped
as separators instead of accent-folded, mangling the query into garbage
before retrieval even runs (`clean_query()` doesn't use the existing
`_ascii_fold()` helper that `_tokens()` already uses correctly — this is a
narrow, mechanical fix: call `_ascii_fold()` inside `clean_query()` too).
With the accent stripped correctly (tested via `"creme fraiche"` directly),
the pipeline finds the exact match at `strong` on its own, no reranker
needed.

**Corrected picture of what's actually broken in production right now**,
in priority order:
1. **`clean_query()` doesn't accent-fold** — breaks any query with
   é/è/î/etc (crème fraîche, likely others: pâté, jalapeño, etc — not yet
   swept for the full extent). Cheap, mechanical, high-confidence fix.
2. **Candidate-side `"other"`-hole** (sprat, cooking pot, and probably
   more) — the `food_group`-mapping refactor from §2b is the right fix,
   already scoped as a real architecture change, not started.
3. Query-side classification order-collisions (plum tomatoes' underlying
   class bug is real, just not currently causing a wrong match) — same
   whack-a-mole class as pepper/soy, no systemic fix identified yet beyond
   "check `_CLASS_PATTERNS` order when adding new class keywords."

**Recommendation for next session, revised**: fix `clean_query()`'s
accent-folding first — smallest, most certain, already root-caused, no
architecture decision needed. Then re-run the full 2343-name EU sweep (this
alone might surface more currently-invisible bugs the same way `crème
fraîche` was found, since any accented ingredient name is silently
mis-scored right now). The reranker question and the `food_group` refactor
are still open and worth pursuing, but the reranker experiment's evidence
for or against is weaker than first reported — re-run it, if still wanted,
*with* the existing `_STOP`/overlap protections active (i.e. reranking on
top of `_base_score`'s survivors, not replacing the whole scoring step),
since the bare-CrossEncoder-on-raw-names setup this session used isn't a
fair comparison to what a real integration would look like.

**Status: the accent-fold fix above is DONE, not just planned** — see §6c
below, which was written after actually applying it and running the full
cross-region sweep it recommends.

## 6c. `clean_query()` accent-fold fix — applied, verified, then a full
## cross-region bug sweep run on top of it

Applied the fix: `clean_query()` now calls the existing `_ascii_fold()`
helper on the raw name before anything else runs (previously only `_tokens()`
did this). `clean_query("crème fraîche")` now returns `"creme fraiche"`
instead of the mangled `"cr me fra che"`. Full test suite re-run clean: 51
passed, 10 subtests. Then, per the plan above, ran the full sweep across all
four candidate pools to look for more bugs the same way — not just the
2-3 spot-checked earlier, the whole corpus:

| Source | Names | strong/curated | weak | none |
|---|---|---|---|---|
| EU | 2343 | 2221 (95%) | 54 | 68 |
| Irish | 1039 | 1009 (97%) | 17 | 13 |
| Hungarian | 369 | 360 (98%) | 4 | 5 |
| Slovenian | 341 | 325 (95%) | 7 | 9 |

Confidence rate looks fine on its own — the real problem is how many
`strong` matches are **confidently wrong**, invisible in this table.

**Finding A (new, and the biggest single finding of the session) — single
shared-modifier-word collisions are systemic, not rare.** Scanned every
`strong`/`curated` match whose candidate classifies `"other"` (376 across
all 4 pools), filtered out the many that are correct-but-just-unclassified
(`avocado`→`"Avocado"` is fine), and manually confirmed **~40 genuinely
wrong matches in the EU pool alone**. All follow one pattern: query and
wrong candidate share exactly **one non-head-noun word** — a prep-state or
attribute term, not the actual food — and that's enough to win under
`_base_score()`'s `+0.15 × overlap` term (`nutrition_match.py` ~line 942).
Confirmed table:

| Query | Wrong match | Shared word (not the food) |
|---|---|---|
| `ground meat` / `ground sausage` / `ground walnuts` (+4 more) | `"Coffee, ground"` | ground |
| `head lettuce`, `head of broccoli` | `"Sheep, head, raw"` | head |
| `wild garlic` | `"Wild boar, raw"` | wild |
| `smoked garlic` | `"Sprat fillet smoked"` | smoked |
| `savoy cabbage`, `savoy or napa cabbage` | `"Savoy-style sponge cake"` | savoy |
| `tub margarine` | `"Tub gurnard, raw"` (a fish) | tub |
| `low-sodium stewed tomatoes`, `stewed tomatoes` | `"Heart, pig, stewed"` | stewed |
| `dry lentils`, `dry mixed herbs` | `"Cider, dry"` | dry |
| `celery flakes` | `"Chocolate flakes av"` | flakes |
| `english cucumber` | `"Muffins, English style, white"` | english |
| `corn meal` | `"Meal replacement, low calorie, ready-to-drink"` | meal |
| `crushed tomatoes`, `crushed crackers`, `diced or crushed tomatoes` | `"Aniseed comfits crushed..."` | crushed |
| `non-stick cooking spray`, `almond sticks` | `"Seafood sticks"` | stick(s) |
| `orange extract`, `almond extract` | `"Yeast extract"` | extract |
| `pea shoots`, `bamboo skewers` | `"Bamboo shoots, raw"` | shoot(s)/bamboo |
| `vegetable stock pot` | `"cooking pot"` | pot |
| `white turnip` | `"Chocolate white"` | white |
| `light muscovado sugar` | `"Juice drink light"` | light |
| `mild chiles`, `mild green chiles` | `"Juice multifruit mild w vit C"` | mild |

Every one of these is `strong`, not `weak` — silently wrong, no flag for a
downstream consumer. This is the same underlying mechanism as the sprat and
`crème fraîche` bugs found earlier tonight, just far more widespread than
those 2-3 cases suggested — this is the single biggest concrete finding of
the whole session, bigger in scope than estimated before running the full
sweep.

**User's read on this, going into next steps below**: the scoring is
giving too much weight to a token that's often just a *descriptive
modifier* (state/prep/colour word) rather than the food itself — matches
the pattern, and is the framing to hand to `advisor` for fix options.

**Finding B — non-food items reaching the matcher at all.** Several
`none`/`weak` misses are kitchen equipment, not food: `stainless steel
turner`, `wooden skewers`, `skewers`, `toothpicks`, `paper cups`, `spoons`,
`piping bag, open star tip`, `aluminum foil`. These come from the parser
picking up equipment mentioned in recipe steps as if they were ingredients
— an **upstream parsing/filtering gap** (§1), not a matching bug. Cheap
fix: a simple equipment-word filter before a name ever reaches
`best_nutrition_match()`.

**Finding C — the rest of the `none`/`weak` misses are genuine, lower-priority
gaps**: foreign/regional terms with no EU composition-table equivalent
(`csipetke`, `piros arany`, `hungarian kolbász`, `tokaji aszú 6 puttonyos`,
`unicum riserva`, `verjus`, `nam pla`, `tamari`), less-common produce
(`jicama`, `nopalitos`, `tomatillos`, `yucca`, `teff`, `kamut`, `farro`),
compound-brand items (`fiesta lime seasoning`, `green saffron rogan josh
spice blend`). Expected long tail, ongoing vocabulary/alias work, not
urgent.

**Priority ranking for next session, revised after this sweep** (supersedes
the priority list at the end of §6b):
1. **Finding A is the real fire** — confidently wrong, silent, ~40+
   instances in one pool alone. See §6d below for advisor's fix
   recommendations — investigated same session, not left as just a finding.
2. Finding B — filter equipment/non-food terms before they reach the
   matcher. Cheap, upstream, in `parse_recipe_tool.py` or right before the
   `best_nutrition_match()` call site.
3. `food_group`-based candidate gate (§2b) — still valid, addresses the
   `"other"`-hole class specifically (sprat, cooking pot survived the gate
   because of it).
4. Finding C / vocabulary long tail — lowest priority, ongoing background
   work.

## 6d. Advisor's fix recommendations for Finding A — verified against real
## data before acting on them

Asked advisor for fix options on Finding A. Advisor's first response
proposed an *alternative* diagnosis worth checking before trusting the
"descriptive-word overlap" framing: maybe the class gate is actually firing
correctly on these pairs and losing anyway (a penalty/floor defect), not an
`"other"`-hole issue — and gave a concrete 5-row check to tell the two
apart.

**Ran the check. Advisor's alternative was wrong — the original `"other"`-hole
framing holds, confirmed on real data, not assumed:**

| Query | class | Wrong match | class | `classes_compatible`? |
|---|---|---|---|---|
| `wild garlic` | vegetable | `Wild boar, raw` | **other** | True |
| `head lettuce` | leafy_green | `Sheep, head, raw` | **other** | True |
| `ground meat` | animal_protein | `Coffee, ground` | **other** | True |
| `dry lentils` | legume | `Cider, dry` | **other** | True |
| `stewed tomatoes` | vegetable | `Heart, pig, stewed` | **other** | True |

All 5 are the `"other"`-hole mechanism, all pass the gate for that reason,
none are a gate-fired-but-lost/penalty-floor case. **Found the concrete
reason for the gap, and it's a real, separate bug**: `_ANIMAL_KIND_PATTERNS`
(extended earlier tonight with `sheep`, `boar`, etc. — see §2 vocabulary
batch) and the `animal_protein` entry in `_CLASS_PATTERNS`
(`nutrition_match.py` ~line 271: `beef|steak|chuck|...|lamb|mutton|chicken|
...`) are **two separate word lists that drifted out of sync** — `sheep`
and `boar` were added to the animal-kind list (used by
`animal_kinds_compatible`) but never added to the class-pattern list (used
by `food_class`/`classes_compatible`), so `"Sheep, head, raw"` still
classifies `"other"` despite obviously being meat. `"Coffee, ground"` and
`"Cider, dry"` are a different, smaller gap — there's no beverage-adjacent
class covering coffee/cider at all in `_CLASS_PATTERNS`, so they fall to
`"other"` unconditionally regardless of the animal-kind list.

**This changes the fix priority**: the `food_group`-based candidate gate
(§2b) is the *right* fix, more strongly confirmed now than before —
it would close all 5 of these at once, structurally, without needing
`_CLASS_PATTERNS`/`_ANIMAL_KIND_PATTERNS` kept in permanent lockstep by
hand (a maintenance burden that's already demonstrably failing, tonight,
hours after the two lists were extended in the same sitting). The
"descriptive-word overlap" framing (from the original report to the user)
is still real as a *contributing* factor — the shared modifier word is why
these specific wrong candidates rank above the (also `"other"`-classified,
so equally gate-permitted) correct answer — but it's not the root cause to
fix first; closing the `"other"`-hole removes the wrong candidates from the
pool entirely, which matters more than reweighting how survivors get
scored.

**Immediate, cheap, separate fix available right now, before the bigger
`food_group` refactor**: sync the two word lists — add `sheep`/`mutton`
(done for lamb only currently — check), `boar`, `quail`, `pheasant`,
`rabbit`, `venison`, `bison` (already animal-kind, check class-pattern
coverage) into `_CLASS_PATTERNS`' `animal_protein` entry. This is the same
"extend the vocabulary" pattern as earlier tonight, but this time it's
closing a **self-inconsistency bug** (two lists that should already agree
but don't) rather than guessing new words — much lower regression risk,
worth doing immediately, small diff, testable with the existing sweep.
`Coffee`/`Cider` need a different, smaller fix (a beverage-adjacent class
or an explicit non-food-beverage exclusion) — not urgent, narrower impact.

**Advisor's other two questions, not yet answered — still open for next
session**:
- Does the `_STOP` colour-word precedent (`brown`/`red`/`green` already
  stopped from the overlap term for this exact reason, with a documented
  regression when `black`/`white` were included too — see the code comment
  at `_STOP`'s definition) generalize safely to state words (`ground`,
  `stewed`, `dry`, `smoked`, `crushed`)? Risk: for some pairs the state word
  is the *only* thing distinguishing two real, different foods (`ground
  black pepper` vs `whole black pepper`), same trap as the colour-word case
  already documented in code.
- Composition-table naming puts modifiers after the food
  (`"Pepper, cayenne, ground"`) while recipe text puts them before
  (`"ground black pepper"`) — opposite conventions on the two sides of the
  match. A "shared token must be the head noun" rule would need to handle
  both orderings, not just one.

**Also flagged by advisor, unresolved**: the `clean_query()` accent-fold
change (§6c) was applied and the full cross-region sweep was run *after*
it, but the standard before/after regression diff against
`nutrition_match_results_baseline.json` (the methodology used for every
other change tonight) was never run for this specific change in isolation.
Almost certainly an improvement (verified directly on 3 accented test
strings + full test suite clean), but note this as procedurally
unverified by the usual method — run that diff next session before
treating it as fully checked in.

**Commit question, still open, now hours old**: `nutrition_match.py` and
`tests/test_nutrition_match.py` — 758+ lines changed, tests clean, multiple
real bugs found and one fixed (accent-fold) — still not committed, still
waiting on explicit user go-ahead.

## 6e. Both immediate fixes from §6d applied and verified — 13/17 confirmed bugs fixed

Applied both fixes recommended in §6d, same session, not deferred:

1. **Class-list sync**: added `sheep`/`boar` to `_CLASS_PATTERNS`'
   `animal_protein` entry (`quail`/`pheasant`/`rabbit`/`venison`/`bison`
   turned out to already be present — smaller gap than assumed).
2. **`_STOP` extended** with the confirmed false-friend state/prep words:
   `ground`, `stewed`, `dry`, `smoked`, `crushed`, `head`, `tub`, `savoy`,
   `flakes`, `meal`, `stick`, `sticks`, `extract`, `shoot`, `shoots`, `pot`,
   `light`, `mild`, `wild`, `bamboo`, `english` — same mechanism as the
   existing colour-word stop entries, with the full bug list documented
   inline as the comment (`nutrition_match.py` ~line 124). Deliberately did
   **not** add `white`/`black` — already excluded for a documented reason
   (spice-identity conflict), unrelated to this batch.

**Verification**: full test suite (51 passed, 10 subtests) + full EU sweep,
diffed against baseline. 11 confidence-level drops flagged by the diff —
checked each one individually rather than trusting the raw count: **all 11
were previously-wrong `strong` matches correctly demoted to `weak`/`none`**,
not real regressions (a wrong-but-confident answer becoming
honestly-uncertain is the desired direction, not a loss). No case of a
previously-correct match breaking. Also confirmed `crème fraîche` still
fixed (`none`→`strong`, from the earlier accent-fold change,
`none→strong` count: 1, correct).

Re-checked all 17 originally-confirmed Finding-A bugs directly:

| Query | Before | After |
|---|---|---|
| `wild garlic` | `Wild boar, raw` | `Garlic, raw` ✓ |
| `head lettuce` | `Sheep, head, raw` | `Lettuce, raw` ✓ |
| `ground meat` | `Coffee, ground` | `Mutton, meat, raw` ✓ |
| `dry lentils` | `Cider, dry` | `Lentils green and brown dried` ✓ |
| `stewed tomatoes` | `Heart, pig, stewed` | `Tomato, green, raw` ✓ |
| `smoked garlic` | `Sprat fillet smoked` | `Garlic, raw` ✓ |
| `savoy cabbage` | `Savoy-style sponge cake` | `Cabbage, green, raw` ✓ |
| `tub margarine` | `Tub gurnard, raw` | `Low fat margarine Jumbo` ✓ |
| `crushed tomatoes` | `Aniseed comfits...` | `Tomatoes, standard, raw` ✓ |
| `corn meal` | `Meal replacement...` | `Corn pudding, homemade` ✓ |
| `pea shoots` | `Bamboo shoots, raw` | `Snow pea, raw` ✓ |
| `light muscovado sugar` | `Juice drink light` | `Cane sugar` ✓ |
| `almond sticks` | `Seafood sticks` | `Flour almond` (~ improved, imperfect) |
| `almond extract` | `Yeast extract` | `Almond oil` (~ improved, imperfect) |
| `celery flakes` | `Chocolate flakes av` | `Chocolate flakes av` (still wrong — no real celery-flakes entry in corpus, data gap not a scoring bug) |
| `vegetable stock pot` | `cooking pot` | `Sushi, vegetable` (still wrong, but no longer matching a kitchen utensil — data gap, no real stock-pot entry) |
| `white turnip` | `Chocolate white` | `Chocolate white` (unchanged — expected, `white` deliberately excluded from `_STOP`) |

**13/17 fully fixed, 2 improved but imperfect, 2 unresolved (expected, data
gaps not scoring bugs).**

**Still open / not done this session**:
- `food_group`-based candidate gate (§2b) — the bigger structural fix,
  would likely close the remaining data-gap cases and any future instance
  of this bug class without needing hand-maintained word lists. Not
  started.
- Finding B (equipment/non-food filtering) — not started.
- The `clean_query()` accent-fold diff-against-baseline (flagged in §6d as
  procedurally unverified) — still not run in isolation, though it's now
  bundled into and implicitly covered by this section's full sweep.
- **Commit question — now covers an even larger, further-verified diff.**
  Still waiting on explicit go-ahead.

## 7. Uncommitted changes — ask before committing
`src/recipe_wrangler/tools/nutrition_match.py` and
`tests/test_nutrition_match.py` have a substantial uncommitted diff from this
session's matching work. **Not yet committed — user has not confirmed.**
Do not commit without asking first. Do not add any AI/assistant
co-authorship attribution to the commit message under any circumstance (hard
global rule, see `~/.claude/CLAUDE.md`).

## 8. Behavioral notes for next session
- Never use the Monitor tool for simple wait-for-completion polling on
  background jobs. Use a silent `tmux` + direct log/file check instead. The
  user was explicit and heated about this earlier in this session — do not
  repeat it.
- Long-running jobs (anything multi-minute) go in `tmux`, never bare
  `nohup … &`.
- Keep answers dense, no filler, no re-narrating what was just done.
- Don't start new narrow one-off regex patches for matching bugs without
  checking whether the fix generalizes — this was an explicit, repeated user
  directive this session. Ask `advisor` when a fix's scope is unclear.

# codex — BEGIN: identity-aware nutrition matching implementation (2026-09-22)

## What Codex changed

Implemented the non-generative-LLM matching path in production. The important
change is that the new parser's two aligned values are no longer collapsed:

- `ingredient_names` is passed as the core food identity;
- `ingredient_match_names` remains the detailed nutrition lookup phrase,
  including meaningful state/form information.

`Recipe_Profiling_Tool` and `Nutrition_Node` now pass both values into
`nutritional_tool_vector`, which passes `identity_name` separately to
`best_nutrition_match`. Legacy callers remain compatible because the detailed
name is used as identity when no separate identity is supplied.

The matcher still uses the existing hybrid retrieval and lexical ranking for
coverage, but a shared word is no longer sufficient evidence that two foods
are the same. Added:

- identity-aware ranking penalties;
- an independent validation of the selected top candidate;
- conservative abstention (`top_candidate_incompatible`) instead of silently
  promoting the next candidate;
- explicit checks for high-impact forms/parts such as skin, spread, juice,
  oil, extract, seed, shoots/tops, flour, concentrates, composite foods and
  snack products;
- state and cooking-method checks (raw/cooked/frozen/canned/dried and
  baked/fried/boiled/steamed/grilled);
- colour-sensitive identity handling for foods such as black rice, white fish
  and coloured beans/peppers;
- a generic-potato versus sweet-potato guard;
- early rejection of unambiguous equipment/non-food rows, including baking
  paper and wooden skewers.

No cross-encoder, generative LLM, new service or new dependency was added.
This was deliberately the smallest architecture change that reused the
parser's existing identity/detail separation.

## Curated aliases activated locally

Added and loaded into the local `pipeline_static_data` Postgres row
`ingredient_composition_aliases` (46 rows total):

- `vegetable stock pot` -> `cofid:17-727` (`Stock cubes, vegetable`);
- `firm tofu` / `extra firm tofu` -> `nevo:5519` (`Tofu unprepared`);
- `almond milk` -> `ciqual:18111` (`Almond drink, prepacked (average)`);
- `unsweetened almond milk` -> `nevo:5464`;
- `corn meal` -> `cofid:11-1045` (`Flour, corn`).

The CSV is under ignored `/data`, so every other deployment/database must
refresh its `ingredient_composition_aliases` static-data row before bulk
profiling. The local database was updated and all six aliases were verified as
`curated` matches.

## Verification and measured trade-off

Representative live outcomes after the change:

- `white turnip` -> `Turnip raw`;
- `pineapple chunks` -> `Pineapple`;
- `european blend salad greens` -> `Salad, green`;
- `firm tofu` -> curated `Tofu unprepared`;
- generic `chicken` -> `Chicken, meat, raw`, not chicken skin;
- `white fish` -> `European whitefish, raw`, not fish pie/fishcake;
- `black rice`, `almond extract` and `pea shoots` abstain when no defensible
  composition row is available instead of returning an unrelated food.

Complete sweep over the newest 4,320 unique parsed ingredient names, before
activating the six new aliases in Postgres:

- 3,498 strong;
- 70 curated;
- 91 weak;
- 661 none;
- 447 old `strong` results became `none` after identity/form validation.

This is intentionally lower coverage than the old matcher. Inspection showed
many removed `strong` links were dangerous false positives, but these counts
are not an accuracy score: a human-labelled gold set is still required before
claiming a measured accuracy improvement.

Tests:

- focused matching/profile/concentrate tests: 89 passed, 21 subtests passed;
- full repository suite: 948 passed; one unrelated existing OpenRouter parser
  test failed because it expects the old constructor call without
  `max_tokens=2000`.

## Correct next step after reparsing: validate weights, then bulk profile

Yes: weight estimation is the remaining mechanism to audit before running
matching/profiling for every reparsed recipe. The real pipeline order is:

`Recipe_Parser -> Weight_Calculator -> Recipe_Profiling_Node`

Nutrition matching happens inside profiling, and every matched per-100-g
composition row is multiplied by the estimated ingredient grams. A correct
food linked to a bad weight still produces a badly wrong recipe profile.

Run the weight audit on all newly reparsed databases before the bulk profile:

1. verify index alignment among `ingredient_names`,
   `ingredient_match_names`, `measurements` and generated weights;
2. count missing/zero weights and report the fallback/match source used;
3. flag implausible per-item weights and implausible total recipe/serving
   weights;
4. rank outliers by downstream nutrient impact, not only row count;
5. inspect high-frequency units and fallback paths by source database;
6. correct weight-estimation problems, rerun the audit, then run the
   structured profiling chain over all recipes without reparsing again.

Weights are region-independent. For multi-region nutrition runs, calculate
them once and reuse the first run's aligned weights, as already supported by
`Recipe_Profiling_Chain_Structured`.

# codex — END: identity-aware nutrition matching implementation (2026-09-22)

# codex — BEGIN: exact, safe-parent and conflicting match policy (2026-09-22)

## Why this follow-up was necessary

The first identity guard represented compatibility as a boolean. That made two
different situations indistinguishable:

- `rice vinegar` -> `vinegar`: the candidate omits a subtype but does not
  contradict it;
- `vanilla paste` -> `curry paste`: the candidate supplies a different food
  identity and merely shares the form word `paste`.

The same problem appeared across forms, plurals, compound spellings and
multi-food parser output. A shared word could still produce a false positive,
while a harmless omitted qualifier could cause an unnecessary abstention.

## What Codex changed

Compatibility now has three internal outcomes:

- `exact`: identity and required form agree;
- `safe_parent`: an explicitly approved generic parent omits detail but does
  not introduce conflicting detail;
- `incompatible`: identity, subtype, form, cooking method or compound
  components conflict.

The initial safe-parent policy is deliberately narrow:

- sourced vinegar may fall back to plain `vinegar`, but not to a different
  named vinegar;
- sea/kosher/Himalayan/coarse/table salt may fall back to plain `salt`, while
  `garlic salt` may not;
- a smoked ingredient may use its plain parent when no smoked row exists;
- cream-style food may use a plain parent, but not a starch, syrup, bread,
  confectionery or snack product.

Additional deterministic protections and equivalences were added:

- final identity anchors replace the old "any shared token" check;
- arbitrary prefix identity matching was removed (`corn` no longer matches
  `Cornetto`); explicit synonyms handle real vocabulary differences;
- source/product forms now include drink, syrup, starch, bread, pesto,
  passata, flavoured products and confectionery;
- candidate form polarity recognizes `without skin`/`skinless` correctly;
- multi-food names must be covered component by component;
- real alternatives containing `or` or `/` abstain unless the alternatives
  are synonyms such as `zucchini/courgette`;
- explicit parser identity text retains separators and is no longer damaged
  by retrieval-query cleanup;
- normalized equivalents cover broccolini/broccoli, cornflour/corn starch,
  silken/silky tofu, peppercorn/pepper, cumin powder/cumin seed, chia seed
  dried, and common joined/plural spellings such as cornstarch, kiwifruit,
  passionfruit, tortillas, peas and avocados;
- temperature/cooking descriptors no longer become false product sources for
  water, milk or cooking oil;
- ground pepper cannot resolve to a raw bell/capsicum vegetable.

Representative verified behavior:

- `rice vinegar` -> `Vinegar`, strong with
  `reason=safe_parent_fallback`;
- `wine vinegar` -> `Vinegar, red wine`, exact;
- `vanilla paste` -> `Curry paste`, rejected;
- `yoghurt` -> plain full-fat yoghurt, not yoghurt drink/mayonnaise;
- `red chilli` -> chilli vegetable, not chilli ketchup;
- `basil pesto` -> green pesto, not fresh basil;
- `ground pepper` -> black/white pepper, not capsicum;
- `salt and pepper` and `almonds or peanuts` abstain rather than selecting one
  component;
- `broccolini`, `cornflour`, `silken tofu`, `tortillas`, `black peppercorns`,
  `cumin powder` and `chia seeds` resolve to defensible composition rows.

## Final corpus measurement

The final sweep used all 4,320 unique names from the completed new-parser
dataset (43,971 ingredient occurrences):

- 2,802 strong names / 35,188 occurrences;
- 76 curated names / 5,247 occurrences;
- 57 weak names / 242 occurrences;
- 1,385 none names / 3,294 occurrences, including 19 intentional non-food
  names;
- 31 strong safe-parent fallbacks / 201 occurrences.

Accepted coverage is 40,435 / 43,971 occurrences (91.96%). The lower unique
name coverage is intentional: most newly rejected names are rare malformed,
compound or alternative labels. The matcher now avoids false precision rather
than converting one shared word into nutrition data.

Focused verification: `tests/test_nutrition_match.py` passes 74 tests and 46
subtests. The refreshed local review inventory is
`artifacts/nutrition_matching/match_failure_inventory_2026-09-22.csv`.

The remaining unmatched/weak rows are primarily composition-table coverage,
curation or parser-structure work. They must not be solved by globally relaxing
identity/form guards. Weight-estimation validation remains the next gate before
bulk profiling all reparsed recipes.

# codex — END: exact, safe-parent and conflicting match policy (2026-09-22)

# codex — BEGIN: shared-word overmatch hardening and final validation (2026-09-22)

## What Codex changed

This pass supersedes the corpus counts in the preceding Codex block. The
retriever still supplies candidates, but acceptance now follows this order:

1. retrieve the normal regional + EU pool;
2. score the original compatible pool (preserving the established ranking);
3. walk that ranking and select the first candidate whose food identity,
   source/subtype, form, colour, animal species and cooking state are
   compatible;
4. abstain when no compatible candidate exists.

The important change is that a shared descriptive word is no longer enough to
establish food identity. Shape/part words such as `flake`, `clove`, `heart`,
`root`, `skin`, `pack` and `pulp`; product words such as `paste`, `dressing`,
`jam`, `nectar`, `bran`, `cereal`, `spice` and `seasoning`; and preparation
words such as `dried`, `instant`, `smoked` and `wholemeal` are checked as forms
or sources instead of being allowed to substitute for the actual food.

The implementation remains deterministic and local. No LLM and no new
dependency were added. An LLM is not required for this matching stage: it
would make the safety boundary harder to reproduce and audit. Curated aliases
remain the escape hatch for genuine vocabulary/table gaps.

Other changes in `nutrition_match.py` and `non_food_ingredients.py` include:

- class/species incompatibilities and candidate-primary-food checks;
- source-aware product matching, including the narrow safe generic-parent
  behavior for vinegar (`rice vinegar` -> generic `vinegar` is allowed, but
  `white wine vinegar` -> `red wine vinegar` is not);
- candidate-added manufactured-form rejection;
- explicit normalization/equivalence for chilli/chili, cornflour/corn starch,
  silver beet/chard, cannellini spelling, bocconcini/mozzarella, pasta shapes,
  five/`5`, allspice and several common recipe terms;
- special handling for spray oils so `olive spray oil` retains both `olive`
  and `oil`, while “spray oil” appended as a cooking instruction does not
  change the preceding ingredient;
- additional non-food and placeholder rejection;
- generic `oil` and `cheese` prefer composition-table average rows;
- ranking selects the highest-ranked compatible candidate rather than
  recalculating BM25 after filtering, which avoids unrelated rank changes.

Representative final behavior from the live indexes:

- `vanilla paste` -> `Tahini paste`: rejected (the previous curry-paste bug is
  also rejected; no vanilla-paste row is available);
- `rice vinegar` and `white wine vinegar` -> generic `Vinegar`, strong and
  labelled `safe_parent_fallback`;
- `chilli powder` -> `Chilli powder`, strong;
- `chilli flakes` -> raw chilli: rejected because flakes imply dried chilli
  and no compatible dried row was retrieved;
- `fresh low salt tomato pasta sauce` -> tomato-based pasta sauce, strong;
- `tahini dressing` -> tahini paste/French dressing: rejected rather than
  treating either shared product word as sufficient;
- `beetroot vacuum pack` -> raw beetroot, strong;
- `green veg stir-fry mix` -> vegetable stir-fry mix, strong, not Thai curry;
- `olive spray oil` -> olive oil, strong;
- `Easter eggs` -> milk chocolate, strong, not raw chicken egg;
- `cannelini beans` -> canned cannellini beans, strong;
- `rice vermicelli noodles` -> dry rice vermicelli, strong;
- `Chinese five spice`, `bran flakes`, `cornflakes` and `allspice` retain
  correct strong matches after the stricter product checks.

## Final corpus measurement

The final `v7` sweep used all 4,320 unique ingredient names from the completed
new-parser dataset, representing 43,971 ingredient uses. Results are in
`/tmp/nutrition_matches_after_shared_word_fixes_v7.json` on the development
machine:

- strong: 2,567 names / 34,645 uses;
- curated: 76 names / 5,247 uses;
- weak: 31 names / 233 uses;
- none: 1,646 names / 3,846 uses;
- strong + curated automatic coverage: 39,892 / 43,971 = 90.72%;
- including weak review candidates: 40,125 / 43,971 = 91.25%.

The unsafe pre-hardening baseline was 91.96% strong + curated. The 1.24-point
reduction is deliberate abstention: 357 formerly `strong` unique names became
`none`, while 122 formerly unmatched names became `strong`. These figures are
coverage, not accuracy; a labelled evaluation set is still needed for a formal
precision/recall claim. Weak and `none` rows should be sent to review/curation,
not enabled by relaxing the shared-word guards globally.

## Verification and next operational step

- focused matcher: 78 passed, 162 subtests passed;
- focused weight/concentrate/USDA helpers: 70 passed, 40 subtests passed;
- full repository: 965 passed, 244 subtests passed, one unrelated pre-existing
  failure in `test_parser_builds_openrouter_client` because production passes
  `max_tokens=2000` and the test still expects the old constructor call.

Weight estimation is still the next operational gate before bulk matching and
profiling. The weight tests pass, but that does not replace a full-data weight
audit. Run `python -m recipe_wrangler.tools.backfill_profiles --dry-run` (and a
small `--limit` sample) first, audit zero/missing/implausible weights and total
recipe/serving mass, then run the resume-safe full backfill. Matching occurs
inside `Recipe_Profiling_Chain_Structured`; weights are region-independent and
should be calculated once and reused across IE/HU/EU/SI. Do not reparse again,
and do not start the write-enabled bulk backfill until the weight audit is
accepted.

# codex — END: shared-word overmatch hardening and final validation (2026-09-22)

# codex — BEGIN: alternatives, form fallback and composition-gap follow-up (2026-09-22)

## Alternative ingredients

Explicit `or` phrases are no longer treated as one compound food. The matcher
selects exactly one alternative, stores it as `selected_alternative`, prefixes
the reason with `alternative_selected:<choice>`, and emits a human-readable
`nutrition_match_note`. The nutritional calculator now preserves that note in
each profiling detail.

The final alternative is used because recipe grammar normally places the
complete shared noun there (`orange or red bell pepper`, `vegetable or beef
stock`). Short shared context is carried where it is unambiguous:

- `margarine or butter` -> butter;
- `ground beef or turkey` -> ground turkey, not whole turkey;
- `dried cilantro or coriander` -> dried coriander;
- `white fish fillet or steak` -> white fish steak, not beef steak;
- `vanilla essence or paste` tests vanilla paste and remains unmatched rather
  than treating bare `paste` as tahini/curry paste.

If the selected alternative has no compatible composition row, the matcher
returns `no_compatible_alternative`; it does not silently use a shortened
earlier fragment. Explicit `and`/`with` compounds remain unmatched with
`compound_ingredient_requires_split`. In particular, `salt and pepper` must be
split into separately weighted ingredients if sodium is to be calculated;
salt has negligible energy but cannot be discarded from nutrition profiling.

## Additional fixes

- `flour tortillas` -> `Tortilla, wheat, soft`;
- canola is normalized to rapeseed, so `canola oil` -> `Rapeseed oil`;
- passata retrieval expands to tomato puree/coulis, so `passata` and `tomato
  passata` -> `Tomato puree`;
- `onion powder` may use dried onion (same food and dehydrated form), rather
  than garlic powder;
- named bean sources must be preserved, preventing garbanzo/pinto beans from
  becoming green beans;
- requested exact forms are selected before generic parents regardless of a
  small ranking-score difference;
- when no smoked-chicken record exists, `smoked chicken breast` may use raw
  chicken breast as an explicit `safe_parent_fallback`, with a visible note;
- permitted safe-parent fallbacks can be strong when identity is exact and
  similarity clears the normal threshold, so the fallback is not immediately
  discarded by the calculator;
- product names must occur in the candidate's primary product segment; a sauce
  query can no longer select an entire pasta dish merely because its later
  description mentions sauce;
- geographic seasoning qualifiers and candidate-added sugared/confectionery
  forms receive compatibility checks.

## Confirmed data limitations

These remain deliberately unmatched because the current Irish/EU indexes do
not contain a defensible equivalent:

- dried chilli/chilli flakes (only raw chilli, chilli powder and sauces were
  retrieved; raw chilli is not an acceptable dried-flake substitute);
- fresh lemongrass (only lemongrass powder is present/retrieved);
- vanilla paste;
- mirin (generic sake/rice wine exists, but it can materially understate
  mirin's sugar and is not enabled without curation);
- plain `chickpea` when preparation is unknown (dried and canned/cooked rows
  exist and differ substantially in water and nutrients);
- some exact stock, seasoning and branded sauce variants.

These should be addressed by adding verified composition rows or curated
aliases with explicit approximation notes, not by relaxing identity rules.

## Final validation

The final read-only `v10` sweep used the same completed-parser dataset: 4,320
unique names and 43,971 ingredient uses. Results:

- strong: 2,794 names / 35,099 uses;
- curated: 92 names / 5,269 uses;
- weak: 32 names / 233 uses;
- none: 1,402 names / 3,370 uses;
- strong + curated automatic coverage: 40,368 / 43,971 = 91.81%;
- including weak review candidates: 40,601 / 43,971 = 92.34%.

The result file is
`/tmp/nutrition_matches_after_alternative_and_gap_fixes_v10.json`. Focused
matcher verification passes 82 tests and 169 subtests. The full repository
suite passes 969 tests and 251 subtests; the sole failure is the unrelated
pre-existing OpenRouter constructor mock that omits production's
`max_tokens=2000` argument.

# codex — END: alternatives, form fallback and composition-gap follow-up (2026-09-22)

# codex — BEGIN: deterministic salt/pepper splitting and common-gap fixes (2026-09-23)

## Salt and pepper is now split before nutrition profiling

The completed parser corpus still contained standalone compound rows even
though the LLM prompt already explicitly requested a split:

- `salt and pepper`: 144 corpus uses;
- `salt and black pepper`: 39 corpus uses.

The live Neo4j graph contains additional variants, but nearly all are
quantity-free (`to taste`, `to season`, optional, or the ingredient text
copied into the measurement). Only one inspected row had a numeric shared
measurement (`1 dash`). This confirmed that another full LLM reparse would be
unnecessary and would not make the rule reliable.

`parse_recipe_tool.py` now enforces a narrow deterministic split for standalone
salt/pepper expressions, including `&`, sea/kosher/coarse salt, and freshly
ground/cracked black pepper. It deliberately does not split other foods such
as `salt and vinegar crisps`. The rule is applied in three places:

1. `parse_ingredient_lines()` for future batch reparsing/imports;
2. `Recipe_Parser_Node` for future full recipe parsing;
3. `Recipe_Profiling_Node` for already-reparsed/stored recipes, so the pending
   all-recipe matching/profile run does not require another LLM reparse or an
   immediate graph rewrite.

Quantity-free compound rows become separate blank measurements and therefore
receive the existing conservative seasoning-weight fallback. A numeric shared
measurement is divided equally. Existing precomputed combined weights are also
halved across the two rows, preserving total recipe mass instead of assigning
the full amount twice.

Verified composition records:

- `salt` -> CoFID `Salt`, 39,300 mg sodium/100 g (curated EU fallback);
- `sea salt` -> `Salt sea`, 33,800 mg sodium/100 g;
- `black pepper` -> `Pepper, black`, 20 mg sodium/100 g.

Thus salt must not be hardcoded to zero: 1 g contributes 393 mg sodium. The
regional sources without their own salt row correctly use the EU record.

## Explicit negligible-seasoning policy

When no exact composition row exists, the application now intentionally uses
zero contribution for the user-approved negligible group instead of reporting
an unexplained matching failure. The current narrow policy covers chilli/chili
or red-pepper flakes, generic seasoning, Italian/Tuscan/Moroccan/taco
seasoning, pumpkin-pie spice/seasoning, and sumac. An exact compatible row
still wins if one is later added to the composition indexes.

The match detail carries:

- `reason=intentional_negligible_seasoning`;
- `nutrition_intentionally_ignored=true`;
- a human-readable `nutrition_match_note`.

These intentionally ignored grams count as handled in nutrition coverage but
still contribute zero nutrients. Salt is explicitly not part of this policy.

## High-frequency false negatives fixed

Deterministic phrase normalization, synonym compatibility, inherent-product
form handling, and one narrow generic stock-powder fallback now recover these
previously bad cases:

- applesauce / apple puree -> `Apple sauce, homemade`;
- instant yeast -> dried yeast, not fresh yeast;
- tamari -> soy sauce, while `tamari almonds` remains almonds;
- chicken tenderloins -> raw skinless chicken breast;
- jalapeno/jalapeño/green chiles -> chilli pepper, not black pepper or beans;
- heavy cream -> fresh whipping cream;
- prosciutto -> dry-cured ham;
- almond meal -> almond flour;
- bread soda -> bicarbonate of soda;
- mandarin oranges -> raw mandarin/clementine, not juice;
- harissa and harissa paste -> harissa sauce;
- refried beans -> re-fried beans;
- baked beans -> baked beans in sauce;
- sun-dried tomato pesto -> red pesto;
- portobello mushrooms -> generic raw mushroom parent;
- garlic-infused olive oil -> olive oil;
- orange kumara -> raw sweet potato;
- jasmine rice -> raw white long-grain rice;
- soy milk -> average soy drink;
- kecap manis -> sweet soy sauce/Ketjap;
- dry mustard -> mustard powder;
- chicken/vegetable/beef stock powder -> generic stock powder as a visible
  `safe_parent_fallback`; liquid stock is still prohibited from using a
  concentrated powder row.

The audit also exposed a nondeterministic matcher bug: the product head was
chosen by iterating a Python `set` of equivalent names. Depending on the
process hash seed, `angel hair pasta` could use `pasta` and match, or use
`noodle` and fail. Head selection now prefers the raw final product word and
uses a sorted fallback. Five fresh processes with randomized hash seeds all
return the same exact result.

## Chickpea decision remains explicit

Bare `chickpea`, `chickpeas`, `chick peas`, and `garbanzo beans` currently
return `ambiguous_preparation_state` with a note requesting dried, cooked, or
canned state. Prepared variants (`canned`, `drained`, `rinsed`, `cooked`,
`boiled`, or `dried`) continue through normal matching. This deliberately
removes 45 previously accepted direct uses plus one alternative until the
product decision is made: default unspecified chickpeas to canned/cooked, or
keep requiring state. Dried and canned rows are not nutritionally equivalent
because their water content differs substantially.

## Final v15 corpus result

Read-only sweep over the same completed-parser corpus (4,320 unique names,
43,971 original ingredient uses):

- strong: 2,848 names / 35,416 uses;
- curated: 91 names / 5,251 uses;
- weak: 32 names / 233 uses;
- none: 1,349 names / 3,071 uses;
- strong + curated: 40,667 / 43,971 = 92.49%;
- intentional negligible seasoning: 294 uses;
- accepted + intentional negligible: 93.15% of original uses.

The original corpus counts the 183 combined salt/pepper rows once each. At
runtime they become 366 separately accepted salt and black-pepper rows, so the
effective post-split totals are 41,033 accepted rows out of 44,154 = 92.93%,
or 93.60% including intentional-negligible seasoning.

Result file:
`/tmp/nutrition_matches_after_salt_split_and_common_fixes_v15.json`.

Remaining high-frequency unresolved items are genuine composition/policy gaps,
not candidates for globally relaxing identity checks: mirin, vanilla paste,
fresh lemongrass, liquid beef stock/broth, balsamic dressing, soba/Hokkien/
udon/ramen noodles without a defensible exact row/state, hoisin/Tabasco/
sriracha, chilli/tandoori paste, garlic salt, reduced-fat ricotta, cranberry
or plum sauce, dukkah, almond butter, pumpkin-seed oil, coconut cream, borlotti/
Great Northern beans, choy sum, lamb shank, black rice, and generic `stock`.
These require a verified composition row or an explicit approximation choice.

Verification:

- focused split/matcher/concentrate suite: 98 passed, 204 subtests passed;
- full repository: 974 passed, 277 subtests passed;
- sole failure is the pre-existing unrelated OpenRouter constructor mock,
  which still omits production's intentional `max_tokens=2000` argument.

# codex — END: deterministic salt/pepper splitting and common-gap fixes (2026-09-23)

# codex — BEGIN: high-frequency accepted-match corrections (2026-09-23)

Reviewed the remaining high-frequency suspicious accepted matches against all
Irish and EU composition-index rows, then checked their actual recipe
measurements/original text in Neo4j. Ten ingredients had defensible rows and
are now curated in `ingredient_composition_aliases`:

- `pepper` -> `Pepper, black` (with runtime measurement-context handling for
  whole/gram-weight bell peppers -> average raw sweet pepper);
- `margarine` -> generic 75–90% baking fat/margarine, not a branded 60% row;
- `rice` -> white raw rice, not red rice;
- `spaghetti` -> white dried raw pasta, not boiled pasta;
- `bread` -> the composition-table average, not French baguette;
- `red lentils` -> red split dried raw lentils, not boiled lentils;
- `dates` -> dried dates, not fresh dates;
- `chicken thighs` -> raw thigh/upper-leg meat, not casseroled thighs;
- `mixed salad leaves` -> green salad, not spinach;
- `rice noodles` -> dry raw rice vermicelli, not boiled noodles.

The previous `vegetable stock` and `vegetable broth` aliases to dehydrated
concentrate were removed. Corpus inspection showed liquid cup/ml/litre
quantities, and the current tables have no equivalent prepared vegetable-stock
row. Plain `vegetable stock`, `vegetable broth`, `broth`, `stock`, and `beans`
now abstain with an explanatory `ambiguous_food_identity` result instead of
guessing a concentrate, Scotch broth, or green beans. Specific phrases such as
`baked beans`, `chicken broth`, and `vegetable stock powder` still follow the
normal matcher because their identity/state is explicit.

The local Postgres `ingredient_composition_aliases` static data was refreshed
to 55 rows, and every new source ID was verified against the live EU
composition index. A fresh sweep of the same 4,320-name / 43,971-use completed
parser corpus produced:

- strong: 2,829 names / 34,822 uses;
- curated: 88 names / 5,572 uses;
- weak: 32 names / 233 uses;
- none: 1,371 names / 3,344 uses.

This deliberately trades a small amount of coverage for eliminating unsafe
generic guesses. The ten corrected high-frequency names represent 514 corpus
uses and remain accepted through reviewed rows. Prepared vegetable stock is a
composition-data gap, not a matcher bug; add a verified liquid vegetable-stock
record before restoring automatic nutrition for it.

Verification:

- focused matcher/concentrate suite: 98 passed, 223 subtests passed;
- full repository: 977 passed, 296 subtests passed;
- sole failure remains the pre-existing unrelated OpenRouter constructor mock,
  which expects no `max_tokens=2000` argument although production supplies it.

# codex — END: high-frequency accepted-match corrections (2026-09-23)

# codex — BEGIN: prepared stock composition gap filled (2026-09-23)

Added three reviewed prepared-liquid records to the EU composition-table build
instead of approximating liquid recipe quantities with dehydrated cubes or
powder:

- `fineli:29026`, `Vegetable bouillon, dissolved`, from the Finnish Institute
  for Health and Welfare (THL), Fineli:
  https://fineli.fi/fineli/en/elintarvikkeet/29026
- `frida:534`, `Bouillon, beef, cube, prepared`, from DTU Food Institute's
  Danish Frida database: https://frida.fooddata.dk/food/534
- `frida:277`, `Bouillon, chicken, prepared`, from Frida:
  https://frida.fooddata.dk/index.php/food/277

The builder now retains `source`, original provider-prefixed ID, and
`source_url` in `nutrients-ingredients-eu`. It imports the profiling nutrients
needed by the application (energy, protein, carbohydrate, fat, sugars,
saturated fat, sodium, and fibre). The local table now contains 8,151 unique rows,
including one Fineli and two Frida rows, and the three corresponding vector
documents were added to the active `ingredient_vectors_v1` index.

Added curated prepared-liquid aliases:

- `vegetable stock`, `vegetable broth`, and `vegetable bouillon` ->
  `fineli:29026`;
- `beef stock`, `beef broth`, and `beef bouillon` -> `frida:534`.

Existing `chicken stock` and `chicken broth` continue to use the already-good
CoFID ready-made liquid row (`cofid:17-681`); the Frida chicken row is now also
available to retrieval without unnecessarily replacing that curated mapping.
Explicit `vegetable stock cube` still maps to the concentrate row, while bare
`stock` and `broth` still abstain because the food source is unknown.
Reduced/no-added-salt stock also abstains unless a verified reduced-salt row is
added; it must not silently inherit regular stock's sodium value.

Live matching checks after refreshing the 61-row alias table:

- `vegetable stock` / `vegetable broth` -> Fineli prepared vegetable bouillon,
  curated confidence;
- `beef stock` / `beef broth` -> Frida prepared beef bouillon, curated
  confidence;
- `chicken stock` / `chicken broth` -> CoFID ready-made chicken stock, curated
  confidence;
- `stock` / `broth` -> no match (`ambiguous_food_identity`);
- `low-sodium vegetable stock` -> no match
  (`unsupported_nutrition_variant`);
- `vegetable stock cube` -> CoFID vegetable stock cubes, curated confidence;
- `beef stock powder` -> generic stock powder only as a visible
  `safe_parent_fallback`.

An end-to-end calculator probe using 1,000 g vegetable stock and 500 g beef
stock consumed the new composition rows and preserved the full liquid weights.
It used 543.8 mg sodium/100 g for Fineli vegetable stock and 326 mg/100 g for
Frida beef stock; neither was treated as a concentrate.

Verification:

- focused matcher/composition/concentrate suite: 101 passed, 226 subtests;
- full repository: 980 passed, 299 subtests;
- sole failure remains the unrelated pre-existing OpenRouter constructor mock,
  which omits production's intentional `max_tokens=2000` argument.

# codex — END: prepared stock composition gap filled (2026-09-23)

# codex — BEGIN: verified Frida gap rows and unsafe-variant guards (2026-09-23)

Searched current official European national food-composition sources before
adding any more records. DTU Food Institute's Frida 6.1 dataset contains three
exact corpus gaps, now appended by `scripts/build_eu_global_dataset.py` with
provider-prefixed IDs, country/source metadata, source URL, and the eight
nutrients used by profiling:

- `frida:1831`, `Mixed berries, frozen`:
  https://frida.fooddata.dk/food/1831?lang=en
- `frida:1771`, `Feta, 5% fat (salad cheese)`:
  https://frida.fooddata.dk/food/1771?lang=en
- `frida:1962`, `Salad dressing, Italian`:
  https://frida.fooddata.dk/food/1962?lang=en

The official downloadable Frida 6.1 workbook is the value source:
https://doi.org/10.11583/DTU.32312844. The live EU table was rebuilt and now
contains 8,154 unique records. The three new rows were also embedded into the
active `ingredient_vectors_v1` index. The reviewed alias table was refreshed
in Postgres and now contains 77 rows.

Live curated matches now resolve:

- `frozen mixed berries`, `frozen berries`, and `mixed berries` -> Frida's
  mixed-berry row instead of arbitrarily choosing elderberry;
- `reduced-fat feta` / `reduced-fat feta cheese` -> Frida's 5%-fat feta-style
  salad cheese instead of regular feta;
- `Italian dressing` -> Frida Italian dressing instead of French dressing.

The same review separated composition-data gaps from matcher bugs. Suitable
EU rows already existed, so no duplicate records were created for these:

- `chicken fillet(s)` -> raw skinless chicken breast (`ciqual:36017`), not
  prepared chicken fillet;
- `chicken thigh fillets` -> raw thigh meat (`ciqual:36019`), not casseroled
  chicken;
- `corn tortilla(s)` -> maize/corn tortilla (`ciqual:7813`), not wheat;
- `flour tortillas` remain correctly matched to wheat soft tortilla;
- recipe shorthand `cocoa` -> cocoa powder (`cofid:12-545`), not cocoa butter;
- `lasagne/lasagna sheets` -> dried raw white pasta (`cofid:11-716`) as a
  reviewed generic-parent fallback because sheet shape does not change the
  composition and no exact sheet row exists.

No exact current European row was verified for reduced/low-sodium ordinary soy
sauce or pickled ginger. These are not nutritionally interchangeable with the
available regular soy sauce and fresh ginger rows. The matcher now abstains:

- reduced/low-sodium soy sauce -> `unsupported_nutrition_variant`;
- pickled ginger -> `top_candidate_incompatible` (pickled is now a required
  preparation form).

The latest Frida dataset also contains `Bean mix, frozen`, but the corpus cases
are canned mixed beans. It was deliberately not used as a silent substitute.
Likewise, ordinary soy sauce in Fineli/Frida and sweet low-sodium Ketjap in
NEVO are not valid replacements for ordinary low-sodium soy sauce.

Verification:

- focused matcher/composition suite: 92 passed, 214 subtests passed;
- live matcher probes returned the curated IDs listed above;
- an end-to-end 100 g-per-ingredient calculator probe read the new Frida
  nutrients (mixed berries 52.8 kcal, reduced-fat feta 176.4 kcal, Italian
  dressing 102.3 kcal) and returned zero contribution plus an explicit reason
  for unsupported low-sodium soy sauce and pickled ginger.

# codex — END: verified Frida gap rows and unsafe-variant guards (2026-09-23)

# codex — BEGIN: source-context recovery and additional European rows (2026-09-23)

The ambiguity audit found that many apparently bare foods were not actually
ambiguous in the source recipe. In particular, 128 of 135 HealthyFoods uses of
the canonical name `chickpea garbanzo garbanzos` have an original line that
explicitly says `can`; only seven remain genuinely unspecified or merely say
drained/rinsed. The recompute path now builds a separate nutrition match name
from the original ingredient line while keeping the clean canonical display
name unchanged.

Only nutrition-relevant context is restored: canned/tinned, cooked, raw,
frozen, dried, smoked, pickled, reduced/no-added salt, packing medium
(water/brine/oil), and tortilla grain. Preparation prose such as chopped or
rinsed is ignored. Recovery requires a shared ingredient-identity token, so a
shifted relationship position cannot lend `canned` or `frozen` to a neighbouring
food. Text after the first `or` is excluded so alternatives such as `canned or
frozen` do not become an impossible combined state. `can` is recognized only
as a quantified container, not in prose such as `water can take...`.

The structured profiling tool now accepts aligned `ingredient_match_names`,
and `scripts/recompute_all_profiles.py` passes recovered match names both when
reusing stored weights and when running weight estimation. The old generic
chickpea aliases were removed: explicit canned/cooked context now selects the
right row, while genuinely bare chickpeas return `ambiguous_preparation_state`
instead of receiving an invented default.

Searched the current official Swedish Food Composition Database (SLV version
2026-07-01), Norwegian Food Composition Table API, Finland Fineli open-data
catalogue, and the already-reviewed Frida data. Four defensible exact rows were
added with provider IDs, original URLs, country/source metadata, and all eight
profiling nutrients:

- `matvaretabellen:10.197`, `Hoisin sauce, home-made`:
  https://www.matvaretabellen.no/en/hoisin-sauce-home-made/
- `slv:2557`, `Taco shells`:
  https://soknaringsinnehall.livsmedelsverket.se/Home/FoodDetailsMeta/2557
- `matvaretabellen:06.140`, `Ginger, canned (pickled)` (classified as Pickles;
  its analytical source is Finland Fineli):
  https://www.matvaretabellen.no/en/ginger-canned/
- `slv:4025`, `Wasabi paste`:
  https://soknaringsinnehall.livsmedelsverket.se/Home/FoodDetailsMeta/4025

The live EU table was rebuilt to 8,158 unique rows; the four vector documents
were added to `ingredient_vectors_v1`; and the reviewed alias table was
refreshed to 82 rows. Live matcher verification now gives curated matches for
hoisin sauce, hard taco shells, pickled/sushi ginger, and wasabi paste.
Explicit canned chickpeas match `Chick pea, canned, drained`; canned tuna in
water matches `Tuna in water tinned`; corn tortillas retain corn identity;
and smoked chicken searches the smoked form first, then uses the existing raw
chicken safe-parent fallback only when no valid smoked row exists.

No unsafe proxy was added for reduced-sodium soy sauce or reduced-salt prepared
stock. The Swedish table has low-salt stock paste/powder concentrates, but using
those for cup/ml quantities of prepared liquid would badly overstate sodium.
Likewise, ordinary soy sauce is not a valid reduced-sodium row. These variants
continue to abstain with `unsupported_nutrition_variant` until an exact prepared
product row is verified.

Verification:

- source-context and focused composition/matcher/profiling suite: 116 passed,
  214 subtests passed;
- live Postgres/Elasticsearch matcher probes confirmed all four new curated
  IDs and the intended chickpea/tuna/tortilla/smoked-chicken behavior;
- full repository: 990 passed, 301 subtests passed; the sole failure remains
  the pre-existing unrelated OpenRouter constructor mock, which omits
  production's intentional `max_tokens=2000` argument.

# codex — END: source-context recovery and additional European rows (2026-09-23)

# codex — BEGIN: remaining-match queue and continuation procedure (2026-09-23)

## Important correction: the short list is not the whole unmatched tail

The user-facing summary immediately before this handoff listed the most common
actionable failures, not every unmatched spelling. The latest diagnostic
reran the current matcher over the 1,366 names that were `none` or `weak` in
the v15 corpus snapshot. It produced 1,319 `none` names / 2,692 uses, 21
`weak` names / 127 uses, and recovered 26 names / 191 uses as strong or
curated. Of the `none` group, 935 names occur once, 192 occur twice, 57 occur
three times, and 135 occur at least four times. Twenty-five names / 70 uses
are recognized non-food objects. The long tail therefore must not be
described as 1,319 known nutrition bugs.

The diagnostic file is
`/tmp/current_remaining_from_v15.json`. It is a raw canonical-name rerun, not
an end-to-end source-aware profile run. Consequently it still shows cases
that runtime now handles correctly:

- `salt and pepper` and `salt and black pepper` are split before profiling;
- 128 of 135 high-frequency `chickpea garbanzo garbanzos` source rows recover
  explicit canned context; genuinely bare chickpeas still abstain;
- hoisin, taco shells, wasabi, pickled ginger, and prepared beef stock/broth
  now have curated rows;
- skewers, sticks, moulds, baking paper, and toothpicks are intentionally
  non-food;
- rocket/arugula, bok choy, and ciabatta have semantically correct weak
  candidates and need confidence calibration, not a different food.

No new high-frequency catastrophically wrong **accepted** match was found in
this pass. Most alarming candidates in the report are rejected with
`confidence=none`, which is the intended safety behavior. The remaining work
is improving useful coverage without turning those rejected candidates into
false matches.

## Current actionable queue, in descending frequency

First address the unresolved names with at least five corpus uses. Counts
below are from the canonical-name diagnostic and must be rechecked against
the original parsed line before changing matching behavior:

- 46 `mirin`;
- 43 vanilla paste (`vanilla paste` 33, `vanilla bean paste` 10);
- 37 lemongrass (`lemongrass` 31, `lemongrass paste` 6);
- 21 `balsamic dressing`;
- 21 `soba noodles`;
- 20 generic `stock` (genuinely ambiguous unless source context identifies
  the stock type);
- 17 reduced/low-fat ricotta;
- 16 `tabasco sauce`;
- 14 roasted capsicum variants;
- 11 each `chilli paste` and `garlic salt`;
- 10 each `cranberry sauce`, `hokkien noodles`, and `tandoori paste`;
- 9 each `dukkah` and `udon noodles`;
- 8 each `almond butter`, `grainy bread`, `granola`, `great northern beans`,
  `pumpkin seed oil`, and `sriracha`;
- 7 each `baking mix`, `borlotti beans`, `choy sum`, `coconut cream`, `lamb
  shanks`, `plum sauce`, and `ramen noodles`;
- 6 each `black rice`, `fettuccine`, `green apple`, `kimchi`, `kumara`,
  `marjoram`, `nori`, `palm sugar`, `slaw mix`, `steamed rice`, `tasty
  cheese`, `whole wheat pasta`, and `zucchinis`;
- 5 each `balsamic glaze`, `black-eyed peas`, `chipotle sauce`, `creamed corn`,
  `cremini mushrooms`, `dairy-free spread`, `farmer's cheese`, `four-bean
  mix`, `gai lan`, `garlic puree`, `green chilies`, `heavy whipping cream`,
  `hominy`, `lemon pepper seasoning`, `LSA`, `Monterey Jack cheese`, non-fat
  milk, reduced-fat tasty cheese, `seafood marinara mix`, `soda water`,
  `sushi rice`, `tamarind paste`, and `unsweetened cocoa powder`.

The four-use tier is the next queue: adobo/Cajun/poultry seasoning, almond
extract, cauliflower rice, chilli oil, Chinese rice wine, chipotle/laksa
paste, coconut yoghurt, cookie crumbs, cream-cheese spread, cream-style corn,
dark brown sugar, fat-free/non-fat dry milk, gluten-free flour, halloumi,
instant polenta, jicama, kiwi, lamb backstrap, lasagna noodles, liquid stock,
matzo meal, nori sheets, nut butter, penne, poppadums, pork cutlets, pumpkin
puree/soup, quinoa flakes, ranch dressing, rashers, rockmelon, salsa, sambal
oelek, slaw, snow-pea shoots, sponge fingers, sun-dried tomato paste, Swiss
brown mushrooms, teriyaki marinade, unsaturated oil, vanilla bean, vanilla
whey protein, and wholemeal pita pockets. Recreate the frequency report after
the high-frequency pass rather than treating this prose list as permanent.

Some entries already have an apparently suitable table row and are probably
matcher/alias fixes rather than data imports: fettuccine -> dry regular pasta,
kumara -> sweet potato, nori -> dried nori, zucchini -> courgette/zucchini,
heavy whipping cream -> whipping cream, unsweetened cocoa powder -> cocoa
powder, Chinese rice wine -> rice wine/sake, and possibly Great Northern
beans -> large white beans when preparation state is compatible. Validate
identity, form, and preparation before curating them; do not add broad shared-
word exceptions.

## Procedure for the next chat

Repeat the same evidence-first procedure used for stock, chickpeas, tortillas,
hoisin, wasabi, and pickled ginger:

1. Run a **full end-to-end read-only audit using the newly parsed recipes**,
   including `original_ingredient_line` and the recovered
   `ingredient_match_name`. Do not use only clean canonical names. Sort both
   rejected and weak results by recipe-use frequency, and separately inspect
   suspicious accepted matches.
2. For each high-frequency name, inspect representative source lines and
   measurements. Decide whether information was lost by parsing, recoverable
   from source context, genuinely ambiguous, non-food/negligible, or already
   explicit enough to match.
3. Search every existing Irish/EU composition row first. If a compatible row
   exists, fix a narrow alias, normalization, or compatibility rule and add a
   regression test. An alias must preserve the food identity; it must not be
   justified by a shared word alone.
4. If no row exists, search current official European national composition
   sources. Add a row only when its identity and state fit the recipe food.
   Keep provider-prefixed ID, source, country, source URL, and the eight
   profiling nutrients in `scripts/build_eu_global_dataset.py`; rebuild the EU
   table, add/update the vector document, and refresh the curated alias table.
5. Use a visible `safe_parent_fallback` only for a nutritionally defensible
   parent. Never discard species/food type, liquid-versus-concentrate state,
   canned/dried/raw state, or reduced-salt/reduced-fat qualifiers merely to
   gain coverage. Keep abstention for genuine ambiguity and unsupported
   variants.
6. Verify each change at three levels: focused matcher regression, live
   Postgres/Elasticsearch probe using the expected composition ID, and an
   end-to-end calculator/profile probe confirming weight and nutrient use.
7. Rerun the complete source-aware corpus audit after every batch. Report
   strong/curated/weak/none counts by both unique name and occurrence, list
   accepted safe-parent fallbacks, and manually review the highest-frequency
   changed matches. Then run the focused suite and full `uv run pytest`.
8. Append a new dated `# codex — BEGIN/END` section here with exact code/data
   changes, source URLs, database/index refreshes, audit counts, remaining
   abstentions, and test results. Never overwrite the historical results.

Start with the likely existing-row fixes above, then the 10+-use data gaps,
then the 5–9-use queue. A rejected match is preferable to a wrong nutrition
row; do not globally lower the similarity threshold or loosen the food-type
conflict checks to clear the list.

# codex — END: remaining-match queue and continuation procedure (2026-09-23)

# codex — BEGIN: accepted-match corrections for penne, herbs, corn, and kaffir lime (2026-09-23)

Rechecked the previously reported suspicious accepted matches against current
runtime behavior, original Neo4j ingredient lines, and existing EU composition
rows. Most of that report came from the stale v15 artifact and was already
fixed in the current tree: corn tortillas, generic broth, low-sodium broth,
mixed berries, pickled ginger, and chicken thigh fillets now resolve safely or
abstain. Tamari -> soy sauce remains a reviewed compatible identity (one source
line explicitly calls it Japanese soy sauce), and the existing frozen/boiled
edamame row fits the audited steamed/thawed recipe uses.

This batch made the remaining narrow corrections:

- plain `penne` / `penne pasta` -> CoFID `cofid:11-716`, `Pasta, white,
  dried, raw`;
- plain `italian herbs`, `italian herb mix`, and `italian herb blend` ->
  CoFID `cofid:13-884`, `Mixed herbs, dried`;
- unqualified `sweet corn` / `sweetcorn` -> CoFID `cofid:13-622`,
  `Sweetcorn, kernels, raw`; explicit canned and frozen source context still
  selects the corresponding prepared-state row;
- added a general gluten-free form boundary so ordinary pasta cannot invent a
  gluten-free formulation; explicit gluten-free pasta remains accepted;
- `kaffir lime leaves` can no longer match lime fruit. No complete compatible
  European composition row was found in the indexed national tables or the
  official-source search, so it now safely abstains rather than importing or
  fabricating a proxy.

No composition-table row was added: all accepted corrections reuse compatible
existing CoFID records. Postgres `ingredient_composition_aliases` was refreshed
to 89 rows. Live matcher and end-to-end calculator probes confirmed the
expected composition IDs and nutrient use.

The complete source-aware audit of affected Neo4j rows covered 203 uses / 74
distinct canonical-plus-source contexts: 64 curated uses, 62 strong uses, and
77 safe abstentions. The targeted bad-accepted check returned zero. The 77
abstentions include compound/alternative names outside this narrow batch and
are not all nutrition bugs. An unrestricted whole-corpus live sweep was
stopped after approximately ten minutes because serial Elasticsearch lookups
had not completed; it made no writes.

Verification: focused matcher/source-context/concentrate suite: 110 passed,
232 subtests passed. Live Postgres/Elasticsearch probes and an end-to-end
calculator probe passed for the changed rows.

# codex — END: accepted-match corrections for penne, herbs, corn, and kaffir lime (2026-09-23)

# codex — BEGIN: final accepted-match state, qualifier, and identity corrections (2026-09-23)

Ran a broad accepted-match scan followed by source-aware Neo4j validation of
the suspicious names. The scan found two systematic classes: explicit
low/reduced/no-added-sodium qualifiers were being stripped before final
compatibility checks, and state-less ingredients could still accept cooked
rows. It also found narrower identity errors such as green onion -> bulb
onion, salad greens -> spring greens, ground coriander -> fresh coriander,
spaghetti sauce -> carbonara, and spaghetti noodles -> chicken-noodle soup.

General matcher corrections:

- explicit low/reduced/no-added-sodium queries may only accept a row carrying
  the same qualifier; this check also applies after alternative selection and
  curated-alias lookup;
- cooked/boiled/fried/mashed state is now a dangerous extra form and cannot be
  invented by a candidate; explicit cooked/refried/stir-fry queries remain
  compatible;
- bare black/kidney/cannellini/white/pinto beans and brown/green/general/split
  lentils or peas abstain until dried, cooked, or canned state is known;
- generic mixed beans, meatballs, noodles, and unspecified vegetable mixtures
  abstain instead of selecting an arbitrary species, preparation, or dish;
- baby corn cannot collapse to ordinary sweetcorn, orange zest cannot use
  lemon zest, and `frying pan` is recognized as non-food equipment.

Reviewed aliases reuse existing EU rows (no composition rows added):

- green onion/scallion variants -> CoFID `13-352`, spring onions with bulbs
  and tops, raw;
- salad-green variants -> CoFID `15-648`, green salad;
- wholemeal couscous -> NEVO `5518`, unprepared wholemeal couscous;
- generic corn and corn-cob variants -> CoFID raw kernel/on-cob rows `13-622`
  and `13-623`;
- floury/waxy potatoes -> NEVO `1`, raw potato;
- mixed herbs -> CoFID `13-884`, dried mixed herbs;
- ground coriander -> CoFID `13-875`, coriander seed;
- rice-stick noodle variants -> Ciqual `9900`, dry raw rice vermicelli;
- dry spaghetti/fusilli/spaghetti-noodle variants -> CoFID `11-716`, white
  dried raw pasta;
- generic spaghetti sauce -> CoFID `17-621`, tomato-based pasta sauce;
- plural chillies -> Ciqual `20151`, raw chilli pepper.

Postgres `ingredient_composition_aliases` was refreshed to 114 rows. A
source-aware audit of the affected identities covered 721 uses / 77 distinct
contexts and found zero targeted bad accepted matches. A separate audit of
1,624 low/reduced/no-added-sodium source rows found zero regular-sodium rows
still accepted. End-to-end calculator probes confirmed reviewed IDs and safe
abstention for unsupported baby corn and reduced-sodium black beans.

Verification: focused matcher/non-food/source-context/concentrate suite: 116
passed, 234 subtests passed. Full repository: 996 passed, 307 subtests passed;
the sole failure remains the pre-existing unrelated OpenRouter mock that omits
production's intentional `max_tokens=2000` argument.

# codex — END: final accepted-match state, qualifier, and identity corrections (2026-09-23)

# codex — BEGIN: durable new-parser snapshot and deterministic weights (2026-09-24)

Recovered and preserved the paid-LLM parser outputs under
`data/processed/ingredient_parsing_final/2026-09-24/parsed/`. These nine files
are byte-for-byte copies of the recovered finals: 4,514 recipes and 43,975
ingredient rows. Their hashes are in the adjacent `MANIFEST.md`; the old
`data/processed/ingredient_parsing_review/` directory is not authoritative.

Created a reproducible, non-destructive correction layer at
`weight_ready_parsed/` using `scripts/prepare_weight_ready_parser_snapshot.py`.
The generic explicit-unit recovery lives in production
`parse_recipe_tool.py` and is called for every new parse; the snapshot script
imports that same function. The correction layer is therefore only a
migration for already-paid immutable outputs, not an alternative parser.
The paid output remains immutable. `CORRECTIONS.json` records 598 adjacent
exact duplicate removals, 52 individually source-verified measurement
repairs, 175 count units restored from exact parser display text, and 417
explicit source units restored from bare/empty measurements (397 sprays and
20 handfuls). The resulting corpus has 43,377 raw rows and 43,563 uses after
production's salt/pepper split.

Reran matching against that exact corrected corpus, without an LLM:

- canonical-name audit: 39,718 strong/curated accepted (91.1737%), 291
  intentionally ignored, 91.8417% handled;
- source-context audit: 38,940 strong/curated accepted (89.3878%), 291
  intentionally ignored, 90.0558% handled.

Reports are `matching/weight_ready_canonical_match_audit.json` and
`matching/weight_ready_source_context_match_audit.json` beneath the dated
snapshot directory. No database was modified.

Finalized the deterministic weight path in
`src/recipe_wrangler/tools/ingredient_weight_tool.py`:

- live LLM fallback has an explicit `LIVE_WEIGHT_LLM_ENABLED` switch and the
  corpus audit forces it off;
- unverified cached/offline LLM estimates are disabled by default;
- explicit mass and package parsing no longer turns large bare gram totals
  into counts (for example, `400` canned tomatoes no longer becomes 400
  cans);
- inferred leaf/slice/stalk units cannot inherit an unrelated food-ID-only
  FDA portion;
- reviewed count, volume, density, spray, herb, produce, seafood, pastry, and
  package references cover recurring deterministic signatures;
- low-similarity hybrid USDA lookup now requires lexical food identity after
  reviewed regional-name normalization. This removes false unit-compatible
  matches such as frisée→triticale, silverbeet→canned beets,
  orecchiette→peach pie, lasagne sheets→cookies, microgreens→soybeans, and
  stems→maple syrup. Unsupported signatures abstain instead.

The final report is
`weight/final_deterministic_weight_audit.json`: 39,804/43,563 resolved
(91.3711%), 3,759 unresolved, 0 direct-mass mismatches, and 0 live-LLM uses.
All >1 kg audited ingredient weights are now source-plausible bulk quantities;
the known catastrophic parser/unit explosions are absent. This is a coverage
and anomaly result, not a formal accuracy score: complete dataset-wide
accuracy still requires a human-labeled grams sample. The leading remaining
abstentions are mostly genuinely missing quantities (for example oils, herbs,
and water) or unsupported portion nouns (`bunch`, `handful`, generic pieces).
Do not inflate coverage by guessing those amounts.

The audit is reproducible with `scripts/audit_new_parser_weights.py`. Neo4j
and PostgreSQL have not been synchronized to this corrected snapshot. vLLM
was not used and no vLLM/API-server process remained running.

Verification: the focused weight/profiling/matching suite passed 202 tests and
279 subtests. The full repository passed 1,009 tests and 321 subtests with one
pre-existing unrelated failure: `test_parser_builds_openrouter_client` expects
the old constructor call without production's intentional `max_tokens=2000`.

# codex — END: durable new-parser snapshot and deterministic weights (2026-09-24)

# codex — BEGIN: deterministic-weight completion and anomaly review (2026-09-24)

Finished the deterministic pass against the durable **new-parser and
new-matching** snapshot. No paid parser file was edited. The production parser
recovery function and its migration caller now preserve explicit leaf, head,
bulb, piece, pod, rasher, cob, spear, wedge, centimetre, spray, and handful
units from source `display`/`note` fields, including when the parsed
measurement contained only a size word. The regenerated weight-ready snapshot
contains 43,377 rows, 598 removed adjacent duplicates, 54 source-verified
measurement repairs, 176 restored display count units, 523 restored source
units, and 1,263 explicit optional/to-taste blanks marked for the existing
0.5 g negligible policy. The immutable paid outputs remain under `parsed/`.

Expanded only narrow deterministic references or inference rules with a
reviewable food identity. This covers recurring counts and portions such as
eggs, produce, tortillas/breads, poultry/meat pieces, bunches, handfuls,
sprays, pinches, sheets, stalks, pods, wedges, spears, and regional ingredient
names. Local USDA data supplied the drained oil-packed sun-dried-tomato piece
reference (3 g); a 10 cm cucumber source corruption was repaired to preserve
the dimension. Non-food twine/string/tool rows receive the existing negligible
non-food treatment.

The large-weight review also fixed false calculations that coverage alone hid:
lettuce leaves no longer become whole heads; chicken legs no longer become
whole chickens; squash blossoms no longer become whole squash; dried arbol
chiles no longer become bell peppers; bread cubes no longer match ice cubes;
ice water no longer uses ice-cube cup density; spaced `silver beet` now uses
the raw chard cup reference; fruit and vegetable cups no longer inherit
unrelated USDA portions; and vegetable stock/broth remains water-like rather
than matching generic vegetables. Unsupported generic lettuce counts now
abstain instead of guessing.

Final reproducible report:
`data/processed/ingredient_parsing_final/2026-09-24/weight/final_deterministic_weight_audit.json`
(SHA-256
`380a4e3032deff9d100fb711d2afb97f2977373bddfb9f0ad810d4bd30a6eada`).
It covers 4,514 recipes / 43,563 post-split ingredient uses:

- 42,281 resolved deterministically (97.0571% coverage);
- 1,282 deliberate abstentions: 505 missing supported portion conversion,
  359 missing defensible unit, 247 missing quantity, 171 missing compatible
  food identity;
- 0 direct-mass arithmetic mismatches;
- 0 live-LLM uses, with live and cached unverified LLM fallbacks disabled;
- 277 rows over 1 kg retained in the report for continued review.

The 97.0571% value is deterministic **coverage**, not labeled accuracy. The
remaining rows are the current conservative ceiling; many require source-line
repair, a reviewed portion reference, or human labels. Do not turn blank oil,
water, flour, meat, or generic portion nouns into guessed weights merely to
raise the percentage. `input_quality` also retains 403 review flags (243
display/measurement disagreements, 125 duplicate signatures across recipes,
and 35 display-mass clues absent from measurement); these are audit leads, not
all confirmed calculation errors.

Verification: focused parser/weight/non-food suite passed 82 tests and 40
subtests during development. The broader parser, weight, matching,
source-context, and profiling suite passed 203 tests and 271 subtests. vLLM
was not started or used. Neo4j and PostgreSQL were not mutated.

# codex — END: deterministic-weight completion and anomaly review (2026-09-24)

# codex — BEGIN: LLM plan for remaining deterministic abstentions (2026-09-24)

The deterministic stage is complete enough to proceed. Its final result is
42,281/43,563 resolved uses (97.0571%) and 1,282 abstentions (2.9429%), with
zero direct-mass arithmetic mismatches and zero LLM calls. The implementation
recovered explicit source units, added reviewed count/portion/density
references, fixed unsafe cross-food and whole-item inferences, protected mass
and package arithmetic, handled unambiguous non-food rows, and disabled live
and unverified cached LLM fallbacks. The remaining LLM stage must augment this
result, not replace or silently weaken these safeguards.

Deduplicate before calling a model. The 1,282 unresolved uses contain 1,102
unique `(ingredient, measurement, error)` signatures:

- missing supported portion conversion: 505 uses / 477 signatures;
- missing defensible unit: 359 uses / 300 signatures;
- missing quantity: 247 uses / 157 signatures;
- missing compatible food identity: 171 uses / 168 signatures.

Recommended model: pinned `gpt-5.4-mini-2026-03-17`, through the Responses
Batch API, with structured JSON output and low reasoning effort. It is a
better fit than nano for ambiguous ingredient identity, unit, preparation,
and source-context judgments, while the full flagship model is unnecessary
for this small, constrained extraction/estimation task. Official model page:
https://developers.openai.com/api/docs/models/gpt-5.4-mini. The Batch API runs
asynchronously within its 24-hour completion window and discounts input and
output by 50%:
https://platform.openai.com/docs/api-reference/batch/object?api-mode=responses.

Each unique request should include the canonical ingredient, parsed
measurement, original `display`, `note`, recipe title, original source line
when available, deterministic failure reason, and any compatible USDA/FDA or
saved-candidate evidence. Require a schema containing at least: inferred unit,
quantity if recoverable, grams per unit, total grams, plausible range,
confidence, evidence/rationale, and an explicit `abstain` decision. Missing-
quantity rows must be allowed to abstain; an LLM cannot recover information
that is absent from both the source line and recipe context.

Never write model output directly into accepted production references. Save
new results as `pending_review`, reject arithmetic/range/source-line
inconsistencies, manually sample high-weight and low-confidence results, and
promote only validated signatures to `accepted_deterministic`. Preserve the
model snapshot, prompt version, input signature, raw response, token usage,
validation result, and supporting examples in the reviewed portion table.

Cost estimate as of 2026-09-24, using official prices of $0.75 per million
input tokens and $4.50 per million output tokens for GPT-5.4 mini:

- compact run (about 600 input + 120 output tokens × 1,102 signatures):
  approximately $1.09 standard or $0.55 with Batch;
- conservative run (about 1,000 input + 200 output tokens × 1,102):
  approximately $1.82 standard or $0.91 with Batch;
- budget roughly $1–$2 for one Batch run, or $2–$4 if ambiguous rows are
  retried/reviewed with a second pass. Actual cost depends on prompt length,
  generated/reasoning tokens, retries, and account/region pricing.

GPT-5.4 nano is cheaper ($0.20/M input and $1.25/M output; roughly $0.15 for
the compact Batch estimate) but should be used only for optional triage, not
as the final authority for the ambiguous 2.94%. Official nano page:
https://developers.openai.com/api/docs/models/gpt-5.4-nano.

OpenRouter-specific correction: if this run is sent through OpenRouter rather
than directly to OpenAI, benchmark `openai/gpt-oss-120b:exacto` first. Its
listed OpenRouter rate on 2026-09-24 is $0.03/M input and $0.17/M output,
making the estimated 1,102-signature run about $0.04 compact or $0.07 with the
conservative token assumption (reasoning tokens/retries can increase this).
`openai/gpt-oss-20b` is $0.02/M input and $0.10/M output, approximately
$0.03--$0.04 for the same run, but the saving versus 120B is only a few cents;
prefer 120B if a stratified reviewed benchmark confirms acceptable errors and
abstention behavior. Pricing pages:
https://openrouter.ai/openai/gpt-oss-120b and
https://openrouter.ai/openai/gpt-oss-20b. Keep GPT-5.4 Mini as the comparison
or escalation model, not the automatic default under OpenRouter.

Historical model clarification: the repository's local vLLM parser was served
as `ingredient-tagger` on port 8008 and was a Llama 3.1 8B model. The durable
paid parsing corpus was not generated exclusively by that local model: the
ingredient-line parser was configured for
`meta-llama/llama-3.3-70b-instruct` through OpenRouter or local vLLM, and the
handoff records that MyPlate ran partly locally and partly through OpenRouter.
GPT-OSS 20B was also a verified Groq parser option, but GPT-OSS 120B was not
the historical local parser.

# codex — END: LLM plan for remaining deterministic abstentions (2026-09-24)

# codex — BEGIN: GPT-OSS 120B OpenRouter runner status (2026-09-24)

Added `scripts/run_openrouter_weight_review.py`, a resumable deduplicated runner
for `openai/gpt-oss-120b:exacto`. It sends up to ten signatures per structured
request, includes recovered display/note/recipe context, permits explicit
abstention, validates confidence/ranges/arithmetic, and appends only
`pending_review` records to
`weight/llm/gpt_oss_120b_pending_review.jsonl`. It never updates accepted
references or production databases. The corpus contains 1,102 signatures; the
runner recovered display or note context for 1,022 of them. Its focused test
passes.

The first four-case probe was rejected before inference with OpenRouter HTTP
403 `Key limit exceeded`. The configured key reports a $12 total limit, $0
remaining, and $12.215738975 usage. No model output was generated and no new
charge was incurred. After increasing the key limit or replacing
`OPENROUTER_API_KEY`, run:

```
uv run python scripts/run_openrouter_weight_review.py --limit 40
```

Review the sample output, then resume all remaining signatures with the same
command without `--limit`. Existing JSONL signature IDs are skipped.

# codex — END: GPT-OSS 120B OpenRouter runner status (2026-09-24)

# codex — BEGIN: end-to-end nutrition validation plan (2026-09-24)

The work is one ordered pipeline, not three independent audits:

1. enrich and correct parsing so ingredient identity, quantity, unit, and
   preparation reach downstream code correctly;
2. enrich and correct nutrition matching so each parsed ingredient resolves
   to the appropriate regional composition-food identity;
3. enrich and correct weight calculation so matched ingredients receive a
   defensible edible gram weight, while genuinely ambiguous rows abstain.

All three improvements are inputs to the actual objective: more accurate
recipe nutrition calculations. Parsing, matching, and weight coverage are
necessary diagnostics, but none is the final accuracy score.

After the remaining weight candidates are reviewed and accepted or rejected,
regenerate the nutrition profiles from the latest parsing, matching, and
weight artifacts. Evaluate those new profiles in two complementary ways:

1. compare calculated recipe nutrition against available recipe-level source
   truth, using aligned serving counts and nutrient units;
2. calculate each recipe with the supported regional composition profiles and
   compare those regional outputs with one another.

Report per-nutrient absolute and relative errors for source-truth recipes, plus
median, percentile, and outlier differences between regional profiles. Small
regional differences are expected because composition tables and foods differ,
but large divergences should be traced back to parsing, selected food identity,
weight, serving normalization, or nutrient-unit conversion. Do not force
regional profiles to be identical; use unexpectedly large differences as an
error-finding signal. Preserve the exact artifact versions and coverage for
each comparison so improvements can be attributed to this new pipeline.

The GPT-OSS weight-review run is currently resumable in tmux as
`weight-oss120b`, using five-signature batches after larger responses proved
less structurally reliable. Existing JSONL records are retained across every
resume and remain `pending_review`; they must not enter profile generation
until validation and promotion are complete.

# codex — END: end-to-end nutrition validation plan (2026-09-24)

# codex — BEGIN: final semantic weight hardening (2026-09-24)

The completed GPT-OSS run did **not** make every ingredient weight robust.
Its 1,102 responses were treated as candidates only. Arithmetic validation
alone had marked 258 signatures as pending-valid, but semantic review found
unsafe guesses (generic `stems`, inferred dressing portions, oversized
blueberries and squash, package assumptions, and recipe-specific evidence
leaking across repeated signatures). No LLM confidence score is sufficient
for promotion by itself.

Added `scripts/review_openrouter_weight_candidates.py`. It promotes a
candidate only when the result is independently reproduced by an existing
code-reviewed portion, a physical density already used by the calculator, or
an explicit source mass that belongs to exactly one recipe occurrence. The
review is durable at
`weight/llm/gpt_oss_120b_semantic_review.json`; independently accepted rows
are at `weight/reviewed_openrouter_weight_references.csv`. The final overlay
contains only two unique references: light coconut milk at 1.03 g/ml and
cannellini beans at 1.066667 g/ml (the source gives the equivalent 400 g
alternative). The calculator loads these as `accepted_deterministic`; all
other LLM outputs remain abstained or rejected and never enter profiles.

Source-context recovery was also tightened before weighting. Standard units
dropped from `display`/leading `note` fields are now restored, including
teaspoons, tablespoons, cups, metric volumes, sprigs, stalks, slices, pieces,
bunches, and cubes. Ambiguous alternatives such as “oil or cooking spray” are
still not inferred. The regenerated snapshot has 65 exact source-verified
mass repairs, 181 restored display-count units, and 690 restored source units.
Eleven new mass repairs are recipe-specific rather than signature-wide. This
corrected a concrete LLM error: the source's half large bunch of kale is about
100 g total; the model had incorrectly halved that again to 50 g.

Added exact quart, pint, and decilitre volume conversions. Removed the old
blank-quantity seasoning fallback that silently assigned 0.5 g to 291 rows,
including powdered sugar, soy sauce, dressing, stuffing, and crumbs. Explicit
optional/to-taste rows still receive the reviewed negligible policy, but a
truly blank quantity now abstains. This intentionally lowers coverage while
improving accuracy.

Definitive report:
`data/processed/ingredient_parsing_final/2026-09-24/weight/final_deterministic_weight_audit.json`
(SHA-256
`ea2ccee0270d63fe9bfaf5444b7d76f099339f3888ee2d9d2e67191ed1c0ccfe`).
It covers 4,514 recipes / 43,563 post-split ingredient uses:

- 42,100 defensibly resolved uses (96.6416%);
- 1,463 deliberate abstentions: 536 missing quantity, 493 missing supported
  portion, 263 missing defensible unit, and 171 missing compatible food
  identity;
- zero direct-mass arithmetic mismatches and zero live-LLM uses;
- all reviewed >2.5 kg rows are source-plausible bulk water/milk, whole birds,
  potatoes, tomatoes, sugar, or similarly explicit recipe quantities.

The lower 96.6416% is the honest accuracy-oriented result and supersedes the
earlier 97.0571%/97.31% coverage snapshots. It is not a labeled accuracy
score. The remaining rows must stay unresolved unless new source evidence or
reviewed references are supplied. Focused parser, weight, portion, USDA, and
semantic-review verification passed 93 tests and 40 subtests.

# codex — END: final semantic weight hardening (2026-09-24)

# claude — BEGIN: blank-quantity policy for water/herbs/salt/oil (2026-09-25)

Parser recovery work (compound-volume arithmetic, display/note unit recovery, "N g each",
raw-source quantity recovery of 617 rows, section-header removal) is complete; snapshot
regenerated. Blank-quantity policy, decided with the user by nutritional impact:

- **water / ice / garnish herbs** (blank quantity): `_is_blank_negligible_ingredient()` in
  `ingredient_weight_tool.py` routes them to the existing 0.5 g `to_taste_min` policy.
  Nutrient contribution is nil at any plausible amount.
- **salt**: stays unresolved (sodium feeds Nutri-Score). Decide in the profile stage by
  comparing sodium error vs source-truth recipes for: excluded / 0.5 g / ~1 g.
- **oil**: stays unresolved; `unquantified_oil_recipes` in the audit JSON lists flagged recipes
  (41). Same source-truth test can decide a policy.
- Bug fixed: blank `olive oil` resolved to 4 g (one olive) because `_infer_unit_from_name`
  treated the modifier "olive" as a countable unit with inferred qty 1. Names containing
  `oil` now return no inferred unit.

Audit: 42,347/43,571 resolved (97.19%), 1,224 abstentions, 0 mass mismatches. Next: bare `1.0`
measurements (118 uses), then the salt/oil source-truth test. Nothing committed.
# claude — END

# claude — BEGIN: bare-measurement fixes, alternatives, stock cubes (2026-09-25)

Snapshot layer (`prepare_weight_ready_parser_snapshot.py`) + `parse_recipe_tool.restore_*`:
- Fixed `_SOURCE_PORTION_UNIT_RE` (`pinches?` never matched "pinch"/"dash"/"splash").
- Display unit+quantity now overrides a wrong bare quantity ("1/2 teaspoon" parsed 1.0) but NOT
  for raw-source lines (`allow_quantity_override=False`; fuzzy line assignment can pick a sibling line).
- "a few sprigs" = 3 sprigs; `bags` added; note mass ("60g / 2oz.") recovered for qty 1 or dual-unit form.
- Alternatives: note cut at leading/inner "or"/";" (regex `(?:^|\s)or\s+|;`) so alternative units/masses are ignored.
  4 source-verified repairs where the parser took the "or" clause (chicken breast, onion, eggplant, enchilada sauce).
- `_title_key` strips SafeFood "(dinner|lunch|...)" suffix: previously all such recipes silently skipped raw-source recovery.
- Rows with to-season/optional notes no longer borrow a quantity from a same-name raw line.
- Stock cubes (Option A): "N stock cube dissolved in W ml water" -> `N cube`, name gets " cube" so the matcher picks
  dry "Stock cubes, beef" (sodium) not "Bouillon, beef, cube, prepared". 8 renames. 10 g/cube reference.
Audit: 42,370/43,571 (97.24%), 0 mass mismatches. Open: salt/oil source-truth test; group A per-item weights
(true counts only, container-implied rows abstain); "low-salt ... stock cube" has no matcher entry (confidence none);
whole-chicken/`1 whole` units; herbs-in-Nutri-Score check. Nothing committed.
# claude — END

# claude — BEGIN: edible yield, per-item weights, stock/bouillon forms, source-quantity repairs (2026-09-25)

Weight tool (`ingredient_weight_tool.py`):
- Whole-bird edible yield: name must contain "whole" + chicken/duck (the snapshot layer adds it via
  `restore_whole_bird_name` from display/note evidence; parts exempt). Purchase weight (direct mass or the 1600 g chicken /
  2000 g duck ready-to-cook reference) x USDA yield (chicken 0.608 meat+skin / 0.434 skinless, duck 0.633 / 0.302).
  Match type `direct_mass_edible_yield`; detail carries `purchased_grams`, `edible_yield_factor`; audit validates qty x unit x factor.
  NOT applied to bird parts, plain "chicken 1 kg", or turkey (no local USDA yield; roast turkey 6 kg still counted whole).
- USDA per-item refs: iceberg 539 g/head, cos 626 g/head, brussels sprout 19 g, portobello cap 84 g, turnip 61/122/183 g,
  English muffin 57 g, cauliflower whole=head; bare counts of iceberg/cos lettuce -> head, turnip -> whole, mangos plural.
  Container-implied bare counts (chickpeas 3, yogurt 1, walnuts 10) still abstain.
Snapshot layer / parser recovery:
- SuperValu "1 litre Chicken Stock Cube" (volume) -> prepared "chicken stock". Cube rows -> dry cube names.
- `restore_bouillon_form`: bouillon + tsp/tbsp -> "... granules"; "cube" in primary note -> cube; low-sodium note -> "low sodium ..." (matcher abstains).
- `restore_leading_quantity_from_source`: raw line leading "<qty> <unit>" corrects a wrong parsed quantity only if same unit, no
  range/alt, and the row's own display leads with the same quantity (fixed FoodHero "1 1/2 cups" parsed 2.5, "2 1/2" parsed 5.0, etc; 12 rows).
- 4+3+4 VERIFIED_REPAIRS (alternatives taken instead of primary; whole-bird kg from raw line; buns 12->6, hot peppers 3->1.5, ...).
Matcher/data: 8 concentrated stock rows added to `SUPPLEMENTAL_FOODS` (fineli:29009, frida:1253, 6 retail labels: UNVERIFIED, the
source pages returned 403; energy/macros are internally consistent). Loaded to Postgres `nutrients-ingredients-eu` (backup
`backups/nutrients-ingredients-eu_20260925_pre_stock_rows.sql.gz`) and ES `ingredient_vectors_v1` via
`scripts/elasticsearch/add_supplemental_vectors.py`. 27 alias rows added to `ingredient_composition_aliases.csv` (backup in
`backups/`) and upserted to `pipeline_static_data`; reduced-salt aliases now bypass the unsupported-stock-qualifier abstention
(only for exact alias names). duck/whole duck -> ciqual:36204 (was Duck terrine).
Audit: 42,420/43,571 (97.36%), 0 mass mismatches. Open: salt/oil source-truth test (SafeFood/HealthyFoods have recipe
nutrition), 418->~150 input-quality leads (259 disagreements mostly corrupted display digits, measurement is right), herbs in Nutri-Score check,
chicken row "Chicken, meat, raw" skin status unknown (factor assumes meat+skin), turkey yield, hamburger bun/baguette refs (no USDA row).
Nothing committed.
# claude — END

# claude — BEGIN: salt/oil blank-quantity policy from source-truth test (2026-09-25)
- Blank plain salt = 1 pinch (0.3 g), user decision, flagged `blank_quantity_policy: salt_pinch`
  (`_is_blank_salt` in ingredient_weight_tool.py). Black salt / other seasonings still abstain.
- Test: `scripts/composition/evaluate_salt_oil_policy.py` (report: data/analysis/salt_oil_policy/report_eu.md), 112 recipes
  with blank salt/oil AND source nutrition (SafeFood 43, HealthyFoods 61 for salt). Sodium MAE per serving, n-weighted:
  0 g 212 mg, 0.3 g 207 mg, 0.5 g 230 mg, 1 g 340 mg, 2 g 700 mg, 5 g 1900 mg. SafeFood best at 0 g, HealthyFoods best at 0.3-0.5 g.
  Pinch is at least as good as nothing; larger defaults clearly hurt. Differences at 0-0.5 g are inside noise (base MAE 150-250 mg).
- Oil: too few truth recipes (grease 3, fry 1, other 7) to calibrate. Implemented only: blank oil whose note/display says
  grease/non-stick/brush -> unit `greasing` = 2 g (GREASING_OIL_GRAMS, flagged placeholder; 0-10 g changes recipe kcal error <5%).
  2 corpus rows. Frying/unspecified blank oil stays unresolved, listed in `unquantified_oil_recipes` (35 recipes).
  EuroFIR recipe guideline: for frying fat count only the amount absorbed. To calibrate frying, need truth recipes with
  nutrition that contain frying oil (MyPlate/FoodHero JSON has none).
Audit: 42,625/43,571 (97.83%), 946 unresolved (231 quantity, 409 portion, 185 unit, 121 identity), 0 mass mismatches. Nothing committed.
# claude — END

# claude — BEGIN: weight tool completion pass (2026-09-25)
Final audit: 43,459/43,571 uses have a weight (99.74%); 112 unresolved (0.26%); 0 direct-mass mismatches.
**Measured coverage excluding policy defaults: 43,038 (98.78%).** Policy defaults flagged via `blank_quantity_policy`:
salt_pinch 203 (0.3 g), oil_default 35 (5 g placeholder, uncalibrated), negligible_default 183 (0.5 g). They are not measurements.
Added this pass:
- Blank-quantity policy (`_apply_blank_quantity_defaults`): lines with no amount never show 0 g; bare-number salt/oil too.
- `_REVIEWED_CUP_GRAMS` (USDA cup weights, ~50 foods) + `_GENERIC_SPOON_GRAMS` (4/8/12 g convention) for volume rows.
- `reviewed_item_weights.py`: ~150 per-item/container rows (USDA where in local table, else flagged `convention`); bare counts
  infer unit via `reviewed_default_unit`. Match types: reviewed_cup_density_fallback, reviewed_item_weight_fallback,
  generic_spoon_convention (confidence 0.35-0.65). Multi-word units ("small scoops", "few drops") resolve via measurement words.
- Parser fixes: "1½ cups" (digit+unicode fraction) parsed as 1.5 ml; "1 tbsp X combined with 2 tbsp Y" first-term only.
Remaining 112 (need new source data, not code): ~22 "1 portion" of another recipe component (tomato sauce, bean mix...),
~25 unspecified products/mixes/kits (baking mix, seasoning mix, slaw mix, dressing), ~35 bare numbers with unknown unit
(peas 1.25, rice 1, pasta 4, chicken 0.5/6, pesto 1.5, ham 1), ~30 odd units/foods (44 cm pizza base, "couple", "twist", "crown",
nopalitos/stems in cups, elderflower blossoms, vegeta). Also unchanged: turkey 6 kg counted whole (no yield in local USDA),
retail stock rows unverified, herbs-in-Nutri-Score not checked, 0.3 g salt pinch is inside test noise.
Convention values to review before publishing: chicken thigh/drumstick/wing use bone-in USDA weights (nutrient row is edible meat).
Nothing committed.
# claude — END

# claude — BEGIN: LLM pass and recipe exclusion (2026-09-25)
LLM pass (gpt-oss-120b, OpenRouter, $0.002): 63 unresolved signatures (bare numbers and odd units, excluding sub-recipe
portions and unspecified mixes) -> 56 abstained, 5 invalid, 2 valid (dry cider 500 ml, small jug of water). The LLM has no
information beyond the row; nothing accepted from it except the cider density (already deterministic). Output:
weight/llm/gpt_oss_120b_final_pending_review.jsonl (pending_review, not used).
Decision (user, 2026-09-25): recipes that still had an ingredient without a weight are dropped from the weight-ready corpus:
99 recipes (excluded_recipes.json in the snapshot dir lists each with its unresolved rows). Done by
prepare_weight_ready_parser_snapshot.py (reads excluded_recipes.json); immutable parsed/ files untouched. Neo4j/Postgres/ES were
NOT changed (not yet synced to this snapshot); when syncing, skip these recipes, or hide live copies with scripts/disable_recipes.py.
Corpus now 4,415 recipes / 42,478 rows. Tests: pytest LIVE_WEIGHT_LLM_ENABLED=false in tests/conftest.py (a working OpenRouter key made
the live fallback answer in tests). NOTE production default LIVE_WEIGHT_LLM_ENABLED=true.
# claude — END

# claude — BEGIN: final advisor scan and fixes (2026-09-25)
Final corpus: 4,414 recipes / 42,650 ingredient uses; 0 without a weight; 0 direct-mass mismatches.
Weight basis: measured/USDA 92.70% (39,538), reviewed table or convention 6.27% (2,675), policy placeholder 1.02% (437:
salt_pinch 196, oil_default 31, negligible_default 210). Quote the 92.7% as measured.
Advisor findings fixed:
- Blank lines leaked a whole-item weight via `reviewed_default_unit` (blank yoghurt = 150 g). `_infer_unit_from_name(bare_count=False)` for
  lines with no amount; blank now always gets the 0.5 g default (test added).
- Reviewed cup table ran before the audited USDA cup lookup: moved after it (gap filler). It still fires ahead of a direct USDA
  portion in some flows; a full per-use diff (REVIEWED_TABLES_ENABLED=false vs true, scripts/audit_new_parser_weights.py --dump)
  showed 2,421 changed uses, mostly corrections (rolled oats 219 -> 81 g/cup, caster sugar 120 -> 200, curry paste 4 -> 11 g/2 tsp,
  baby spinach 960 -> 120 g/4 cups). Over-captures found and fixed: salad potatoes (salad leaves), split peas, ricotta/mascarpone
  (cheese), mustard greens, squash blossoms, corn husks/tacos (corn ears), broccoli florets (608 g bunch), kale leaves, chickpeas
  as tins (container-implied count: stays unresolved; recipe "Mean Bean Salad" added to excluded_recipes.json, now 100 recipes).
- LIVE_WEIGHT_LLM_ENABLED default changed to false (was true; live estimates would replace low-confidence reviewed weights).
  REVIEWED_TABLES_ENABLED env switch kept for diff runs.
Open: convention values need human review (chicken thigh/drumstick/wing use bone-in USDA weights; turkey 6 kg counted whole),
retail stock rows unverified, herbs-in-Nutri-Score check, 0.3 g salt pinch inside test noise.
Working tree has many unrelated changes from other sessions: stage only weight-tool files, tests, scripts, handoff when committing.
# claude — END

# claude — BEGIN: end-to-end accuracy check (2026-09-26)
`scripts/composition/evaluate_profile_accuracy.py`: per-serving nutrition recomputed with the new weights (EU profile) vs each recipe's
published nutrition. SafeFood 324 recipes, HealthyFoods 1,500 (random sample). Reports in data/analysis/profile_accuracy/tables_{on,off}.
Reviewed tables ON: median abs error kcal 14% (SafeFood) / 22% (HF); fat 33% / 29%; sat fat 38% / 38%; sugar 22% / 27%;
sodium 38% / 42%. Within 25%: kcal 65% / 56%. Median calc/truth: kcal 0.98 / 0.93, sodium 0.96 / 0.77.
Tables OFF: kcal 16% / 23%, sodium 37% / 43%: the reviewed tables are neutral to slightly better, never worse in aggregate.
The remaining error is mostly not the weight tool (ingredient matching, cooking losses, serves, composition-table differences). Sodium is
under-counted on HealthyFoods (0.77): missing salted products/seasoning. Chicken wings now 41 g edible (was 107 g bone-in piece).
Thigh/drumstick USDA portions are edible weights (no clean bone-in yield available locally); turkey yield still missing.
# claude — END

# claude — BEGIN: closure (2026-09-26)
User decision: the 4 whole-turkey/turkey-leg recipes (Parcel-baked roast turkey with cider glaze, Roast turkey with traditional herb stuffing,
Roast turkey (dinner), Christmas turkey leg (dinner)) are excluded like the other unresolvable ones. excluded_recipes.json: 104 recipes.
Corpus: 4,410 recipes. Known limitations left as stated: convention weights (6.3% of uses), bone-in parts by mass/count in ~10 recipes
(Shorba lamb bones, Citrus Chicken, Chicken Curry, Chicken Paprikash, Roast Turkey Breast, lamb shanks 200 vs 340 g), chicken thigh/drumstick
USDA weights unverified as edible, retail stock rows unverified (46 uses / 45 recipes), HealthyFoods sodium ~23% low.
Committed locally, not pushed.
# claude — END
