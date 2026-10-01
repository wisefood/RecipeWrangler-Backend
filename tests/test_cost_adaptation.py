from unittest.mock import patch

from recipe_wrangler.services.adaptation import service


def _row():
    return {
        "title": "Beef stew",
        "nutri_score_breakdown": {"nutri_score": "Nutriscore_B"},
    }


def _details():
    return [
        {"ingredient": "beef", "graph_name": "beef", "weight_g": 300.0},
        {"ingredient": "salt", "graph_name": "salt", "weight_g": 5.0},
    ]


def _fake_resolve(name: str, country: str) -> dict:
    prices = {
        "beef": {"economic_reference_price_eur_kg": 10.0, "food_category": "meat"},
        "pork": {"economic_reference_price_eur_kg": 6.0, "food_category": "meat"},
        "chicken liver": {"economic_reference_price_eur_kg": 2.0, "food_category": "offal"},
    }
    hit = prices.get(name.strip().lower())
    if not hit:
        return {"match_status": "unmatched"}
    return {"match_status": "matched", **hit}


class _FakeCatalogue:
    def resolve(self, name: str, country: str) -> dict:
        return _fake_resolve(name, country)


def _fake_substitutes(graph_name: str) -> list[dict]:
    if graph_name == "beef":
        return [
            {"name": "pork", "source": "miskg", "category_distance": "low"},
            {"name": "chicken liver", "source": "miskg", "category_distance": "low"},
        ]
    return []


def test_cost_mode_prefers_cheaper_same_category_substitute():
    with (
        patch.object(service, "_load_profile", return_value=_row()),
        patch.object(service, "_recompute_ingredient_details", return_value=_details()),
        patch.object(service, "_serves_from_row", return_value=2.0),
        patch.object(service, "_authoritative_grade", return_value="B"),
        patch.object(service, "load_cost_catalogue", return_value=_FakeCatalogue()),
        patch.object(service, "find_substitute_candidates", side_effect=_fake_substitutes),
        patch.object(service, "_select_graph_name", side_effect=lambda raw, hints: raw),
        patch.object(service, "get_ingredient_allergens", return_value=[]),
        patch.object(service, "flavor_similarity", return_value=0.2),
        patch.object(service, "_fetch_candidate_profile", return_value=None),
        patch.object(service, "_food_class_compatible", return_value=True),
    ):
        result = service.generate_suggestions("r1", "IE", mode="cost")

    assert result.get("status", "ok") == "ok"
    assert result["mode"] == "cost"
    assert result["offending_ingredient"] == "beef"
    assert result["unpriced_ingredients"] == ["salt"]
    assert result["current_cost_total_eur"] == 3.0  # only beef: 0.3kg * 10 eur/kg

    suggestion = result["suggestions"][0]
    assert suggestion["substitute_name"] == "pork"
    assert suggestion["food_category"] == "meat"
    assert suggestion["cost_reduction_pct"] == 0.4
    assert suggestion["candidate_price_eur_kg"] == 6.0


def test_cost_mode_rejects_swap_below_minimum_saving():
    with (
        patch.object(service, "_load_profile", return_value=_row()),
        patch.object(
            service, "_recompute_ingredient_details",
            return_value=[{"ingredient": "beef", "graph_name": "beef", "weight_g": 300.0}],
        ),
        patch.object(service, "_serves_from_row", return_value=2.0),
        patch.object(service, "_authoritative_grade", return_value="B"),
        patch.object(service, "load_cost_catalogue", return_value=_FakeCatalogue()),
        patch.object(
            service, "find_substitute_candidates",
            side_effect=lambda g: [{"name": "beef roast", "source": "miskg", "category_distance": "low"}],
        ),
        patch.object(service, "_select_graph_name", side_effect=lambda raw, hints: raw),
        patch.object(service, "get_ingredient_allergens", return_value=[]),
        patch.object(service, "_fetch_candidate_profile", return_value=None),
        patch.object(service, "_food_class_compatible", return_value=True),
    ):
        # "beef roast" isn't in the fake price table -> unmatched -> no suggestions.
        result = service.generate_suggestions("r1", "IE", mode="cost")

    assert result["status"] == "no_suggestions"
    assert result["suggestions"] == []
