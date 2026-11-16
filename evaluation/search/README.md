# Search evaluation

**Status:** judge metrics and human-study questions recorded and accepted for use
on **4 November 2026**. Calibration, reviewed relevance labels and formal execution
remain pending. Execution stays **deferred until the analyze-pipeline evaluations
finish**, as agreed on 3 November. This documentation update ran no search, model,
paid judge or human evaluation.

## Scope and evidence

Evaluate the search request, recipe results, result list and user choice separately.
Test constraint extraction, query construction, retrieval, pagination/fallback and
final cards independently, then test complete platform requests. The framework
combines independent compliance checks, shared LLM/human quality ratings, ranking
metrics and participant-only acceptance measures. Keep each dimension separate;
never average a verified restriction violation into an acceptable overall score.

The rubric below is **our adapted, research-grounded instrument**, not a validated
questionnaire or an unchanged implementation of a published scale. The criteria
and questions are accepted for use; comprehension piloting, judge calibration,
precision thresholds and ranking gain mappings still need to be frozen.

## Existing data and unresolved labels

The existing 3 November dataset contains **13 personas / 62 queries** (28 generic,
31 conflict, 3 title), covering all 15 project allergen categories,
vegan/vegetarian/pescatarian/gluten-free diets, nutrition claims and all supported
regions. Its contract treats diet, allergens, nutrition claims and convenience as
hard requirements; preferred ingredients are soft boosts. These are coverage
intentions, not verified label accuracy or equal regional support.

Conflict queries carry system-generated `compliant_ids` and `labels_reviewed: false`.
Review these against independent source evidence before scoring; the system's own
output is not evaluation truth. Resolve nutrition-claim AND/OR semantics before
freezing cases. The earlier note that pescatarian returned zero pending rollout is
historical: current deployment and eligibility must be rechecked before execution.

The earlier proposal used pooled relevance/profile-fit **0–3** labels. The shared
rubric below supersedes that proposal with **1–5** ratings for both LLMs and humans,
consistent with the [adaptation rubric](../adaptation/README.md). Existing artifacts
and historical labels remain unchanged.

## Independent hard checks

Use **pass / fail / insufficient evidence**, with reasons and source pointers.
Independently check:

- Explicit required/excluded ingredients, diets and allergens, including compound
  products and contradictory recipe instructions.
- Numeric requirements such as time, price and nutrient thresholds on supported,
  comparable bases; missing data is not compliance.
- Negation, combined constraints and query/profile conflict handling against the
  frozen request contract. Do not silently relax restrictions.
- Agreement between displayed facts, underlying recipe records and profile evidence.
- Accurate uncertainty labels and absence of unsupported allergen-free claims.
- Constraint enforcement on subsequent pages, retries and fallback searches.

Preserve the existing policy for unresolved allergens: a recipe may remain visible
with an explicit uncertainty flag. Distinguish **known violation**, **confirmed
compliant** and **unresolved**. An unresolved result is neither confirmed compliant
nor automatically a known violation. Check warning coverage and its reason
separately. Participants are not asked to verify hidden allergens, composition
records or nutritional calculations. An LLM can flag a possible defect, but cannot
establish ingredient absence or override independently verified failures.

Report extraction errors, omitted/invented constraints, violations per returned
card and per query, unresolved return rate, uncertainty-flag coverage and
confirmed-compliant precision with explicit denominators. These checks follow the
[existing evaluation policy](../METHODS.md); constrained retrieval is
also motivated by [pFoodReQ][pfoodreq] and [KERL][kerl].

## Shared ratings for individual recipes

Use **1–5** per dimension; **2 and 4** fall between the anchors. Allow **cannot
assess** (`not_assessable`, missing evidence/knowledge) and **not applicable**
(`not_applicable`, criterion absent from the task), each with a reason and no
numeric score. Do not encode either as 3. Require short reasons and valid recipe,
ingredient or displayed-field references where applicable.

