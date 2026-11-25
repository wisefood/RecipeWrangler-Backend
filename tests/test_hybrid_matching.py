import json
import hashlib
from pathlib import Path

import pytest

from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator, HybridMatcher, assess_compatibility
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary


@pytest.fixture
def matcher():
    terms, families = [], {}
    for i, (label, family) in enumerate([('egg','egg'),('chicken breast','meat'),
            ('milk','milk'),('pineapple','generic'),('mustard (prepared)','mustard'),
            ('rice','cereal'),('tomato paste','generic'),('tomato','generic'),
            ('mayonnaise','generic'),('cream cheese','generic'),('coconut milk','generic'),('soy sauce','generic')]):
        iri=f'test:{i}'
        terms.append({'term_id':iri,'preferred_label':label,'definition':'','curation_status':'',
                      'labels':[{'text':label,'type':'preferred'}]})
        if family!='generic': families[iri]={family}
    parser=ContextualParser(IngredientDictionary({'schema_version':1,'provenance':{},'terms':terms}),families)
    return HybridMatcher(HybridAnnotator(parser))


@pytest.mark.parametrize('query,candidate,relation',[
    ('eggs, chicken, whole, raw','eggs, duck, whole, raw','reject'),
    ('eggs, chicken, whole, raw','eggs, chicken, whole, boiled','reject'),
    ('eggs not boiled','eggs, boiled','reject'),
    ('eggs for frying','eggs, fried','needs_review'),
    ('boiled or fried eggs','eggs, boiled','needs_review'),
    ('eggs, chicken, whole, fried','eggs, chicken, whole, fried in butter','needs_review'),
    ('eggs, chicken, whole, fried without fat','eggs, chicken, whole, fried in butter','reject'),
    ('eggs, chicken, whole, boiled','eggs, chicken, whole, boiled','compatible_proposal'),
    ('chicken breast, raw, sliced','chicken breast, raw','compatible_proposal'),
    ('boneless chicken breast, raw','chicken breast with bone, raw','needs_review'),
    ('tomato paste','tomato, raw','reject'),
    ('milk, no added sugar','milk, sugar free','needs_review'),
    ('peas, fresh','peas','needs_review'),
    ('mystery ingredient','eggs','needs_review'),
])
def test_explicit_policy_contrasts(matcher,query,candidate,relation):
    actual=assess_compatibility(matcher.annotate(query),matcher.annotate(candidate))
    assert actual.relation==relation
    assert actual.rules


@pytest.mark.parametrize('text',['light mayonnaise','Mayonnaise, light, 30% fat','125g of light cream cheese',
    'Coconut milk, light, canned','lite coconut milk','half-fat mayonnaise','Mayonnaise, half fat'])
def test_light_and_half_fat_read_as_reduced_fat(matcher,text):
    assert matcher.annotator.annotate(text)['facets']['fat_level']=='reduced'


@pytest.mark.parametrize('text',['light soy sauce','light rice','tomato, cut in half'])
def test_light_and_half_elsewhere_say_nothing_about_fat(matcher,text):
    assert matcher.annotator.annotate(text)['facets']['fat_level'] is None


def test_size_is_portion_context_and_evidence_is_grounded(matcher):
    a=matcher.annotate('large slice of pineapple')
    assert a['facets']['size_target']=='piece'
    assert assess_compatibility(a,matcher.annotate('pineapple')).relation=='compatible_proposal'
    for item in a['modifiers']:
        assert a['parsed_text'][item['start']:item['end']]==item['quote']


@pytest.mark.parametrize('coating', ['breaded', 'in batter'])
def test_plain_cut_rejects_coated_product(matcher, coating):
    query = matcher.annotate('skinless chicken breast, raw')
    candidate = matcher.annotate('chicken breast, ' + coating + ', raw')
    result = assess_compatibility(query, candidate)
    assert result.relation == 'reject'
    assert any(r['rule_id'] == 'identity.anatomical_cut_vs_coated_product' for r in result.rules)


def test_abbreviated_skin_and_bone_evidence(matcher):
    a = matcher.annotate('chicken breast, raw w/o skin, bone-in')
    assert a['facets']['skin_status'] == 'absent'
    assert a['facets']['bone_status'] == 'present'
    for item in a['modifiers']:
        assert a['parsed_text'][item['start']:item['end']] == item['quote']


def test_process_outside_stated_alternatives_rejects(matcher):
    query = matcher.annotate('boiled or fried eggs')
    assert assess_compatibility(query, matcher.annotate('scrambled eggs')).relation == 'reject'
    assert assess_compatibility(query, matcher.annotate('boiled eggs')).relation != 'reject'
    assert assess_compatibility(query, matcher.annotate('eggs, sliced')).relation != 'reject'


@pytest.mark.parametrize('wording', ['without bone and skin', 'without skin and bone', 'w/o bone and skin'])
def test_joint_bone_and_skin_exclusion(matcher, wording):
    a = matcher.annotate('chicken breast, ' + wording + ', raw')
    assert a['facets']['skin_status'] == a['facets']['bone_status'] == 'absent'
    assert a['unresolved'] == []


def test_plain_boiling_water_does_not_add_matching_requirement(matcher):
    a, b = matcher.annotate('tomato, boiled in water'), matcher.annotate('tomato, boiled')
    assert a['facets']['processes'] == ['boiled']
    assert a['qualifiers']['cooking_medium'] == 'water'
    assert assess_compatibility(a, b).relation == 'compatible_proposal'
    assert assess_compatibility(b, a).relation == 'compatible_proposal'


