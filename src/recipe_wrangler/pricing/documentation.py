"""Generate evidence-backed pricing pipeline documentation."""

from __future__ import annotations

import pandas as pd

from .constants import DOCS_DIR, WINDOW_END, WINDOW_START


def _markdown_table(frame: pd.DataFrame, digits: int = 3) -> str:
    data = frame.copy()
    for column in data.select_dtypes(include="number"):
        data[column] = data[column].map(lambda value: f"{value:.{digits}f}")
    headers = [str(column) for column in data.columns]
    rows = [[str(value) for value in row] for row in data.itertuples(index=False, name=None)]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def write_pipeline_doc(
    normalized: pd.DataFrame,
    rejected: pd.DataFrame,
    country_aggregated: pd.DataFrame,
    lookup: pd.DataFrame,
    base_lookup: pd.DataFrame,
) -> None:
    fish_rows = normalized[normalized["source_dataset"] == "eumofa_online_retail_fish"]
    rejection_counts = (
        rejected.groupby(["source_dataset", "reject_reason"])
        .size()
        .reset_index(name="rows")
        .sort_values("rows", ascending=False)
    )
    text = f"""# Ingredient Price Pipeline

## Purpose and boundary

This layer provides auditable economic proxies in EUR/kg for recipe ingredients in Ireland (IE), Hungary (HU), and Slovenia (SI). Agricultural sources cover several supply-chain stages; fish and seafood use EUMOFA monthly median online-retail prices. The layer does not define recipe-level cost categories. Recipe cost must later be calculated as ingredient weight in kg × country estimate, summed and divided by servings.

Raw inputs are organized under `data/cost/raw/`: EC agricultural workbooks in `ec_agri_food/`, the Eurostat PLI workbook in `eurostat/`, and EUMOFA files in `eumofa/daily/` and `eumofa/monthly/`. Generated clean outputs are written only under `data/cost/processed/`.

## Reproduction

```bash
uv sync --extra pricing
uv run python -m recipe_wrangler.pricing.pipeline
uv run pytest tests/test_pricing_pipeline.py -q
```

## Processing sequence

1. Audit every workspace Excel workbook and deeply profile the pricing family.
2. Extract only Eurostat 2025 `Price level indices (EU27_2020=100)` rows for EU27 and the ten food categories. PPP values are rejected by indicator validation.
3. Extract the latest current EUR/kg monthly median from each EUMOFA fish product-country series; reject stale series and latest-price temporal outliers.
4. Normalize schema, country identifiers, dates, products, product detail, exact price type, market stage, original unit, and EUR/kg.
5. Log every rejected row with source filename, sheet, row number, raw values, and reason.
6. Restrict time aggregation to the most recent complete-month window: **{WINDOW_START} through {WINDOW_END}**.
7. Collapse multiple same-country/same-stage/same-date markets to one daily/period median, then compute one temporal representative per ingredient/detail × country × stage.
8. Convert each country representative using `source price × target category PLI / source category PLI`.
9. Take an equal-country median for each target so observation-rich countries cannot dominate.
10. Retain every market stage in `ingredient_price_lookup_by_stage`; choose one proxy stage using a documented downstream-stage priority for the convenience lookup.
11. Calculate one explicit general price per base product and country as the median of its selected detail prices.
12. Derive one EU reference price for each base product, then learn global tertile thresholds from those base rows only.
13. Apply the frozen thresholds to base and detailed products, calculate separate within-category ranks for bases, and preserve IE/HU/SI prices and price indices alongside the EU-relative tier.

Manual canonical-product and food-group/PLI mappings live in the editable `src/recipe_wrangler/pricing/product_mapping_overrides.json` resource. `data/cost/processed/product_mapping.csv` expands that configuration against every observed source concept for review.

## Market-stage handling

The selected-stage priority is: retail selling, retail buying, selling, ex-packaging, non-retail buying, unspecified market price, producer/farm-gate, first-customer/processor/silo/port/incoterm stages, then unspecified commodity stages. Selection never averages stages. The selected stage is an economic proxy and is returned by `get_price`.

EUMOFA fish prices are explicitly tagged `retail_selling`. Fresh, frozen, smoked, and prepared/preserved forms remain separate details; package size is retained in provenance but does not create a separate ingredient concept.

This choice is intentionally conservative but cannot make heterogeneous EC reporting stages economically identical. Commodity cereals, EU-aggregate dairy, raw milk, eggs, rice, olive oil, and lamb should not be described as supermarket shelf prices.

## Unit policy

- `€/100kg` → divide by 100.
- `€/tonne` or `€/ton` → divide by 1,000.
- `€/kg` → retain.
- `€/head` → reject unless an authoritative edible-yield denominator becomes available.
- `€/hl` → reject unless a product-specific density supports mass conversion.
- Missing unit → reject.

The piglet workbook's `€/100kg` header conflicts with the official EC endpoint, which reports piglets per animal (`P`); all piglet rows are rejected.

## Confidence rule

Confidence is a point score based on source-country count, observation count, relative country dispersion, stage suitability, direct target-country observation, and recency. High requires at least 10 points, Medium 6–9, and Low fewer than 6. EU-only non-retail aggregates are capped at Low; undefined commodity stages cannot be High. Broad `all types`/`average` concepts receive a penalty.

## Outputs from this run

- Normalized valid rows: **{len(normalized):,}**
- Latest normalized EUMOFA fish series: **{len(fish_rows):,}**
- Rejected rows: **{len(rejected):,}**
- Country/product/stage representatives: **{len(country_aggregated):,}**
- Target-country lookup rows: **{len(lookup):,}**
- Canonical ingredient/detail concepts: **{lookup['ingredient_id'].nunique():,}**
- Base-product target-country rows: **{len(base_lookup):,}**
- Canonical base products: **{base_lookup['ingredient_id'].nunique():,}**

The canonical classified output is `ingredient_prices_classified.csv` (and Parquet), with its frozen thresholds and version in `cost_classification_metadata.json`. The validation tables are in `cost_classification_validation/`, and the human-readable scientific review is `docs/COST_CLASSIFICATION_VALIDATION.md`. Fish-specific review tables include `eumofa_fish_latest_prices`, `fish_price_lookup`, `fish_price_reference`, `fish_price_base_lookup`, `fish_price_base_reference`, and `fish_prices_classified`. The unified ingredient tables contain the same fish rows alongside all other food groups.

### Rejection summary

{_markdown_table(rejection_counts.head(30), digits=0)}

## Runtime API

```python
from recipe_wrangler.pricing import get_price

get_price(ingredient="pork minced meat", country="SI")
get_price(ingredient="apple", country="SI")  # explicit base-product median
get_price(ingredient="salmon", country="IE")
get_price(ingredient="salmon fresh fillets", country="IE")
```

A bare canonical name returns its base-product median; an exact detailed name returns the detailed price. Each result includes the global EU-relative cost tier, global percentile, requested-country price index, within-category context, boundary margin, version, and separate evidence confidence. Detailed rows also include their ratio and relationship to the parent base price. The symbols `€`, `€€`, and `€€€` are relative labels—not literal one-, two-, or three-euro prices. Unsupported concepts such as pork loin or lobster raise a lookup error because the supplied workbooks contain no verified price for them.

## Provenance and limitations

Every output retains source dataset/file/sheet, exact source price type, normalized stage, source countries, source dataset list, reference dates, observation counts, dispersion, and PLI year. Base-product rows additionally retain their variant count and the aggregation method `median_of_selected_detail_prices`; `mixed_selected_stages` is reported when their details use different market stages. The EC describes its portal data as market information collected across stages of the supply chain; this pipeline therefore uses “economic proxy,” not “retail price.” See the [EC market-transparency explanation](https://agriculture.ec.europa.eu/common-agricultural-policy/agri-food-supply-chain/market-transparency_en) and [Agri-food Data API documentation](https://agridata.ec.europa.eu/extensions/API_Documentation/GeneralInfo.html).
"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "PRICE_PIPELINE.md").write_text(text, encoding="utf-8")
