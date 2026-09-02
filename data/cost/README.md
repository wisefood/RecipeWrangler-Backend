# Cost data

This directory separates immutable source downloads from reproducible pipeline outputs.

```text
data/cost/
├── raw/
│   ├── ec_agri_food/    # European Commission agricultural workbooks
│   ├── eurostat/        # Food price-level index workbook
│   └── eumofa/
│       ├── daily/       # Original daily online-retail export
│       └── monthly/     # Monthly median fish-price workbooks used by the pipeline
└── processed/           # Generated normalized, lookup, reference, and class tables
```

Do not edit files under `raw/`. Regenerate `processed/` with:

```bash
uv run python -m recipe_wrangler.pricing.pipeline
```

The generated catalogue is imported into PostgreSQL for runtime use. The local
files remain reproducible pipeline artefacts, not the runtime source of truth:

```bash
uv run alembic upgrade head
uv run python scripts/pricing/import_cost_catalogue_to_postgres.py
```

`cost_products` stores canonical base/detail products and provenance,
`cost_prices` stores one EUR/kg value per `EU`/`IE`/`HU`/`SI` region, and
`cost_aliases` stores reviewed phrase-to-product links. The active regional
Q33/Q67 category thresholds are stored in `cost_recipe_calibrations`.

Recipe categories are built separately from the PostgreSQL catalogue. The
backfill uses persisted EU profiling weights, reviewed direct/base product
matches and regional recipe-cost distributions. It writes the calibration report and
public non-monetary facets under `processed/recipe_cost_categories/`, while the
active calibration records are written to PostgreSQL:

```bash
uv run python scripts/one_off/backfill_recipe_cost_categories.py --apply
```

The public facet contains one `EU`/`IE`/`HU`/`SI` object per recipe, with
`low`/`medium`/`high`, coverage, contributors and explanations. Internal EUR
calculations remain in the PostgreSQL recipe profile for audit and
recalculation.
