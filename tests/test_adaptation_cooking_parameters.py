"""Cooking parameter edits use the full card flow and preserve the input calculation."""
from copy import deepcopy
import json

import pytest

from test_adaptation_enhancement_pipeline import flow, inputs, model, setup, catalogue
from recipe_wrangler.services.adaptation import contextual_assessment as culinary
from recipe_wrangler.services.adaptation import instruction_editor as editor
from recipe_wrangler.services.adaptation.enhancement_pipeline import enhance_recipe
from recipe_wrangler.services.adaptation.recipe_response import assemble_adaptation_response
from recipe_wrangler.services.adaptation.models import AdaptationPreview
from test_adaptation_objective_approval import context
from test_adaptation_enhancement import breakdown


def configure(flow, monkeypatch, *, identity='chicken', source_part='thigh', target_part='breast',
              description='Adjust the cooking time to avoid overcooking the replacement.'):
    snapshot, targets = flow[0]
    original = snapshot.ingredients[0]
    original.name = f'{identity} {source_part}'
    original.original_text = f'200 g raw boneless skinless {original.name}'
    for properties in [original.observed_properties, original.composition.properties] + [t.properties for t in targets]:
        properties['base_identity'].value = properties['biological_source'].value = identity
        properties['food_part'].value = source_part if properties in [original.observed_properties, original.composition.properties] else target_part
    steps = [f'Cook the {original.name} gently.', 'Cover the pot and cook gently for 1 hour.']
    request = culinary._request
    def assessment(payload, response_model, config, **kwargs):
        value = json.loads(request(payload, response_model, config, **kwargs))
        quotes = [{'kind': 'ingredient', 'reference_id': 'ingredient:0', 'quote': original.name},
                  {'kind': 'instruction', 'reference_id': 'step:0', 'quote': steps[0]}]
        if 'roles' in value:
            for role in value['roles']:role['evidence'] = quotes
        for item in value.get('assessments', []):
            item.update(verdict='suitable_with_changes', evidence=quotes,
                required_changes=[{'kind': 'cooking_method', 'ingredient_positions': [0],
                    'step_ids': ['step:0'], 'description': description}])
            item['role_fit'][0]['status'] = 'needs_changes'
        return json.dumps(value)
    monkeypatch.setattr(culinary, '_request', assessment)
    request_editor = editor._request
    def draft(payload, response_model, config, **kwargs):
        value = json.loads(request_editor(payload, response_model, config, **kwargs))
        if value['status'] == 'proposed' and payload.get('cooking_parameter_slots'):
            slot = payload['cooking_parameter_slots'][-1]
            value['cooking_adjustments'] = [{'change_index': slot['change_index'], 'parameter': slot['parameter'],
                'patch': {key: slot[key] for key in ('reference_id', 'start', 'end', 'before')}
                         | {'kind': 'instruction', 'after': '15–20 minutes'},
                'reason': 'Shorter gentle cooking limits overcooking; retained ingredients remain in the pot.'}]
        return json.dumps(value)
    monkeypatch.setattr(editor, '_request', draft)
    return snapshot, steps


def run(snapshot, steps):
    return enhance_recipe(snapshot, title='Dinner', instructions=steps, score_context=context(snapshot),
                          nutri_score_breakdown=breakdown(energy=7))


@pytest.mark.parametrize('identity,source_part,target_part', [
    ('chicken', 'thigh', 'breast'), ('duck', 'leg', 'breast'), ('pork', 'shoulder', 'loin')])
