# RecipeWrangler — Consolidated Feedback and Issue Register

**Scope:** WiseFood Living Lab Round 1, Living Lab Round 2, and the feedback pasted from the WiseFood platform (September–October 2026).
**Compiled:** 30 October 2026.

| Category | Total feedback entries | Issues / requests | Observations | Recorded status |
|---|---:|---:|---:|---|
| Round 1 | 31 | 29 | 2 | 18 Resolved · 5 In progress · 8 Open |
| Round 2 | 27 | 24 | 3 | 23 Resolved / No action · 3 In progress · 1 Needs review |
| Platform feedback | 17 | 17 | 0 | 4 Marked resolved · 13 Not marked resolved |
| **Total** | **75** | **70** | **5** | — |

**Reading notes:** Feedback is reproduced as written in the source (except reporter contact details, which are omitted). Round 1 and Round 2 statuses/owners/progress notes are taken from the spreadsheets without independent verification. “Resolved / No action” is kept as a single source status; it does not necessarily indicate an implemented fix. Platform feedback uses “Marked resolved” when the pasted interface explicitly displayed `resolved`; otherwise “Not marked resolved” means the `Resolve` control was displayed. Repeated reports are preserved rather than deduplicated.

---

## 1. Round 1 — Living Lab feedback

**Source workbook:** `Living_Lab_feedback_aggregated.xlsx` → worksheet `RecipeWrangler`.
**Entries:** 31 total (29 reported issues or requests; 2 positive/non-actionable observations).
**Recorded status counts (all entries):** **Resolved**: 18, **Open**: 8, **In progress**: 5.

### Issues and feature requests

#### R1-01 — Feature / Filters

**Status:** Resolved  
**Source:** Use case scenarios (master)  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!3`  

**Reported feedback:**

> Allow choosing more than one of the quick filters at the same time.

#### R1-02 — Bug / Localization

**Status:** Open  
**Source:** Green Point – Saša & Tamara  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!4`  

**Reported feedback:**

> Recipe Search page is not translated to Slovene.

#### R1-03 — Bug / Localization

**Status:** Open  
**Source:** Green Point – Saša & Tamara  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!5`  

**Reported feedback:**

> Error message: some parts are translated, some are not.

#### R1-04 — Bug / Data / Localization

**Status:** Resolved  
**Source:** Green Point – Saša & Tamara  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!6`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> No Slovene nutritional information yet for the recipes.

**Progress notes (as recorded in workbook):**

> Slovenian nutritional information is available in backend only for the 100 slovenian recipes provided as the ground truth nutrition. Slovenian composition tables are essential ro provide slovenian nutrition for each recipe of the database.

#### R1-05 — Bug / Localization

**Status:** Open  
**Source:** Green Point – Saša & Tamara  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!7`  

**Reported feedback:**

> Ingredients and recipe instructions are not translated.

#### R1-06 — Bug / Data quality / Tagging

**Status:** Resolved  
**Source:** Green Point – Saša & Tamara  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!8`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Inconsistencies in recipe tagging – 'main dish' label is not used consistently and is sometimes applied to items where it doesn't fit (smoothies, sauces). This also affects how meals appear in FoodChat (e.g. as lunch in a high-protein plan).

**Progress notes (as recorded in workbook):**

> New recipe enrichment pipeline

#### R1-09 — Feature / UX / Education

**Status:** Open  
**Source:** Kasia & Sandra  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!11`  
**Owner (as recorded):** Dimitris, Konstantinos  

**Reported feedback:**

> Looks useful and user-friendly; provides calculation details. As a non-tracker, NutriScore E is not understandable – include explanations of what the score means and tips to improve it (especially for beginners).

**Progress notes (as recorded in workbook):**

> Explainations in UI should be added. Explanations are exposed by backend and new improve mode is added to enhance the nutrition improvement

#### R1-10 — Bug / Search

**Status:** Resolved  
**Source:** RCSI - Pauline  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!12`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Asked for egg recipes – no recipes found.

**Progress notes (as recorded in workbook):**

> LLM sometimes identified 'egg' as an allergen. Hardened the prompt and added a conflict validator. 

#### R1-11 — Bug / Search / Filters

