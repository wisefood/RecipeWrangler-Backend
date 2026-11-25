"""Default enhancement tries other nutrients; explicit goals remain explicit."""
from test_adaptation_preview import inputs
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement import breakdown

from recipe_wrangler.services.adaptation.enhancement_screening import screen_enhancement


def test_default_can_improve_energy_when_highest_ranked_sodium_has_no_benefit(inputs, catalogue):
    snapshot, candidates = inputs
    snapshot.ingredients[0].composition.values_per_100g['sodium_mg'] = 500.0
    for candidate in candidates:
        candidate.values_per_100g['sodium_mg'] = 500.0
    catalogue(candidates)
    result = screen_enhancement(snapshot, mode='nutrition', nutri_score_breakdown=breakdown(sodium=5, energy=3))
    assert result['status'] == 'candidates_screened'
    assert result['numerical_objective']['metric'] == 'energy_kcal'
    assert [item['objective']['metric'] for item in result['objective_attempts']] == ['sodium_mg', 'energy_kcal']
    assert result['target_plan']['objective']['selection_basis'] == 'automatic_fallback_after_failed_nutrient_objective'


def test_explicit_sodium_goal_is_not_replaced_with_an_energy_goal(inputs, catalogue):
    snapshot, candidates = inputs
    snapshot.ingredients[0].composition.values_per_100g['sodium_mg'] = 500.0
    for candidate in candidates:
        candidate.values_per_100g['sodium_mg'] = 500.0
    catalogue(candidates)
    result = screen_enhancement(snapshot, mode='nutrition', goal_nutrients=['sodium'],
        nutri_score_breakdown=breakdown(sodium=5, energy=3))
    assert result['status'] != 'candidates_screened'
    assert result['numerical_objective']['metric'] == 'sodium_mg'
    assert 'objective_attempts' not in result


def test_joint_nutrient_gains_and_unchanged_score_reach_culinary_screen_before_graph(inputs, catalogue, monkeypatch):
    from copy import deepcopy
    from unittest.mock import Mock
    from recipe_wrangler.services.adaptation import candidate_selection
    from recipe_wrangler.services.adaptation.comparison import nutrition_basis
    from test_adaptation_objective_approval import context

    snapshot, candidates = inputs
    snapshot.input_policy = 'production_estimate'
    snapshot.serves = 4.
    snapshot.ingredients[0].quantity.weight_g = 1000.
    source = snapshot.ingredients[0].composition
    source.values_per_100g.update(energy_kcal=116., protein_g=19.7, carbs_g=0.,fat_g=4.12,
                                 sugar_g=0.,saturated_fat_g=1.1,sodium_mg=95.,fibre_g=0.)
    for target in candidates:
        target.values_per_100g.update(energy_kcal=105.,protein_g=24.4,carbs_g=0.,fat_g=.8,
                                     sugar_g=0.,saturated_fat_g=.3,sodium_mg=50.,fibre_g=0.)
    # An unchanged water component keeps both sides within the same score bands.
    water = deepcopy(snapshot.ingredients[0])
    water.position, water.name, water.original_text = 1, 'water', '500 g water'
    water.quantity.weight_g = 500.
    water.composition.ref.candidate_id = 'water'
    water.composition.values_per_100g = dict.fromkeys(source.values_per_100g,0.)
    for properties in (water.observed_properties,water.composition.properties):
        properties['base_identity'].value = 'water'
        for key in ('food_part','biological_source','skin_status','bone_status'):
            properties.pop(key,None)
    snapshot.ingredients.append(water)
    catalogue(candidates)
    monkeypatch.setattr(candidate_selection,'nutritional_variant_records',lambda *a: [])
    graph = Mock(side_effect=AssertionError('Try the available saturated-fat benefit before graph fallback'))
    monkeypatch.setattr(candidate_selection,'production_candidate_records',graph)
    score = context(snapshot)
    score.total_weight_g = 1500.
    score.basis = nutrition_basis(snapshot)
    result = screen_enhancement(snapshot,mode='nutrition',
        nutri_score_breakdown=breakdown(energy=1,saturated_fats=0,sodium=0),
        score_context=score,require_score_inputs=True,allow_general_substitutions=True)
    assert result['status']=='candidates_screened'
    assert result['numerical_objective']['metric']=='saturated_fat_g'
    assert [a['objective']['metric'] for a in result['objective_attempts']]==['energy_kcal','saturated_fat_g']
    assert all(a['search_scope']=='preferred_catalogue' for a in result['objective_attempts'])
    approval=next(item['objective_preflight'] for attempt in result['attempts']
        for item in (attempt.get('shortlist') or {}).get('screening',[]) if item['status']=='eligible')
    assert approval['status']=='approved_input_objective'
    assert approval['before']['score']==approval['after']['score']
    assert approval['before']['nutri_score']==approval['after']['nutri_score']=='Nutriscore_A'
    assert approval['nutrient_deltas_per_serving']['energy_kcal']==-27.5
    assert approval['nutrient_deltas_per_serving']['saturated_fat_g']==-2.
    assert approval['nutrient_deltas_per_serving']['sodium_mg']==-112.5
    graph.assert_not_called()
