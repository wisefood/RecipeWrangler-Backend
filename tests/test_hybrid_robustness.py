from types import SimpleNamespace

import pytest

from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary
from recipe_wrangler.matching.hybrid import HybridAnnotator, HybridMatcher, assess_compatibility
from recipe_wrangler.matching.normalization import normalized_value
from recipe_wrangler.matching.composition_annotation import annotate_composition
from recipe_wrangler.matching.models import FoodFacets
from dataclasses import asdict


@pytest.fixture
def annotator():
    names = [('milk', 'milk'), ('skimmed milk', 'milk'), ('milk powder', 'milk'),
        ('tuna food product', 'generic'), ('in brine', 'generic'), ('pasta', 'pasta'),
        ('tagliatelle', 'pasta'), ('sweetcorn', 'cereal'), ('rice', 'cereal'), ('egg', 'egg'), ('ginger', 'generic'), ('ground ginger', 'generic'), ('fat', 'generic')]
    terms = [{'term_id': str(i), 'preferred_label': name, 'definition': '', 'curation_status': '',
              'labels': [{'text': name, 'type': 'preferred'}]} for i, (name, _) in enumerate(names)]
    parser = ContextualParser(IngredientDictionary({'schema_version': 1, 'provenance': {}, 'terms': terms}),
                              {str(i): {family} for i, (_, family) in enumerate(names)})
    return HybridAnnotator(parser)


@pytest.mark.parametrize('value', ['1% fat', '1% FAT', '1 % fat', '1.0% fat', 'fat 1,00%'])
def test_percent_normalization(value):
    assert normalized_value('fat_content_description', value) == '1% fat'
    assert normalized_value('fat_content_description', value) != normalized_value('fat_content_description', '1.5% fat')


@pytest.mark.parametrize('label', ['milk, 1% FAT', 'MILK, 1 % fat', 'milk, fat 1,0%'])
def test_equal_percentages_do_not_reject(annotator, label):
    query, candidate = annotator.annotate('milk, 1% fat'), annotator.annotate(label)
    assert candidate['facets']['fat_content_description'] == '1% fat'
    assert assess_compatibility(query, candidate).relation == 'compatible_proposal'
    for event in candidate['modifiers']:
        assert candidate['parsed_text'][event['start']:event['end']] == event['quote']


def test_different_percentages_reject(annotator):
    assert assess_compatibility(annotator.annotate('milk, 1% fat'), annotator.annotate('milk, 1.5%')).relation == 'reject'


@pytest.mark.parametrize('text', ['tuna, canned in brine, drained', 'TUNA, CANNED IN BRINE, DRAINED',
                                 'tuna canned in brine drained'])
def test_packing_is_not_food_head(annotator, text):
    result = annotator.annotate(text)
    assert result['main_ingredient'] == 'tuna'
    assert result['facets']['packing_medium'] == 'brine'
    assert result['facets']['drainage_status'] == 'drained'
    assert result['unresolved'] == []
    expected = asdict(FoodFacets(base_identity='tuna', packing_medium='brine',
        preservation_methods=frozenset({'canned'}), processes=frozenset({'drained'}), drainage_status='drained'))
    expected['processes'], expected['preservation_methods'] = ['drained'], ['canned']
    expected['biological_source'] = 'tuna'
    assert result['facets'] == expected


def test_fat_class_is_separate_from_identity(annotator):
    a, b = annotator.annotate('skimmed milk'), annotator.annotate('milk skimmed')
    assert a['main_ingredient'] == b['main_ingredient'] == 'milk'
    assert assess_compatibility(a, b).relation == 'compatible_proposal'


def test_partly_skimmed_is_not_skimmed(annotator):
    assert annotator.annotate('milk, partly skimmed')['facets']['fat_level'] == 'semi_skimmed'


def test_reconstitution_preserves_starting_form_without_asserting_final_powder(annotator):
    a = annotator.annotate('milk powder, reconstituted')
    assert a['facets']['physical_form'] is None
    assert a['facets']['reconstitution_state'] == 'reconstituted'
    assert a['qualifiers']['starting_physical_form'] == 'powder'
    assert not any(m['field'] == 'physical_form' and m['value'] == 'powder' for m in a['modifiers'])
    assert any(m['field'] == 'qualifiers.starting_physical_form' for m in a['modifiers'])
    assert 'reconstituted_final_form_unspecified' in a['review_reasons']


def test_maize_part_is_consumed(annotator):
    a = annotator.annotate('Sweetcorn kernels, canned in water, drained')
    assert a['facets']['food_part'] == 'kernel'
    assert a['unresolved'] == []


def test_pasta_properties_have_separate_targets_from_egg_anatomy(annotator):
    a = annotator.annotate('Pasta, egg, white, tagliatelle, fresh, boiled in unsalted water')
    assert a['main_ingredient'] == 'pasta'
    assert a['facets']['food_part'] is None
    assert a['facets']['biological_source'] is None
    assert a['facets']['colour'] == 'white'
    assert a['qualifiers']['contains_egg'] is True
    assert a['qualifiers']['pasta_shape'] == 'tagliatelle'
    assert a['qualifiers']['cooking_medium_added_salt'] is False
    assert a['unresolved'] == []


def test_pasta_egg_exclusion_is_not_presence(annotator):
    a = annotator.annotate('pasta without egg')
    assert a['qualifiers']['contains_egg'] is False
    assert assess_compatibility(a, annotator.annotate('pasta, egg')).relation == 'reject'


def test_exact_generic_description_keeps_exact_candidate(annotator):
    result = HybridMatcher(annotator).match('eggs', [
        {'key': 'exact', 'label': 'eggs', 'retrieval_reason': 'exact_label_and_vector'},
        {'key': 'vector', 'label': 'eggs, boiled', 'retrieval_reason': 'vector'}])
    assert result['match_method'] == 'exact_description'
    assert result['proposed_candidate_key'] == 'exact'
    assert result['facet_validation'] == 'provisional_checks_passed'


