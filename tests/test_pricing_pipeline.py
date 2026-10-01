from __future__ import annotations

import pandas as pd
import pytest

from recipe_wrangler.pricing.aggregate_prices import aggregate_country_prices
from recipe_wrangler.pricing.build_lookup import build_base_lookup
from recipe_wrangler.pricing.constants import PLI_CATEGORIES, PLI_SOURCE_DIR
from recipe_wrangler.pricing.convert_country_prices import convert_country_prices
from recipe_wrangler.pricing.lookup import _load_tables, get_price
from recipe_wrangler.pricing.normalize_fish import FISH_SOURCE_DIR, normalize_fish_prices
from recipe_wrangler.pricing.normalize_pli import normalize_pli
from recipe_wrangler.pricing.normalize_prices import (
    _precheck,
    _unit_and_value,
    normalize_country,
    normalize_stage,
)
from recipe_wrangler.pricing.normalize_products import map_fish_product


def test_numeric_member_state_ids_are_explicitly_mapped() -> None:
    assert normalize_country(70) == ("IE", "Ireland")
    assert normalize_country(160) == ("HU", "Hungary")
    assert normalize_country(230) == ("SI", "Slovenia")
    assert normalize_country(0) == ("EU27", "European Union")


def test_mass_units_convert_to_eur_per_kg() -> None:
    value, unit, normalized = _unit_and_value(
        pd.Series({"Price (€/100kg)": 381.9}), "beef_cuts"
    )
    assert value == 381.9
    assert unit == "€/100kg"
    assert normalized == pytest.approx(3.819)

    _, _, tonne_normalized = _unit_and_value(
        pd.Series({"Price (€/Tonne)": 600}), "rice"
    )
    assert tonne_normalized == pytest.approx(0.6)


def test_incompatible_or_mislabelled_sources_are_rejected() -> None:
    with pytest.raises(ValueError, match="per_head"):
        _precheck("pigmeat_piglets", pd.Series(dtype=object))
    with pytest.raises(ValueError, match="source_unit_missing"):
        _precheck("protein_crops", pd.Series(dtype=object))
    with pytest.raises(ValueError, match="volume_unit"):
        _precheck("wine", pd.Series(dtype=object))


def test_market_stages_are_normalized_without_collapsing_distinctions() -> None:
    assert normalize_stage("Retail selling price", "fruit_vegetables") == "retail_selling"
    assert normalize_stage("Retail buying price", "beef_cuts") == "retail_buying"
    assert normalize_stage("Non-retail buying price", "beef_cuts") == "non_retail_buying"
    assert normalize_stage("Price at farm gate", "cereals") == "farm_gate"


def test_fish_mapping_retains_preservation_and_product_form() -> None:
    salmon = map_fish_product("Salmon fillets", "Salmon fillets", "Frozen")
    assert salmon.canonical_product == "salmon"
    assert salmon.product_detail == "frozen, fillets"
    assert salmon.food_group == "fish and seafood"

    pollock = map_fish_product(
        "Fish fillets, breaded",
        "Alaska pollock fillets, breaded and battered",
        "Frozen",
    )
    assert pollock.canonical_product == "alaska pollock"
    assert pollock.product_detail == "frozen, fillets, breaded and battered"


def test_temporal_aggregation_gives_each_country_one_representative() -> None:
    rows = []
    for country_code, country, prices in (
        ("DE", "Germany", [1.0, 3.0, 5.0]),
        ("FR", "France", [8.0, 10.0]),
    ):
        for index, price in enumerate(prices):
            rows.append(
                {
                    "product_normalized": "test food",
                    "product_detail": "",
                    "food_group": "cereals",
                    "pli_category": "Cereals and cereal products",
                    "source_country_code": country_code,
                    "source_country": country,
                    "market_stage": "retail_selling",
                    "date": f"2026-0{index + 1}-01",
                    "eur_per_kg": price,
                    "source_dataset": "test",
                    "source_file": "test.xlsx",
                    "price_type_raw": "Retail selling price",
                }
            )
    result = aggregate_country_prices(pd.DataFrame(rows))
    representatives = result.set_index("source_country_code")["representative_eur_per_kg"]
    assert representatives["DE"] == 3.0
    assert representatives["FR"] == 9.0
    assert len(result) == 2