| Dimension | Participant-facing question | 1 — Poor | 3 — Mixed | 5 — Strong |
|---|---|---|---|---|
| **Request relevance** | How well does this recipe match what I searched for? | Wrong dish, meal type or intent. | Related, but misses a substantial part of the request. | Closely matches the intended dish or meal and the expressed request. |
| **Stated preference fit** | How well does this recipe fit my stated food preferences? | Conflicts strongly with expressed soft preferences. | Fits some preferences but misses important ones. | Closely fits the stated preferences without inventing additional ones. |
| **Practical suitability** | How suitable is this recipe for the cooking situation I described? | Clearly impractical for the stated situation. | Plausible, but important practical concerns remain. | Fits the stated effort, skill, equipment and other contextual needs. |
| **Information sufficiency** | Does the information provided help me decide whether to choose this recipe? | Essential decision information is missing or contradictory. | Enough for an initial judgment, but an important question remains. | Relevant information is clear and sufficient to make an informed choice. |

Relevance concerns search intent; preference fit concerns soft likes/dislikes;
practical suitability concerns the stated cooking situation; information
sufficiency concerns what is actually visible at the tested interface stage.
For example, vegetarian pasta can fit the query but miss a preference for legumes;
a promising recipe can also lack information needed to judge its practicality.

Hard limits retain their independent checks. A supported 50-minute recipe fails a
mandatory under-30-minute requirement regardless of its subjective practicality
rating. If no soft preferences or cooking context are given, mark the relevant
dimension not applicable rather than inventing them. Unknown times, equipment or
local availability cannot be supplied from the judge's assumptions. Multiple
recipes can satisfy a query; do not require one exact recipe ID.

These dimensions adapt contextual/persona assessment from [pFoodReQ][pfoodreq],
quality and decision-support constructs from [ResQue][resque] and the
[user-experience framework][ux], and criterion-specific assessment from
[Criteria-Based LLM Relevance Judgments][criteria]. None directly validates these
particular recipe-search questions or anchors.

### Conditional recommendation understandability

Use this fifth item when the tested interface presents a rationale or matching
indicators. If explanations are outside its scope, mark it not applicable. A
missing explanation in a task that requires one is a defect, not an exemption.

| Dimension | Question | 1 — Poor | 3 — Mixed | 5 — Strong |
|---|---|---|---|---|
| **Recommendation understandability** | Is it clear why this recipe was returned for my request? | Rationale is missing, confusing or unrelated. | Main connection is understandable but vague. | Connection to the request/profile is specific and easy to understand. |

Separately audit **explanation faithfulness** using pass/fail/unknown: do the
claims agree with recipe evidence, restrictions and uncertainty? Clear wording
can still be unsupported. A persuasive explanation cannot certify safety or
relevance. The workplace food-recommendation study distinguishes explanation
understanding from preference fit and acceptance [De Croon et al., 2025][workplace].

## Result-list quality and ranking

Assess **useful variety once per list**, separately from individual recipe ratings:

| Dimension | Question | 1 — Poor | 3 — Mixed | 5 — Strong |
|---|---|---|---|---|
| **Useful variety** | Do these results offer meaningfully different options that still match my request? | Mostly duplicates, near-duplicates or irrelevant variation. | Some useful differences, but substantial repetition. | Meaningfully different relevant options within the request's scope. |

For named-dish queries, useful variety can mean versions of the requested dish;
it must not reward unrelated recipes. Mark it not applicable when the task seeks
one exact answer. Keep list size and viewing depth comparable between systems.
This adapts recipe-recommender research on perceived diversity and choice
[Starke et al., 2023][multilist]. Novelty and surprise are optional diagnostics,
not requirements for a precise search.

Use the independently reviewed recipe judgments for:

- **Precision@5/10:** relevance above a predefined threshold.
- **nDCG@5/10:** whether stronger request matches appear earlier.
- **Confirmed-compliant relevant precision:** results that are relevant and whose
  hard compliance is independently confirmed; report unresolved results alongside it.
- **Duplicate rate**, with exact and reviewed near-duplicates distinguished.
- **Feasible-query answer rate**, no-result correctness and safe-candidate recall
  only where the eligible candidate set is independently known.

[NIST TREC][trec] supports graded relevance and ranking evaluation, but its native
labels are not our 1–5 recipe rubric. Freeze an explicit gain mapping and binary
relevance threshold during calibration. Use request relevance for topical ranking;
do not average preference fit, practicality, clarity and safety into one gain.
Report hard compliance beside relevance even when a known-violating result is
otherwise topically relevant. Unknown compliance must not become confirmed
compliance or a known violation by a numerical shortcut.