def test_native_powder_is_used_for_each_source(annotator):
    catalogue = SimpleNamespace(version='test', sha256='hash')
    stored = annotate_composition({'source': {}, 'catalogue_key': 'powder',
        'label': 'milk', 'metadata': {'Physical state FoodEx2': 'Powder'}}, annotator, catalogue)
    result = HybridMatcher(annotator, {'powder': stored}).match('milk', [
        {'key': 'milk', 'label': 'milk', 'records': [{'catalogue_key': 'powder'}]}])
    candidate = result['candidates'][0]
    assert candidate['annotation']['facets']['physical_form'] == 'powder'
    assert any(r['rule_id'] == 'property.physical_form' for r in candidate['compatibility']['rules'])


def test_native_powder_cannot_become_reconstituted_final_form(annotator):
    stored = annotate_composition({'source': {}, 'label': 'milk powder, reconstituted',
        'metadata': {'Physical state FoodEx2': 'Powder'}}, annotator, SimpleNamespace(version='test', sha256='hash'))
    assert stored['facets']['physical_form'] is None
    assert 'native_physical_form_basis_unspecified' in stored['issues']


def test_salt_contradiction_rejects(annotator):
    a = annotator.annotate('rice, cooked without added salt')
    b = annotator.annotate('rice, cooked with salt')
    assert assess_compatibility(a, b).relation == 'reject'
    assert annotator.annotate('rice, boiled WITHOUT SALT')['facets']['sodium_level'] == 'no_added_salt'


def test_fallback_is_explicit_and_unverified(annotator):
    result = HybridMatcher(annotator).match('unknown food', [
        {'key': '1', 'label': 'milk', 'retrieval_reason': 'vector', 'cosine': .8}])
    assert result['status'] == 'fallback_proposal'
    assert result['proposed_candidate_key'] == '1'
    assert result['match_method'] == 'embedding_fallback'
    assert result['facet_validation'] == 'unverified'
    assert result['notes']
    assert result['production_eligible'] is False


@pytest.mark.parametrize('text', ['eggs not boiled', 'eggs for frying', 'eggs, raw, boiled', 'milk, 1% fat'])
def test_fallback_cannot_override_constraints(annotator, text):
    result = HybridMatcher(annotator).match(text, [
        {'key': '1', 'label': 'milk, 1.5% fat', 'retrieval_reason': 'vector'}])
    assert result['match_method'] != 'embedding_fallback'


def test_fallback_cannot_drop_matching_qualifiers(annotator):
    result = HybridMatcher(annotator).match('pasta, egg', [
        {'key': 'pasta', 'label': 'pasta', 'retrieval_reason': 'vector'}])
    assert result['status'] == 'needs_review'
    assert result['proposed_candidate_key'] is None


def test_native_variant_conflicts_do_not_leak_to_other_variants(annotator):
    catalogue = SimpleNamespace(version='test', sha256='hash', resolve=lambda _: SimpleNamespace(issues=(), terms=()))
    def record(key, metadata):
        return annotate_composition({'catalogue_key': key, 'source': {}, 'label': 'eggs, chicken, whole, raw',
            'metadata': metadata}, annotator, catalogue)
    records = {'good': record('good', {}), 'conflict': record('conflict', {'FoodEx2 code': 'A032B'})}
    result = HybridMatcher(annotator, records).match('eggs, chicken, whole, raw', [
        {'key': 'group', 'label': 'eggs, chicken, whole, raw',
         'records': [{'catalogue_key': key} for key in records]}])
    assert result['proposed_record_keys'] == ['good']
    assert result['candidates'][0]['source_assessments'][1]['compatibility']['relation'] == 'needs_review'


def test_ground_ginger_word_order_preserves_identity_and_process(annotator):
    query = annotator.annotate('ground ginger')
    candidate = annotator.annotate('Ginger, ground')
    assert query['main_ingredient'] == candidate['main_ingredient'] == 'ginger'
    assert query['facets']['processes'] == candidate['facets']['processes'] == ['ground']
    assert assess_compatibility(query, candidate).relation == 'compatible_proposal'


@pytest.mark.parametrize('label', ['low-fat milk', 'low fat milk', 'milk, low fat'])
def test_low_fat_milk_retains_constraint_and_rejects_zero_fat(annotator, label):
    query = annotator.annotate(label)
    assert query['main_ingredient'] == 'milk'
    assert query['facets']['fat_level'] == 'low'
    assert query['unresolved'] == []
    zero = annotator.annotate('milk, 0% fat')
    assert assess_compatibility(query, zero).relation == 'reject'
    assert assess_compatibility(zero, query).relation == 'reject'
    assert assess_compatibility(query, annotator.annotate('milk, low fat')).relation == 'compatible_proposal'


def test_low_fat_matcher_prefers_matching_fat_to_zero(annotator):
    result = HybridMatcher(annotator).match('low-fat milk', [
        {'key': 'zero', 'label': 'milk, 0% fat', 'retrieval_reason': 'vector'},
        {'key': 'low', 'label': 'milk, low fat', 'retrieval_reason': 'vector'},
    ])
    assert result['proposed_candidate_key'] == 'low'
    assert next(c for c in result['candidates'] if c['key'] == 'zero')['compatibility']['relation'] == 'reject'


def test_low_fat_and_semi_skimmed_are_not_a_certain_contradiction(annotator):
    query = annotator.annotate('low-fat milk')
    candidate = annotator.annotate('milk, partly skimmed')
    assert assess_compatibility(query, candidate).relation == 'needs_review'
