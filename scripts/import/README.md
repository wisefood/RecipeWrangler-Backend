# Source acquisition pipelines

These scripts fetch or normalize current source datasets before the supported
database import commands in [`../README.md`](../README.md) run. Execute them
from the repository root. Raw outputs stay under ignored `data/` directories.

## FoodHero

1. `foodhero/export_foodhero_category_recipe_urls.py` discovers recipe URLs.
2. `foodhero/export_foodhero_recipes_to_json.py` scrapes resumable raw JSON.
3. `foodhero/transform_foodhero_recipes_json.py` creates normalized importer JSON.
4. `../neo4j/import_foodhero_recipes_to_neo4j.py` and
   `../postgres/import_foodhero_profile_trace.py` load the stores.

## HealthyFoods

1. `healthyfoods/export_healthyfoods_recipe_urls.py` discovers recipe URLs.
2. `healthyfoods/export_healthyfoods_recipes_to_json.py` scrapes recipe JSON.
3. `healthyfoods/export_healthyfoods_nutrition_to_json.py` scrapes published
   reference nutrition.
4. `healthyfoods/transform_healthyfood_recipes_json.py` normalizes records and
   assigns deterministic IDs.
5. `healthyfoods/enrich_healthyfoods_canonical_ingredients.py` parses and
   weights ingredient lines.
6. `../postgres/import_healthyfoods_profile_trace.py` and
   `../postgres/import_healthyfoods_to_neo4j.py` load the stores.

If an interrupted or older scrape has empty method fields,
`../maintenance/backfill_healthyfoods_instructions.py` reparses only those
records; it writes only when passed `--write`.

## MyPlate

1. `myplate/export_myplate_recipe_urls.py` discovers recipe URLs.
2. `myplate/export_myplate_recipes_to_json.py` scrapes recipe JSON.
3. `myplate/transform_myplate_recipes_json.py` normalizes records.
4. `myplate/enrich_myplate_canonical_ingredients.py` parses and weights
   ingredient lines.
5. `myplate/normalize_myplate_recipe_ids.py` assigns collision-checked stable
   IDs across the generated JSON files before import.
6. `myplate/import_myplate_recipes_to_neo4j.py` loads both original and direct
   canonical ingredient relationships into the current graph schema.
7. `../scrape_myplate_nutrition.py` and `../import_myplate_nutrition.py` load
   source-published reference nutrition.

## Slovenian composition tables

1. `slovenian_comp_tables/parse_plant.py` parses the plant PDF.
2. `slovenian_comp_tables/parse_meat.py` parses the meat PDF.
3. `slovenian_comp_tables/build_crosswalk.py` creates the reviewed nutrient map.
4. `slovenian_comp_tables/build_unified_excel.py` creates a human-review workbook.
5. `slovenian_comp_tables/build_slovenian_ingest.py` builds and loads the
   runtime composition table.

## Curated recipe websites

1. Run the matching scraper: `web/scrape_bestofhungary_recipes.py`,
   `web/scrape_irishheart_recipes.py`,
   `web/scrape_slovenian_kitchen_recipes.py`,
   `web/scrape_supervalu_recipes.py`, or
   `web/scrape_thehungarysoul_recipes.py`.
2. For SuperValu, `web/scrape_supervalu_methods.py` can repair missing method
   text in an existing scrape.
3. `web/import_web_scraped_recipes.py` imports the five normalized datasets
   into Neo4j, PostgreSQL, and Elasticsearch. Resume state is generated under
   ignored `data/checkpoints/`, never inside this source directory.

The canonical dump/restore path does not depend on these raw-source flows.
