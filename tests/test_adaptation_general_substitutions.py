"""Production discovery feeds the controlled generic raw substitution path."""

import json
from unittest.mock import Mock

import pytest

from test_adaptation_preview import inputs, model
from test_adaptation_instruction_editor import setup
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement_pipeline import flow
from test_adaptation_enhancement import breakdown
from test_adaptation_objective_approval import context
from recipe_wrangler.services.adaptation import enhancement_pipeline as pipeline, contextual_assessment, service
from recipe_wrangler.services.adaptation.models import RoleAssessment
from recipe_wrangler.services.adaptation.models import ComparisonConstraints
from recipe_wrangler.services.adaptation.production_candidates import make_substitution_edit
from recipe_wrangler.services.adaptation.comparison import compare_raw_part
from recipe_wrangler.services.adaptation.objective_approval import approve_nutrition_candidate
from recipe_wrangler.services.adaptation.models import NutrientObjective


@pytest.fixture
def general(flow, monkeypatch):
    monkeypatch.setattr(pipeline.matching_store, "food_part_composition_records", lambda *a, **kw: [])
    snapshot, targets = flow[0]
    original = snapshot.ingredients[0]
    original.name = "beef"
    original.original_text = "200 g raw boneless skinless beef leg"
    for props in (original.observed_properties, original.composition.properties):
        for field, value in {"base_identity": "beef", "biological_source": "cow", "food_part": "leg"}.items():
            props[field].value = value
    for target in targets:
        for field, value in {"base_identity": "pork", "biological_source": "pig", "food_part": "leg"}.items():
            target.properties[field].value = value
        target.provenance["candidate"]["document"] = "Raw boneless skinless pork leg"
    monkeypatch.setattr(service, "_select_graph_name", lambda *args: "beef")
    graph = Mock(return_value=[{"name": "pork", "source": "production_graph", "category_distance": "low"}])
    monkeypatch.setattr(service, "find_substitute_candidates", graph)
    monkeypatch.setattr(service, "_food_class_compatible", lambda *args: True)
    profile = {"catalogue_release": snapshot.release_id, "selected_candidate_id": targets[0].ref.candidate_id,
        "selected_composition_facts": {"source_key": "eu"}}
    lookup = Mock(return_value=profile)
    monkeypatch.setattr(service, "_fetch_candidate_profile", lookup)
    rows = Mock(return_value=[{**targets[0].ref.model_dump(), "candidate": {"document": "pork"}}])
    monkeypatch.setattr(pipeline.matching_store, "composition_records", rows)
    def request(payload, response_model, config, **kwargs):
        evidence = [{"kind": "ingredient", "reference_id": "ingredient:0", "quote": "beef"},
            {"kind": "instruction", "reference_id": "step:0", "quote": "Cook the beef gently."}]
        if response_model is RoleAssessment:
            return json.dumps({"status": "inferred", "roles": [{"role": "main_component", "reason": "Synthetic role", "evidence": evidence}], "uncertainties": []})
        return json.dumps({"assessments": [{"candidate_id": item["candidate_id"], "verdict": "suitable", "reason": "Synthetic suitability",
            "role_fit": [{"role": "main_component", "status": "preserved"}], "evidence": evidence, "required_changes": [], "uncertainties": []}
            for item in payload["candidates"]]})
    monkeypatch.setattr(contextual_assessment, "_request", request)
    return flow, graph, lookup, rows


def run(general, **kwargs):
    flow = general[0]
    return pipeline.enhance_recipe(flow[0][0], title="Beef dish", instructions=["Cook the beef gently."],
        nutri_score_breakdown=breakdown(energy=7), score_context=context(flow[0][0]), **kwargs)


def test_generic_raw_substitution_uses_production_discovery_and_returns_edited_card(general):
    result = run(general)
    assert result["status"] == "draft_requires_review"
    card = result["adapted_recipe"]
    assert card["title"] == "Pork dish" and card["instructions"] == ["Cook the pork gently."]
    assert card["ingredients"][0]["name"] == "pork"
    assert card["ingredients"][0]["original_text"] == "200 g raw boneless skinless pork leg"
    assert result["changes"][0]["edit"]["operation"] == "REPLACE_INGREDIENT"
    general[1].assert_called_once_with("beef")
    general[3].assert_called_once_with("release-1", "nutritional_ingredients_eu", candidate_id="breast-a")


