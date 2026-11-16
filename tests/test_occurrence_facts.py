import copy
from unittest.mock import Mock

import pytest

from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary
from recipe_wrangler.matching.nutrition_policy import calculation_decision
from recipe_wrangler.matching.occurrence_facts import (
    occurrence_inputs, occurrence_key, recover_occurrence_facts,
)


@pytest.fixture
def annotate():
    specs = [('pork', 'meat'), ('beef', 'meat'), ('chicken', 'meat'), ('tuna', 'generic'),
             ('milk', 'milk'), ('rice', 'cereal'), ('olive oil', 'generic'), ('chickpea', 'generic'),
             ('yogurt','generic')]
    terms = [{'term_id': name, 'preferred_label': name, 'definition': '', 'curation_status': '',
              'labels': [{'text': name, 'type': 'preferred'}]} for name, _ in specs]
    parser = ContextualParser(IngredientDictionary({'schema_version': 1, 'terms': terms}),
                              {name: {family} for name, family in specs})
    return HybridAnnotator(parser).annotate


@pytest.mark.parametrize('name,source',[
    ('brown rice','3 cups brown rice (cooked)'),
    ('black rice','¾ cup cooked black or brown rice'),
])
def test_direct_state_and_shared_rice_alternative_state_are_retained(annotate,name,source):
    result=recover_occurrence_facts(name,source,annotate)
    assert result['facets']['preparation_state']=='cooked'
    assert result['input_text']==name
    for claim in result['occurrence_recovery']['recovered']:
        for evidence in claim['evidence']:
            for span in evidence['spans']:
                assert source[span['start']:span['end']]==evidence['quote']


@pytest.mark.parametrize('name,source,field,value', [
    ('pork', '480g diced lean pork', 'meat_fat_description', 'lean'),
    ('pork', '450g boneless pork', 'bone_status', 'absent'),
    ('chicken', '200g skinless chicken', 'skin_status', 'absent'),
    ('tuna', '1 x 185g tin tuna, drained', 'drainage_status', 'drained'),
    ('tuna', '100g canned tuna', 'preservation_methods', ['canned']),
    ('tuna', '1 tin of tuna in brine, drained', 'packing_medium', 'brine'),
    ('rice', '100g rice, cooked', 'preparation_state', 'cooked'),
    ('milk', '200ml low-fat milk', 'fat_level', 'low'),
])
def test_source_properties_are_separate_from_name_and_have_source_evidence(annotate, name, source, field, value):
    facts = recover_occurrence_facts(name, source, annotate)
    assert facts['input_text'] == name
    assert facts['facets'][field] == value
    claim = next(c for c in facts['occurrence_recovery']['recovered'] if c['field'] == field)
    for evidence in claim['evidence']:
        assert evidence['spans']
        for span in evidence['spans']:
            assert source[span['start']:span['end']] == evidence['quote']


def test_container_inference_is_not_presented_as_observed_processing(annotate):
    facts = recover_occurrence_facts('tuna', '1 x 185g tin tuna, drained', annotate)
    assert facts['facets']['preservation_methods'] == ['canned']
    claim = next(c for c in facts['occurrence_recovery']['recovered'] if c['field'] == 'preservation_methods')
    assert claim['evidence'][0]['status'] == 'inferred_from_container'


@pytest.mark.parametrize('name,source', [
    ('pork', '200g pork with lean beef'),
    ('pork', '200g lean beef'),
    ('pork', ''),
])
def test_ambiguous_or_different_food_does_not_supply_properties(annotate, name, source):
    facts = recover_occurrence_facts(name, source, annotate)
    assert facts['facets'] == annotate(name)['facets']
    assert facts['occurrence_recovery']['deferred_reason']


@pytest.mark.parametrize('name,line,field,expected', [
    ('tuna', '200g canned tuna or salmon', 'preservation_methods', ['canned']),
    ('chicken thighs', '8 chicken thighs, skin and bone removed (or 4 chicken legs, with skin)', 'skin_status', 'absent'),
    ('chicken thighs', '8 chicken thighs, skin and bone removed (or 4 chicken legs, with skin)', 'bone_status', 'absent'),
    ('pork', '200g pork, bones removed (or beef, bones removed)', 'bone_status', 'absent'),
])
def test_first_option_retains_only_its_own_source_qualifiers(annotate, name, line, field, expected):
    facts = recover_occurrence_facts(name, line, annotate)
    assert facts['facets'][field] == expected
    recovery = facts['occurrence_recovery']
    assert recovery['original_text'] == line and recovery['deferred_reason'] is None
    assert line.lstrip().startswith(recovery['selected_source_text'])
    end = line.index(recovery['selected_source_text']) + len(recovery['selected_source_text'])
    for claim in recovery['recovered']:
        for evidence in claim['evidence']:
            assert evidence['spans']
            for span in evidence['spans']:
                assert line[span['start']:span['end']] == evidence['quote']
                assert span['end'] <= end


