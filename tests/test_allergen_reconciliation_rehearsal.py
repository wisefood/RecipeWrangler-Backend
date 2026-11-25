from copy import deepcopy

import pytest

from scripts.neo4j.preview_allergen_reconciliation import digest
from scripts.neo4j.rehearse_allergen_reconciliation import rehearse_journal, rollback_rehearsal


def fixture(*, supported=False, version='fato-foodon-v1'):
    allergen = 'milk' if supported else 'fish'
    name = 'milk' if supported else 'pork fillet'
    props = {'classification_version':version, 'presence':'contains',
             'evidence_status':'inferred', 'sources':['foodon', 'keyword'] if supported else ['foodon']}
    edge = {'element_id':'edge', 'allergen':allergen, 'target':'allergen', 'properties':deepcopy(props)}
    declaration = {'element_id':'declaration', 'edge_id':'has_declaration', 'edge_properties':{},
                   'properties':{**deepcopy(props), 'declaration_type':'inferred_ingredient_presence'},
                   'concerns':[{'allergen':allergen, 'element_id':'concern', 'target':'allergen', 'properties':{}}]}
    link = {'element_id':'link', 'target':'old', 'foodon_id':'FOODON_old', 'properties':{}}
    ingredient = {'element_id':'i', 'properties':{'canonical_id':'canonical', 'name':name},
                  'links':[link], 'edges':[edge], 'declarations':[declaration]}
    unrelated = {'element_id':'other', 'properties':{'name':'source original'}, 'links':[], 'edges':[], 'declarations':[]}
    target = {'element_id':'target', 'properties':{'foodon_id':'FOODON_00001256' if supported else 'FOODON_00001006',
                                                'label':'dairy food product' if supported else 'mammalian meat food product'}, 'parents':[]}
    old = {'element_id':'old', 'properties':{'foodon_id':'FOODON_old', 'label':'old'}, 'parents':[]}
    recipe = {'element_id':'r', 'properties':{'recipe_id':'r', 'declared_allergens':['peanut']},
              'ingredients':[{'target':'i'}, {'target':'other'}], 'tags':[{'name':'original'}]}
    operation = {'action':'refresh_selected_supported_pair' if supported else 'retire_selected_derived_pair',
                 'allergen':allergen}
    if supported:
        operation.update(exact_before_edges=[deepcopy(edge)], exact_before_declarations=[deepcopy(declaration)],
                         proposed_evidence={'classification_version':'fato-foodon-v2', 'presence':'contains',
                                            'evidence_status':'inferred', 'sources':['keyword', 'foodon'],
                                            'foodon_ids':['FOODON_00001256'], 'keyword_matches':['milk']})
    else:
        operation.update(exact_edges=[deepcopy(edge)], exact_declarations=[deepcopy(declaration)])
    case = {'name':name, 'ingredient_id':'canonical', 'element_id':'i', 'before_sha256':digest(ingredient),
            'guarded_recipe_membership':['r'], 'evidence_operations':[operation],
            'link_operation':{'before':deepcopy(link), 'target_element_id':'target',
                              'target_foodon_id':target['properties']['foodon_id'],
                              'target_class_before_sha256':digest(target), 'properties':{'classification_version':'foodon-link-v2'}}}
    journal = {'cases':[case], 'affected_recipes':[{'element_id':'r', 'recipe_id':'r', 'before_sha256':digest(recipe)}]}
    return journal, [ingredient, unrelated], [recipe], [old, target]


def test_selected_rehearsal_and_rollback_preserve_original_inputs_and_unrelated_facts():
    values = fixture()
    before = deepcopy(values)
    result = rehearse_journal(*values)
    assert values == before
    selected = result['after'][0]
    assert selected['edges'] == [] and selected['declarations'] == []
    assert selected['links'][0]['target'] == 'target'
    assert result['after'][1] == values[1][1]
    assert rollback_rehearsal(result, result['after'], values[2]) == values[1]
    assert values[2][0]['properties']['declared_allergens'] == ['peanut']


def test_previous_v4_rule_evidence_keeps_rehearsal_and_rollback_ownership():
    values = fixture(version='fato-foodon-v4')
    result = rehearse_journal(*values)
    assert result['after'][0]['edges'] == []
    assert rollback_rehearsal(result, result['after'], values[2]) == values[1]


def test_supported_refresh_retains_keyword_and_root_provenance():
    values = fixture(supported=True)
    result = rehearse_journal(*values)
    row = result['after'][0]
    assert row['edges'][0]['properties']['keyword_matches'] == ['milk']
    assert row['edges'][0]['properties']['foodon_ids'] == ['FOODON_00001256']
    assert row['declarations'][0]['properties']['classification_version'] == 'fato-foodon-v2'
    assert rollback_rehearsal(result, result['after'], values[2]) == values[1]


def test_retirement_rejects_surviving_keyword_support():
    journal, ingredients, recipes, classes = fixture()
    ingredients[0]['properties']['name'] = 'pork fillet with fish'
    journal['cases'][0]['before_sha256'] = digest(ingredients[0])
    with pytest.raises(ValueError, match='independently supported'):
        rehearse_journal(journal, ingredients, recipes, classes)


def test_foreign_evidence_is_never_retired():
    journal, ingredients, recipes, classes = fixture()
    ingredients[0]['edges'][0]['properties']['sources'] = ['manual']
    case = journal['cases'][0]
    case['before_sha256'] = digest(ingredients[0])
    case['evidence_operations'][0]['exact_edges'] = deepcopy(ingredients[0]['edges'])
    with pytest.raises(ValueError, match='foreign'):
        rehearse_journal(journal, ingredients, recipes, classes)


@pytest.mark.parametrize('drift', ['ingredient', 'recipe', 'membership'])
def test_rollback_refuses_intervening_edits(drift):
    values = fixture()
    result = rehearse_journal(*values)
    current = deepcopy(result['after']); recipes = deepcopy(values[2])
    if drift == 'ingredient':
        current[0]['properties']['name'] = 'user edit'
    elif drift == 'recipe':
        recipes[0]['properties']['declared_allergens'] = []
    else:
        recipes.append({**deepcopy(recipes[0]), 'element_id':'new_sibling'})
    with pytest.raises(ValueError, match='Rollback'):
        rollback_rehearsal(result, current, recipes)


def test_rollback_leaves_unrelated_ingredient_edits_untouched():
    values = fixture()
    result = rehearse_journal(*values)
    current = deepcopy(result['after'])
    current[1]['properties']['name'] = 'unrelated user edit'
    restored = rollback_rehearsal(result, current, values[2])
    assert restored[0] == values[1][0]
    assert restored[1]['properties']['name'] == 'unrelated user edit'