def test_conditional_timing_adjustment_returns_complete_unsaved_card(flow, monkeypatch, identity, source_part, target_part):
    snapshot, steps = configure(flow, monkeypatch, identity=identity, source_part=source_part, target_part=target_part)
    before = deepcopy(snapshot.model_dump())
    result = run(snapshot, steps)
    assert result['status'] == 'draft_requires_review', result['reasons']
    card = result['adapted_recipe']
    assert card['ingredients'][0]['name'] == f'{identity} {target_part}'
    assert card['ingredients'][0]['quantity']['weight_g'] == 200
    assert card['serves'] == 2 and card['nutrition']['per_serving']['energy_kcal'] == 100
    assert card['instructions'] == [f'Cook the {identity} {target_part} gently.',
                                     'Cover the pot and cook gently for 15–20 minutes.']
    selected = next(c for c in result['candidates'] if c['system_decision'] == 'selected')
    assert selected['culinary_status'] == 'suitable_with_changes'
    assert selected['instruction_decision']['status'] == 'draft_requires_review'
    assert result['provenance']['cooking_adjustments'][0]['change_index'] == 0
    assert len(result['model_calls']) == 3
    assert snapshot.model_dump() == before and not result['saved'] and not result['recipe_ready']


@pytest.mark.parametrize('mutation', ['extra_ingredient', 'new_method', 'quantity', 'offset', 'unrelated_step',
                                     'missing', 'duplicate', 'change_id', 'zero', 'unchanged', 'huge'])
def test_model_cannot_expand_cooking_adjustments_or_leave_requirements_unresolved(flow, monkeypatch, mutation):
    snapshot, steps = configure(flow, monkeypatch)
    request = editor._request
    def malicious(*args, **kwargs):
        value = json.loads(request(*args, **kwargs));changes = value['cooking_adjustments'];patch = changes[0]['patch']
        if mutation == 'extra_ingredient':patch['after'] = '20 minutes with added butter'
        elif mutation == 'new_method':patch['after'] = 'deep-fry for 20 minutes'
        elif mutation == 'quantity':patch.update(before='200 g', after='100 g')
        elif mutation == 'offset':patch['start'] += 1
        elif mutation == 'unrelated_step':patch['reference_id'] = 'step:0'
        elif mutation == 'missing':value['cooking_adjustments'] = []
        elif mutation == 'duplicate':changes.append(deepcopy(changes[0]))
        elif mutation == 'change_id':changes[0]['change_index'] = 99
        elif mutation == 'zero':patch['after'] = '0 minutes'
        elif mutation == 'unchanged':patch['after'] = '60 minutes'
        elif mutation == 'huge':patch['after'] = '999999 hours'
        return json.dumps(value)
    monkeypatch.setattr(editor, '_request', malicious)
    result = run(snapshot, steps)
    assert result['adapted_recipe'] is None and result['status'] == 'unavailable'


def test_card_assembly_revalidates_cooking_adjustments(flow, monkeypatch):
    snapshot, steps = configure(flow, monkeypatch)
    captured = []
    draft = editor.draft_raw_part_instructions
    from recipe_wrangler.services.adaptation import enhancement_pipeline
    def capture(*args, **kwargs):
        result = draft(*args, **kwargs);captured.append(result);return result
    monkeypatch.setattr(enhancement_pipeline, 'draft_raw_part_instructions', capture)
    result = run(snapshot, steps)
    assert result['status'] == 'draft_requires_review'
    altered = captured[0].model_copy(deep=True)
    altered.cooking_adjustments[0].patch.after = '20 minutes with butter'
    card = assemble_adaptation_response(snapshot, AdaptationPreview.model_validate(result['preview']), altered)
    assert card.status == 'blocked' and card.adapted_recipe is None


def test_unsupported_method_change_still_abstains_before_editor(flow, monkeypatch):
    snapshot, steps = configure(flow, monkeypatch, description='Replace the cooking method with deep frying.')
    result = run(snapshot, steps)
    assert result['status'] == 'changes_required' and result['adapted_recipe'] is None
    assert not flow[2][2]


