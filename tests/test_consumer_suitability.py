import re

from recipe_wrangler.utils.consumer_suitability import (
    DIETARY_ORIGINS,
    GROUP_RULES,
    SUITABILITY_CLASSIFICATION_VERSION,
    VEGAN_NAME_EXCLUSIONS,
    keyword_regex,
    positive_keyword_regex,
)


def test_rule_version_and_supported_source_categories() -> None:
    assert SUITABILITY_CLASSIFICATION_VERSION == "vegan-vegetarian-v10"
    origins = {str(item["name"]): item for item in DIETARY_ORIGINS}
    assert origins["dairy"]["vegan_status"] == "not_suitable"
    assert origins["dairy"]["vegetarian_status"] == "suitable"
    assert origins["egg"]["vegan_status"] == "not_suitable"
    assert origins["egg"]["vegetarian_status"] == "suitable"
    assert origins["bee_product"]["vegan_status"] == "not_suitable"
    assert origins["bee_product"]["vegetarian_status"] == "suitable"
    assert origins["animal_meat"]["vegetarian_status"] == "not_suitable"
    assert origins["plant"]["vegan_status"] == "suitable"


def test_animal_derived_standard_terms_are_covered() -> None:
    vegan_terms = set(GROUP_RULES["vegan"]["blocking_keywords"])
    vegetarian_terms = set(GROUP_RULES["vegetarian"]["blocking_keywords"])
    assert {"honey", "beeswax", "propolis", "colostrum", "lanolin"} <= vegan_terms
    assert not {"honey", "beeswax", "propolis", "colostrum", "lanolin"} & (
        vegetarian_terms
    )
    assert {
        "honey",
        "beeswax",
        "propolis",
        "colostrum",
        "lanolin",
    } <= set(GROUP_RULES["vegetarian"]["positive_keywords"])
    assert {"gelatin", "collagen", "animal rennet"} <= vegetarian_terms


def test_keyword_matching_uses_word_boundaries() -> None:
    egg = re.compile(keyword_regex("egg"))
    assert egg.match("whole egg")
    assert not egg.match("eggplant")


def test_vegan_plant_alternatives_are_excluded_from_negative_terms() -> None:
    assert any(re.match(pattern, "oat milk") for pattern in VEGAN_NAME_EXCLUSIONS)
    assert any(
        re.match(pattern, "vegan sausage") for pattern in VEGAN_NAME_EXCLUSIONS
    )
    assert any(
        re.match(pattern, "vegetable suet") for pattern in VEGAN_NAME_EXCLUSIONS
    )
    assert not any(
        re.match(pattern, "vegetarian cheese")
        for pattern in VEGAN_NAME_EXCLUSIONS
    )


def test_alternative_exceptions_do_not_hide_independent_animal_ingredients() -> None:
    from recipe_wrangler.utils.consumer_suitability import VEGETARIAN_NAME_EXCLUSIONS
    for patterns in [VEGAN_NAME_EXCLUSIONS,VEGETARIAN_NAME_EXCLUSIONS]:
        for name in ['vegan sausage with pork','oat milk and cream','butter beans with bacon',
                     'dairy-free cream with egg','butternut with lard']:
            assert not any(re.fullmatch(pattern,name) for pattern in patterns), name
    assert re.fullmatch(positive_keyword_regex('milk'),'milk')
    assert not re.fullmatch(positive_keyword_regex('milk'),'milk and unresolved sauce')
    assert re.fullmatch(positive_keyword_regex('vegan'),'vegan sausage')
    assert not re.fullmatch(positive_keyword_regex('vegan'),'vegan sausage with pork')
    assert not re.fullmatch(positive_keyword_regex('vegan'),'vegetarian cheese')
    assert re.fullmatch(positive_keyword_regex('milk'),'low fat milk')
    assert not re.fullmatch(positive_keyword_regex('milk'),'milk and mystery sauce')
