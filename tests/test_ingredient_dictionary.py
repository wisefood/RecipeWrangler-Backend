import csv
import json
from pathlib import Path

import pytest

from recipe_wrangler.matching.ingredient_dictionary import (
    FOOD_MATERIAL, IngredientDictionary, build_dictionary,
)

PREFIX = 'http://purl.obolibrary.org/obo/'


@pytest.fixture
def dictionary(tmp_path):
    terms = [
        ('FOODON_00001274', 'egg food product', 'false'),
        ('FOODON_02020001', 'animal egg white', 'false'),
        ('FOODON_02010009', 'animal egg yolk', 'false'),
        ('FOODON_TEST1', 'chicken egg', 'false'),
        ('FOODON_TEST2', 'coconut milk', 'false'),
        ('FOODON_TEST3', 'peanut butter', 'false'),
        ('FOODON_03301946', 'baked beans (dish)', 'false'),
        ('FOODON_TEST5', 'old food', 'true'),
        ('FOODON_TEST6', 'boiling process', 'false'),
        ('FOODON_TEST7', 'chicken breast', 'false'),
        ('FOODON_TEST8', 'chicken breast (raw)', 'false'),
        ('FOODON_TEST9', 'eggplant', 'false'),
    ]
    def write(name, fields, rows):
        with (tmp_path / name).open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)
    write('foodon_terms.csv', ['iri', 'label', 'definition', 'deprecated', 'curation_status'],
          [(PREFIX + code, label, 'Source definition', deprecated, '') for code, label, deprecated in terms])
    write('foodon_named_parents.csv', ['child_iri', 'parent_iri'],
          [(PREFIX + code, FOOD_MATERIAL) for code, _, _ in terms if code not in {'FOODON_TEST6', 'FOODON_02020001', 'FOODON_02010009'}])
    write('foodon_synonyms.csv', ['iri', 'type', 'label', 'language'], [
        (PREFIX + 'FOODON_TEST1', 'hasBroadSynonym', 'egg', 'en'),
        (PREFIX + 'FOODON_TEST1', 'hasRelatedSynonym', 'breakfast', 'en'),
        (PREFIX + 'FOODON_03301946', 'hasExactSynonym', 'baked beans', 'en'),
        (PREFIX + 'FOODON_TEST8', 'hasExactSynonym', 'chicken breast', 'en'),
        (PREFIX + 'FOODON_TEST9', 'hasExactSynonym', 'aubergine', 'en'),
        (PREFIX + 'FOODON_TEST9', 'hasExactSynonym', 'berenjena', 'es'),
    ])
    (tmp_path / 'inventory_summary.json').write_text(json.dumps({'snapshot': 'test'}))
    return build_dictionary(tmp_path)


def test_egg_identity_and_facets_are_separate(dictionary):
    result = dictionary.extract('eggs, chicken, boiled')
    assert result['main_ingredient'] == 'egg'
    assert result['identity_status'] == 'resolved'
    assert result['facets']['biological_source'] == 'chicken'
    assert result['facets']['preparation_state'] == 'cooked'
    assert 'boiled' in result['facets']['processes']
    assert result['concepts'][0]['term_id'] == PREFIX + 'FOODON_00001274'
    assert result['original_text'] == 'eggs, chicken, boiled'
    assert len(result['dictionary_provenance']['files']['foodon_terms.csv']) == 64


def test_broad_synonym_does_not_infer_species(dictionary):
    result = dictionary.extract('eggs')
    assert result['facets']['biological_source'] is None
    assert [h['preferred_label'] for h in result['concepts']] == ['egg food product']
    assert dictionary.lookup('breakfast') == ()


@pytest.mark.parametrize('phrase,identity', [
    ('coconut milk', 'coconut milk'), ('baked beans', 'baked bean'),
    ('egg white', 'egg white'), ('egg yolk', 'egg yolk'),
    ('roasted peanut butter', 'peanut butter'),
])
def test_complete_food_names_and_order(dictionary, phrase, identity):
    result = dictionary.extract(phrase)
    assert result['main_ingredient'] == identity
    assert result['identity_status'] == 'resolved'


def test_ambiguity_is_retained(dictionary):
    result = dictionary.extract('chicken breast')
    assert result['identity_status'] == 'ambiguous'
    assert len(result['concepts']) == 2


def test_unknown_words_are_not_discarded(dictionary):
    result = dictionary.extract('mysterious tuber, boiled')
    assert result['main_ingredient'] == 'mysterious tuber'
    assert result['identity_status'] == 'unresolved'
    assert result['concepts'] == []


def test_filters_and_exact_synonyms(dictionary):
    assert dictionary.lookup('aubergine')[0].preferred_label == 'eggplant'
    assert dictionary.lookup('berenjena') == ()
    assert dictionary.lookup('old food') == ()
    assert dictionary.lookup('boiling process') == ()


@pytest.mark.parametrize('phrase', ['not fried eggs', 'eggs for frying', 'eggs or beans'])
def test_context_requires_review(dictionary, phrase):
    assert dictionary.extract(phrase)['identity_status'] == 'needs_context_review'


def test_roundtrip(dictionary, tmp_path):
    path = tmp_path / 'dictionary.json'
    path.write_text(json.dumps(dictionary.payload))
    assert IngredientDictionary.load(path).extract('eggs') == dictionary.extract('eggs')
    with pytest.raises(ValueError):
        dictionary.extract(' ')