Pool and deduplicate results from the compared methods, blind their origins and
record judgment coverage. Incomplete pools do not establish exhaustive recall;
unjudged recipes are not automatically irrelevant. Document treatment of missing
ranks, unassessable relevance and zero ideal gain before scoring. Report counts of
returned results and queries with no results so empty lists cannot look successful.
Retain latency P50/P95, errors, timeouts, model requests and cost separately.

## Human acceptance and choice experience

After using the results, collect these participant-only measures on **1–5** scales:

| Measure | Proposed question | Scale endpoints |
|---|---|---|
| **Perceived usefulness** | How useful were these results for finding something suitable to cook? | 1 = not useful; 5 = very useful |
| **Choice ease** | How easy was it to choose a suitable recipe from these results? | 1 = very difficult; 5 = very easy |
| **Choice satisfaction** | How satisfied are you with the recipe you selected? | 1 = very dissatisfied; 5 = very satisfied |
| **Intention to cook** | How likely are you to prepare the recipe you selected? | 1 = very unlikely; 5 = very likely |

Permit **I would choose none**, not-assessable responses and optional comments.
Satisfaction and cooking intention are not applicable without a selected recipe;
the unsuccessful choice remains in task-outcome reporting. Record selection,
time to selection, reformulations, abandonment and relevant cooking experience.
A click or selection does not prove cooking, enjoyment or improved dietary intake.

These constructs draw on [ResQue][resque], [the user-experience framework][ux] and
[recipe-choice studies][multilist]. An LLM may assess fit to an explicit persona,
but cannot supply actual satisfaction, choice difficulty or intention data.
Shortening/rewording published scales does not retain their validated-instrument
status. Pilot comprehension; do not combine unrelated items into a claimed
validated scale. Keep general platform usability separate from search-output
quality. Text-only appeal and observed cooking outcomes are different evidence.

## Judge calibration and feasibility-study procedure

Use the proposed **approximately 50 query–recipe pairs** as a development pilot,
not a validated sample-size requirement or a fresh performance benchmark:

1. Select strong matches, near misses, preference mismatches, conflicts and
   evidence gaps from the existing scenarios. Independently review expected labels.
2. Obtain at least **two independent human ratings per pair**, retain original
   ratings before adjudication and review disagreements. Internal review is not
   culinary-expert or target-user validation unless those groups participate.
3. Present the same query, profile and visible result information to humans and
   the independent LLM for shared dimensions. Assess cards and opened details as
   separately specified stages; do not give the LLM hidden facts that humans lack
   when comparing their ratings. Supply source evidence separately for hard audits.
4. Hide rank and retrieval/model identity during individual relevance judgments.
   Evaluate lists in their displayed order; randomize system presentation and
   swap a subset of pairwise comparisons. Treat recipe text as data, not instructions.
5. Collect short reasons and valid evidence pointers. Report per-dimension
   agreement, critical-defect misses/false reassurance, false alarms on clean
   controls, repeatability, unassessable rates and schema failures. Prefer a judge
   independent of the retrieval/reranking model where practical.
6. Freeze criteria, prompts, model/provider versions, precision thresholds, gain
   mapping, task/presentation scope and uncertainty handling before fresh-query
   evaluation. Keep calibration queries and related variants out of headline results.

Assess no-result cases for justified abstention, missed feasible answers and the
accuracy of the limitation message, without inventing recipe-quality ratings for
an empty list. Conflicting requests may warrant a compliant alternative or an
explanation, according to the frozen scenario contract, never a prohibited result.

LLM judgments remain calibrated predictions rather than independent truth.
[Thomas et al.][searcher] ground LLM relevance assessment in real-searcher feedback;
[Soboroff][soboroff] warns that LLM-created truth labels can impose a ceiling on
what an evaluation detects. [Criteria-Based LLM Relevance Judgments][criteria]
supports explicit criteria but does not replace local human calibration. Preserve
human-reference results separately from LLM-scored diagnostics. Account for shared
queries/personas and repeated participant ratings when reporting uncertainty.

