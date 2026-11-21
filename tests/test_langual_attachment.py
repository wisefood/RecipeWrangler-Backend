import pytest

from recipe_wrangler.matching.langual import LangualVocabulary
from recipe_wrangler.matching.hybrid import HybridAnnotator, assess_compatibility
from recipe_wrangler.matching.facet_vocabulary import FacetVocabulary
from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary


def annotator():
    langual = LangualVocabulary({'version': '2017', 'terms': [
        {'code': 'C0170', 'facet': 'C', 'label': 'BROTH OR STOCK', 'active': True}]})
    vocabulary = FacetVocabulary({'catalogue_version': 'test', 'facets': [
        {'code': 'source', 'terms': [{'code': 'A057Z', 'label': 'Gallus gallus (chicken) (as animal)',
                                    'status': 'APPROVED', 'parent_codes': []}]}]})
    dictionary = IngredientDictionary({'schema_version': 1, 'provenance': {}, 'terms': [
        {'term_id': 'test:chicken', 'preferred_label': 'chicken', 'definition': '',
         'curation_status': '', 'labels': [{'text': 'chicken', 'type': 'preferred'}]}]})
    parser = ContextualParser(dictionary, {'test:chicken': {'meat'}})
    return HybridAnnotator(parser, vocabulary, langual=langual)


@pytest.mark.parametrize('text', ['chicken stock', 'chicken broth', 'Stock, chicken',
                                 'Chicken stock RTE', 'Stock, chicken, ready made, retail'])
def test_stock_source_and_descriptor_are_linked(text):
    result = annotator().annotate(text)
    assert result['main_ingredient'] == 'chicken stock'
    assert result['facets']['biological_source'] == 'chicken'
    assert result['facets']['derived_substance'] == 'broth_or_stock'
    assert result['facets']['food_part'] is None
    assert result['facets']['preparation_state'] is None
    assert result['facets']['physical_form'] is None
    event = next(m for m in result['modifiers'] if m.get('langual_code') == 'C0170')
    assert text[event['start']:event['end']] == event['quote']
    assert event['target_food_id'] == 'food:0'
    assert not any(r == 'unknown_food_head' for r in result['review_reasons'])
    if 'RTE' in text:
        assert any(u['text'] == 'RTE' for u in result['unresolved'])


@pytest.mark.parametrize('text', ['chicken stock cubes', 'chicken stock powder',
    'chicken stock paste', 'chicken stock concentrate', 'eggs with chicken stock',
    'not chicken stock', 'chicken stock or duck stock', 'chicken for stock',
    'chicken stock with vegetables', 'chicken stock, dehydrated'])
def test_no_plain_stock_assertion_for_concentrates_or_unsupported_scope(text):
    assert annotator().annotate(text)['facets']['derived_substance'] is None


def test_generic_stock_does_not_invent_animal_source():
    result = annotator().annotate('stock')
    assert result['main_ingredient'] == 'stock'
    assert result['facets']['biological_source'] is None
    assert result['facets']['derived_substance'] == 'broth_or_stock'


def test_stock_and_meat_reject_but_stock_and_broth_agree():
    a = annotator()
    stock = a.annotate('chicken stock')
    assert assess_compatibility(stock, a.annotate('chicken breast, raw')).relation == 'reject'
    assert assess_compatibility(stock, a.annotate('chicken broth')).relation == 'compatible_proposal'


def test_unexpected_descriptor_release_fails_visibly():
    with pytest.raises(ValueError):
        LangualVocabulary({'version': 'test', 'terms': [
            {'code': 'C0170', 'facet': 'C', 'label': 'changed', 'active': True}]})
