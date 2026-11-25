from types import SimpleNamespace

import pytest

from recipe_wrangler.matching.composition_annotation import annotate_composition
from recipe_wrangler.matching.facet_vocabulary import FacetVocabulary
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary


@pytest.fixture
def annotator():
    dictionary = IngredientDictionary({'schema_version': 1, 'provenance': {}, 'terms': [
        {'term_id': 'egg', 'preferred_label': 'egg', 'definition': '', 'curation_status': '',
         'labels': [{'text': 'egg', 'type': 'preferred'}]}]})
    vocabulary = FacetVocabulary({'catalogue_version': 'test', 'facets': [
        {'code': 'process', 'terms': [
            {'code': code, 'label': label, 'status': 'APPROVED', 'parent_codes': []}
            for code, label in [('A07GL', 'Boiling'), ('A07GJ', 'Simmering')]]}]})
    return HybridAnnotator(ContextualParser(dictionary, {'egg': {'egg'}}), vocabulary)


class Catalogue:
    version, sha256 = 'test', 'testhash'

    def resolve(self, expression):
        return SimpleNamespace(issues=(), terms=())


@pytest.mark.parametrize('text,polarity,asserted', [
    ('eggs, simmered', 'asserted', True), ('eggs not simmered', 'negated', False),
    ('eggs for simmering', 'intended', False),
    ('eggs with simmering water', 'unresolved', False)])
def test_process_normalization_keeps_scope(annotator, text, polarity, asserted):
    result = annotator.annotate(text)
    events = [m for m in result['modifiers'] if m.get('foodex2_code') == 'A07GJ']
    assert len(events) == 1
    assert events[0]['polarity'] == polarity
    assert ('simmered' in result['facets']['processes']) is asserted
    assert text[events[0]['start']:events[0]['end']] == events[0]['quote']


def test_native_conflict_is_unknown_and_preserves_both_assertions(annotator):
    source = {'dataset': 'test', 'record_id': '1', 'release': '1'}
    record = {'source': source, 'catalogue_key': 'variant:1', 'label': 'eggs, raw',
              'metadata': {'FoodEx2 code': 'A031G#F28.A07GL'}}
    result = annotate_composition(record, annotator, Catalogue())
    assert result['catalogue_key'] == 'variant:1'
    assert result['text_annotation']['facets']['preparation_state'] == 'raw'
    assert result['native_facets']['preparation_state'] == 'cooked'
    assert result['facets']['preparation_state'] is None
    assert result['facets']['processes'] == []
    assert 'source_label_disagreement:preparation_state' in result['issues']
    assert not result['production_eligible']


def test_native_code_does_not_override_negation(annotator):
    result = annotate_composition({'source': {}, 'label': 'eggs not boiled',
        'metadata': {'FoodEx2 code': 'A032B'}}, annotator, Catalogue())
    assert 'source_label_negated_process' in result['issues']
    assert result['facets']['processes'] == []
    assert result['facets']['preparation_state'] is None


def test_unmapped_native_descriptor_remains_visible(annotator):
    result = annotate_composition({'source': {}, 'label': 'eggs',
        'metadata': {'FoodEx2 code': 'A031G#F02.A069M'}}, annotator, Catalogue())
    assert result['facets']['food_part'] is None
    assert 'unprojected_native_descriptor:F02.A069M' in result['issues']
    assert result['native_evidence'][0]['expression'] == 'A031G#F02.A069M'


def test_missing_properties_preserve_record_for_later_retrieval(annotator):
    result = annotate_composition({'source': {'record_id': 'unknown:1'},
        'catalogue_key': 'variant:unknown', 'label': 'unrecognized product',
        'metadata': {}}, annotator, Catalogue())
    assert result['label'] == 'unrecognized product'
    assert result['catalogue_key'] == 'variant:unknown'
    assert result['facets']['preparation_state'] is None
    assert result['facets']['processes'] == []
    assert result['text_annotation']['main_ingredient'] is None
    assert result['review_required']