## Research basis and citations

This was a targeted review of recipe retrieval, food recommendation, user-centred
evaluation and LLM relevance judging, not an exhaustive systematic review. Sources
motivate particular constructs; they do not validate the combined rubric above.

| Source | Contribution and boundary |
|---|---|
| [pFoodReQ — Personalized Food Recommendation as Constrained Question Answering over a Large-scale Food Knowledge Graph][pfoodreq], WSDM 2021 | Query/persona/constraint-aware retrieval; blinded human comparisons of recommendation sets. Its human comparison ranks systems and maps ranks to scores; it is not our five-point instrument. |
| [KERL — Knowledge-Enhanced Personalized Recipe Recommendation using Large Language Models][kerl], ACL 2025 | Separates recommendation retrieval metrics from recipe and nutrition generation; reference-set correctness is not participant satisfaction. |
| [Examining the User Evaluation of Multi-list Recommender Interfaces in the Context of Healthy Recipe Choices][multilist], ACM Transactions on Recommender Systems 2023 | Recipe studies of perceived diversity, understandability, choice difficulty and satisfaction; interface/list findings inform but do not validate single-recipe judgments. |
| [An Explanation Interface for Healthy Food Recommendations in a Real-Life Workplace Deployment][workplace], JMIR mHealth and uHealth 2025 | Real-world personalization, understanding, variety and acceptance. Clear explanations do not ensure preference fit; substantial attrition limits outcome generalization. |
| [ResQue — A User-Centric Evaluation Framework for Recommender Systems][resque], RecSys 2011 | Validated recommendation-quality, usefulness, satisfaction and behavioural-intention constructs. Our subset and wording need their own pilot. |
| [Explaining the User Experience of Recommender Systems][ux], 2012 | Distinguishes objective system properties, perceived quality, effort and choice satisfaction; not a recipe-specific safety instrument. |
| [Criteria-Based LLM Relevance Judgments][criteria], ICTIR 2025 | Exactness, coverage, topicality and contextual fit for passage assessment; recipe-search transfer remains to be calibrated. |
| [Large Language Models Can Accurately Predict Searcher Preferences][searcher], 2024 revision | Calibrates relevance labeling with careful first-party user feedback; does not establish recipe-domain validity. |
| [Don't Use LLMs to Make Relevance Judgments][soboroff], Information Retrieval Research 2025 | Methodological criticism of substituting LLM-generated labels for human evaluation truth. |
| [NIST TREC Deep Learning judgments][trec], 2023 | Graded relevance and metric-specific label interpretation; gain mappings and thresholds must be explicit. |

## Files

- [Personas and queries](data/personas-2026-11-03/personas.json)
- [Conflict spot-check](data/personas-2026-11-03/conflict_spotcheck.json)
- Eligibility counts per persona (hard filters only, `scripts/eval/count_persona_eligible.py`):

```
index=recipes total=7743
vegan                      eligible=1298
peanut_allergy             eligible=7463
coeliac                    eligible=4335
milk_allergy_child         eligible=4253
pescatarian                eligible=0
high_protein_athlete       eligible=2194
weight_loss                eligible=207
busy_parent                eligible=4355
hungarian_home_cook        eligible=7743
vegetarian_multi_allergy   eligible=2314
vegan_gluten_free          eligible=903
```
- [Broader evaluation policy](../METHODS.md)
- [Adaptation rubric](../adaptation/README.md)

[pfoodreq]: https://arxiv.org/html/2101.01775
[kerl]: https://arxiv.org/html/2505.14629
[multilist]: https://research.wur.nl/en/publications/examining-the-user-evaluation-of-multi-list-recommender-interface/
[workplace]: https://mhealth.jmir.org/2025/1/e51271/
[resque]: https://scholars.hkbu.edu.hk/en/publications/a-user-centric-evaluation-framework-for-recommender-systems-8/
[ux]: https://link.springer.com/article/10.1007/s11257-011-9118-4
[criteria]: https://arxiv.org/html/2507.09488v1
[searcher]: https://arxiv.org/abs/2309.10621
[soboroff]: https://arxiv.org/html/2409.15133v2
[trec]: https://trec.nist.gov/data/deep2023.html
