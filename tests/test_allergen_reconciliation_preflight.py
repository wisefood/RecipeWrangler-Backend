from copy import deepcopy

import pytest

from scripts.neo4j.preflight_allergen_reconciliation import compare_journal
from scripts.neo4j.preview_allergen_reconciliation import digest


def snapshot():
    ingredient = {'element_id':'i', 'properties':{'canonical_id':'canonical'}, 'links':[], 'edges':[], 'declarations':[]}
    recipe = {'element_id':'r', 'properties':{'recipe_id':'recipe'}, 'ingredients':[{'target':'i'}], 'tags':[]}
    target = {'element_id':'class', 'properties':{'foodon_id':'FOODON_target'}, 'parents':[]}
    journal = {'cases':[{'name':'food', 'ingredient_id':'canonical', 'element_id':'i',
                        'before_sha256':digest(ingredient), 'guarded_recipe_membership':['r'],
                        'link_operation':{'target_element_id':'class', 'target_foodon_id':'FOODON_target',
                                          'target_class_before_sha256':digest(target)}}],
               'affected_recipes':[{'element_id':'r', 'recipe_id':'recipe', 'before_sha256':digest(recipe)}]}
    return journal, [ingredient], [recipe], [target]


def test_unchanged_snapshot_passes_without_mutation():
    values = snapshot()
    before = deepcopy(values)
    assert compare_journal(*values)['passed']
    assert values == before


@pytest.mark.parametrize('change,kind', [
    ('ingredient', 'ingredient_before_drift'), ('recipe', 'recipe_before_drift'),
    ('membership', 'inverse_membership_drift'), ('target', 'target_class_drift'),
    ('duplicate', 'target_class_not_unique'), ('canonical', 'ingredient_identity_missing_or_duplicated'),
])
def test_guard_rejects_drift(change, kind):
    journal, ingredients, recipes, classes = snapshot()
    if change == 'ingredient':
        ingredients[0]['edges'].append({'allergen':'milk'})
    elif change == 'recipe':
        recipes[0]['properties']['declared_allergens'] = ['milk']
    elif change == 'membership':
        recipes.append({**deepcopy(recipes[0]), 'element_id':'sibling'})
    elif change == 'target':
        classes[0]['parents'].append({'target':'new_ancestor'})
    elif change == 'duplicate':
        classes.append({**deepcopy(classes[0]), 'element_id':'duplicate'})
    else:
        ingredients.append({**deepcopy(ingredients[0]), 'element_id':'duplicate'})
    result = compare_journal(journal, ingredients, recipes, classes)
    assert not result['passed']
    assert kind in {row['kind'] for row in result['issues']}
