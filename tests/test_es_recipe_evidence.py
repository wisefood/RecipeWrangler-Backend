from recipe_wrangler.utils.es_recipe_evidence import (
    normalize_allergen_evidence,
    normalize_consumer_suitability,
    suitable_groups,
)


def test_normalizes_allergen_evidence_without_losing_pairing() -> None:
    evidence = normalize_allergen_evidence(
        [
            {
                "allergen": "Milk",
                "ingredient": "Whey Powder",
                "ingredient_id": "ingredient-1",
                "declaration_id": "declaration-1",
                "presence": "contains",
                "evidence_status": "inferred",
                "sources": ["keyword"],
                "foodon_ids": [],
                "keyword_matches": ["Whey"],
                "classification_version": "fato-foodon-v1",
            }
        ]
    )

    assert evidence == [
        {
            "allergen": "milk",
            "ingredient": "whey powder",
            "ingredient_id": "ingredient-1",
            "declaration_id": "declaration-1",
            "presence": "contains",
            "evidence_status": "inferred",
            "sources": ["keyword"],
            "foodon_ids": [],
            "keyword_matches": ["whey"],
            "classification_version": "fato-foodon-v1",
        }
    ]


def test_consumer_suitability_always_contains_both_groups() -> None:
    assessments = normalize_consumer_suitability(
        [
            {
                "group": "Vegetarian",
                "status": "suitable",
                "blocking_ingredients": [],
                "reason_codes": ["all_ingredients_suitable"],
                "sources": ["ingredient_suitability"],
            }
        ],
        classification_version="vegan-vegetarian-v1",
    )

    assert [item["group"] for item in assessments] == [
        "vegan",
        "vegetarian",
    ]
    assert assessments[0]["status"] == "unknown"
    assert assessments[1]["status"] == "suitable"
    assert suitable_groups(assessments) == ["vegetarian"]