**Status:** Resolved  
**Source:** RCSI - Pauline  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!13`  

**Reported feedback:**

> Clicked on 'quick recipes' – no recipes found.

#### R1-12 — Bug / Data quality / Branding

**Status:** Resolved  
**Source:** RCSI - Pauline  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!14`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Recipe 'Pesto Platter Snacks' includes 'Farrah Wraps' – Farrah is a brand of tortilla wrap; branded items should be omitted.

**Progress notes (as recorded in workbook):**

> Addressed with brand normalization mechanism. 

#### R1-13 — Bug / Data quality

**Status:** Resolved  
**Source:** RCSI - Pauline  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!15`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Asked for omelette recipe – Spinach and Turkey Omelette returned. Number of servings omitted, calorie information inaccurate, ingredients list needs refinement (butter is omitted, 'dash' of cheese?).

**Progress notes (as recorded in workbook):**

> Current data status: removed recipe1m recipes entirely due to missing details in a large amount of them.

#### R1-14 — Bug / Search relevance

**Status:** Resolved  
**Source:** RCSI – Claire & Niamh (apps feedback)  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!16`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Text search not working / not retrieving relevant results, e.g. 'Irish fish dishes' returns a bratwurst dish.

**Progress notes (as recorded in workbook):**

> Built the cuisine/mood vocabulary and lexical fallback matching,

#### R1-15 — Bug / Performance / Filters

**Status:** Resolved  
**Source:** RCSI – Claire & Niamh (apps feedback)  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!17`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Error filtering by Irish SafeFood recipes: 'Error loading recipes. Request timeout after 60 seconds. The search is taking longer than expected. Please try again or simplify your query.'

**Progress notes (as recorded in workbook):**

> This was caused by data backends bottlenecks, fixed

#### R1-16 — Bug / Data quality / Composition mapping

**Status:** Resolved  
**Source:** RCSI – Claire & Niamh (apps feedback)  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!18`  

**Reported feedback:**

> Baked cod with lemon and olive oil' – cod portion 350 g vs original recipe 175 g; composition data linked to 'Lamb, neck fillet, raw, lean and fat' (incorrect); potato portion incorrect; olives linked to 'Eel, yellow, raw' (incorrect); paprika linked to raspberries (incorrect); thyme linked to 'Lamb, mince, raw' (incorrect).

**Progress notes (as recorded in workbook):**

> Fixed weight calculation pipeline + ingredient matching

#### R1-17 — Bug / Allergen handling

**Status:** Resolved  
**Source:** RCSI – Claire & Niamh (apps feedback)  
**Scenario:** Recipe Search / Allergens  
**Tracker row:** `RecipeWrangler!19`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Nut allergy set in profile – chicken satay recipe was returned with no warning that it contained nuts.

**Progress notes (as recorded in workbook):**

> Enhanced tagging mechanism, added FATO enrhichment pipeline for evidence

#### R1-18 — Bug / Data quality

**Status:** Resolved  
**Source:** RCSI – Claire & Niamh (apps feedback)  
**Scenario:** Recipe data  
**Tracker row:** `RecipeWrangler!20`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Some calories for recipes are very off – especially in sources that we did not have input into.

**Progress notes (as recorded in workbook):**

> Performed outlier removal

#### R1-19 — Feature / Filters

**Status:** In progress  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe Search / Filters  
**Tracker row:** `RecipeWrangler!21`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Add a filter for NutriScore.

**Progress notes (as recorded in workbook):**

> Currently integrating in the UI and API

#### R1-20 — Feature / Transparency

**Status:** In progress  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!22`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Showcase the original (source) information of a recipe alongside the values calculated by RecipeWrangler so users can distinguish them.

**Progress notes (as recorded in workbook):**

> Backend already exposes it

#### R1-21 — Feature / UX / Education

**Status:** Open  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!23`  

**Reported feedback:**

> Add an explanation for the Sustainability Score (what it means and how to interpret it).

**Progress notes (as recorded in workbook):**

> backend already exposes calculation details 

#### R1-22 — Feature / Transparency

**Status:** In progress  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!24`  

**Reported feedback:**

> Provide a disclaimer for calculations that are not very confident.

#### R1-23 — Feature / Personalisation

**Status:** Resolved  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!25`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Add favourites and wish-list functionality for different purposes (e.g., to try later, for shopping).

