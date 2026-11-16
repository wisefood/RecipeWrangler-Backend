"""Candidate audit fields describe performed stages, including abstentions."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from test_adaptation_preview import inputs, model
from test_adaptation_instruction_editor import setup
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement_pipeline import flow, active, run
from test_adaptation_general_substitutions import general, run as run_general
from recipe_wrangler.services.adaptation import enhancement_pipeline as pipeline
from recipe_wrangler.services.adaptation.enhancement_response import EnhancementResponse


def test_success_exposes_candidate_origins_and_one_final_selection(flow):
    result = run(flow)
    assert result['response_schema_version'] == 'enhancement_response_v2'
    assert len(result['candidates']) == 2
    selected = [item for item in result['candidates'] if item['system_decision'] == 'selected']
    assert len(selected) == 1
    candidate = selected[0]
    assert candidate['mechanism'] == 'same_identity_food_part_variant'
    assert candidate['screening_decision']['status'] == 'eligible'
    assert candidate['culinary_status'] == 'suitable'
    assert candidate['culinary_evidence_status'] == 'inferred'
    assert candidate['objective_decision']['status'] == 'approved_input_objective'
    assert result['final_decision']['selected_candidate_id'] == candidate['candidate_id']
    assert result['final_decision']['outcome'] == 'adapted_recipe_draft'
    assert result['final_decision']['target_position'] == 0
    other = next(item for item in result['candidates'] if item != candidate)
    assert other['system_decision'] == 'not_selected'
    assert other['objective_decision']['status'] == 'not_evaluated'


def test_generic_candidates_identify_production_discovery(general):
    result = run_general(general)
    candidate = result['candidates'][0]
    assert candidate['mechanism'] == 'production_graph_substitution'
    assert candidate['proposed_edit']['operation'] == 'REPLACE_INGREDIENT'
    assert candidate['system_decision'] == 'selected'


def test_culinary_rejection_is_distinct_from_objective_rejection(flow):
    flow[1][1]['breast-b'] = 'unsuitable'
    result = run(flow)
    candidate = next(item for item in result['candidates'] if item['composition']['candidate_id'] == 'breast-b')
    assert candidate['system_decision'] == 'rejected'
    assert candidate['culinary_status'] == 'unsuitable'
    assert candidate['objective_decision']['status'] == 'not_evaluated'


def test_preflight_objective_rejections_are_explained_without_selection(flow):
    flow[0][0].ingredients[0].composition.values_per_100g['energy_kcal'] = 200
    result = run(flow, nutrition_policy="strict_grade_v2")
    assert result['final_decision']['outcome'] == 'no_adaptation'
    assert result['final_decision']['selected_candidate_id'] is None
    for candidate in result['candidates']:
        assert candidate['culinary_status'] == 'not_evaluated'
        assert candidate['system_decision'] == 'rejected'
        assert candidate['objective_preflight']['status'] == 'rejected'
        assert candidate['objective_decision']['status'] == 'not_evaluated'
        assert 'no_strict_letter_grade_improvement' in candidate['decision_reasons']


def test_required_changes_are_not_presented_as_rejection_or_selection(flow):
    for name in ('breast-a', 'breast-b'): flow[1][1][name] = 'suitable_with_changes'
    result = run(flow)
    assert result['final_decision']['outcome'] == 'changes_required'
    assert all(item['system_decision'] == 'requires_changes' for item in result['candidates'])


def test_preflight_evidence_gap_still_has_consistent_final_decision(flow, active):
    flow[0][0].ingredients[0].original_text = ''
    active[0]['original_ingredients'][0] = '100g unrelated food'
    result = pipeline.enhance_active_recipe('recipe-1', 'irish')
    assert result['baseline_run_id'] == flow[0][0].run_id
    assert result['candidates'] == []
    assert result['final_decision']['outcome'] == 'needs_evidence'
    assert result['final_decision']['stage'] == 'preflight'
    assert result['final_decision']['reasons'] == ['profile_original_ingredient_text_incomplete']


def test_stale_recipe_preserves_assessed_candidate_audit_without_selection(flow, active, monkeypatch):
    original = pipeline.preview_raw_part_candidates
    def assess(*args, **kwargs):
        result = original(*args, **kwargs)
        active[0]['title'] = 'Changed recipe'
        return result
    monkeypatch.setattr(pipeline, 'preview_raw_part_candidates', assess)
    from test_adaptation_objective_approval import context
    result = pipeline.enhance_active_recipe('recipe-1', 'irish', score_context=context(flow[0][0]))
    assert result['status'] == 'blocked' and result['candidates']
    assert all(item['system_decision'] != 'selected' for item in result['candidates'])
    assert result['final_decision']['reasons'] == ['recipe_text_changed']
    assert any(item['culinary_status'] == 'suitable' for item in result['candidates'])


@pytest.mark.parametrize('mutation', ['decision_id', 'outcome', 'duplicate', 'candidate_selection'])
def test_schema_rejects_inconsistent_final_decisions(flow, mutation):
    result = deepcopy(run(flow))
    if mutation == 'decision_id': result['final_decision']['selected_candidate_id'] = 'invented'
    elif mutation == 'outcome': result['final_decision']['outcome'] = 'no_adaptation'
    elif mutation == 'duplicate': result['candidates'].append(deepcopy(result['candidates'][0]))
    else:
        for item in result['candidates']: item['system_decision'] = 'not_selected'
    with pytest.raises(ValidationError): EnhancementResponse.model_validate(result)


def test_openapi_exposes_typed_candidate_and_decision_contract():
    from fastapi import FastAPI
    from recipe_wrangler.services.adaptation.router import router
    app = FastAPI()
    app.include_router(router)
    schemas = app.openapi()['components']['schemas']
    assert 'candidates' in schemas['EnhancementResponse']['properties']
    assert 'final_decision' in schemas['EnhancementResponse']['properties']
    assert 'mechanism' in schemas['EnhancementCandidate']['properties']
