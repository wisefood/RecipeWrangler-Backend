# Data and database scripts

This directory contains supported commands for restoring, importing, enriching,
validating, and exporting the current RecipeWrangler data model. These modules
are not part of the running API unless a test explicitly imports a helper.

## Reproduction contract

The supported way to reproduce the **exact current database state** is a
three-store bundle made by `maintenance/dump_all_lite.py` and restored by
`maintenance/restore_all_lite.py`. A recipe is split across Neo4j, PostgreSQL,
and Elasticsearch, so a dump of only one store is not a complete baseline.

```bash
# Create a consistent baseline under dumps/local/<timestamp>-baseline/
uv run python scripts/maintenance/dump_all_lite.py --label baseline

# Restore a received baseline. This wipes the target stores before loading.
uv run python scripts/maintenance/restore_all_lite.py \
  dumps/received/<timestamp>-baseline

# Check owner/index agreement after restore.
uv run python scripts/maintenance/reconcile.py
```

Raw-source rebuilding is a different workflow. It recreates the current schema
and calculations, but is not byte-for-byte deterministic because source files
under `data/` are distributed out of band and several enrichment stages use an
LLM. Every dump bundle includes a manifest and record counts; keep the raw input
version and model settings with any rebuild that needs scientific provenance.

The current corpus contains these `Recipe.source` values:

- Best of Hungary
- Curated Hungarian Recipes
- Curated Irish Recipes
- Curated Slovenian Recipes
- FoodHero
- HealthyFoods
- Irish Heart Foundation
- MyPlate
- Slovenian Kitchen
- SuperValu
- The Hungary Soul

Recipe1M is retired and is not part of the current corpus or supported scripts.

## Prerequisites

- Configure `.env` from `.env.example`.
- Start Neo4j, PostgreSQL, and Elasticsearch.
- Run `uv sync --dev`.
- Obtain the required raw datasets or a dump bundle out of band.
- Run mutating commands without `--apply` or `--write` first when that mode is
  available.

## Script inventory

“Writes” describes the primary side effect. Commands marked dry-run default do
not write until `--apply` or `--write` is supplied.

### Exact-state, synchronization, and catalog operations

| Script | Purpose | Writes |
|---|---|---|
| `maintenance/dump_all_lite.py` | Canonical native three-store backup with manifest and counts. | `dumps/local/` |
| `maintenance/restore_all_lite.py` | Restore a canonical bundle into local stores. | All three stores; destructive |
| `export_sync_bundle.py` | Package selected stores and image assets for transfer. | `dumps/sent/` |
| `maintenance/reconcile.py` | Detect owner/index drift and optionally repair Elasticsearch projections. | Dry-run default; Elasticsearch with `--apply` |
| `maintenance/reproject_all_recipes.py` | Rebuild every active Elasticsearch recipe document from Neo4j and PostgreSQL owners. | Elasticsearch |
| `catalog/build_recipes.py` | Build and atomically activate a versioned recipe index. | Elasticsearch with `--apply` |
| `catalog/annotate_recipes.py` | Add controlled cuisine, flavour, mood, and related annotations. | Elasticsearch with apply flags |
| `elasticsearch/import_vector_embeddings.py` | Load a validated NDJSON vector collection and move its stable alias. | Elasticsearch |
| `elasticsearch/restore_recipes_dump.py` | Restore a standalone logical recipe-index dump. | Elasticsearch |
| `disable_recipes.py` | Soft-disable or re-enable selected recipes in the graph and index. | Dry-run default; Neo4j and Elasticsearch |
| `maintenance/purge_source.py` | Hard-delete one exact source across all stores after a backup. | Dry-run default; destructive with `--apply` |

### Composition tables and runtime reference data

| Script | Purpose | Writes |
|---|---|---|
| `build_eu_nutrient_mapping.py` | Build the reviewed source-column-to-canonical nutrient map. | `data/nutrition/composition_tables/eu_national/` CSV |
| `build_eu_global_dataset.py` | Merge current Ciqual, CoFID, and NEVO inputs into the EU composition table. | `data/nutrition/composition_tables/eu_national/` outputs |
| `composition/export_hungarian_comp_table_csv.py` | Normalize the Hungarian composition workbook for ingestion. | `data/` CSV |
| `postgres/import_irish_ingredients_nutrition_psql.py` | Load the Irish composition table. | PostgreSQL |
| `postgres/import_hungarian_ingredients_nutrition_psql.py` | Load the normalized Hungarian composition table. | PostgreSQL |
| `postgres/import_pipeline_static_data.py` | Load versioned runtime lookup files, including USDA portion weights. | PostgreSQL |
| `pricing/import_cost_catalogue_to_postgres.py` | Load the generated ingredient cost catalogue. | PostgreSQL |