**Progress notes (as recorded in workbook):**

> Added it

#### R1-24 — Feature / Sources / Transparency

**Status:** Resolved  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe data  
**Tracker row:** `RecipeWrangler!26`  

**Reported feedback:**

> Link each recipe to its original source URL.

#### R1-25 — Feature / Data / Usability

**Status:** Open  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe data  
**Tracker row:** `RecipeWrangler!27`  

**Reported feedback:**

> Provide unit conversion (e.g., teaspoons to grams).

#### R1-26 — Feature / Recipe Adaptation

**Status:** In progress  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe Adaptation  
**Tracker row:** `RecipeWrangler!28`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Provide adaptation suggestions for recipes – optimize for nutrition, sustainability and cost – with portioning and meal-plan integration (next period).

**Progress notes (as recorded in workbook):**

> First version available

#### R1-27 — Feature / Data ingestion

**Status:** Resolved  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe data  
**Tracker row:** `RecipeWrangler!29`  

**Reported feedback:**

> Allow users to add a recipe via a URL from another site.

#### R1-28 — Feature / Knowledge / Content

**Status:** Open  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe Education  
**Tracker row:** `RecipeWrangler!30`  

**Reported feedback:**

> Show short technique/skill videos for recipes and include food-safety and food-storage tips to mitigate food waste (possibly out of scope; consider LLM-suggested videos).

#### R1-29 — Feature / Personalisation / Cost

**Status:** Open  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe Analysis – Cost  
**Tracker row:** `RecipeWrangler!31`  

**Reported feedback:**

> Add a cost index for recipes and meal plans (more than half of participants want this); product-link integration likely out of scope.

#### R1-30 — Feature / Knowledge / Sources

**Status:** In progress  
**Source:** Budapest Plenary meeting  
**Scenario:** Knowledge base  
**Tracker row:** `RecipeWrangler!32`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Integrate SafeFood (Ireland) content as a source for food-safety guidance.

#### R1-31 — Other / Data / Tagging governance

**Status:** Resolved  
**Source:** Budapest Plenary meeting  
**Scenario:** Recipe Tagging  
**Tracker row:** `RecipeWrangler!33`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Living Labs to define the threshold values used for 'high protein' / 'low fat' and similar recipe tags. – Pauline (RCSI)

**Progress notes (as recorded in workbook):**

> Currently using : https://food.ec.europa.eu/food-safety/labelling-and-nutrition/nutrition-and-health-claims/nutrition-claims_en

### Positive or non-actionable observations (retained for completeness)

- **R1-07** — Everything seems fine and useful, from a non-expert in this field.  
  Source: Kasia & Sandra. Status in tracker: **Resolved**. Row: `RecipeWrangler!9`.
- **R1-08** — We are not professional enough on nutrition to evaluate the key metrics of a recipe.  
  Source: Green Point – Saša & Tamara. Status in tracker: **Resolved**. Row: `RecipeWrangler!10`.

---

## 2. Round 2 — Living Lab feedback

**Source workbook:** `WiseFood_Round2_Living_Lab_feedback_aggregated.xlsx` → worksheet `RecipeWrangler`.
**Entries:** 27 total (24 reported issues or requests; 3 positive/non-actionable observations).
**Recorded status counts (all entries):** **Resolved / No action**: 23, **In progress**: 3, **Needs review**: 1.

### Issues and feature requests

#### R2-01 — Bug / Data quality / Regional profiles

**Status:** Needs review  
**Source:** RCSI – Round 2 use case scenarios  
**Scenario:** Recipe Search / Regional profiles  
**Tracker row:** `RecipeWrangler!2`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Switching the same recipe between EU, Ireland, Hungary and Slovenia produced very large per-serving differences (e.g., vegetable soup).

**Progress notes (as recorded in workbook):**

> Changes have been perform to the e2e nutritional pipeline. Please review again the flagged recipes.

#### R2-02 — Bug / Units / Presentation

**Status:** In progress  
**Source:** RCSI – Round 2 use case scenarios  
**Scenario:** Recipe Search / Ingredients  
**Tracker row:** `RecipeWrangler!3`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Culinary quantities are converted to less usable units (e.g., oil shown as 13.8 g instead of 1 tbsp; garlic in grams instead of cloves).

