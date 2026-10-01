#!/usr/bin/env python3
"""Import the generated cost catalogue into PostgreSQL for runtime use."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd
from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from recipe_wrangler.pricing.constants import PROCESSED_DIR  # noqa: E402
from recipe_wrangler.pricing.cost_calculator import (  # noqa: E402
    ALIAS_PATH,
    normalize_cost_name,
)
from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection  # noqa: E402


PRICE_COLUMNS = {
    "EU": "eu_reference_price_eur_kg",
    "IE": "price_ie_eur_kg",
    "HU": "price_hu_eur_kg",
    "SI": "price_si_eur_kg",
}
PRODUCT_COLUMNS = (
    "product_id",
    "source_ingredient_id",
    "canonical_name",
    "product_detail",
    "product_level",
    "food_category",
    "pli_category",
    "global_cost_tier",
    "price_evidence_confidence",
    "cost_reference_version",
)


def _json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _provenance(row: pd.Series) -> dict[str, Any]:
    return {
        column: _json_value(row[column])
        for column in row.index
        if column not in PRODUCT_COLUMNS and column not in PRICE_COLUMNS.values()
    }


def main() -> None:
    catalogue = pd.read_csv(PROCESSED_DIR / "ingredient_prices_classified.csv")
    aliases = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))["aliases"]
    cfg = _get_config()
    schema = cfg["schema"]
    products_table = f'"{schema}"."cost_products"'
    prices_table = f'"{schema}"."cost_prices"'
    aliases_table = f'"{schema}"."cost_aliases"'

    products = []
    prices = []
    for _, row in catalogue.iterrows():
        product = {column: _json_value(row[column]) for column in PRODUCT_COLUMNS}
        product["product_detail"] = product["product_detail"] or None
        product["provenance"] = json.dumps(_provenance(row), default=str)
        products.append(product)
        for region, column in PRICE_COLUMNS.items():
            prices.append(
                {
                    "product_id": product["product_id"],
                    "region": region,
                    "price_eur_kg": float(row[column]),
                }
            )
    alias_rows = [
        {
            "alias_normalized": normalize_cost_name(alias),
            "product_id": product_id,
        }
        for alias, product_id in aliases.items()
    ]

    with get_connection() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f"DELETE FROM {aliases_table}"))
            connection.execute(text(f"DELETE FROM {prices_table}"))
            connection.execute(text(f"DELETE FROM {products_table}"))
            connection.execute(
                text(
                    f"""
                    INSERT INTO {products_table} (
                        product_id, source_ingredient_id, canonical_name,
                        product_detail, product_level, food_category,
                        pli_category, global_cost_tier,
                        price_evidence_confidence, cost_reference_version, provenance
                    ) VALUES (
                        :product_id, :source_ingredient_id, :canonical_name,
                        :product_detail, :product_level, :food_category,
                        :pli_category, :global_cost_tier,
                        :price_evidence_confidence, :cost_reference_version,
                        CAST(:provenance AS jsonb)
                    )
                    """
                ),
                products,
            )
            connection.execute(
                text(
                    f"""
                    INSERT INTO {prices_table} (product_id, region, price_eur_kg)
                    VALUES (:product_id, :region, :price_eur_kg)
                    """
                ),
                prices,
            )
            connection.execute(
                text(
                    f"""
                    INSERT INTO {aliases_table} (alias_normalized, product_id)
                    VALUES (:alias_normalized, :product_id)
                    """
                ),
                alias_rows,
            )
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
    print(
        f"Imported {len(products)} cost products, {len(prices)} regional prices, "
        f"and {len(alias_rows)} reviewed aliases into PostgreSQL."
    )


if __name__ == "__main__":
    main()
