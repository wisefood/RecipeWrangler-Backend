from scripts.tag_gluten_free_options import (
    find_gluten_free_option_evidence,
    generate_predictions,
)


def test_detects_explicit_gluten_free_adaptation() -> None:
    recipe = {
        "recipe_id": "1",
        "title": "Pasta",
        "description": "",
        "notes": ["Make it gluten free: use gluten-free pasta instead."],
        "categories": [],
    }

    assert find_gluten_free_option_evidence(recipe)
    assert generate_predictions([recipe])[0]["generated_tag"] == (
        "gluten_free_option"
    )


def test_detects_recipe_that_can_be_made_gluten_free() -> None:
    recipe = {
        "description": "This slice can be made gluten free.",
        "notes": [],
    }

    assert find_gluten_free_option_evidence(recipe)


def test_does_not_use_reference_badge_as_prediction_input() -> None:
    recipe = {
        "description": "A standard wheat pasta recipe.",
        "notes": [],
        "categories": ["Gluten-free option"],
    }

    assert find_gluten_free_option_evidence(recipe) is None


def test_exact_gluten_free_statement_is_not_automatically_an_option() -> None:
    recipe = {
        "description": "This recipe is gluten free.",
        "notes": [],
    }

    assert find_gluten_free_option_evidence(recipe) is None