def test_existing_temperature_parameter_can_change_without_changing_nutrition(flow, monkeypatch):
    snapshot, steps = configure(flow, monkeypatch, description='Adjust the cooking temperature for the replacement.')
    steps[1] = 'Bake at 180 C.'
    request = editor._request
    def temperature(*args, **kwargs):
        value = json.loads(request(*args, **kwargs))
        value['cooking_adjustments'][0]['patch']['after'] = '160 C'
        return json.dumps(value)
    monkeypatch.setattr(editor, '_request', temperature)
    result = run(snapshot, steps)
    assert result['status'] == 'draft_requires_review', result['reasons']
    assert result['adapted_recipe']['instructions'][1] == 'Bake at 160 C.'
    assert result['adapted_recipe']['nutrition']['per_serving']['energy_kcal'] == 100


def test_shortening_only_browning_cannot_leave_the_long_cooking_phase_unchanged(flow, monkeypatch):
    snapshot, steps = configure(flow, monkeypatch)
    steps[0] = 'Brown the chicken thigh for 30 seconds.'
    result = run(snapshot, steps)
    assert result['status'] == 'draft_requires_review', result['reasons']
    slots = flow[2][2][-1][0]['cooking_parameter_slots']
    assert {slot['reference_id'] for slot in slots} == {'step:1'}
    assert result['adapted_recipe']['instructions'][0] == 'Brown the chicken breast for 30 seconds.'
    assert result['adapted_recipe']['instructions'][1].endswith('15–20 minutes.')


def test_every_equal_long_cooking_phase_must_be_resolved(flow, monkeypatch):
    snapshot, steps = configure(flow, monkeypatch)
    steps.append('Continue cooking gently for 1 hour.')
    result = run(snapshot, steps)
    assert result['adapted_recipe'] is None and result['status'] == 'unavailable'


@pytest.mark.parametrize('description', [
    'Add the chicken later to avoid overcooking; adjust the cooking time.',
    'Add the chicken after the vegetables are tender to avoid overcooking.',
    'Delay adding the chicken and reduce its cooking time.',
    'Reorder the steps to adjust the cooking time.',
])
def test_changing_addition_order_is_not_resolved_by_a_duration_patch(flow, monkeypatch, description):
    snapshot, steps = configure(flow, monkeypatch, description=description)
    result = run(snapshot, steps)
    assert result['status'] == 'changes_required' and result['adapted_recipe'] is None
    assert not flow[2][2]


def test_casserole_instruction_structure_edits_simmer_not_browning(flow, monkeypatch):
    # Source instructions with synthetic nutrition and mocked model responses.
    snapshot, steps = configure(flow, monkeypatch)
    steps[:] = [
        'Heat the oil in a large pot and when it is hot, quickly brown the chicken thighs on all sides, about 30 seconds per side',
        'Remove the browned chicken and set it aside for a few minutes',
        'Add the chopped onion to the oil and cook it until it starts to soften',
        'Add the chopped or minced garlic and cook for about 2 minutes',
        'Next add the flour and mix it well until it has soaked up the oil and juices',
        'Add the chicken, followed by the chopped carrot and celery, the bay leaf, thyme and one sprig of parsley',
        'Add the chicken stock and mix well',
        'Put the lid on the pot and allow to cook gently for about 1 hour',
        'Sprinkle with the chopped parsley and serve',
    ]
    before = deepcopy(snapshot.model_dump())
    result = run(snapshot, steps)
    assert result['status'] == 'draft_requires_review', result['reasons']
    slots = flow[2][2][-1][0]['cooking_parameter_slots']
    assert {(s['reference_id'], s['before']) for s in slots} == {('step:7', '1 hour')}
    expected = list(steps)
    expected[0] = expected[0].replace('chicken thighs', 'chicken breasts')
    expected[7] = expected[7].replace('1 hour', '15–20 minutes')
    assert result['adapted_recipe']['instructions'] == expected
    assert result['adapted_recipe']['nutrition']['per_serving']['energy_kcal'] == 100
    assert snapshot.model_dump() == before
    assert not result['saved'] and not result['recipe_ready']
