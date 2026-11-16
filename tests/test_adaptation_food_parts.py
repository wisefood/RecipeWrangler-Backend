"""General food-part discovery, evidence, text and complete-card regressions."""

from copy import deepcopy
import json
from unittest.mock import Mock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from test_adaptation_preview import inputs, model
from test_adaptation_instruction_editor import setup
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement_pipeline import flow
from test_adaptation_objective_approval import context
from test_adaptation_production_integration import profile
from recipe_wrangler.repositories import matching_store
from recipe_wrangler.services.adaptation import candidate_selection, contextual_assessment, enhancement_pipeline, instruction_editor
from recipe_wrangler.services.adaptation.comparison import compare_raw_part
from recipe_wrangler.services.adaptation.models import RawPartEdit
from recipe_wrangler.services.adaptation.models import RoleAssessment
from recipe_wrangler.services.adaptation.production_candidates import food_part_candidate_records, make_substitution_edit


FAMILIES = [
    ("chicken", "chicken", "thigh", "thighs", "breast", "breasts"),
    ("turkey", "turkey", "thigh", "thighs", "breast", "breasts"),
    ("duck", "duck", "wing tip", "wing tips", "breast fillet", "breast fillets"),
    ("beef", "cow", "shoulder", "shoulders", "loin", "loins"),
    ("pork", "pig", "shoulder", "shoulders", "loin", "loins"),
    ("lamb", "sheep", "shoulder", "shoulders", "loin", "loins"),
    ("broccoli", "broccoli", "stem", "stems", "floret", "florets"),
    ("celery", "celery", "leaf", "leaves", "stalk", "stalks"),
]


@pytest.fixture
def general_parts(flow, monkeypatch):
    monkeypatch.setattr(candidate_selection, "nutritional_variant_records", lambda *a: [])
    graph = Mock(return_value=([], []))
    monkeypatch.setattr(candidate_selection, "production_candidate_records", graph)

    def reply(payload, response_model, config, **kwargs):
        recipe = payload["context"]
        quotes = [{"kind": "ingredient", "reference_id": "ingredient:0", "quote": recipe["ingredients"][0]["original_text"]},
            {"kind": "instruction", "reference_id": "step:0", "quote": recipe["steps"][0]["text"]}]
        if response_model is RoleAssessment:
            return json.dumps({"status": "inferred", "roles": [{"role": "main_component", "reason": "Synthetic role", "evidence": quotes}], "uncertainties": []})
        return json.dumps({"assessments": [{"candidate_id": item["candidate_id"], "verdict": "suitable",
            "reason": "Synthetic suitability", "role_fit": [{"role": "main_component", "status": "preserved"}],
            "evidence": quotes, "required_changes": [], "uncertainties": []} for item in payload["candidates"]]})
    monkeypatch.setattr(contextual_assessment, "_request", reply)

    def configure(family, plural=False, estimated=False, counted=False):
        identity, biological, source_part, source_plural, target_part, target_plural = family
        snapshot, targets = flow[0]
        original = snapshot.ingredients[0]
        plant = identity in {"broccoli", "celery"}
        for props, part in [(original.observed_properties, source_part), (original.composition.properties, source_part)] + [
                (target.properties, target_part) for target in targets]:
            for key, value in {"base_identity": identity, "biological_source": biological, "food_part": part}.items():
                props[key].value = value
            if plant:
                props.pop("skin_status", None)
                props.pop("bone_status", None)
                props["nutri_score_fvl_member"] = props["base_identity"].model_copy(update={"value": True})
        name = identity + " " + (source_plural if plural else source_part)
        original.composition.provenance["candidate"]["document"] = identity + " " + source_part
        for target in targets:
            target.provenance["candidate"]["document"] = identity + " " + target_part
        original.name = name
        original.original_text = ("8 " if counted else "200 g ") + name
        if estimated:
            snapshot.input_policy = "production_estimate"
            original.quantity.basis = "selected_state_edible"
            original.quantity.evidence.status = "proxy"
            original.quantity.evidence.method = "saved_production_input_estimate"
        score = context(snapshot)
        score.basis = "selected_state_input_estimate" if estimated else "raw_edible_input"
        score.fruit_veg_legume_percent = 100. if plant else 0.
        steps = ["Cook the " + name + " gently.", "Serve immediately."]
        def run(**kwargs):
            return enhancement_pipeline.enhance_recipe(snapshot, title=identity.capitalize() + " dish", instructions=steps,
                score_context=score, goal_nutrients=["energy"], nutri_score_breakdown={"negative_points": {
                    "items": {"energy": {"points": 7, "value_per_100g": 2510.4}}}}, **kwargs)
        return snapshot, targets, run, steps, graph
    return configure


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("plural", [False, True])
@pytest.mark.parametrize("estimated", [False, True])
def test_any_supported_source_part_reaches_a_complete_card(general_parts, family, plural, estimated):
    snapshot, _, run, steps, graph = general_parts(family, plural, estimated, counted=plural)
    before = snapshot.model_dump()
    result = run()
    assert result["status"] == "draft_requires_review", result["candidates"]
    card = result["adapted_recipe"]
    identity, _, _, _, part, parts = family
    rendered = identity + " " + (parts if plural else part)
    assert card["ingredients"][0]["name"] == rendered
    assert card["instructions"] == ["Cook the " + rendered + " gently.", steps[1]]
    assert card["ingredients"][0]["original_text"] == ("approximately " if plural and estimated else "") + "200 g " + rendered
    assert card["ingredients"][0]["quantity"]["weight_g"] == 200
    assert card["nutrition"]["per_serving"]["energy_kcal"] == 100
    assert card["serves"] == snapshot.serves
    assert result["candidates"][0]["mechanism"] == "same_identity_food_part_variant"
    assert "raw_chicken_thigh_to_breast" not in result["capabilities"]["executable"]
    assert len(result["model_calls"]) == 3
    assert not result["saved"] and not result["recipe_ready"]
    assert snapshot.model_dump() == before
    graph.assert_not_called()


