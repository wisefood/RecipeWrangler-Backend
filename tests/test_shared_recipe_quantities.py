import copy
import pytest

from recipe_wrangler.matching.quantities import (
    recipe_inputs,source_recipe_index,resolve_quantity,repair_profile,
)


def source_recipe(region='eu'):
    return {'recipe_id':'soup','region':region,'occurrences':[{
        'parsed_name':'coconut milk','saved_weight_g':2224.8,
        'original_text':'½ cup reduced-fat coconut milk',
        'quantity_evidence':{'weight_g':123.6,'kind':'source_volume_estimate',
            'source_line':'½ cup reduced-fat coconut milk'}}]}


def test_source_evidence_is_shared_by_recipe_and_conflicts_are_rejected():
    eu=source_recipe();irish=source_recipe('irish')
    assert set(source_recipe_index([eu,irish]))=={'soup'}
    irish['occurrences'][0]['quantity_evidence']['weight_g']=200
    with pytest.raises(ValueError,match='Conflicting'):source_recipe_index([eu,irish])


def test_source_evidence_must_align_with_baseline_occurrence():
    for names,weights in [(['milk'],[2224.8]),(['coconut milk'],[200])]:
        with pytest.raises(ValueError,match='align'):
            recipe_inputs(names,weights,[''],source_recipe())


def test_quantity_repair_rescales_nutrition_without_changing_food_or_facets():
    original={'mechanism_version':'old','serves':2,'details':[{'position':0,
        'selected_candidate_id':'food','ingredient':'coconut milk','weight_g':2224.8,
        'observed_facts':{'facets':{'fat_content':'reduced_fat'}},
        'values_per_100g':{'energy_kcal':200,'fat_g':20}}]}
    saved=copy.deepcopy(original)
    inputs=recipe_inputs(['coconut milk'],[2224.8],[''],source_recipe())
    repaired=repair_profile(original,inputs,'irish','new')
    assert original==saved
    assert repaired['details'][0]['selected_candidate_id']=='food'
    assert repaired['details'][0]['observed_facts']==saved['details'][0]['observed_facts']
    assert repaired['clean_totals_per_serving']['energy_kcal']==pytest.approx(123.6)
    assert repaired['clean_totals']['protein_g'] is None
    assert repaired['selection_mechanism_version']=='old'


def test_explicit_zero_and_ambiguous_quantities_are_preserved():
    assert resolve_quantity('milk',0,'1 cup milk')['weight_g']==0
    assert resolve_quantity('milk',200,'1 cup milk or water')['weight_g']==200
    assert resolve_quantity('tortillas',200,'8 mini (25g) wholegrain tortillas')['weight_g']==200
    with pytest.raises(ValueError):resolve_quantity('milk',float('nan'))
    with pytest.raises(ValueError):resolve_quantity('milk',2,evidence={'weight_g':float('inf'),'kind':'explicit_source_mass'})


def test_frozen_source_evidence_cannot_bypass_current_per_item_guard():
    line='4 x large (300g) Sebago potatoes, scrubbed and dried'
    result=resolve_quantity('sebago potatoes',1200,line,
        {'weight_g':300,'kind':'explicit_source_mass','source_line':line})
    assert result['weight_g']==1200
    assert result['quantity_evidence'] is None
    with pytest.raises(ValueError,match='disagrees'):
        resolve_quantity('milk',200,evidence={'weight_g':200,'kind':'explicit_source_mass','source_line':'100g milk'})


def test_nutrition_node_forwards_original_quantity_evidence(monkeypatch):
    from unittest.mock import Mock
    from recipe_wrangler.tools import nutritional_calculator as calculator
    from recipe_wrangler.schemas.models import RecipeState
    tool=Mock();tool.invoke.return_value={'details':[],'source_key':'eu'}
    monkeypatch.setattr(calculator,'nutritional_tool_vector',tool)
    state=RecipeState(title='Soup',ingredient_names=['coconut milk'],weights=[2224.8],
        ingredient_original_texts=['½ cup coconut milk'],nutrition_source='eu')
    calculator.Nutrition_Node(state)
    assert tool.invoke.call_args.args[0]['original_texts']==['½ cup coconut milk']


@pytest.mark.parametrize('name,line,expected',[
    ('wholemeal pan bread','2 slices of wholemeal pan bread, approx. 72g / 3 oz. grated into crumbs',72),
    ('lean rashers','6 lean rashers - around 150g / 5½ oz.',150),
])
def test_source_approximate_total_mass_is_not_an_item_count(name,line,expected):
    result=resolve_quantity(name,2,line)
    assert result['weight_g']==expected
    assert result['quantity_evidence']['source_line']==line


def test_source_approximate_per_item_mass_is_not_a_total():
    assert resolve_quantity('potatoes',600,'4 potatoes, around 150g each')['weight_g']==600