def test_country_conversion_uses_pli_ratio() -> None:
    country = pd.DataFrame(
        [
            {
                "product_normalized": "test food",
                "product_detail": "",
                "food_group": "cereals",
                "pli_category": "Cereals and cereal products",
                "source_country_code": "DE",
                "source_country": "Germany",
                "market_stage": "retail_selling",
                "representative_eur_per_kg": 10.0,
                "mean_eur_per_kg": 10.0,
                "min_eur_per_kg": 10.0,
                "max_eur_per_kg": 10.0,
                "std_eur_per_kg": 0.0,
                "iqr_eur_per_kg": 0.0,
                "n_observations": 1,
                "n_periods": 1,
                "date_start": "2026-01-01",
                "date_end": "2026-01-01",
                "source_datasets": '["test"]',
                "source_files": '["test.xlsx"]',
                "price_types": '["Retail selling price"]',
            }
        ]
    )
    rows = []
    values = {"DE": 100.0, "IE": 120.0, "HU": 80.0, "SI": 90.0}
    for code, value in values.items():
        rows.append(
            {
                "country_code": code,
                "country": code,
                "pli_category": "Cereals and cereal products",
                "year": 2025,
                "pli": value,
            }
        )
    converted = convert_country_prices(country, pd.DataFrame(rows)).set_index("target_country")
    assert converted.loc["IE", "converted_eur_per_kg"] == pytest.approx(12.0)
    assert converted.loc["HU", "converted_eur_per_kg"] == pytest.approx(8.0)
    assert converted.loc["SI", "converted_eur_per_kg"] == pytest.approx(9.0)


def test_base_lookup_uses_median_of_selected_details() -> None:
    rows = []
    for detail, price, confidence in (
        ("gala", 1.0, "Medium"),
        ("fuji", 2.0, "Low"),
        ("braeburn", 6.0, "Medium"),
    ):
        rows.append(
            {
                "ingredient_id": f"apple_{detail}",
                "ingredient_name": "apple",
                "ingredient_detail": detail,
                "food_group": "fruit and nuts",
                "pli_category": "Fruits and nuts",
                "target_country": "SI",
                "estimated_eur_per_kg": price,
                "market_stage": "retail_selling",
                "n_source_countries": 2,
                "n_observations": 10,
                "price_dispersion": 0.1,
                "n_source_aggregates": 0,
                "relative_dispersion": 0.1,
                "date_start": "2025-08-01",
                "date_end": "2026-07-31",
                "confidence": confidence,
                "source_countries": '["DE", "SI"]',
                "source_datasets": '["fruit_vegetables"]',
                "provenance": '["fruit_vegetables"]',
                "has_direct_target_observation": True,
                "pli_year": 2025,
            }
        )
    result = build_base_lookup(pd.DataFrame(rows)).iloc[0]
    assert result["ingredient_id"] == "apple"
    assert result["estimated_eur_per_kg"] == 2.0
    assert result["n_variants"] == 3
    assert result["aggregation_method"] == "median_of_selected_detail_prices"