**Progress notes (as recorded in workbook):**

> Some recipes already provide the quantities in grams (for example 200g pasta instead of cups). We could calculate the culinary quantities for every ingredient based on portion tables, however we might not have enough portion information for all ingredients and new mismatches might be introduced. Under discussion.

#### R2-03 — Bug / Time metadata / Tagging

**Status:** Resolved / No action  
**Source:** RCSI – Round 2 use case scenarios  
**Scenario:** Recipe Search / Time  
**Tracker row:** `RecipeWrangler!4`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> A beef curry is shown as taking 5 minutes and tagged '30 minutes or less' despite a cooking time of about 1.5 hours.

**Progress notes (as recorded in workbook):**

> Importer issue, fixed.

#### R2-04 — Bug / Search ranking / Regional relevance

**Status:** Resolved / No action  
**Source:** RCSI – Round 2 use case scenarios  
**Scenario:** Recipe Search / Ranking  
**Tracker row:** `RecipeWrangler!5`  

**Reported feedback:**

> When searching 'vegan meals', Irish curated recipes did not appear first and had to be found manually.

#### R2-05 — Bug / Data parsing / Ingredient quantities

**Status:** Resolved / No action  
**Source:** RCSI – Round 2 use case scenarios  
**Scenario:** Recipe Search / Ingredients  
**Tracker row:** `RecipeWrangler!6`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Malformed quantities were shown for Vegan bolognese, e.g. '1 rice brown' and '2 brown green lentil'.

**Progress notes (as recorded in workbook):**

> Already addressed from latest round of parsing fixes.

#### R2-06 — Bug / Recipe instructions / Completeness

**Status:** In progress  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Instructions  
**Tracker row:** `RecipeWrangler!7`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Some recipe instructions are too short or vague to follow easily.

**Progress notes (as recorded in workbook):**

> Needs clarification or example recipes.

#### R2-07 — Bug / Data integrity / Ingredient extraction

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Ingredients  
**Tracker row:** `RecipeWrangler!8`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> In 'All green chicken fritters', 'fritters' appears in the ingredient list even though it is the final dish.

**Progress notes (as recorded in workbook):**

> Already addressed from latest round of parsing fixes.

#### R2-08 — Bug / Personalisation / Constraint handling

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Personalisation  
**Tracker row:** `RecipeWrangler!9`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> A vegan personalised profile still returned chicken recipes and yoghurt ingredients.

#### R2-09 — Feature / Serving transparency

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Servings  
**Tracker row:** `RecipeWrangler!10`  

**Reported feedback:**

> Nutrition is shown 'per serving' without explaining what one serving represents in grams or another clear unit.

#### R2-10 — Feature / Serving scaling

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Servings  
**Tracker row:** `RecipeWrangler!11`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> There is no obvious option to change the number of servings and recalculate quantities/nutrition.

#### R2-12 — Feature / Visualization / Explainability

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Spider chart  
**Tracker row:** `RecipeWrangler!13`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> The spider chart is difficult to interpret: grid levels, scaling across different units and whether outer values are good or bad are unclear.

#### R2-14 — Bug / Recipe adaptation / Multi-objective quality

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Sustainability adaptation  
**Tracker row:** `RecipeWrangler!15`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Some sustainability substitutions are nutritionally illogical, e.g. replacing milk with condensed milk.

#### R2-15 — Bug / Recipe adaptation / Semantics

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Sustainability adaptation  
**Tracker row:** `RecipeWrangler!16`  

**Reported feedback:**

> A suggestion to replace milk with a vague 'milk drink' is unclear and not obviously a meaningful substitution.

#### R2-16 — Bug / Data quality / Tagging

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Tags  
**Tracker row:** `RecipeWrangler!17`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Tags are not always accurate; a chocolate cake was labelled 'vegetables' and 'refreshing'.

**Progress notes (as recorded in workbook):**

> Resolved FoodOn classifications. Couldn't find any chocolate cake with tag 'refreshing', so no changes applied to LLM enrichment.

#### R2-18 — Feature / Cost / Personalisation

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Cost  
**Tracker row:** `RecipeWrangler!19`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Cost information is missing.

