"""Runtime ingredient price lookup API."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

import pandas as pd

from .constants import PROCESSED_DIR, TARGET_COUNTRIES


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


@lru_cache(maxsize=4)
def _load_tables(
    processed_dir: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base = Path(processed_dir)
    prices = pd.read_csv(base / "ingredient_price_lookup.csv", keep_default_na=False)
    base_prices = pd.read_csv(
        base / "ingredient_price_base_lookup.csv", keep_default_na=False
    )
    classified = pd.read_csv(
        base / "ingredient_prices_classified.csv", keep_default_na=False
    )
    return prices, base_prices, classified


def get_price(
    ingredient: str,
    country: str,
    *,
    processed_dir: Path = PROCESSED_DIR,
) -> dict[str, Any]:
    """Return a provenance-rich country estimate for a supported ingredient."""

    country = country.upper()
    if country not in TARGET_COUNTRIES:
        raise ValueError(f"country must be one of {TARGET_COUNTRIES}, got {country!r}")
    prices, base_prices, classified = _load_tables(str(processed_dir.resolve()))
    candidates = prices[prices["target_country"] == country].copy()
    base_candidates = base_prices[base_prices["target_country"] == country].copy()
    query = _key(ingredient)
    base_candidates["name_key"] = base_candidates["ingredient_name"].map(_key)
    base_candidates["id_key"] = base_candidates["ingredient_id"].map(_key)
    base_matches = base_candidates[
        (base_candidates["name_key"] == query)
        | (base_candidates["id_key"] == query)
    ]
    candidates["combined_key"] = (
        candidates["ingredient_name"] + " " + candidates["ingredient_detail"]
    ).map(_key)
    candidates["id_key"] = candidates["ingredient_id"].map(_key)
    matches = candidates[
        (candidates["combined_key"] == query)
        | (candidates["id_key"] == query)
    ].copy()
    if not base_matches.empty:
        row = base_matches.iloc[0]
        class_row = classified[
            classified["product_level"].eq("base")
            & classified["source_ingredient_id"].eq(row["ingredient_id"])
        ]
        price_scope = "base_product"
    elif not matches.empty:
        row = matches.iloc[0]
        class_row = classified[
            classified["product_level"].eq("detail")
            & classified["source_ingredient_id"].eq(row["ingredient_id"])
        ]
        price_scope = "detailed_product"
    else:
        detail_suggestions = candidates[
            candidates["combined_key"].str.contains(query, regex=False)
        ]["combined_key"].head(8).tolist()
        base_suggestions = base_candidates[
            base_candidates["name_key"].str.contains(query, regex=False)
        ]["name_key"].head(8).tolist()
        suggestions = list(dict.fromkeys(base_suggestions + detail_suggestions))[:8]
        raise LookupError(f"Unsupported ingredient {ingredient!r}; candidates: {suggestions}")
    if class_row.empty:
        raise LookupError(f"No cost classification for {row['ingredient_id']!r}")
    classification = class_row.iloc[0]

    def json_value(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        return value

    country_key = country.lower()
    result = {
        "ingredient_name": row["ingredient_name"],
        "ingredient_detail": row["ingredient_detail"],
        "price_scope": price_scope,
        "estimated_eur_per_kg": float(row["estimated_eur_per_kg"]),
        "pli_category": row["pli_category"],
        "global_cost_tier": classification["global_cost_tier"],
        "global_cost_percentile": float(classification["global_cost_percentile"]),
        "country_price_index": float(classification[f"price_index_{country_key}"]),
        "within_category_rank": _optional_float(
            classification["within_category_rank"]
        ),
        "within_category_percentile": _optional_float(
            classification["within_category_percentile"]
        ),
        "within_category_position": _optional_text(
            classification["within_category_position"]
        ),
        "within_category_resolution": _optional_text(
            classification["within_category_resolution"]
        ),
        "category_size": int(float(classification["category_size"])),
        "tier_boundary_margin_pct": float(
            classification["tier_boundary_margin_pct"]
        ),
        "cost_reference_version": classification["cost_reference_version"],
        "confidence": row["confidence"],
        "market_stage": row["market_stage"],
        "n_variants": int(row["n_variants"]) if price_scope == "base_product" else 1,
        "aggregation_method": (
            row["aggregation_method"]
            if price_scope == "base_product"
            else "selected_detail_price"
        ),
        "source_countries": json_value(row["source_countries"]),
        "source_datasets": json_value(row["source_datasets"]),
        "reference_period": f"{row['date_start']} to {row['date_end']}",
    }
    if price_scope == "detailed_product":
        result.update(
            {
                "parent_base_product_id": classification["parent_base_product_id"],
                "parent_within_category_percentile": _optional_float(
                    classification["parent_within_category_percentile"]
                ),
                "parent_within_category_position": _optional_text(
                    classification["parent_within_category_position"]
                ),
                "parent_within_category_resolution": _optional_text(
                    classification["parent_within_category_resolution"]
                ),
                "detail_to_base_price_ratio": float(
                    classification["detail_to_base_price_ratio"]
                ),
                "detail_vs_base": classification["detail_vs_base"],
            }
        )
    return result


def _optional_float(value: Any) -> float | None:
    """Convert a possibly empty CSV value to a nullable float."""

    if value == "" or pd.isna(value):
        return None
    return float(value)


def _optional_text(value: Any) -> str | None:
    """Convert a possibly empty CSV value to nullable text."""

    if value == "" or pd.isna(value):
        return None
    return str(value)
