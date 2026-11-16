# Evaluation methods

How each step is measured, which labels count as references, and how judges and humans rate open-ended results. Results live in the step folders ([README](README.md)). Report every metric separately with full denominators; never average into one score. A failed deterministic check cannot be overridden by a judge or a human rating. Thresholds marked * are proposals, not frozen.

## Evidence levels and labels

Keep four things distinct: a unit regression passes; a pipeline agrees with authored expected cases; predictions agree with source/reference labels; users find the platform useful. None alone establishes the other three.

Every case and label needs an origin: explicit source record/food ID, published annotation, internally authored expectation, model-proposed label, or unresolved. Use source-derived and public labels where they fit. Handcrafted inputs and LLM judgments are valid internal tools, with their limitations declared; they are not independently expert-validated gold.

The user does not plan an external ingredient/allergen annotation campaign. Resolve internally testable assertions using original source text, authoritative definitions, fixed calculations and public labels. Keep ambiguous expected outcomes as unknown/multiple acceptable answers rather than fabricate independent validation. Study participants will assess whole-platform tasks, not these component labels.

## Metrics by step

### Analyze (deterministic only; no LLM judge)

| Stage | Metrics |
|---|---|
| Parsing | Schema-valid recipes; quantity accuracy; unit accuracy; FOOD-name lexical F1 (eligible-field denominators) |
| Weights | Estimates returned / all cases; within 20% of Fineli / all cases; median and mean absolute error; incompatible references found by trace review |
| Matching | Exact record (same code as expert); identity class (same food other table / different state or part / different food / no match); correct identity among aligned lines; matched occurrences; profiles with complete energy; Nutri-Score grade kept/gained/lost |
| Matching, nutrient view | Exact / equivalent / off / harmful. Proposed*: energy ±10% or ±20 kcal/100 g; macros ±20% or ±2 g; salt ±20% or ±0.2 g; harmful = energy >30% or large salt/sat-fat/sugar gap |
| Allergens, diets | Present allergens detected; possible allergens flagged; false `gluten_free` claims; diet-filter admissions of non-compliant recipes; unknown rate (coverage, not safety) |
| Nutrition vs reference | Per-nutrient MAE, median error, bias, tails; completeness (complete / partial / missing) |
| Sustainability | Ingredient positions matched; complete recipes covered. No accuracy claim possible (no independent footprint gold) |

For public dietary datasets with incomplete positive tags, report three outcomes
on source-positive recipes: `suitable` (positive coverage), `not_suitable`
(reference disagreement, requiring source review), and `unknown` (abstention).
Missing source tags are unlabelled, never automatic negatives. Precision,
specificity and false-admission rates need verified negative references; small
source-reviewed negative controls must be reported separately. A raw-ingredient
text-rule replay does not measure the complete parsed/ontology-linked pipeline.

Component strata and failure classes to report:

| Stage | Required metrics | Useful strata / failure classes |
|---|---|---|
| Parsing | Field/span precision/recall; normalized quantity/unit accuracy; full occurrence alignment; omitted/hallucinated foods | Easy/hard quantities, packages, fractions, alternatives, compound/subrecipe text, multilingual text |
| Facets | Per-facet precision/recall/F1; attachment/polarity/state error; unsupported assertion rate | Species/source, part, cooked/raw, preservation, concentration, nutrition qualifiers |
| Weights/servings | Gram MAE and median absolute error; relative error for positive reference; catastrophic error counts; method/unknown coverage | Explicit mass, density conversion, item weights, packaging, edible/drained/cooked basis, given/inferred servings |
| Matching | Recall@1/5/10 before gates; accepted precision; coverage; no-match P/R/F1; harmful mismatch rate | Region/source, food family, ambiguity, state/form, common/rare ingredients |
| Nutrition | Per-nutrient MAE, median absolute error, signed bias, tails; complete/ref counts; per-recipe errors | Each curated dataset separately, all eight canonical nutrients, reference-quality and formulation cohorts |
| Allergens/diets | Per-group and recipe-level precision/recall; false-negative count; unknown rate; downstream violation rate | All allergen groups, compounds, substitutions, misleading tags, source text versus normalized names |
| Cost/emissions | Mapping correctness, coverage by count/mass, arithmetic, provenance/basis agreement, missingness and sensitivity | Country/source, general versus detailed proxy, factor release, substitution scope |
| Time | Source-field extraction error, total/prep/cook distinction, unsupported metadata rate | Serial/parallel/waiting actions, contradictory tags and source metadata |