**Progress notes (as recorded in workbook):**

> First version of cost factor added,

#### R2-19 — Bug / Recipe instructions / Ingredient-step consistency

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Search / Instructions  
**Tracker row:** `RecipeWrangler!20`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Some listed ingredients are not mentioned in the preparation steps, and some steps are too brief.

**Progress notes (as recorded in workbook):**

> Fixed truncated preparation steps and updated the import process to preserve all method sections. Legitimately brief instructions were retained.

#### R2-20 — Bug / Stability / API

**Status:** Resolved / No action  
**Source:** RCSI – Round 2 use case scenarios  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!21`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Recipe Analyzer request failed on two attempts; a bug was reported with screenshots.

#### R2-21 — Feature / UX / Input guidance

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Analysis / Input  
**Tracker row:** `RecipeWrangler!22`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> It is unclear what input the Recipe Analyzer expects (instructions only, a URL, or the full recipe text).

#### R2-22 — Feature / Workflow / Usability

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Analysis / Workflow  
**Tracker row:** `RecipeWrangler!23`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> A 'Copy recipe' button on recipe pages would make it easier to transfer a full recipe into the Analyzer.

#### R2-23 — Bug / Stability / API

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!24`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Every Analyzer attempt returned 'API request failed with status 503', preventing evaluation of ingredient/quantity/unit detection.

#### R2-24 — Bug / Recipe instructions / Completeness

**Status:** Resolved / No action  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!25`  
**Owner (as recorded):** Dimitris  

**Reported feedback:**

> Personalisation also does not seem to work consistently. 

#### R2-25 — Bug / Recipe instructions / Completeness

**Status:** In progress  
**Source:** Green Point – Round 2 use case scenarios  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!26`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> The recipes are not always clear or detailed enough. 

**Progress notes (as recorded in workbook):**

> Needs clarification or example recipes.

#### R2-26 — Bug / None

**Status:** Resolved / No action  
**Source:** ESSRG - WF - Round 2 use case scenarios  
**Scenario:** Recipe Search  
**Tracker row:** `RecipeWrangler!27`  

**Reported feedback:**

> Recipes appear in English and they remain in English even after you open them

#### R2-27 — Other / Data quality / Regional profiles

**Status:** Resolved / No action  
**Source:** ESSRG - WF - Round 2 use case scenarios  
**Scenario:** Recipe Analysis  
**Tracker row:** `RecipeWrangler!28`  
**Owner (as recorded):** Konstantinos  

**Reported feedback:**

> Nutritional information can be very misleading when you look at a specific product and compare the data from the three countries with the EU Food Composition Table. For example, for the 50/50 sourdough recipe, the calorie count is 1,670 according to the EU, while the others are around 230–250.  Huge differences!!!

**Progress notes (as recorded in workbook):**

> Already addressed from latest round of recipe analyze fixes.

### Positive or non-actionable observations (retained for completeness)

- **R2-11** — The food composition table source is clearly shown and useful.  
  Source: Green Point – Round 2 use case scenarios. Status in tracker: **Resolved / No action**. Row: `RecipeWrangler!12`.
- **R2-13** — The sustainability index and the idea of ingredient replacements are considered interesting/useful.  
  Source: Green Point – Round 2 use case scenarios. Status in tracker: **Resolved / No action**. Row: `RecipeWrangler!14`.
- **R2-17** — Source and preparation time are usually clearly shown, and preparation time generally seems realistic.  
  Source: Green Point – Round 2 use case scenarios. Status in tracker: **Resolved / No action**. Row: `RecipeWrangler!18`.

---

## 3. WiseFood platform feedback

**Source:** User-provided text pasted from the WiseFood platform feedback interface. The original timestamps are preserved.

**Entries:** 17 feedback reports: 15 recipe-specific reports, 2 platform-level reports, and 11 distinct recipe IDs.

### All reported items (including duplicate submissions)

#### PF-01 — Platform page: `/app/recipe-wrangler` (no recipe ID provided)

**Submitted:** 16/10/2026 14:58:58  
**Feedback type:** Bad — 2/5  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> I selected Fish and the recipies RecipeWrangler fetched include Lamb and pork, instead of fish

