import re

from scripts.neo4j import enrich_fato_foodon, tag_allergens
from recipe_wrangler.repositories.neo4j_recipes import _allergen_keyword_regex
from recipe_wrangler.utils.food_ontology import (
    ALLERGEN_ONTOLOGY_MAPPINGS,
    CONSUMER_GROUP_IRIS,
    FATO_ALLERGEN_DECLARATION_CLASS_IRI,
    aggregate_recipe_suitability,
)


def test_internal_allergens_map_to_foodon_label_claims() -> None:
    assert (
        ALLERGEN_ONTOLOGY_MAPPINGS["milk"].foodon_label_claim_id
        == "FOODON_03510220"
    )
    assert (
        ALLERGEN_ONTOLOGY_MAPPINGS["wheat"].foodon_label_claim_id
        == ALLERGEN_ONTOLOGY_MAPPINGS["gluten"].foodon_label_claim_id
    )
    assert set(CONSUMER_GROUP_IRIS) == {
        "coeliac", "vegan", "vegetarian", "halal", "kosher", "infant", "elderly"
    }
    assert FATO_ALLERGEN_DECLARATION_CLASS_IRI.endswith(
        "/AllergenDeclaration"
    )


def test_recipe_is_not_suitable_if_one_ingredient_is_not_suitable() -> None:
    evidence = [
        {
            "ingredient_key": "tomato",
            "group": "vegan",
            "status": "suitable",
        },
        {
            "ingredient_key": "whey",
            "group": "vegan",
            "status": "not_suitable",
        },
    ]

    result = aggregate_recipe_suitability(
        evidence,
        ingredient_count=2,
        groups=["vegan"],
    )

    assert result == {"vegan": "not_suitable"}


def test_recipe_is_suitable_only_with_complete_ingredient_coverage() -> None:
    complete = [
        {
            "ingredient_key": "tomato",
            "group": "vegan",
            "status": "suitable",
        },
        {
            "ingredient_key": "lentil",
            "group": "vegan",
            "status": "suitable",
        },
    ]

    assert aggregate_recipe_suitability(
        complete,
        ingredient_count=2,
        groups=["vegan"],
    ) == {"vegan": "suitable"}
    assert aggregate_recipe_suitability(
        complete[:1],
        ingredient_count=2,
        groups=["vegan"],
    ) == {"vegan": "unknown"}


def test_missing_suitability_evidence_is_unknown() -> None:
    assert aggregate_recipe_suitability(
        [],
        ingredient_count=3,
    ) == {
        "coeliac": "unknown",
        "vegan": "unknown",
        "vegetarian": "unknown",
        "halal": "unknown",
        "kosher": "unknown",
        "infant": "unknown",
        "elderly": "unknown",
    }


def test_explicit_whey_is_not_treated_as_a_plant_milk_alternative() -> None:
    assert not any(
        re.search(pattern, "whey powder")
        for pattern in tag_allergens.MILK_PLANT_EXCLUSION_REGEXES
    )
    assert any(
        re.search(pattern, "coconut milk")
        for pattern in tag_allergens.MILK_PLANT_EXCLUSION_REGEXES
    )


def test_runtime_allergen_edge_regex_uses_word_boundaries() -> None:
    assert re.match(_allergen_keyword_regex("egg"), "whole egg")
    assert not re.match(_allergen_keyword_regex("egg"), "eggplant")


def test_suitability_exclusions_protect_gluten_free_and_warning_text() -> None:
    assert any(
        re.search(pattern, "wheat and gluten-free baking mix")
        for pattern in enrich_fato_foodon.GLUTEN_SAFE_PATTERNS
    )
    assert any(
        re.search(pattern, "*note: usually gluten-free; check the label")
        for pattern in enrich_fato_foodon.POSITIVE_EVIDENCE_EXCLUSIONS
    )