Do not interpret a model confidence or vector score as a calibrated probability. Risk–coverage curves are useful with appropriate labels; Brier/ECE require actual probabilistic predictions and adequate labelled data. Bootstrap paired differences at recipe/family level where appropriate, preserving regional dependence. Small purposive samples support failure discovery, not population risk estimates.

## Nutrition against references

Compare before/after predictions with identical source references on the common complete subset for each nutrient. Also show every arm's completeness against the full reference cohort, newly complete/incomplete counts, exceptions and source anomalies. Improvement on a shrinking subset is insufficient.

Before scoring, audit every available reference row independently of prediction
error: raw literal, units, sign, missing markers, basis, nutrient consistency and
source/cleaned provenance. Preserve signs and qualifiers; never remove arbitrary
characters to obtain a number. Statistical outliers are review candidates, not
automatic exclusions. Use documented cell-level eligibility, retain original
labels and full-cohort denominators, and show sensitivity for any adjudicated
holds. Matching source tables is an arithmetic control, not independent accuracy.

Run a decomposition control: source masses + source assigned food IDs; source masses + predicted matches; predicted masses + fixed matches where possible; full raw-input pipeline. This separates arithmetic, parsing/quantity and matching failures. It cannot prove the source recipe's formulation is accurate.

For regional drift, fix recipe text, occurrence ordering, quantities, serving count, input generation and release; compare all six region pairs. Report absolute differences, symmetric relative differences with an explicit near-zero floor, median/P90/P95, source strata and completeness. Attribute large differences to ingredients and source records. Legitimate table variation can remain: do not force profiles together or choose records to resemble reference totals. Begin with historical vegetable-soup and sourdough complaints.

## Allergen reference and labelling rules