@pytest.mark.parametrize("issue", ["unreleased", "ambiguous", "missing_part", "cooked", "coating", "observed_conflict"])
def test_production_candidate_is_not_automatically_valid(general, issue):
    flow, graph, lookup, rows = general
    if issue == "unreleased": lookup.return_value["catalogue_release"] = "other"
    elif issue == "ambiguous": rows.return_value *= 2
    elif issue == "missing_part": flow[0][1][0].properties.pop("food_part")
    elif issue == "cooked": flow[0][1][0].properties["preparation_state"].value = "cooked"
    elif issue == "coating": flow[0][1][0].properties["qualifiers.coating"].value = "breaded"
    else: flow[0][0].ingredients[0].observed_properties["base_identity"].value = "lamb"
    result = run(general)
    assert result["adapted_recipe"] is None and not flow[2][2]


def test_general_substitution_cannot_bypass_allergen_constraints(general):
    result = run(general, constraints=ComparisonConstraints(forbidden_allergens={"milk"}))
    assert result["status"] == "needs_evidence" and result["adapted_recipe"] is None


def test_cross_class_production_proposals_are_filtered_before_resolution(general, monkeypatch):
    monkeypatch.setattr(service, "_food_class_compatible", lambda *args: False)
    result = run(general)
    assert result["adapted_recipe"] is None
    general[2].assert_not_called()


def test_production_discovery_uses_both_canonical_and_occurrence_nodes(general, monkeypatch):
    from recipe_wrangler.services.adaptation import production_candidates as discovery
    snapshot = general[0][0][0]
    snapshot.input_policy = "production_estimate"
    snapshot.ingredients[0].name = "200g lean beef"
    resolve = Mock(side_effect=lambda name, hints: name)
    monkeypatch.setattr(service, "_select_graph_name", resolve)
    profile = Mock(return_value={"catalogue_release": snapshot.release_id,
        "selected_candidate_id": "breast-a", "selected_composition_facts": {"source_key": "eu"}})
    monkeypatch.setattr(discovery, "_candidate_profile", profile)
    monkeypatch.setattr(discovery, "_same_table_identity_records", Mock(return_value=[]))
    rows, reasons = discovery.production_candidate_records(snapshot, 0)
    assert rows and not reasons
    assert [call.args[0] for call in resolve.call_args_list] == ["beef", "200g lean beef"]
    assert all(call.kwargs == {"expand": True} for call in general[1].call_args_list)
    profile.assert_called_once()


def test_taxonomy_expansion_keeps_default_depth_and_deduplicates(monkeypatch):
    from recipe_wrangler.services.adaptation import neo4j_queries as graph
    query = Mock(return_value=[{"name": "alternative"}])
    monkeypatch.setattr(graph, "run_query", query)
    assert len(graph._foodon_candidates("original")) == 1
    assert query.call_count == 1
    cypher = query.call_args.args[0]
    assert 'WITH DISTINCT c\n' in cypher
    assert 'WITH DISTINCT c, ancestor' in cypher
    assert 'WITH DISTINCT sib' in cypher
    query.reset_mock()
    graph.find_substitute_candidates.cache_clear()
    result = graph.find_substitute_candidates("original", expand=True)
    assert len(result) == 1 and result[0]["source"] == "miskg"
    assert query.call_count == 5  # Two curated queries and three bounded depths.
    graph.find_substitute_candidates.cache_clear()


def test_graph_name_tie_preserves_existing_node(monkeypatch):
    monkeypatch.setattr(service, "find_substitute_candidates", lambda name: [{"name": "olive oil"}])
    monkeypatch.setattr(service, "resolve_graph_name_excluding_self", lambda name: "oil spray")
    monkeypatch.setattr(service, "resolve_graph_name", lambda name, hints: name)
    monkeypatch.setattr(service, "_classified_same_class_share", lambda *args: 1.0)
    assert service._select_graph_name("olive oil", []) == "olive oil"


@pytest.mark.parametrize('eligible', [False, True])
def test_same_table_rows_skip_global_match_only_after_eligibility(general, monkeypatch, eligible):
    from recipe_wrangler.services.adaptation import production_candidates as discovery
    snapshot = general[0][0][0]
    snapshot.input_policy = 'production_estimate'
    direct = general[3].return_value
    monkeypatch.setattr(discovery, '_same_table_identity_records', Mock(return_value=direct))
    fallback = Mock(return_value=None)
    monkeypatch.setattr(discovery, '_candidate_profile', fallback)
    state = {'eligible': False}
    rows, reasons = discovery.production_candidate_stream(snapshot, 0,
        has_eligible=lambda: state['eligible'])
    assert next(rows) == direct[0]
    state['eligible'] = eligible
    assert list(rows) == []
    assert fallback.call_count == (0 if eligible else 1)