@pytest.mark.parametrize("issue", ["species", "same_part", "state", "source", "skin", "bone", "coating", "missing_target_source"])
def test_general_discovery_does_not_authorize_incompatible_records(general_parts, issue):
    snapshot, targets, run, _, _ = general_parts(FAMILIES[1])
    for target in targets:
        if issue == "species": target.properties["biological_source"].value = "duck"
        elif issue == "same_part": target.properties["food_part"].value = "thigh"
        elif issue == "state": target.properties["preparation_state"].value = "cooked"
        elif issue == "source": target.source_dataset = "different"
        elif issue == "skin": target.properties["skin_status"].value = "with_skin"
        elif issue == "bone": target.properties["bone_status"].value = "bone_in"
        elif issue == "coating": target.properties["qualifiers.coating"].value = "breaded"
        else: target.properties.pop("biological_source")
    result = run()
    assert result["adapted_recipe"] is None and not result["model_calls"]


def test_missing_source_facts_never_query_or_invent_a_part(general_parts, monkeypatch):
    snapshot, _, _, _, _ = general_parts(FAMILIES[6])
    snapshot.ingredients[0].composition.properties.pop("food_part")
    query = Mock(side_effect=AssertionError("Unanchored query"))
    monkeypatch.setattr(matching_store, "food_part_composition_records", query)
    rows, reasons = food_part_candidate_records(snapshot, 0)
    assert not rows and reasons == ["food_part_discovery_source_fact_missing:food_part"]
    query.assert_not_called()


def test_candidate_scan_budget_is_explicit(general_parts, monkeypatch):
    snapshot, _, _, _, _ = general_parts(FAMILIES[1])
    monkeypatch.setattr(matching_store, "food_part_composition_records", lambda *a, **kw: [{} for _ in range(501)])
    rows, reasons = food_part_candidate_records(snapshot, 0)
    assert len(rows) == 500 and reasons == ["food_part_candidate_scan_limit_reached"]


