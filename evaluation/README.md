# Evaluation

Each analyze step has its own folder with the same layout: `README.md` (data, results, limits), `data/` (inputs and gold labels) and `results/` (predictions and scores). Methods and metric definitions are in [METHODS.md](METHODS.md). Last updated 13 November 2026.

| Step | Data | Headline result | Folder |
|---|---|---|---|
| Parsing | TASTEset, 500 recipes in 5 sets | Quantity and units about 99%; unseen sets 92-96% of recipes clean | [parsing](parsing/README.md) |
| Weights | 348 Fineli reference portions | Legacy route answers 259/348 and 157 are within 20% of the reference (60.6% of answers); count items weakest | [weight_calculation](weight_calculation/README.md) |
| Matching | 1,767 expert-labelled ingredient lines (Hungarian, Slovenian living labs) | Correct food 98.9% Hungarian, 98.5% Slovenian | [matching](matching/README.md) |
| Nutrition | 6 reference sets, 6,000+ recipes with published panels | Energy error 36-61 kcal per serving; sodium weakest | [nutrition](nutrition/README.md) |
| Allergens and diets | 1,300 EU Allergen Dataset recipes; 100 TASTEset recipes | Allergen precision 98.99%, recall 82.13%; vegan recall 19/22, vegetarian 40/61 | [allergens_diets](allergens_diets/README.md) |
| Sustainability | 13,032 reference factors | No accuracy evaluation possible | [sustainability](sustainability/README.md) |
| Search | 13 personas, 62 queries | Not run | [search](search/README.md) |
| Adaptation | 537 Living Lab recipes, four modes | Nutrition 363 adapted (63 up a letter); cost 48; sustainability ~150 candidates; time 49 overlap candidates | [adaptation](adaptation/README.md) |

## Limits that apply to every result

- All recipes used for tuning are development-exposed. Only parsing has unseen sets, and each was used once.
- No labels are independent: they are source-derived, silver, or assistant-reviewed.
- Everything is local, uncommitted and undeployed. The stored data is behind the code.
- These results do not validate the full analyze pipeline end to end.

Cleaned folders keep only data and results. Earlier working files were moved to `analysis/evaluation-archive-2026-11-12/` or remain in the original `analysis/` folders.