@pytest.mark.parametrize('line', [
    '8 chicken thighs (or 4 skinless chicken legs)',
    '8 chicken thighs, skin to be removed (or chicken legs)',
    '8 chicken thighs, to have skin removed (or chicken legs)',
    '8 chicken thighs, not skin removed (or chicken legs)',
    '4 chicken legs (or 8 skinless chicken thighs)',
    '8 chicken thighs (skin removed or bone removed)',
    '8 chicken thighs (or 4 chicken legs), skin removed',
])
def test_future_or_unselected_source_qualifier_is_never_borrowed(annotate, line):
    facts = recover_occurrence_facts('chicken thighs', line, annotate)
    assert facts['facets']['skin_status'] is None


@pytest.mark.parametrize('source', ['200g pork for frying', '200g pork to be fried', '200g pork, not fried'])
def test_intended_or_negated_cooking_is_not_the_input_state(annotate, source):
    facts = recover_occurrence_facts('pork', source, annotate)
    assert facts['facets']['preparation_state'] is None
    assert facts['facets']['processes'] == []


def test_conflicting_input_descriptions_are_not_silently_overwritten_or_calculated(annotate):
    facts = recover_occurrence_facts('raw pork', '200g cooked pork', annotate)
    assert facts['facets']['preparation_state'] == 'raw'
    assert facts['occurrence_recovery']['conflicts']
    decision = calculation_decision(facts, None, None, selected=False)
    assert decision['blockers'] == ['input.conflicting_occurrence_evidence']
    assert all(v is None for v in decision['values_per_100g'].values())


def test_recovery_cannot_mutate_a_shared_name_annotation(annotate):
    base = annotate('pork')
    original = copy.deepcopy(base)
    recover_occurrence_facts('pork', '200g lean pork', lambda s: base if s == 'pork' else annotate(s))
    assert base == original


def test_cache_shares_semantics_but_not_different_properties(annotate):
    lean = recover_occurrence_facts('pork', '480g lean pork', annotate)
    more = recover_occurrence_facts('pork', '200g lean pork', annotate)
    plain = recover_occurrence_facts('pork', '200g pork', annotate)
    assert occurrence_key(lean) == occurrence_key(more)
    assert occurrence_key(lean) != occurrence_key(plain)


def test_every_occurrence_keeps_its_own_source_evidence(annotate):
    pack = {'recipes': [{'recipe_id': 'r', 'title': 'Test', 'occurrences': [
        {'occurrence_id': 'a', 'parsed_name': 'pork', 'original_text': '480g lean pork'},
        {'occurrence_id': 'b', 'parsed_name': 'pork', 'original_text': '200g lean pork'},
        {'occurrence_id': 'c', 'parsed_name': 'pork', 'original_text': '200g pork'},
    ]}]}
    jobs, keys, ledger = occurrence_inputs(pack, annotate)
    assert len(jobs) == 2
    assert keys['a'] == keys['b'] != keys['c']
    assert ledger[0]['recovery']['original_text'] == '480g lean pork'
    assert ledger[1]['recovery']['original_text'] == '200g lean pork'
    pack['recipes'][0]['occurrences'][2]['occurrence_id'] = 'a'
    with pytest.raises(ValueError, match='Duplicate occurrence'):
        occurrence_inputs(pack, annotate)


@pytest.mark.parametrize('name,source', [
    ('pork', '450g stewing pork'),
    ('chicken', '1.5kg whole chicken (100g cooked chicken per person)'),
])
def test_intended_cut_use_and_parenthetical_yields_do_not_assert_cooked_state(annotate,name,source):
    result=recover_occurrence_facts(name,source,annotate)
    assert result['facets']['preparation_state'] is None


def test_unqualified_percentage_is_not_fat(annotate):
    result=annotate('70% chickpea')
    assert result['facets']['fat_content_description'] is None
    assert annotate('3% milk')['facets']['fat_content_description'] is not None


