from recipe_wrangler.repositories.neo4j_recipes import (
    detect_allergens_from_names,
    infer_diet_tags,
)


def test_detects_missing_eu_allergen_groups() -> None:
    ingredients = [
        "celeriac mash",
        "Dijon mustard",
        "sodium metabisulphite",
        "lupini beans",
        "steamed mussels",
    ]

    assert detect_allergens_from_names(ingredients) == [
        "celery",
        "lupin",
        "molluscs",
        "mustard",
        "sulphites",
    ]


def test_molluscs_are_not_classified_as_crustaceans() -> None:
    assert detect_allergens_from_names(["scallops", "oysters"]) == ["molluscs"]
    assert detect_allergens_from_names(["shrimp", "crab"]) == [
        "crustacean_shellfish"
    ]


def test_molluscs_prevent_pescatarian_safe_tag() -> None:
    assert "pescatarian_safe" not in infer_diet_tags({"molluscs"})


def test_plant_dairy_alternatives_do_not_trigger_milk() -> None:
    ingredients = [
        "coconut milk",
        "reduced-fat coconut milk",
        "soy milk",
        "oat cream",
        "almond yogurt",
        "peanut butter",
        "butter beans",
        "butternut squash",
        "vegan parmesan cheese",
        "cream substitute",
    ]
    assert "milk" not in detect_allergens_from_names(ingredients)


def test_genuine_dairy_still_triggers_milk() -> None:
    ingredients = [
        "whole milk",
        "cheddar cheese",
        "unsalted butter",
        "double cream",
        "Greek yogurt",
    ]
    assert "milk" in detect_allergens_from_names(ingredients)


def test_gluten_free_flours_and_tamari_are_not_flagged() -> None:
    ingredients = [
        "gluten-free flour",
        "buckwheat flour",
        "rice flour",
        "tapioca flour",
        "potato flour",
        "almond flour",
        "coconut flour",
        "besan flour",
        "tamari soy sauce",
        "fresh ginger",
        "white wine vinegar",
    ]
    allergens = detect_allergens_from_names(ingredients)
    assert "gluten" not in allergens
    assert "wheat" not in allergens


def test_genuine_gluten_sources_still_trigger() -> None:
    ingredients = [
        "wheat flour",
        "rye bread",
        "barley",
        "spelt flour",
        "ordinary pasta",
    ]
    allergens = detect_allergens_from_names(ingredients)
    assert "gluten" in allergens
    assert "wheat" in allergens


def test_lossy_gluten_free_canonical_names_are_not_flagged() -> None:
    ingredients = [
        "gluten baking flour",
        "gluten self raising flour",
        "gluten soy sauce",
        "gluten bread",
        "gluten pasta",
        "rice noodle",
        "pulse pasta",
    ]
    allergens = detect_allergens_from_names(ingredients)
    assert "gluten" not in allergens
    assert "wheat" not in allergens


def test_allergen_keywords_use_word_boundaries() -> None:
    ingredients = ["eggplant", "butternut squash", "butter beans"]
    allergens = detect_allergens_from_names(ingredients)
    assert "egg" not in allergens
    assert "milk" not in allergens