**Decided 3 November 2026, clarified by the owner 7 November:** the allergen reference is **Regulation (EU) No 1169/2011, Annex II** (14 allergens; the system's 15 values split gluten/wheat). Every expected label is `present`, `possible` or `absent`, mirroring the system's detected/possible/unresolved states. Cross-contact ("may contain") is out of scope: recipe text cannot establish it and the system does not claim it. Named-food defaults below implement the owner's composition policy; they do not measure final-product concentration or certify a formulation.

| Case | Expected label | Basis |
|---|---|---|
| Oats, wheat/spelt/khorasan, rye, barley and products (bread, pasta, couscous, flour) | `present` gluten (and wheat where wheat-derived) unless the line says gluten-free | Annex II item 1; exemptions: wheat glucose syrups incl. dextrose, wheat maltodextrins, barley glucose syrups, cereals used for distillates |
| Tree nuts | Only almond, hazelnut, walnut, cashew, pecan, Brazil, pistachio, macadamia/Queensland | Annex II item 8 is exhaustive; **pine nut and coconut are not EU tree nuts** |
| Generic "nuts" | `possible` tree_nut and peanut | Unspecified species |
| Wine | `present` sulphites | Owner's named-wine default; actual final-product concentration is not measured |
| Dried fruit, wine vinegar, mirin, cider | `possible` sulphites | Owner clarification 7 November; presence/concentration is not established by the name alone |
| Stock cubes, bouillon, gravy granules, seasoning blends | `possible` celery (plus wheat/gluten if not stated free) | Celery is common in these compound ingredients |
| Mustard (Dijon, wholegrain, English, yellow) | `present` mustard only; not wheat/gluten unless stated | Most Dijon/wholegrain mustard is naturally gluten-free (user decision 3 November 2026) |
| Worcestershire sauce | `present` fish | Anchovy in standard formulation |
| Mayonnaise | `present` egg | Owner's standard-formulation default; explicitly egg-free/vegan versions are excluded |
| Pesto | `present` milk; `possible` tree_nut | Parmesan standard; some pestos use cashews |
| Compound/branded ingredients | Declared allergens of the compound count, even in small amounts | Compound ingredients must be declared regardless of quantity |
| Alternatives ("butter or margarine") | `possible` for allergens only in some options | User may choose either |
| Optional / serving-suggestion lines | `possible` | May or may not be eaten |
| Explicit "gluten-free …", "dairy-free …" product | `absent` for that allergen | Stated formulation |

Use **original ingredient lines**, not normalized names, as evaluation input; normalization can drop qualifiers such as "gluten-free". Sources: [Annex II (UK-retained text)](https://www.legislation.gov.uk/eur/2011/1169/annex/II), [EU nut list guidance](https://elelmiszerlanc.kormany.hu/akadalymentes/download/2/1f/d1000/labelling_legislation_guidance_allegens-2017-4864_en%20published.pdf), [FSAI sulphites](https://fsai.ie/Business-Advice/Running-a-Food-Business/Food-Safety-and-Hygiene/Additives/Sulphur-Dioxide-and-Sulphites). Verify against the current EUR-Lex consolidated text before publication.

## Search

### Deterministic (pass / fail / insufficient evidence)

- Constraint extraction: omitted or invented constraints
- Violations per returned card and per query (diet, allergens, ingredients, time, nutrients), including later pages and fallbacks
- Per result: known violation / confirmed compliant / unresolved; unresolved needs an uncertainty flag; no unsupported "allergen-free" claim
- Displayed facts match recipe records
- Ranking: Precision@5/10, nDCG@5/10*, confirmed-compliant relevant precision, duplicate rate
- Feasible-query answer rate, no-result correctness, safe-candidate recall (only where the eligible set is known)
- Latency P50/P95, errors, timeouts, model cost

### Judge and human ratings (1–5, or `not_assessable` / `not_applicable` with a reason; never use 3 for "don't know"; give a short reason and valid references)

| Dimension | Question | 1 | 3 | 5 |
|---|---|---|---|---|
| Request relevance | How well does this recipe match what I searched for? | Wrong dish or intent | Related, misses a big part | Closely matches the request |
| Preference fit | How well does this recipe fit my stated food preferences? | Conflicts with stated preferences | Fits some | Fits all stated preferences, none invented |
| Practical suitability | How suitable is this recipe for the cooking situation I described? | Impractical for the situation | Plausible, concerns remain | Fits effort, skill and equipment |
| Information sufficiency | Does the information provided help me decide whether to choose this recipe? | Key info missing or contradictory | Enough for a first judgment | Clear enough to choose |
| Recommendation understandability (only if a rationale is shown) | Is it clear why this recipe was returned for my request? | Missing or confusing | Vague link to the request | Specific and easy to follow |
| Useful variety (once per list) | Do these results offer meaningfully different options that still match my request? | Duplicates or irrelevant variation | Some useful differences | Meaningfully different relevant options |

Faithfulness (pass / fail / unknown): do the explanation's claims match the recipe evidence, restrictions and uncertainty?

### Human-only (1–5)

| Measure | Question | 1 | 5 |
|---|---|---|---|
| Perceived usefulness | How useful were these results for finding something suitable to cook? | Not useful | Very useful |
| Choice ease | How easy was it to choose a suitable recipe from these results? | Very difficult | Very easy |
| Choice satisfaction | How satisfied are you with the recipe you selected? | Very dissatisfied | Very satisfied |
| Intention to cook | How likely are you to prepare the recipe you selected? | Very unlikely | Very likely |

Also record selection, time to selection, reformulations, abandonment, "would choose none".

### Principles

Test extraction, query construction, retrieval, pagination/fallback and final cards independently, then run the platform-shaped request with profile fields and explicit filters together. The current natural-language path extracts typed constraints and builds Elasticsearch queries deterministically; it does not simply generate arbitrary Elasticsearch DSL.

Deterministic measures: extraction exactness per hard field; omitted/invented constraints; allergen/diet/ingredient/time/numeric violations per returned card and per query; eligible-result precision@k; no-result correctness; feasible-query answer rate; safe-candidate recall where the candidate set is known; duplicate rate; P50/P95 latency; timeout/error rate; model cost. Empty results cannot earn a useful recommendation score.

For objective safety cases, source-backed permitted/forbidden ingredients and the expected constraint semantics define pass/fail. Do not evaluate the production detector solely against labels generated by that same detector. Include unknown and compound products where no absence claim is warranted.

Under the user-confirmed search policy, unresolved allergen status does not
automatically exclude a recipe: retain it with a visible uncertainty flag. Test
known-conflict exclusion, unresolved-result flag coverage and reason, and absence
of false allergen-free claims separately. Report unresolved return rate and
confirmed-compliant precision with explicit denominators; do not count a flagged
unknown as a confirmed-compliant result or automatically as a known violation.

Open-ended relevance uses multiple acceptable recipes and graded judgments, not one exact recipe ID. Use precision@5/10 and nDCG where judged pools support them. Include hard-negative recipes and near-miss queries, positive allergen mentions, combined diets/allergies, impossible constraints, damaged tags, stale projections, extractor outages, zero-result retries and later pages.

## Adaptation

### Deterministic (pass / fail / insufficient evidence)

- Nutrition: target nutrient change per serving, Nutri-Score points and letter, other nutrient regressions (within-grade reported separately)
- Cost: whole-recipe and per-serving saving, comparable regional prices
- Sustainability: whole-recipe and per-serving emissions reduction, comparable factors
- Time: elapsed-meal saving against a supported baseline, with dependencies and equipment checked; a component saving is not a meal saving
- Restrictions and allergens kept, including edited instructions
- Valid source IDs, quantity and state basis, serving comparability, baseline not stale
- Claimed numbers match computed ones. Relative reduction = (before − after) / before, absolute benefit also reported
- Useful-draft yield (all attempts); violation rate per returned draft; success and avoidable abstention (known-feasible cases); correct abstention (infeasible cases); failure-cause split

### Judge and human ratings (1–5, or `not_assessable` with a reason; give a short reason with ingredient or step references)

| Dimension | Question | 1 | 3 | 5 |
|---|---|---|---|---|
| Culinary compatibility | Do the proposed changes make sense in this particular dish? | Breaks an ingredient's role | Plausible, concerns remain | Supports needed function, flavour and texture |
| Instruction consistency and feasibility | Do the ingredients and steps fit together into a workable cooking process? | Contradictory or unworkable | Workable only with a correction | Coherent ingredients, quantities and steps |
| Dish preservation | Does the adapted recipe remain recognisably the original dish? | Becomes another dish | Recognisable, key trait weakened | Defining traits kept |
| Clarity and actionability | Is it clear what I need to do differently? | Confusing or unusable | Understandable, needs interpretation | Easy to find and follow |
| Benefit and trade-off clarity | Is it clear what this adaptation offers and what its limitations are? | Misleading or unclear | Main benefit clear, a qualification unclear | Benefit, trade-offs and uncertainty clear |

Faithfulness (pass / fail / unknown): benefits match checked results; regressions disclosed; draft status and uncertainty accurate; component savings not shown as whole-recipe savings.

Judge protocol: first four dimensions after seeing request and diff; fifth after seeing explanation and checked benefit summary. Prefer a different model family from the runtime assessor. Treat recipe text as data. Abstentions get a decision evaluation, not a quality score.

### Human-only (1–5)

| Measure | Question | 1 | 5 |
|---|---|---|---|
| Perceived usefulness | Considering the benefit and the changes required, how worthwhile is this adaptation for me? | Not worthwhile | Very worthwhile |
| Intention to use | How likely would I be to use this adaptation when cooking this recipe? | Very unlikely | Very likely |

### Principles

Keep deterministic eligibility ahead of subjective scoring: constraints preserved; valid candidate/source ID; comparable quantity and nutrient basis; full recalculation; meaningful declared objective benefit; no prohibited regression; all consequential ingredient/step edits applied; no unapproved amounts/times/temperatures; stale-baseline rejection. Measure end-to-end supported-card rate against all attempted recipes, conditional success among feasible cases, valid/avoidable abstentions and failure stage. Report success separately for each objective and source.

Compare the current strict-grade policy and the agreed within-grade policy on the same frozen cases. Retain grade/point deltas plus target nutrient changes per serving/per 100 g, other nutrient deltas, cost/emissions evidence and missing dimensions. Quantity reduction should not appear successful merely because the serving shrank.

## Judge protocol and calibration

Judge input contains the request/profile, exact recipe/candidate facts, instructions, deterministic checks, before/after totals, completeness and source evidence. Its output includes case ID, per-dimension score/assessability, evidence pointers, short reasons, uncertainty and detected violations. The judge cannot override a hard-gate failure.

Correctness asks whether the output is factually right; faithfulness asks whether
its claims are grounded in the retrieved recipe/composition evidence. Calculations
and explicit restrictions are checked deterministically and supplied to the judge,
whose opinion cannot overturn those results. This makes the original RAG-style
evaluation analogy explicit while allowing several acceptable recommendations or
adaptations.

Use a different model family from the generator when practical, hide model/method labels, randomize pair order and check swapped order. Recipe text is evaluation data, never judge instructions. Pin prompts and versions; retain raw responses and schema failures; repeat a subset to measure stability. Calibrate on a small source-checkable/internal anchor set, inject obvious violations and inspect disagreements. This is internal calibration unless independent human ratings actually exist. [MT-Bench research](https://arxiv.org/abs/2306.05685) documents position, verbosity and self-preference bias; a judge score alone is not ground truth.

Study participants can rate relevance, practicality, clarity and whether the result fits their stated needs during complete platform scenarios. They are not asked to verify hidden allergens, exact composition IDs, nutritional chemistry or emissions correctness. Compare their judgments with internal scores only on dimensions they can assess.

Calibration metrics (search and adaptation):

Per-dimension LLM/human agreement; critical-defect misses (false reassurance); false alarms on clean controls; repeatability; `not_assessable` rate; schema failures; position-swap bias.

## Acceptance and reporting

Any known returned allergen/diet violation blocks release of the affected workflow. Zero observed violations in the fixed internal suite is necessary and does not prove zero real-world risk. Report positives, negatives, unknowns, returned cards and eligible opportunities so abstaining on everything cannot pass.

An unresolved allergen result is permitted by the agreed visibility policy only
when clearly flagged. Missing uncertainty flags or claims of confirmed absence
on unresolved cases fail that contract. Reducing the unknown rate requires
better evidence and assessment, not defaulting unknown values to absence.

Numerical accuracy, coverage, judge quality and latency thresholds must be chosen and frozen after the initial baseline and before tuning against the evaluation split. Label proposed thresholds as proposals. The inherited 10% cost/emissions rule is current policy, not a scientifically validated universal threshold.

Publish one concise report per run with the full denominator, regressions, uncertainty, representative failures and exact artifacts. Keep all original before/after matching evidence. This targeted literature search is not a systematic review; benchmark downloads, complete license checks and reproductions remain execution work.

## Run records

Use one simple versioned JSONL/CSV case format where practical, with task-specific fields: case ID; source/recipe/formulation family; region/language; raw input; profile constraints; expected assertions or multiple acceptable IDs; provenance/basis; split; severity; and feedback IDs. Each evaluation folder keeps its inputs and gold under `data/` and predictions and scores under `results/`; raw run outputs stay in the local `analysis/<run-id>/`.

A run manifest records input/code hashes, catalogue/ontology releases, parser/model/provider/prompt versions, seed, requested settings, cohort counts, exclusions, denominator and failure reasons, timestamps, call/token/latency totals and estimated inference cost. Never include credentials. A git revision alone is insufficient while the working tree is dirty; hash relevant changed files too.

Freeze development and evaluation groups before tuning. Keep all regions of a recipe, duplicates, alternate wording and source-overlapping RCSI/SafeFood recipes in the same group. Published model training overlap may be unknown; disclose it. Existing feedback cases are a permanent regression set rather than an unseen performance sample.

## Public resources considered

Checked 30 October 2026; availability of a page is not a validated download. Before adoption record release/hash, labels, language, access, reuse terms, duplicate/leakage checks and a small runnable adapter.

| Resource | Suitable use | Important boundary / next decision |
|---|---|---|
| [NYT ingredient phrase tagger](https://github.com/nytimes/ingredient-phrase-tagger) | Public ingredient quantity/unit/name/comment annotations and a parsing baseline | Repository includes a roughly 180k-row snapshot and Apache-2.0 code notice. Verify annotation/data terms and overlaps; it does not label edible grams or food-composition equivalence. |
| [Ingredient Parser training data](https://ingredient-parser.readthedocs.io/en/latest/explanation/data.html) | More detailed parsing labels and source-diverse ingredient sentences | Training data can overlap NYT and recipe corpora. Publicly reported model evaluation is not independent evaluation of a model trained on those same rows. Preparation labels still need target/polarity interpretation. |
| [TASTEset 2.0](https://github.com/taisti/TASTEset-2.0) | Ingredient spans for food, quantity, unit, process, part, purpose, diet and exclusion/substitution distinctions | Authors describe 1,000 manually annotated recipe ingredient lists. Reported baseline metrics exclude the rarest exclusion/substitution types; inspect those cases directly and check source overlap. |
| [JFC-Recipe](https://aclanthology.org/2026.lrec-1.504/) | Manual ingredient-to-composition links and amount/unit annotations convertible to grams | Japanese recipes and a different target table; potentially useful transfer and schema benchmark. Inspect the paper's release/access conditions before budgeting an adapter. Its derived grams are not universal household-measure truth. |
| [Nutrition5k](https://github.com/google-research-datasets/Nutrition5k) | Structured ingredient-mass and dish-macro evaluation | Measured component masses and USDA-derived nutrient labels; not chemical assays or free-text recipe parsing labels. Use metadata, with no image download needed. European-table deviation includes table differences; also run a source-fixed calculation control. |
| [FoodMapper author data](https://github.com/RichardStoker-USDA/usda-food-mapping) | Retrieval/selection and genuine no-match behavior | ASA24-to-FoodB assumes a match; NHANES-to-DFG2 allows no match. Inspect its study-specific acceptable-proxy definition before importing labels into a multi-nutrient equivalence task. |
| [FEAST FoodEx2 dataset](https://huggingface.co/datasets/disi-unibo-nlp/feast-foodex2) and [paper](https://arxiv.org/abs/2603.03176) | FoodEx2 base, facet-family and descriptor recovery | Classification rather than approved composition matching. Existing local diagnostics found duplicate/conflicting labels and code/release issues; reuse those findings and audit splits. Dataset card currently states CC-BY-4.0; pin the actual release. |
| [PFoodReq](https://github.com/hugochan/PFoodReq) | Constrained recipe questions with sets of acceptable answers | Author repository provides generation code and external download links. Download availability and data terms remain untested. Native answers refer to FoodKG; a Wrangler corpus port needs newly computed candidate sets. |
| [GISMo](https://github.com/facebookresearch/gismo) / [paper](https://arxiv.org/abs/2302.07960) | Context-aware substitution ranking, standardized splits and MRR | Observed substitutions do not prove allergen safety, equivalent quantities or health benefit. Repository states majority CC-BY-NC; confirm exact data/model terms before reuse. |
| [English Recipe Flow Graph Corpus](https://aclanthology.org/2020.lrec-1.638/) | Ingredient/action/intermediate-product attachment for later instruction changes | Relevant process annotations, not nutritional benefit or cooking-yield approval. |
| [RecipeDB methodology](https://cosylab.iiitd.edu.in/recipedb/FAQ) | Additional recipe structure and comparison of calculation approaches | Nutrition is estimated by mapping ingredients to USDA and summing contributions. Do not call it independent measured nutrition truth. |
| [Indonesian recipe allergen annotations](https://data.mendeley.com/datasets/mb544fn3mg/1) | Token-level allergen entity recognition | Released 13 October 2026 with 24,000 tokens and CC-BY-4.0 notice. Inspect label origin, categories and quality; different language and entity labels do not establish complete EU recipe/allergen absence or cross-contact labels. |
| [388-recipe carbon-footprint/nutrition dataset](https://www.nature.com/articles/s41597-023-02702-1) | Secondary emissions-methodology and recipe-calculation comparison | Japanese recipes with modelled environmental/nutrition estimates. Check ingredient/cooking scope, factors, loss assumptions and denominators; an estimate from another LCA method is not a measured European recipe footprint. |
| [Open Prices](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/tutorials/product-prices/) | Receipt/shelf-price evidence for a small external price check | Public product-price observations have proof, place and date. Regional/product coverage, pack conversion and reuse terms need checking; retail observations test a different quantity from agricultural economic proxies. |
| [USDA Purchase to Plate](https://www.ers.usda.gov/data-products/purchase-to-plate) | External example of food-code-linked average prices | US data and historical periods; useful methodology/control, not current IE/HU/SI recipe-price truth. |

Priority: test access/schema for NYT/TASTEset, Nutrition5k metadata, PFoodReq and GISMo; assess JFC-Recipe's access and language cost; use existing FEAST diagnostics. Inspect the allergen, emissions and price resources before choosing task-specific handcrafted cases. No checked resource establishes a complete benchmark of actual elapsed cooking-time savings from adaptations; video timestamps are not equivalent to elapsed preparation/cooking time. The local curated living-lab references remain the principal project-specific end-to-end evaluation.

Additional discovered leads, not adopted benchmarks: [CARROT](https://aclanthology.org/2024.emnlp-main.61/) for cultural retrieval, [ReciFine](https://aclanthology.org/2026.findings-eacl.210/) for richer instruction annotations, and [FoodGuardBench](https://arxiv.org/abs/2604.01444) for food-safety prompts. Their tasks and access need inspection before use; they do not replace an allergen-through-search benchmark for our corpus.

## Similar systems and design implications

| System/source | Observed approach | Consequence for Wrangler |
|---|---|---|
| [Edamam nutrition analysis](https://developer.edamam.com/edamam-nutrition-api) and [recipe search](https://developer.edamam.com/edamam-docs-recipe-api) | Ingredient/whole-recipe nutrition, dietary/allergen labels, separate source URLs and generated nutrition; documented data-use restrictions | Retain ingredient-level evidence and original-versus-calculated distinction. Vendor outputs can be comparison baselines under appropriate access terms, not reference truth. No paid comparison is required for this plan. |
| [Spoonacular official documentation](https://spoonacular.com/food-api/docs) | Explicit diet/intolerance parameters, nutrient/time filters, ingredient parsing/conversions, substitutions and price breakdowns | Typed constraints and dedicated calculation tools are established patterns. Their advertised features do not establish measured constraint accuracy or equivalent data coverage. |
| [FEAST](https://arxiv.org/abs/2603.03176) | Separate base identity, facet families and descriptor assignments | Evaluate each extraction level and rare qualifiers separately. An ontology code is not nutrient equivalence. |
| [FoodMapper](https://github.com/RichardStoker-USDA/usda-food-mapping) | Candidate retrieval followed by selection/no-match tasks | Separate recall from selection quality; retain explicit no-match examples. |
| [GISMo](https://arxiv.org/abs/2302.07960) and [DIISH/FoodKG](https://foodkg.github.io/subs.html) | Context and ingredient relationships guide substitutions | Candidate similarity cannot replace a recipe-role check or whole-recipe recalculation. |
| [FoodOn transformation model](https://foodon.org/food-facets/food-transformation-process/) and [substitution design pattern](https://pmc.ncbi.nlm.nih.gov/articles/PMC8837940/) | Material state and recipe process matter to a substitution | Model consequential instruction edits and quantity/state dependencies before method adaptation. |
| [FAO/INFOODS guidelines](https://www.fao.org/infoods/infoods/standards-guidelines/en/) and [recipe guidance](https://www.fao.org/infoods/infoods/recipes/en/) | Matching needs descriptors and compatible definitions; recipe calculations need supported yield/retention treatment | Keep food matching, quantity conversion and cooking calculation independently testable. |

These are architectural inferences from documented methods, not a measured competitive ranking. Prior matching literature review, FEAST experiments, FoodSEM/FoodOntoRAG notes and unverified novelty hypotheses remain in the local recovery archive. No need to rebuild models, adopt paid APIs, train a matcher, or extract a standalone library before the internal benchmark identifies a concrete need.