def test_positive_selection_prefers_supported_fat_class_over_unknown_proxy(annotate):
    from recipe_wrangler.matching.source_suitability import choose_supported_occurrence_candidate
    query=recover_occurrence_facts('milk','200ml low-fat milk',annotate)
    labels={'unknown':'Milk','low':'Milk, low fat','wrong':'Milk, whole','other':'Rice'}
    records={key:{'id':key,'document':label} for key,label in labels.items()}
    facts={key:{'label':label,'text_annotation':annotate(label),'blockers':[],
                'core_complete':True} for key,label in labels.items()}
    policy=Mock()
    policy.catalogue.by_id.side_effect=lambda collection,key:records[key]
    policy.source.side_effect=lambda collection,candidate:facts[candidate['id']]
    checked=[{'collection':'test','candidate_id':key,'blocked':False} for key in labels]
    choice=choose_supported_occurrence_candidate(query,facts['unknown'],checked,policy)
    assert choice['candidate']['id']=='low'
    assert choice['supported_fields']==['fat_level']
    rescue=choose_supported_occurrence_candidate(query,None,checked,policy)
    assert rescue['candidate']['id']=='low'
    assert choose_supported_occurrence_candidate(query,facts['low'],checked,policy) is None
    facts['low']['core_complete']=False
    assert choose_supported_occurrence_candidate(query,facts['unknown'],checked,policy) is None
    facts['low']['core_complete']=True
    checked[1]['blocked']=True
    assert choose_supported_occurrence_candidate(query,facts['unknown'],checked,policy) is None


def test_low_fat_greek_yogurt_replaces_flavoured_legacy_only_with_plain_evidence(annotate):
    from recipe_wrangler.matching.source_suitability import choose_supported_occurrence_candidate
    query=recover_occurrence_facts('low fat Greek yogurt','2 tbsp low fat Greek yogurt',annotate)
    labels={'toffee':'Yogurt, low fat, toffee','plain':'Yogurt, low fat, plain'}
    records={key:{'id':key,'document':label} for key,label in labels.items()}
    facts={key:{'label':label,'text_annotation':annotate(label),'blockers':[],
                'core_complete':True} for key,label in labels.items()}
    policy=Mock()
    policy.catalogue.by_id.side_effect=lambda collection,key:records[key]
    policy.source.side_effect=lambda collection,candidate:facts[candidate['id']]
    checked=[{'collection':'test','candidate_id':key,'blocked':False} for key in labels]
    # A legacy selection must also be blocked at the final calculation boundary.
    blocked=calculation_decision(query,records['toffee'],facts['toffee'],selected=True,retained=True)
    assert blocked['kind']=='unresolved'
    assert blocked['blockers']==['product.unsupported_yogurt_flavour']
    assert all(v is None for v in blocked['values_per_100g'].values())
    choice=choose_supported_occurrence_candidate(query,facts['toffee'],checked,policy)
    assert choice['candidate']['id']=='plain'
    checked[1]['blocked']=True
    assert choose_supported_occurrence_candidate(query,facts['toffee'],checked,policy) is None


def test_positive_selection_does_not_demand_cosmetic_handling(annotate):
    from recipe_wrangler.matching.source_suitability import recovered_property_support
    query=recover_occurrence_facts('pork','200g diced pork',annotate)
    requested,supported=recovered_property_support(query,{'text_annotation':annotate('Pork')})
    assert 'processes' not in requested
    assert requested==supported


def test_positive_support_keeps_original_properties_and_rejects_an_unrequested_method(annotate):
    from recipe_wrangler.matching.source_suitability import recovered_property_support, choose_supported_occurrence_candidate
    query=recover_occurrence_facts('pork','200g cooked pork',annotate)
    query['facets']['colour']='black'
    requested,supported=recovered_property_support(query,{'text_annotation':annotate('Pork, cooked')})
    assert 'colour' in requested and 'colour' not in supported
    query['facets']['colour']=None
    candidate={'id':'fried','document':'Pork, fried'}
    facts={'label':'Pork, fried','text_annotation':annotate('Pork, fried'),'core_complete':True,'blockers':[]}
    current={'text_annotation':annotate('Pork')}
    policy=Mock();policy.catalogue.by_id.return_value=candidate;policy.source.return_value=facts
    assert choose_supported_occurrence_candidate(query,current,[{'collection':'test','candidate_id':'fried','blocked':False}],policy) is None


def test_no_match_recovery_requires_complete_input_identity(annotate):
    from recipe_wrangler.matching.source_suitability import choose_supported_occurrence_candidate
    query=recover_occurrence_facts('rice','rice, cooked',annotate)
    query['input_text']='rice cakes'  # A rice head alone does not identify the complete product.
    policy=Mock()
    assert choose_supported_occurrence_candidate(query,None,[],policy) is None
    policy.catalogue.by_id.assert_not_called()