def test_get_price_returns_provenance_rich_result(tmp_path) -> None:
    pd.DataFrame(
        [
            {
                "ingredient_id": "pork_minced_meat",
                "ingredient_name": "pork",
                "ingredient_detail": "minced meat",
                "target_country": "SI",
                "estimated_eur_per_kg": 4.2,
                "pli_category": "Live animals, meat and other parts of slaughtered land animals",
                "confidence": "High",
                "market_stage": "retail_buying",
                "source_countries": '["DE", "HU", "SI"]',
                "source_datasets": '["pigmeat_cuts"]',
                "date_start": "2025-08-01",
                "date_end": "2026-07-31",
                "n_source_countries": 3,
                "n_observations": 36,
            }
        ]
    ).to_csv(tmp_path / "ingredient_price_lookup.csv", index=False)
    pd.DataFrame(
        [
            {
                "ingredient_id": "pork",
                "ingredient_name": "pork",
                "ingredient_detail": "",
                "target_country": "SI",
                "estimated_eur_per_kg": 5.0,
                "pli_category": "Live animals, meat and other parts of slaughtered land animals",
                "confidence": "Medium",
                "market_stage": "mixed_selected_stages",
                "n_variants": 3,
                "aggregation_method": "median_of_selected_detail_prices",
                "source_countries": '["DE", "HU", "SI"]',
                "source_datasets": '["pigmeat_cuts"]',
                "date_start": "2025-08-01",
                "date_end": "2026-07-31",
                "n_source_countries": 3,
                "n_observations": 72,
            }
        ]
    ).to_csv(tmp_path / "ingredient_price_base_lookup.csv", index=False)
    classification_common = {
        "global_cost_tier": "€€€",
        "global_cost_percentile": 0.88,
        "price_index_ie": 1.2,
        "price_index_hu": 0.8,
        "price_index_si": 0.9,
        "within_category_rank": "",
        "within_category_percentile": "",
        "within_category_position": "",
        "within_category_resolution": "",
        "category_size": 4,
        "tier_boundary_margin_pct": 12.0,
        "cost_reference_version": "EU-2026-v1",
        "parent_base_product_id": "",
        "parent_within_category_percentile": "",
        "parent_within_category_position": "",
        "parent_within_category_resolution": "",
        "detail_to_base_price_ratio": "",
        "detail_vs_base": "",
    }
    pd.DataFrame(
        [
            {
                **classification_common,
                "source_ingredient_id": "pork",
                "product_level": "base",
                "within_category_rank": 1,
                "within_category_percentile": 0.125,
                "within_category_position": "Low",
                "within_category_resolution": "coarse",
            },
            {
                **classification_common,
                "source_ingredient_id": "pork_minced_meat",
                "product_level": "detail",
                "parent_base_product_id": "base__pork",
                "parent_within_category_percentile": 0.125,
                "parent_within_category_position": "Low",
                "parent_within_category_resolution": "coarse",
                "detail_to_base_price_ratio": 0.84,
                "detail_vs_base": "lower",
            },
        ]
    ).to_csv(tmp_path / "ingredient_prices_classified.csv", index=False)
    _load_tables.cache_clear()
    result = get_price("pork minced meat", "SI", processed_dir=tmp_path)
    assert result["estimated_eur_per_kg"] == 4.2
    assert result["global_cost_tier"] == "€€€"
    assert result["global_cost_percentile"] == pytest.approx(0.88)
    assert result["country_price_index"] == pytest.approx(0.9)
    assert result["detail_to_base_price_ratio"] == pytest.approx(0.84)
    assert result["parent_within_category_position"] == "Low"
    assert result["cost_reference_version"] == "EU-2026-v1"
    assert result["source_countries"] == ["DE", "HU", "SI"]
    assert result["reference_period"] == "2025-08-01 to 2026-07-31"
    assert result["price_scope"] == "detailed_product"

    base_result = get_price("pork", "SI", processed_dir=tmp_path)
    assert base_result["estimated_eur_per_kg"] == 5.0
    assert base_result["price_scope"] == "base_product"
    assert base_result["n_variants"] == 3
    assert base_result["within_category_rank"] == pytest.approx(1)


@pytest.mark.skipif(
    not any(FISH_SOURCE_DIR.glob("*.xlsx")),
    reason="EUMOFA fish workbooks are not distributed with source control",
)
def test_actual_fish_workbooks_yield_latest_current_prices() -> None:
    normalized, rejected, mappings = normalize_fish_prices()
    assert len(normalized) == 214
    assert len(rejected) == 5
    assert normalized["product_normalized"].nunique() == 17
    assert normalized["source_country_code"].nunique() == 17
    assert normalized["date"].between("2025-08-01", "2026-07-31").all()
    assert normalized["market_stage"].eq("retail_selling").all()
    assert normalized["unit_original"].eq("€/kg").all()
    assert normalized["eur_per_kg"].between(1, 100).all()
    assert rejected["reject_reason"].str.startswith("stale_fish_price:").all()
    assert mappings["canonical_product"].nunique() == 17


@pytest.mark.skipif(
    not any(PLI_SOURCE_DIR.glob("*prc_ppp_ind_1*.xlsx")),
    reason="Workspace PLI workbook is not distributed with source control",
)
def test_actual_pli_workbook_contains_only_validated_eu27_rows() -> None:
    frame = normalize_pli()
    assert len(frame) == 27 * len(PLI_CATEGORIES)
    assert frame["country_code"].nunique() == 27
    assert frame["pli_category"].nunique() == len(PLI_CATEGORIES)
    assert frame["year"].unique().tolist() == [2025]
    assert not frame.duplicated(["country_code", "pli_category", "year"]).any()
    assert frame["pli"].between(40, 250).all()
