"""Generate supported-ingredient ranking and recipe cost demonstrations."""

from __future__ import annotations

import pandas as pd

from .constants import PROCESSED_DIR, TARGET_COUNTRIES
from .io import write_table


RECIPES = {
    "Pork, rice and tomato": {
        "servings": 4,
        "ingredients": {
            "pork_minced_meat": 0.60,
            "rice_milled_non_parboiled_japonica_variety_average": 0.30,
            "tomato_round": 0.40,
        },
    },
    "Chicken and rice": {
        "servings": 4,
        "ingredients": {
            "chicken_breast_fillet": 0.60,
            "rice_milled_non_parboiled_japonica_variety_average": 0.30,
        },
    },
    "Beef, olive oil and tomato": {
        "servings": 4,
        "ingredients": {
            "beef_minced_meat": 0.60,
            "olive_oil_extra_virgin_up_to_0_8": 0.04,
            "tomato_round": 0.40,
        },
    },
}

RANKING_FAMILIES = (
    "rye", "wheat", "onion", "sugar", "carrot", "milk", "rice", "potato",
    "apple", "tomato", "egg", "olive oil", "cheese", "pork", "butter",
    "strawberry", "bean", "chicken", "beef", "lamb",
)


def build_demonstrations(
    lookup: pd.DataFrame,
    classified: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    details = classified[classified["product_level"] == "detail"]
    representative_rows = []
    for family in RANKING_FAMILIES:
        group = details[details["canonical_name"] == family].copy()
        if group.empty:
            continue
        median = group["eu_reference_price_eur_kg"].median()
        representative_rows.append(
            group.iloc[
                (group["eu_reference_price_eur_kg"] - median).abs().argsort().iloc[0]
            ]
        )
    ranking = pd.DataFrame(representative_rows).sort_values(
        "eu_reference_price_eur_kg"
    )
    ranking = ranking[
        [
            "canonical_name",
            "product_detail",
            "eu_reference_price_eur_kg",
            "global_cost_percentile",
            "global_cost_tier",
            "price_evidence_confidence",
        ]
    ].reset_index(drop=True)

    detail_rows = []
    summary_rows = []
    indexed = lookup.set_index(["ingredient_id", "target_country"])
    tier_lookup = details.set_index("source_ingredient_id")["global_cost_tier"]
    for recipe_name, recipe in RECIPES.items():
        servings = int(recipe["servings"])
        for country in TARGET_COUNTRIES:
            total = 0.0
            for item_id, weight_kg in recipe["ingredients"].items():
                if (item_id, country) not in indexed.index:
                    raise ValueError(f"Demonstration ingredient is unsupported: {(item_id, country)}")
                row = indexed.loc[(item_id, country)]
                ingredient_cost = float(weight_kg) * float(row["estimated_eur_per_kg"])
                total += ingredient_cost
                detail_rows.append(
                    {
                        "recipe": recipe_name,
                        "target_country": country,
                        "ingredient_id": item_id,
                        "ingredient": " ".join(
                            part for part in (row["ingredient_name"], row["ingredient_detail"]) if part
                        ),
                        "weight_kg": weight_kg,
                        "estimated_eur_per_kg": row["estimated_eur_per_kg"],
                        "ingredient_cost_eur": ingredient_cost,
                        "market_stage": row["market_stage"],
                        "confidence": row["confidence"],
                        "global_cost_tier": tier_lookup.get(item_id),
                    }
                )
            summary_rows.append(
                {
                    "recipe": recipe_name,
                    "target_country": country,
                    "total_recipe_cost_eur": total,
                    "servings": servings,
                    "estimated_cost_per_serving_eur": total / servings,
                }
            )
    return ranking, pd.DataFrame(detail_rows), pd.DataFrame(summary_rows)


def main() -> None:
    lookup = pd.read_csv(PROCESSED_DIR / "ingredient_price_lookup.csv", keep_default_na=False)
    classified = pd.read_csv(
        PROCESSED_DIR / "ingredient_prices_classified.csv", keep_default_na=False
    )
    ranking, details, summaries = build_demonstrations(lookup, classified)
    write_table(ranking, PROCESSED_DIR / "ingredient_ranking_demo.csv", parquet=False)
    write_table(details, PROCESSED_DIR / "recipe_cost_demo_details.csv", parquet=False)
    write_table(summaries, PROCESSED_DIR / "recipe_cost_demo_summary.csv", parquet=False)
    print(f"Wrote {len(summaries)} recipe/country demonstrations")


if __name__ == "__main__":
    main()