def test_same_table_discovery_pins_dataset_release_and_state(general, monkeypatch):
    from recipe_wrangler.services.adaptation import production_candidates as discovery
    from recipe_wrangler.matching import anatomy
    from recipe_wrangler.repositories import matching_store
    snapshot = general[0][0][0]
    parser = Mock()
    parser.annotate.return_value = {'main_ingredient': 'pork', 'foods': [{'identity': 'pork'}]}
    monkeypatch.setattr(anatomy, '_label_annotator', lambda: parser)
    lookup = Mock(return_value=[])
    monkeypatch.setattr(matching_store, 'identity_composition_records', lookup)
    discovery._same_table_identity_records(snapshot, snapshot.ingredients[0], 'pork')
    assert lookup.call_args.kwargs == {'dataset': snapshot.ingredients[0].composition.source_dataset,
        'source_release': snapshot.ingredients[0].composition.source_release, 'identity': 'pork', 'state': 'raw'}
    parser.annotate.return_value = {'main_ingredient': None, 'foods': []}
    discovery._same_table_identity_records(snapshot, snapshot.ingredients[0], 'prawns')
    assert lookup.call_args.kwargs['identity'] == 'shrimp'


def test_unique_native_reference_resolution_matches_active_profile_loading(general):
    detail = general[2].return_value
    detail.pop("selected_candidate_id")
    detail["selected_composition_facts"]["native_id"] = "native-pork"
    assert run(general)["status"] == "draft_requires_review"
    general[3].assert_called_once_with("release-1", "nutritional_ingredients_eu", native_id="native-pork")


@pytest.mark.parametrize("identity,fvl,status", [("apple", 100., "approved_input_objective"), ("novel food", 0., "needs_evidence")])
def test_general_score_preserves_fvl_or_requires_evidence_instead_of_guessing(general, identity, fvl, status):
    snapshot, targets = general[0][0]
    for props in (snapshot.ingredients[0].composition.properties, snapshot.ingredients[0].observed_properties):
        props["base_identity"].value = identity
        props["biological_source"].value = identity
        props["food_part"].value = "flesh"
        props["bone_status"].value = "not_applicable"
        props["skin_status"].value = "peeled"
    snapshot.ingredients[0].composition.values_per_100g["energy_kcal"] = 100
    target = targets[0]
    for key, value in {"base_identity": "pear", "biological_source": "pear", "food_part": "flesh",
        "bone_status": "not_applicable", "skin_status": "peeled"}.items():
        target.properties[key].value = value
    target.values_per_100g["energy_kcal"] = 50
    edit = make_substitution_edit(snapshot, 0, target)
    comparison = compare_raw_part(snapshot, edit, target)
    score_context = context(snapshot)
    score_context.fruit_veg_legume_percent = fvl
    result = approve_nutrition_candidate(snapshot, comparison, NutrientObjective(metric="energy_kcal"), score_context)
    assert result["status"] == status
    if identity == "apple":
        assert result["after"]["positive_points"]["items"]["fruit_percentage"]["value_per_100g"] == 100
    else:
        assert result["reasons"] == ["substitution_fvl_membership_evidence_required"]


def test_second_same_identity_occurrence_prevents_ambiguous_instruction_rewrites(general):
    snapshot = general[0][0][0]
    other = snapshot.ingredients[0].model_copy(deep=True)
    other.position, other.quantity.weight_g = 1, 1
    snapshot.ingredients.append(other)
    score_context = context(snapshot)
    score_context.total_weight_g = 201
    result = pipeline.enhance_recipe(snapshot, title="Beef dish", instructions=["Cook the beef gently."],
        nutri_score_breakdown=breakdown(energy=7), score_context=score_context)
    assert result["adapted_recipe"] is None


def test_estimate_graph_discovery_can_resolve_other_released_tables(general,monkeypatch):
    from recipe_wrangler.services.adaptation import production_candidates as discovery
    from recipe_wrangler.matching import anatomy
    from recipe_wrangler.repositories import matching_store
    snapshot=general[0][0][0];snapshot.input_policy='production_estimate'
    parser=Mock();parser.annotate.return_value={'main_ingredient':'pork','foods':[{'identity':'pork'}]}
    monkeypatch.setattr(anatomy,'_label_annotator',lambda:parser)
    lookup=Mock(return_value=[]);monkeypatch.setattr(matching_store,'identity_composition_records',lookup)
    discovery._same_table_identity_records(snapshot,snapshot.ingredients[0],'pork')
    assert lookup.call_args.kwargs['include_other_sources'] is True
    assert lookup.call_args.kwargs['state']=='raw'
    assert lookup.call_args.args[0]==snapshot.release_id
