from recipe_wrangler.repositories.neo4j_recipes import (
    detect_allergen_evidence_from_names,
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


def test_satay_ingredients_trigger_peanut() -> None:
    # Satay/sate sauce is peanut-based but recipes routinely name the dish
    # without ever listing "peanut", which let a nut-allergy profile through.
    assert "peanut" in detect_allergens_from_names(["satay sauce"])
    assert "peanut" in detect_allergens_from_names(["sate paste"])
    assert "peanut" in detect_allergens_from_names(["saté sauce"])


def test_satay_keywords_do_not_overmatch() -> None:
    for benign in ["satsuma", "desiccated coconut", "tomato paste"]:
        assert "peanut" not in detect_allergens_from_names([benign]), benign


def test_compound_sauces_and_pastes_trigger_their_hidden_allergens() -> None:
    cases = {
        "mole sauce": "peanut",
        "mole paste": "peanut",
        "romesco sauce": "tree_nut",
        "hazelnut praline": "tree_nut",
        "worcestershire sauce": "fish",
        "worcester sauce": "fish",
        "caesar dressing": "fish",
    }
    for ingredient, allergen in cases.items():
        assert allergen in detect_allergens_from_names([ingredient]), ingredient


def test_compound_keywords_keep_word_boundaries() -> None:
    for benign in ["mole poblano pepper", "romanesco", "Caesarea"]:
        assert detect_allergens_from_names([benign]) == [], benign


def test_unpersisted_analysis_evidence_preserves_ingredient_pairing() -> None:
    evidence = detect_allergen_evidence_from_names(
        ["Caesar dressing", "fresh tomatoes"]
    )
    assert evidence == [
        {
            "allergen": "fish",
            "ingredient": "Caesar dressing",
            "ingredient_id": "",
            "declaration_id": "",
            "presence": "contains",
            "evidence_status": "inferred",
            "sources": ["keyword"],
            "foodon_ids": [],
            "keyword_matches": ["caesar dressing"],
            "classification_version": "fato-foodon-v1",
        }
    ]


def test_molluscs_are_not_classified_as_crustaceans() -> None:
    assert detect_allergens_from_names(["scallops", "oysters"]) == ["molluscs"]
    assert detect_allergens_from_names(["shrimp", "crab"]) == [
        "crustacean_shellfish"
    ]


def test_pescatarian_safety_is_not_inferred_from_allergens() -> None:
    # Seafood is allowed for pescatarians; meat/poultry composition determines
    # this tag, not the presence or absence of a fish-family allergen.
    assert "pescatarian_safe" not in infer_diet_tags(set())
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