### Current source import and profiling

| Script | Purpose | Writes |
|---|---|---|
| `neo4j/import_foodhero_recipes_to_neo4j.py` | Import normalized FoodHero recipes and canonical ingredient links. | Neo4j |
| `postgres/import_foodhero_profile_trace.py` | Build a selected regional FoodHero profile set. | Dry-run default; PostgreSQL with `--write` |
| `postgres/import_healthyfoods_profile_trace.py` | Build a selected regional HealthyFoods profile set. | Dry-run default; PostgreSQL with `--write` |
| `postgres/import_healthyfoods_to_neo4j.py` | Project imported HealthyFoods profiles into the recipe graph. | Dry-run default; Neo4j with `--write` |
| `scrape_myplate_nutrition.py` | Fetch MyPlate-published reference nutrition from recipe JSON-LD. | `data/recipes/myplate/` JSON |
| `import_myplate_nutrition.py` | Load MyPlate-published reference nutrition rows. | PostgreSQL |
| `scrape_safefood_recipes.py` | Fetch the current safefood.net recipe corpus. | `data/recipes/safefood_web/` |
| `safefood_rcsi.py` | Shared pure helpers for matching RCSI lab records to SafeFood web recipes. | None; library module |
| `import_safefood_web.py` | Profile and import SafeFood web recipes, including matched RCSI reference rows. | Dry-run default; all three stores with `--write` |
| `prepare_essrg.py` | Convert the PLANEAT workbook into canonical recipe JSON with CoFID provenance. | `data/recipes/essrg/` JSON |
| `import_planeat.py` | Import prepared PLANEAT recipes and direct CoFID nutrition. | Dry-run default; all three stores with `--write` |
| `profile_planeat_regions.py` | Build the four regional calculated profiles for PLANEAT recipes. | Dry-run default; PostgreSQL with `--write` |
| `import_slovenian.py` | Import curated Slovenian recipes and OPKP reference nutrition. | Dry-run default; all three stores with `--write` |
| `profile_slovenian_regions.py` | Build the four regional calculated profiles for Slovenian recipes. | Dry-run default; PostgreSQL with `--write` |
| `recompute_all_profiles.py` | Recompute four-region nutrition and sustainability for graph-backed sources. | Dry-run default; PostgreSQL with `--write` |

Source acquisition commands live under `scripts/import/`; their README gives
the order for each current raw-source pipeline.

### Graph, facet, and profile materialization

| Script | Purpose | Writes |
|---|---|---|
| `neo4j/enrich_fato_foodon.py` | Materialize current FATO/FoodOn allergen and consumer-group semantics. | Dry-run default; Neo4j with `--apply` |
| `neo4j/tag_allergens.py` | Materialize ingredient allergen evidence from ontology and keyword rules. | Neo4j; always writes |
| `neo4j/classify_vegan_vegetarian.py` | Materialize ingredient and recipe vegan/vegetarian suitability. | Dry-run default; Neo4j with `--apply` |
| `neo4j/tag_recipes.py` | Materialize graph recipe tags derived from ingredient evidence. | Neo4j; always writes |
| `tag_gluten_free_options.py` | Derive HealthyFoods gluten-free-option tags from recipe text. | Neo4j by default; `--dry-run` writes only its audit CSV |
| `facets/tag_diet.py` | Materialize deterministic catalog diet facets. | Dry-run default; Neo4j/Elasticsearch with `--apply` |
| `facets/tag_nutrition_claims.py` | Materialize EU-threshold nutrition claims. | Dry-run default; Neo4j/Elasticsearch with `--apply` |

### Audits, diagnostics, exports, and assets

| Script | Purpose | Writes |
|---|---|---|
| `pricing/generate_base_price_distribution_plot.py` | Generate current cost-catalogue and recipe-category distributions. | Figure artefacts |
| `generate_recipe_images_grokified.py` | Generate and publish missing recipe images via the configured provider. | Image assets and Neo4j |
| `generate_safefood_images.py` | Generate local fallback images for SafeFood recipes lacking images. | `data/recipes/irish_safefood/images/` and Neo4j |

## Maintenance rule

A script belongs here only when it is reusable against the current schema or is
part of the documented restore/import flow. Applied migrations, numbered
experiments, previous corpus versions, checkpoints, generated reports, and
database dumps belong in ignored operational storage, not in Git.
