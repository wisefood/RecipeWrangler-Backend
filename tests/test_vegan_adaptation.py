from unittest.mock import patch

from recipe_wrangler.services.adaptation.llm_judge import _build_prompt
from recipe_wrangler.services.adaptation.schemas import SuggestionsRequest
from recipe_wrangler.services.adaptation.service import (
    _consumer_candidate_pool,
    _functional_alternative_rank,
    _is_explicit_functional_alternative,
    _is_simple_role_variant,
    _nutrition_match_preserves_consumer_identity,
    generate_suggestions,
)


def _context(status: str = "not_suitable") -> dict:
    return {
        "recipe_id": "recipe-1",
        "title": "Bean Toast",
        "status": status,
        "blocking_ingredients": ["cheese"] if status == "not_suitable" else [],
        "unknown_ingredients": ["bread"] if status == "not_suitable" else [],
        "ingredients": [
            {
                "name": "cheese",
                "status": "not_suitable",
                "reason_codes": ["dairy"],
            },
            {
                "name": "bread",
                "status": "unknown",
                "reason_codes": ["insufficient_evidence"],
            },
        ],
    }


def _nutrition_detail(
    ingredient: str,
    *,
    weight_g: float = 25.0,
    matched_name: str | None = None,
) -> dict:
    return {
        "ingredient": ingredient,
        "graph_name": ingredient,
        "matched_nutritional_ingredient": matched_name or ingredient,
        "canonical_food_id": f"id:{ingredient}",
        "source_nutrition": "EU Composite",
        "match_confidence": "strong",
        "similarity": 0.9,
        "weight_g": weight_g,
        "energy_kcal_per_100g": 200.0,
        "carbs_per_100g": 10.0,
        "fat_per_100g": 12.0,
        "sugars_per_100g": 2.0,
        "saturated_fat_per_100g": 5.0,
        "sodium_per_100g_mg": 300.0,
        "fibre_per_100g": 1.0,
        "protein_per_100g": 8.0,
        "energy_kcal": 50.0,
        "carbs_g": 2.5,
        "fat_g": 3.0,
        "sugar_g": 0.5,
        "saturated_fat_g": 1.25,
        "sodium_mg": 75.0,
        "fibre_g": 0.25,
        "protein_g": 2.0,
    }


def test_vegan_mode_is_accepted_by_request_schema() -> None:
    request = SuggestionsRequest(region="IE", mode="vegan", max_swaps=2)
    assert request.mode == "vegan"


def test_vegetarian_mode_is_accepted_by_request_schema() -> None:
    request = SuggestionsRequest(
        region="IE",
        mode="vegetarian",
        max_swaps=2,
    )
    assert request.mode == "vegetarian"


@patch(
    "recipe_wrangler.services.adaptation.service."
    "filter_suitable_ingredients"
)
@patch(
    "recipe_wrangler.services.adaptation.service."
    "find_substitute_candidates"
)
@patch(
    "recipe_wrangler.services.adaptation.service."
    "_semantic_consumer_candidates"
)
def test_candidate_pool_excludes_unknown_and_existing_candidates(
    semantic,
    graph,
    suitability,
) -> None:
    semantic.return_value = [
        {
            "name": "vegan cheese",
            "source": "elastic",
            "category_distance": "high",
            "retrieval_rank": 1,
            "retrieval_score": 0.03,
        },
        {
            "name": "cashew paste",
            "source": "elastic",
            "category_distance": "high",
            "retrieval_rank": 2,
            "retrieval_score": 0.02,
        },
        {
            "name": "bread",
            "source": "elastic",
            "category_distance": "high",
            "retrieval_rank": 3,
            "retrieval_score": 0.01,
        },
    ]
    graph.return_value = []
    suitability.return_value = [
        {
            "name": "vegan cheese",
            "suitability_status": "suitable",
            "suitability_reasons": ["vegan"],
            "classification_version": "vegan-vegetarian-v1",
        }
    ]

    result = _consumer_candidate_pool(
        "cheese",
        "vegan",
        {"bread"},
        "Bean Toast",
    )

    assert [item["name"] for item in result] == ["vegan cheese"]
    suitability.assert_called_once_with(
        ["vegan cheese", "cashew paste"],
        "vegan",
    )
    semantic.assert_called_once_with("cheese", "vegan", "Bean Toast")


def test_functional_gate_is_global_and_rejects_ambiguous_choices() -> None:
    assert _is_explicit_functional_alternative(
        "cheese",
        "vegan cheddar cheese",
    )
    assert _is_explicit_functional_alternative(
        "egg",
        "vegan egg replacer powder",
    )
    assert _is_explicit_functional_alternative(
        "milk",
        "plant-based milk",
    )
    assert not _is_explicit_functional_alternative(
        "milk",
        "reduced-fat milk or plant-based alternative",
    )
    assert not _is_explicit_functional_alternative(
        "butter",
        "almond butter",
    )
    assert _is_simple_role_variant("milk", "almond milk")
    assert _is_simple_role_variant("milk", "unsweetened oat milk")
    assert not _is_simple_role_variant(
        "milk",
        "milk or plant-based alternative",
    )
    assert not _is_explicit_functional_alternative(
        "egg",
        "vegan cheese",
    )
    assert not _is_explicit_functional_alternative(
        "milk",
        "¾ cups plant-based milk",
    )
    assert _functional_alternative_rank(
        "cheese",
        "vegan cheese",
    ) < _functional_alternative_rank(
        "cheese",
        "vegan cheese alternative, grated",
    )