def test_salt_in_boiling_water_still_matters(matcher):
    a = matcher.annotate('tomato, boiled in salted water')
    b = matcher.annotate('tomato, boiled in unsalted water')
    assert assess_compatibility(a, b).relation == 'reject'


def test_matcher_filters_before_proposing_and_never_approves(matcher):
    result=matcher.match('eggs, chicken, whole, boiled',[
        {'key':'wrong','label':'eggs, chicken, whole, raw','cosine':0.99},
        {'key':'right','label':'eggs, chicken, whole, boiled','cosine':0.8}])
    assert result['proposed_candidate_key']=='right'
    assert result['status']=='proposal'
    assert result['production_eligible'] is False
    assert matcher.match('eggs',[])['status']=='unresolved'


def test_inverted_composition_name_restores_original_evidence(matcher):
    text='Chicken, breast, raw'
    parsed=matcher.annotate(text)
    assert parsed['main_ingredient']=='chicken'
    assert parsed['facets']['food_part']=='breast'
    assert parsed['facets']['preparation_state']=='raw'
    assert parsed['evidence'][0]['quote'] in text
    assert assess_compatibility(matcher.annotate('chicken breast, raw'),parsed).relation=='compatible_proposal'
    assert matcher.annotate('eggs with fried chicken')['main_ingredient'] is None


def test_dev_fixture_remains_provisional_and_covers_hard_groups():
    p=Path(__file__).parent/'fixtures/matching/hybrid_dev_v1.json'
    data=json.loads(p.read_text())
    assert data['split']=='development_only'
    assert len(data['cases'])==len({c['case_id'] for c in data['cases']})==48
    assert len({c['group'] for c in data['cases']})==8
    assert all(c['label_status']=='provisional_assistant_authored_not_independently_reviewed' for c in data['cases'])
    assert hashlib.sha256(p.read_bytes()).hexdigest()=='c65bc9dc5eee6919e863f2b218e507d9b78bc2ea9c8946aedac201d4b44b42dd'
    events=json.loads(p.with_name('hybrid_dev_v1_events.json').read_text())['checks']
    by_id={c['case_id']:c for c in data['cases']}
    assert len(events)==9
    assert all(e['quote'] in by_id[e['case_id']]['text'] for e in events)


def test_exact_description_covers_missing_extraction_but_not_extra_words(matcher):
    result = matcher.match(' Milk whole fat dried ', [
        {'key': 'extra', 'label': 'Milk whole fat dried, fortified'},
        {'key': 'exact', 'label': 'milk whole fat dried'}])
    assert result['proposed_candidate_key'] == 'exact'
    assert result['match_method'] == 'exact_description'
    assert result['production_eligible'] is False


@pytest.mark.parametrize('text', ['eggs, raw, boiled', 'boiled or fried eggs', 'eggs for frying', 'eggs with fried chicken'])
def test_exact_description_does_not_override_uncertain_scope(matcher, text):
    result = matcher.match(text, [{'key': 'exact', 'label': text}])
    assert result['proposed_candidate_key'] is None


def test_exact_description_preserves_native_disagreement(matcher):
    text = 'eggs, chicken, whole, raw'
    a = matcher.annotate(text)
    matcher.composition_annotations = {'one': {'label': text, 'text_annotation': a,
        'facets': a['facets'], 'native_facets': {}, 'issues': ['source_label_disagreement:preparation_state'],
        'native_evidence': [], 'metadata': {}}}
    result = matcher.match(text, [{'key': 'exact', 'label': text, 'records': [{'catalogue_key': 'one'}]}])
    assert result['proposed_candidate_key'] is None


def test_exact_description_native_powder_can_cover_missing_query_form(matcher):
    text = 'Milk whole fat dried'
    a = matcher.annotate(text)
    facets = dict(a['facets'], physical_form='powder')
    matcher.composition_annotations = {'one': {'label': text, 'text_annotation': a,
        'facets': facets, 'native_facets': {'physical_form': 'powder'}, 'issues': [],
        'native_evidence': [], 'metadata': {}}}
    result = matcher.match(text, [{'key': 'exact', 'label': text, 'records': [{'catalogue_key': 'one'}]}])
    assert result['match_method'] == 'exact_description'
    assert result['proposed_record_keys'] == ['one']
    rules = result['candidates'][0]['compatibility']['rules']
    assert any(r['relation'] == 'covered_by_exact_description' for r in rules)


def test_exact_normalization_preserves_meaning():
    from recipe_wrangler.matching.hybrid import exact_description_match
    assert exact_description_match(' MILK , 1% fat. ', 'milk,1% fat')
    for other in ['milk, 10% fat', 'milk, 1% fat, raw', '1% fat milk', 'milk, no 1% fat']:
        assert not exact_description_match('milk, 1% fat', other)


def test_identical_labels_with_conflicting_native_variants_need_review(matcher):
    text = 'chicken breast'
    a = matcher.annotate(text)
    matcher.composition_annotations = {state: {'label': text, 'text_annotation': a,
        'facets': dict(a['facets'], preparation_state=state),
        'native_facets': {'preparation_state': state}, 'issues': [],
        'native_evidence': [], 'metadata': {}} for state in ['raw', 'cooked']}
    result = matcher.match(text, [{'key': 'exact', 'label': text,
        'records': [{'catalogue_key': state} for state in ['raw', 'cooked']]}])
    assert result['proposed_candidate_key'] is None
