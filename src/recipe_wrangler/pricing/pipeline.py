"""End-to-end, offline-reproducible ingredient price pipeline."""

from __future__ import annotations

import pandas as pd

from .aggregate_prices import aggregate_country_prices
from .build_lookup import build_base_lookup, build_lookup
from .cost_classification import (
    build_enriched_cost_dataset,
    export_enriched_cost_dataset,
)
from .cost_validation import write_validation_report
from .constants import PROCESSED_DIR, TARGET_COUNTRIES
from .convert_country_prices import convert_country_prices
from .demonstration import build_demonstrations
from .documentation import write_pipeline_doc
from .inspect_sources import main as write_audit
from .io import write_table
from .normalize_pli import normalize_pli
from .normalize_fish import normalize_fish_prices
from .normalize_prices import normalize_prices


def main() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    write_audit()

    pli = normalize_pli()
    write_table(pli, PROCESSED_DIR / "eurostat_food_pli.csv")

    agricultural, agricultural_rejected, agricultural_mappings = normalize_prices()
    fish, fish_rejected, fish_mappings = normalize_fish_prices()
    normalized = pd.concat([agricultural, fish], ignore_index=True)
    rejected = pd.concat(
        [agricultural_rejected, fish_rejected], ignore_index=True
    )
    mappings = pd.concat(
        [agricultural_mappings, fish_mappings], ignore_index=True
    ).drop_duplicates()
    write_table(fish, PROCESSED_DIR / "eumofa_fish_latest_prices.csv")
    write_table(
        fish_rejected,
        PROCESSED_DIR / "eumofa_fish_rejected.csv",
        parquet=False,
    )
    write_table(
        fish_mappings,
        PROCESSED_DIR / "eumofa_fish_product_mapping.csv",
        parquet=False,
    )
    write_table(normalized, PROCESSED_DIR / "ec_prices_normalized.csv")
    write_table(rejected, PROCESSED_DIR / "ec_prices_rejected.csv", parquet=False)
    write_table(mappings, PROCESSED_DIR / "product_mapping.csv", parquet=False)

    country = aggregate_country_prices(normalized)
    write_table(country, PROCESSED_DIR / "ec_prices_country_aggregated.csv")
    converted = convert_country_prices(country, pli)
    write_table(converted, PROCESSED_DIR / "ec_prices_country_converted.csv")

    lookup, reference, stages = build_lookup(converted)
    write_table(lookup, PROCESSED_DIR / "ingredient_price_lookup.csv")
    write_table(reference, PROCESSED_DIR / "ingredient_price_reference.csv")
    write_table(stages, PROCESSED_DIR / "ingredient_price_lookup_by_stage.csv")
    write_table(
        lookup[lookup["food_group"] == "fish and seafood"].copy(),
        PROCESSED_DIR / "fish_price_lookup.csv",
    )
    write_table(
        reference[reference["food_group"] == "fish and seafood"].copy(),
        PROCESSED_DIR / "fish_price_reference.csv",
    )
    base = build_base_lookup(pd.concat([lookup, reference], ignore_index=True))
    base_lookup = base[base["target_country"].isin(TARGET_COUNTRIES)].copy()
    base_reference = base[base["target_country"] == "EU27"].copy()
    write_table(base_lookup, PROCESSED_DIR / "ingredient_price_base_lookup.csv")
    write_table(base_reference, PROCESSED_DIR / "ingredient_price_base_reference.csv")
    write_table(
        base_lookup[base_lookup["food_group"] == "fish and seafood"].copy(),
        PROCESSED_DIR / "fish_price_base_lookup.csv",
    )
    write_table(
        base_reference[base_reference["food_group"] == "fish and seafood"].copy(),
        PROCESSED_DIR / "fish_price_base_reference.csv",
    )

    classified, calibration = build_enriched_cost_dataset(
        base_reference,
        base_lookup,
        reference,
        lookup,
    )
    export_enriched_cost_dataset(classified, calibration)
    write_table(
        classified[classified["food_category"] == "fish and seafood"].copy(),
        PROCESSED_DIR / "fish_prices_classified.csv",
    )
    write_validation_report(classified, calibration)

    ranking, recipe_details, recipe_summary = build_demonstrations(lookup, classified)
    write_table(ranking, PROCESSED_DIR / "ingredient_ranking_demo.csv", parquet=False)
    write_table(recipe_details, PROCESSED_DIR / "recipe_cost_demo_details.csv", parquet=False)
    write_table(recipe_summary, PROCESSED_DIR / "recipe_cost_demo_summary.csv", parquet=False)

    write_pipeline_doc(normalized, rejected, country, lookup, base_lookup)
    print(
        f"Complete: {len(classified)} classified base/detail products, "
        f"{len(lookup)} target-country estimates"
    )


if __name__ == "__main__":
    main()