def test_query_is_general_source_pinned_and_parameterized(monkeypatch):
    connection, engine = MagicMock(), MagicMock()
    connection.execute.return_value.mappings.return_value = []
    engine.connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(matching_store, "get_engine", lambda: engine)
    matching_store.food_part_composition_records("release", "nutritional_ingredients_irish", dataset="source'quoted",
        source_release="native-1", identity="celery", biological_source="celery", source_food_part="leaf")
    statement, params = connection.execute.call_args.args
    sql = str(statement)
    assert "chicken" not in sql and "breast" not in sql and "source'quoted" not in sql
    assert params["part"] == "leaf" and params["identity"] == "celery" and params["dataset"] == "source'quoted"
    assert "r.status='ready'" in sql and "LIMIT 501" in sql and "NOT IN (:part" in sql
    assert "COALESCE(c.annotation->'text_annotation'->'facets'->>'food_part','')" in sql
    assert "COALESCE(c.annotation->'text_annotation'->'facets'->>'biological_source','')" in sql


def test_edit_never_defaults_to_a_food_or_cut():
    with pytest.raises(ValidationError, match="source_identity"):
        RawPartEdit(recipe_id="r", baseline_run_id="run", quantity_input_hash="q", position=0,
            from_composition={"release_id": "v", "collection": "c", "candidate_id": "a"},
            to_composition={"release_id": "v", "collection": "c", "candidate_id": "b"})


def test_method_only_change_still_needs_a_separate_operation(general_parts):
    snapshot, targets, _, _, _ = general_parts(FAMILIES[1])
    target = targets[0]
    target.properties["food_part"].value = "thigh"
    target.properties["preparation_state"].value = "cooked"
    with pytest.raises(ValueError, match="no_executable_identity_or_part_change"):
        make_substitution_edit(snapshot, 0, target)


def test_edit_cannot_hide_a_cut_change_by_omitting_its_metadata(general_parts):
    snapshot, targets, _, _, _ = general_parts(FAMILIES[1], estimated=True)
    edit = make_substitution_edit(snapshot, 0, targets[0])
    edit.source_food_part = edit.target_food_part = None
    result = compare_raw_part(snapshot, edit, targets[0])
    assert result.status == "blocked" and "target.property_conflict:food_part" in result.blockers


def test_unknown_biological_source_cannot_authorize_a_cut_change(general_parts):
    snapshot, targets, _, _, _ = general_parts(FAMILIES[6], estimated=True)
    for props in (snapshot.ingredients[0].composition.properties, snapshot.ingredients[0].observed_properties,
                  targets[0].properties):
        props.pop("biological_source")
    edit = make_substitution_edit(snapshot, 0, targets[0])
    result = compare_raw_part(snapshot, edit, targets[0])
    assert result.status == "needs_evidence"
    assert "source.property:biological_source" in result.missing_evidence
    assert "target.property:biological_source" in result.missing_evidence


@pytest.mark.parametrize("measure", ["2 cups", "½ cup", "1½", "1/2", "8", "8boneless"])
def test_part_change_renders_saved_mass_instead_of_preserving_count_or_volume(general_parts, measure):
    snapshot, _, run, _, _ = general_parts(FAMILIES[1], plural=True, estimated=True)
    snapshot.ingredients[0].original_text = measure + " turkey thighs"
    result = run()
    assert result["status"] == "draft_requires_review", result["candidates"]
    text = result["adapted_recipe"]["ingredients"][0]["original_text"]
    assert text == "approximately 200 g " + ("boneless " if measure == "8boneless" else "") + "turkey breasts"


