"""Release selection must preserve food identity and one method per recipe."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from recipe_wrangler.repositories import sustainability_references as refs


@pytest.fixture
def garlic():
    return {
        "name": "Garlic, fresh", "source": "AGRIBALYSE", "version": "3.2-food-AOUT25",
        "method": "AGRIBALYSE_EF3.1", "market": "FR", "basis": "kg_consumed_product",
        "boundary": "consumer_product_including_intermediate_losses_and_declared_preparation",
        "scenario": {"preparation": "Pas de préparation"},
        "reference_id": "fixture-garlic", "source_record_id": "11000",
        "source_url": "https://doi.org/10.57745/XTENSJ", "source_sha256": "fixture-hash",
        "metrics": {"climate_change": {"value": 0.383, "unit": "kg CO2 eq/kg de produit"}},
    }


def test_source_and_version_are_pinned(garlic):
    assert refs.eligible(garlic)
    for key, value in [("source", "CONCITO"), ("version", "unknown"), ("basis", "kg_or_litre_unresolved")]:
        assert not refs.eligible({**garlic, key: value})


@pytest.mark.parametrize("name", ["garlic powder", "garlic salt", "garlic bread", "garlic in oil", "cooked garlic"])
def test_no_form_or_compound_collapsing(garlic, name):
    assert refs.select_reference(name, [garlic]) is None


def test_ambiguity_abstains_instead_of_selecting_lower_factor(garlic):
    other = deepcopy(garlic)
    other["reference_id"] = "different-scenario"
    other["metrics"]["climate_change"]["value"] = 0.001
    assert refs.select_reference("garlic cloves", [garlic, other]) is None


def test_recipe_policy_cannot_mix_new_and_legacy_factors(garlic):
    selected = {"garlic": garlic}
    assert refs.complete_recipe_selection(["garlic"], [10], selected)
    assert not refs.complete_recipe_selection(["garlic", "unknown"], [10, 100], selected)
    assert not refs.complete_recipe_selection(["garlic", "unknown"], [10], selected)
    assert refs.complete_recipe_selection(["garlic", "unknown"], [10, 0], selected)


def test_cooking_and_unknown_units_cannot_enter_active_policy(garlic):
    other = deepcopy(garlic)
    other["scenario"]["preparation"] = "Four"
    assert not refs.eligible(other)
    other = deepcopy(garlic)
    other["metrics"]["climate_change"]["unit"] = "kg CO2e/litre"
    assert not refs.eligible(other)


def test_missing_catalogue_does_not_break_existing_calculation(monkeypatch):
    monkeypatch.setattr(refs.requests, "post", Mock(side_effect=refs.requests.ConnectionError()))
    assert refs.lookup_references(["garlic"])[1] == "reference_catalogue_unavailable"


def test_truncated_reference_query_cannot_select_first_candidate(monkeypatch):
    response = Mock()
    response.json.return_value = {"hits": {"total": {"value": 101, "relation": "eq"}, "hits": []}}
    monkeypatch.setattr(refs.requests, "post", Mock(return_value=response))
    assert refs.lookup_references(["garlic"])[1] == "reference_query_incomplete"


def test_tool_uses_reviewed_agribalyse_mapping():
    from recipe_wrangler.tools import sustainability_calculator as calculator
    result = calculator.sustainability_tool_vector.invoke({"title": "mince", "ingredient_names": ["minced beef", "chicken fillets"], "weights": [450, 450], "serves": 4})
    assert result["source_sustainability"] == "AGRIBALYSE 3.2"
    assert result["sustainability_status"] == "complete_estimate"
    assert result["sustainability_policy"] == calculator.POLICY_VERSION
    assert result["details"][0]["cf_val"] > 5 * result["details"][1]["cf_val"]
    assert result["total_sustainability_per_serving"] == pytest.approx(result["total_sustainability"] / 4)


def test_unmapped_ingredient_makes_the_total_partial():
    from recipe_wrangler.tools import sustainability_calculator as calculator
    result = calculator.sustainability_tool_vector.invoke({"title": "partial", "ingredient_names": ["garlic", "zzzz unknown thing"], "weights": [10, 100]})
    assert result["sustainability_status"] == "partial"
    assert result["details"][1]["cf_val"] is None and result["details"][1]["contribution"] == 0.0


@pytest.mark.parametrize("weights", [[1, 2], [-1], [float("nan")], [float("inf")]])
def test_invalid_weights_rejected_before_any_lookup(weights):
    from recipe_wrangler.tools import sustainability_calculator as calculator
    with pytest.raises(ValueError):
        calculator.sustainability_tool_vector.invoke({"title": "bad", "ingredient_names": ["garlic"], "weights": weights})