def test_nutrition_identity_rejects_dairy_match_for_vegan_cheese() -> None:
    assert _nutrition_match_preserves_consumer_identity(
        "vegan cheese",
        "cheese",
        _nutrition_detail(
            "vegan cheese",
            matched_name="Plant-based cheese, prepacked",
        ),
        "vegan",
    )
    assert not _nutrition_match_preserves_consumer_identity(
        "vegan cheese",
        "cheese",
        _nutrition_detail(
            "vegan cheese",
            matched_name="cream cheese",
        ),
        "vegan",
    )


def test_nutrition_identity_rejects_meat_match_for_vegetarian_substitute() -> None:
    candidate = _nutrition_detail(
        "vegetarian chicken",
        matched_name="Plant-based chicken-style pieces",
    )
    assert _nutrition_match_preserves_consumer_identity(
        "vegetarian chicken",
        "chicken",
        candidate,
        "vegetarian",
    )
    candidate["matched_nutritional_ingredient"] = "Chicken breast"
    assert not _nutrition_match_preserves_consumer_identity(
        "vegetarian chicken",
        "chicken",
        candidate,
        "vegetarian",
    )


@patch(
    "recipe_wrangler.services.adaptation.service."
    "get_ingredient_allergens",
    return_value=[],
)
@patch(
    "recipe_wrangler.services.adaptation.service."
    "_consumer_candidate_pool"
)
@patch(
    "recipe_wrangler.services.adaptation.service."
    "fetch_recipe_consumer_context"
)
def test_vegan_suggestion_reports_blockers_and_unknowns(
    fetch_context,
    candidate_pool,
    _allergens,
) -> None:
    fetch_context.return_value = _context()
    candidate_pool.return_value = [
        {
            "name": "vegan cheese",
            "source": "elastic",
            "category_distance": "high",
            "suitability_status": "suitable",
            "suitability_reasons": ["vegan"],
            "classification_version": "vegan-vegetarian-v1",
        }
    ]

    profile_row = {
        "recipe_id": "recipe-1",
        "title": "Bean Toast",
        "nutrition_source": "irish",
        "nutri_score_breakdown": {
            "nutri_score": "Nutriscore_C",
            "positive_points": {"items": {}},
        },
        "nutrition_profiling_debug": {
            "profiling_quality": {"serves": 2}
        },
    }
    cheese_detail = _nutrition_detail("cheese")
    vegan_cheese_detail = _nutrition_detail(
        "vegan cheese",
        matched_name="Plant-based cheese, prepacked",
    )
    graph_recipe = {
        "recipe_id": "recipe-1",
        "title": "Bean Toast",
        "source": "test",
        "ingredients": [
            {"name": "cheese", "measurement": "25 g"},
            {"name": "bread", "measurement": "1 slice"},
        ],
        "instructions": ["Top bread with cheese."],
    }
    with (
        patch(
            "recipe_wrangler.services.adaptation.service._load_profile",
            return_value=profile_row,
        ),
        patch(
            "recipe_wrangler.services.adaptation.service."
            "_recompute_ingredient_details",
            return_value=[cheese_detail],
        ),
        patch(
            "recipe_wrangler.services.adaptation.service."
            "fetch_recipe_info_by_id",
            return_value=graph_recipe,
        ),
        patch(
            "recipe_wrangler.services.adaptation.service."
            "_fetch_candidate_profile",
            return_value=vegan_cheese_detail,
        ),
    ):
        result = generate_suggestions(
            "recipe-1",
            "IE",
            mode="vegan",
            max_swaps=1,
        )

    assert result["status"] == "ok"
    assert result["current_consumer_status"] == "not_suitable"
    assert result["blocking_ingredients"] == ["cheese"]
    assert result["unknown_ingredients"] == ["bread"]
    suggestion = result["suggestions"][0]
    assert suggestion["substitute_name"] == "vegan cheese"
    assert suggestion["suitability_status"] == "suitable"
    assert suggestion["simulated_consumer_status"] == "unknown"
    assert suggestion["nutrition_match"][
        "matched_nutritional_ingredient"
    ] == "Plant-based cheese, prepacked"
    assert suggestion["adapted_recipe"]["ingredients"][0]["name"] == (
        "vegan cheese"
    )
    assert "fat_g" in suggestion["adapted_recipe"]["nutrition"][
        "total_nutrients"
    ]
    assert "cannot yet be labelled suitable" in suggestion["explanation"][
        "warning"
    ]


@patch(
    "recipe_wrangler.services.adaptation.service."
    "fetch_recipe_consumer_context"
)
def test_already_vegan_recipe_returns_no_suggestions(fetch_context) -> None:
    fetch_context.return_value = _context(status="suitable")

    result = generate_suggestions("recipe-1", "IE", mode="vegan")

    assert result["status"] == "already_optimal"
    assert result["suggestions"] == []


@patch(
    "recipe_wrangler.services.adaptation.service."
    "fetch_recipe_consumer_context"
)
def test_already_vegetarian_recipe_uses_consumer_mode(fetch_context) -> None:
    fetch_context.return_value = _context(status="suitable")

    result = generate_suggestions(
        "recipe-1",
        "IE",
        mode="vegetarian",
    )

    assert result["mode"] == "vegetarian"
    assert result["target_consumer_group"] == "vegetarian"
    assert result["status"] == "already_optimal"


def test_llm_prompt_knows_candidates_are_already_vegan_filtered() -> None:
    prompt = _build_prompt(
        recipe_title="Bean Toast",
        recipe_ingredients=[{"name": "cheese"}],
        target_nutrient_label=None,
        target_points=None,
        offending_ingredient="cheese",
        offending_pct=0,
        candidates=[
            {
                "rank": 1,
                "substitute_name": "vegan cheese",
                "source": "elastic",
                "introduces_allergen": False,
            }
        ],
        mode="vegan",
    )

    assert "explicitly vegan-suitable" in prompt
    assert "BLOCKING INGREDIENT: 'cheese'" in prompt
