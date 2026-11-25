import pytest
from recipe_wrangler.matching.evaluation.source_quantity import explicit_total_mass, recover_recipe_quantities


@pytest.mark.parametrize('line,expected',[
    ('1 sweet potato / 130g / 4.5 oz',130),('4 new potatoes (160g / 5½oz.)',160),
    ('2 potatoes (0.4 kg)',400),('4 potatoes (160g each)',None),
    ('2 cans tomatoes (400g)',None),('2-4 potatoes (160g)',None),
    ('1 potato or 130g rice',None),('1 potato (160g, 120g drained)',None),
    ('130g potatoes',None),('4 potatoes (0g)',None)])
def test_explicit_total_mass_respects_scope(line,expected):
    assert explicit_total_mass(line)==expected


def test_quantity_recovery_keeps_input_and_rejects_ambiguous_source_join():
    recipe={'recipe_id':'r','original_ingredients':['4 new potatoes (160g / 5½oz.)'],
        'occurrences':[{'occurrence_id':'o','parsed_name':'potatoes','saved_weight_g':692}]}
    assert recover_recipe_quantities(recipe)[0]['new_weight_g']==160
    assert recipe['occurrences'][0]['saved_weight_g']==692
    recipe['original_ingredients'].append('1 potato (100g)')
    assert recover_recipe_quantities(recipe)==[]
