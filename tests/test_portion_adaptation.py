from unittest.mock import patch

from recipe_wrangler.services.adaptation import service


def _row():
    return {
        "title": "Salmon rice",
        "nutri_score_breakdown": {"nutri_score": "Nutriscore_B"},
    }


def _details():
    return [
        {"ingredient": "salmon fillet", "graph_name": "salmon fillet", "weight_g": 200},
        {"ingredient": "rice", "graph_name": "rice", "weight_g": 150},
    ]


def test_portion_mode_scales_profiled_weights_without_rewriting_instructions():
    with (
        patch.object(service, "_load_profile", return_value=_row()),
        patch.object(service, "_recompute_ingredient_details", return_value=_details()),
        patch.object(service, "_serves_from_row", return_value=2),
    ):
        result = service.generate_suggestions(
            "r1", "IE", mode="portion", target_serves=4
        )

    suggestion = result["suggestions"][0]
    assert suggestion["action"] == "scale"
    assert suggestion["scale_factor"] == 2
    assert suggestion["adapted_recipe"]["ingredients"][0]["weight_g"] == 400