def test_unresolved_pack_count_cannot_silently_become_a_replacement_piece_count(general_parts):
    snapshot, _, run, _, _ = general_parts(FAMILIES[1], plural=True, estimated=True)
    snapshot.ingredients[0].original_text = "2 packs of turkey thighs"
    result = run()
    assert result["adapted_recipe"] is None and not result["model_calls"]
    assert all("instruction_preflight:counted_part_quantity_display_unresolved" in c["decision_reasons"] for c in result["candidates"])


def test_unresolved_source_alternative_cannot_remain_on_the_card(general_parts):
    snapshot, _, run, _, _ = general_parts(FAMILIES[1], plural=True, estimated=True)
    snapshot.ingredients[0].original_text = "8 turkey thighs (or 4 turkey legs)"
    result = run()
    assert result["adapted_recipe"] is None and not result["model_calls"]
    assert all("instruction_preflight:source_alternative_selection_required" in c["decision_reasons"] for c in result["candidates"])


def test_a_grounded_derived_product_does_not_block_another_source_cut(general_parts):
    snapshot, targets, run, steps, _ = general_parts(FAMILIES[1])
    original = snapshot.ingredients[0]
    stock = original.model_copy(deep=True)
    stock.position = 1
    stock.quantity.weight_g = 0
    stock.name = "turkey stock"
    stock.original_text = "turkey stock"
    stock.observed_properties["base_identity"].value = "turkey stock"
    stock.composition.properties["base_identity"].value = "turkey stock"
    snapshot.ingredients.append(stock)
    result = run()
    assert result["status"] == "draft_requires_review", result["reasons"]
    assert result["adapted_recipe"]["ingredients"][1]["name"] == "turkey stock"
    stock.original_text += " with turkey thighs"
    result = run()
    assert result["adapted_recipe"] is None and not result["model_calls"]


def test_identity_changes_cannot_rewrite_a_separate_stock_product(general_parts):
    snapshot, targets, _, _, _ = general_parts(FAMILIES[1])
    original = snapshot.ingredients[0]
    stock = original.model_copy(deep=True)
    stock.position = 1
    stock.name = stock.original_text = "turkey stock"
    stock.observed_properties["base_identity"].value = "turkey stock"
    snapshot.ingredients.append(stock)
    target = targets[0]
    for key, value in {"base_identity": "duck", "biological_source": "duck", "food_part": "thigh"}.items():
        target.properties[key].value = value
    edit = make_substitution_edit(snapshot, 0, target)
    ctx = contextual_assessment.build_adaptation_context(snapshot, title="Turkey dish", target_position=0,
        instructions=["Cook the turkey thigh.", "Add the turkey stock."], objective="Reduce energy")
    with pytest.raises(ValueError, match="ambiguous_substitution_occurrences"):
        instruction_editor._allowed_patches(ctx, edit)


def test_http_default_returns_a_non_chicken_part_card(general_parts, monkeypatch):
    snapshot, _, _, steps, _ = general_parts(FAMILIES[2], plural=True, estimated=True)
    item = snapshot.ingredients[0]
    recipe = {"recipe_id": snapshot.recipe_id, "title": "Duck dish", "serves": snapshot.serves,
        "instructions": steps, "original_ingredients": [item.original_text]}
    monkeypatch.setattr(enhancement_pipeline, "load_active_snapshot", lambda *a, **kw: snapshot.model_copy(deep=True))
    monkeypatch.setattr(enhancement_pipeline, "fetch_recipe_info_by_id", lambda *a: deepcopy(recipe))
    monkeypatch.setattr(matching_store, "active_profile", lambda *a, **kw: {"run_id": snapshot.run_id, "profile": profile(snapshot)})
    from recipe_wrangler.services.adaptation.router import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/v1/recipes/recipe-1/adapt/enhance", json={"region": "IE", "goal_nutrients": ["energy"]})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "draft_requires_review", result["reasons"]
    assert result["adapted_recipe"]["ingredients"][0]["name"] == "duck breast fillets"
    assert result["adapted_recipe"]["instructions"][0] == "Cook the duck breast fillets gently."