#### PF-02 — Recipe ID: `5657703444`

**Submitted:** 16/10/2026 14:58:42  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> This recipe is marked in  Food Group as a recipe containing Fish

#### PF-03 — Recipe ID: `0112050065`

**Submitted:** 16/10/2026 14:58:28  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> Food group Fish. Still the recipe is not related to fish

#### PF-04 — Recipe ID: `2875581813`

**Submitted:** 16/10/2026 14:57:54  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> In the ingredient list, "oats oatmeal", is used with 10.13 gr and the protein is 27.38 gr, the carbs are 90.23 gr and fats are 11.18!

#### PF-05 — Platform page: `/app/console/assets/fctables` (no recipe ID provided)

**Submitted:** 16/10/2026 14:57:36  
**Feedback type:** Bad — 2/5  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> Users can't view the information from food composition table. Maybe we should think a way to expose this information to users/experts

#### PF-06 — Recipe ID: `8049618090`

**Submitted:** 21/09/2026 16:21:54  
**Feedback type:** Thumbs down  
**Status shown in paste:** Marked resolved  

**Reported feedback:**

> Check calorie count - seems very high for one serving?
> No oil mentioned in ingredients list, but included in instructions.
> Add a line to instructions to ensure that everything is piping hot before serving.

#### PF-07 — Recipe ID: `1007826438`

**Submitted:** 21/09/2026 16:21:15  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> For ingredients list "grain rice", need to specify if this means 'long grain rice'?
> Also, 'rosemary mayonnaise below' as an ingredient is an error. And the weight of tablespoon and weight of 3.3g do not align.

#### PF-08 — Recipe ID: `8049618090`

**Submitted:** 21/09/2026 16:20:50  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> Nutrition looks wrong

#### PF-09 — Recipe ID: `8049618090`

**Submitted:** 21/09/2026 16:20:30  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> Nutrition looks wrong

#### PF-10 — Recipe ID: `2875581813`

**Submitted:** 21/09/2026 16:19:58  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> Title or description is wrong

#### PF-11 — Recipe ID: `7168644112`

**Submitted:** 21/09/2026 16:18:55  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> The steps do not work

#### PF-12 — Recipe ID: `386c5072-a7cd-53fb-819a-4839b9b3dd15`

**Submitted:** 18/09/2026 15:23:43  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> Nutrition looks wrong

#### PF-13 — Recipe ID: `ESSRG_72`

**Submitted:** 18/09/2026 15:20:33  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> Quantities look wrong

#### PF-14 — Recipe ID: `960c01f9-9a7b-5532-afb9-b243d8a757d6`

**Submitted:** 05/09/2026 03:13:16  
**Feedback type:** Thumbs down  
**Status shown in paste:** Marked resolved  

**Reported feedback:**

> Nutrition looks wrong

#### PF-15 — Recipe ID: `960c01f9-9a7b-5532-afb9-b243d8a757d6`

**Submitted:** 05/09/2026 03:13:10  
**Feedback type:** Thumbs down  
**Status shown in paste:** Marked resolved  

**Reported feedback:**

> Nutrition looks wrong

#### PF-16 — Recipe ID: `d20a9082-69c8-55f5-8b08-035c2c2eb3e5`

**Submitted:** 05/09/2026 02:33:09  
**Feedback type:** Thumbs down  
**Status shown in paste:** Marked resolved  

**Reported feedback:**

> Title or description is wrong

#### PF-17 — Recipe ID: `604dd167-ff06-5400-8889-d5a90e5295b2`

**Submitted:** 05/09/2026 02:20:24  
**Feedback type:** Thumbs down  
**Status shown in paste:** Not marked resolved  

**Reported feedback:**

> Quantities look wrong

### Recipe IDs with multiple platform feedback reports

| Recipe ID | Number of reports | Platform feedback reference(s) |
|---|---:|---|
| `2875581813` | 2 | PF-04, PF-10 |
| `8049618090` | 3 | PF-06, PF-08, PF-09 |
| `960c01f9-9a7b-5532-afb9-b243d8a757d6` | 2 | PF-14, PF-15 |

**Caveat:** The platform reports are user-reported problems and ratings, not independently reproduced technical defects. The statuses above reflect the pasted interface only.
