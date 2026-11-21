import pytest

from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary


@pytest.fixture
def parser():
    specs = [
        ('pork', ['pork'], 'meat'),
        ('lamb', ['lamb'], 'meat'),
        ('fat', ['fat'], 'generic'),
        ('shrimp food product', ['shrimp food product'], 'generic'),
        ('scallop food product', ['scallop food product'], 'generic'),
        ('egg', ['egg', 'eggs'], 'egg'),
        ('pasta', ['pasta', 'tagliatelle'], 'pasta'),
        ('mustard (prepared)', ['mustard (prepared)'], 'mustard'),
        ('milk', ['milk'], 'milk'),
        ('chicken breast', ['chicken breast'], 'meat'),
        ('chicken', ['chicken'], 'meat'),
        ('rice', ['rice'], 'cereal'),
        ('pineapple', ['pineapple'], 'generic'),
        ('peanut butter', ['peanut butter'], 'generic'),
    ]
    terms, families = [], {}
    for number, (label, aliases, family) in enumerate(specs):
        iri = f'test:{number}'
        terms.append({'term_id': iri, 'preferred_label': label, 'definition': '', 'curation_status': '',
                      'labels': [{'text': alias, 'type': 'preferred' if alias == label else 'exact_synonym'} for alias in aliases]})
        if family != 'generic':
            families[iri] = {family}
    return ContextualParser(IngredientDictionary({'schema_version': 1, 'provenance': {}, 'terms': terms}), families)


@pytest.mark.parametrize('text,identity', [('prawns', 'shrimp'), ('scallops', 'scallop')])
def test_plural_and_alias_lookup_retries_generic_ontology_suffix(parser, text, identity):
    result = parser.parse('raw ' + text)
    assert result['main_ingredient'] == identity
    assert len(result['foods']) == 1
    head = result['foods'][0]
    assert ('raw ' + text)[head['start']:head['end']] == text
    assert result['facets']['preparation_state'] == 'raw'


def test_normalized_heads_preserve_compound_foods(parser):
    result = parser.parse('prawns and egg')
    assert result['main_ingredient'] is None
    assert [head['identity'] for head in result['foods']] == ['shrimp', 'egg']


def test_source_and_part_are_attached_to_the_recognized_head(parser):
    egg = parser.parse('eggs, chicken, boiled')
    assert egg['main_ingredient'] == 'egg'
    assert egg['facets']['biological_source'] == 'chicken'
    assert egg['facets']['processes'] == ['boiled']
    meat = parser.parse('chicken breast')
    assert meat['main_ingredient'] == 'chicken'
    assert meat['facets']['food_part'] == 'breast'
    assert meat['facets']['biological_source'] == 'chicken'


def test_pasta_does_not_become_egg_white(parser):
    result = parser.parse('pasta, egg, white, tagliatelle, fresh, boiled in unsalted water')
    assert result['main_ingredient'] == 'pasta'
    assert result['facets']['food_part'] is None
    assert result['facets']['grain_refinement'] is None
    assert any(item['text'] == 'white' for item in result['unresolved'])
    assert result['grammar_status'] == 'partial_or_unresolved'


def test_wholegrain_is_scoped_by_food_family(parser):
    mustard = parser.parse('Mustard, wholegrain')
    assert mustard['main_ingredient'] == 'mustard'
    assert mustard['facets']['grain_refinement'] is None
    assert any(item['field'] == 'product_style' for item in mustard['modifiers'])
    assert parser.parse('wholegrain rice')['facets']['grain_refinement'] == 'wholegrain'


def test_whole_fat_does_not_mean_whole_physical_form(parser):
    result = parser.parse('Milk whole fat dried')
    assert result['facets']['fat_level'] == 'whole'
    assert result['facets']['physical_form'] is None
    assert result['facets']['preservation_methods'] == ['dried']


@pytest.mark.parametrize('phrase,status', [('eggs for frying', 'intended'), ('eggs, not boiled', 'negated')])
def test_non_actual_process_is_retained_but_not_asserted(parser, phrase, status):
    result = parser.parse(phrase)
    assert result['facets']['processes'] == []
    assert result['facets']['preparation_state'] is None
    event = next(item for item in result['modifiers'] if item['field'] == 'processes')
    assert event['polarity'] == status
    assert event['target_food_id'] == 'food:0'


def test_alternatives_keep_common_state_without_selecting_method(parser):
    result = parser.parse('boiled or fried eggs')
    assert result['facets']['processes'] == []
    assert result['facets']['preparation_state'] == 'cooked'
    assert {item['polarity'] for item in result['modifiers'] if item['field'] == 'processes'} == {'alternative'}


def test_additional_food_has_its_own_process_target(parser):
    result = parser.parse('eggs with fried chicken')
    assert result['main_ingredient'] is None
    assert result['facets']['biological_source'] is None
    assert result['facets']['processes'] == []
    assert result['facets']['compound'] is True
    fried = next(item for item in result['modifiers'] if item['value'] == 'fried')
    assert fried['target_food_id'] == 'food:1'
    assert len(result['foods']) == 2


def test_size_target_and_unknown_words_are_preserved(parser):
    result = parser.parse('large slice of pineapple')
    assert result['facets']['size'] == 'large'
    assert result['facets']['size_target'] == 'piece'
    unknown = parser.parse('mysterious eggs')
    assert any(item['text'] == 'mysterious' for item in unknown['unresolved'])
    assert unknown['grammar_status'] == 'partial_or_unresolved'


def test_unsupported_unknown_or_excluded_food_is_never_approved(parser):
    assert parser.parse('without milk')['main_ingredient'] is None
    unknown = parser.parse('unlisted mystery ingredient')
    assert unknown['main_ingredient'] is None
    assert unknown['unresolved']
    for phrase in ['eggs', 'chicken breast', 'without milk', 'unlisted mystery ingredient']:
        result = parser.parse(phrase)
        assert result['acceptance_status'] == 'proposal_only'
        assert result['production_eligible'] is False


def test_relation_spans_point_back_to_input(parser):
    text = 'large eggs, chicken, boiled'
    result = parser.parse(text)
    ids = {node['food_id'] for node in result['foods']}
    for modifier in result['modifiers']:
        assert modifier['target_food_id'] in ids
        assert text[modifier['start']:modifier['end']] == modifier['quote']
    with pytest.raises(ValueError):
        parser.parse(' ')


def test_process_before_other_noun_is_not_assigned_to_main_food(parser):
    result = parser.parse('milk mixed with boiling water')
    assert result['facets']['processes'] == []
    assert result['facets']['preparation_state'] is None
    assert 'process_attachment_unresolved' in result['review_reasons']
    boiling = next(item for item in result['modifiers'] if item['value'] == 'boiled')
    assert boiling['target_food_id'] is None
    assert boiling['polarity'] == 'unresolved'


@pytest.mark.parametrize('label,identity', [
    ('Pork, loin steaks, raw, lean and fat, trimmed', 'pork'),
    ('Pork, loin chops, raw, lean and fat, weighed with bone', 'pork'),
    ('Lamb, best end neck cutlets, raw, lean and fat, weighed with bone', 'lamb'),
])
def test_meat_fat_description_is_not_another_food(parser, label, identity):
    result = parser.parse(label)
    assert result['main_ingredient'] == identity
    assert len(result['foods']) == 1
    assert result['facets']['meat_fat_description'] == 'lean and fat'
    assert result['facets']['preparation_state'] == 'raw'


@pytest.mark.parametrize('label', ['pork and fat', 'pork with fat', 'pork, lean and fat, and eggs'])
def test_actual_added_foods_still_require_review(parser, label):
    result = parser.parse(label)
    assert result['main_ingredient'] is None
    assert 'multiple_food_heads' in result['review_reasons']


def test_conflicting_meat_fat_descriptions_are_not_resolved(parser):
    result = parser.parse('pork, lean and fat, extra lean')
    assert result['facets']['meat_fat_description'] is None
    assert 'conflicting_assertions:meat_fat_description' in result['review_reasons']


def test_literal_animal_without_ontology_family_keeps_meat_fat_scope(parser):
    parser.families.clear()
    result = parser.parse('Pork, loin steaks, raw, lean and fat, trimmed')
    assert result['main_ingredient'] == 'pork'
    assert result['facets']['food_part'] == 'loin'
    assert result['facets']['preparation_state'] == 'raw'


def test_frozen_meat_fat_recovery_is_auditable_and_does_not_mutate(parser, monkeypatch):
    from copy import deepcopy
    from recipe_wrangler.matching import anatomy
    from recipe_wrangler.matching.hybrid import HybridAnnotator
    text = 'Pork, loin steaks, raw, lean and fat, trimmed'
    old = parser.parse(text)
    old['main_ingredient'] = None
    old['foods'].append({'identity': 'fat', 'start': 33, 'end': 36})
    old['facets']['compound'] = True
    old['review_reasons'].append('multiple_food_heads')
    before = deepcopy(old)
    monkeypatch.setattr(anatomy, '_label_annotator', lambda: HybridAnnotator(parser))
    revised = anatomy.source_anatomy({'text_annotation': old})['text_annotation']
    assert revised['main_ingredient'] == 'pork'
    assert revised['label_evidence_recovery'][-1]['version'] == 'meat_fat_scope_recovery_v1'
    from recipe_wrangler.services.adaptation.comparison_store import _assertions
    assert _assertions(revised, 'test/annotation')['base_identity'].value == 'pork'
    assert old == before
    assert anatomy.source_anatomy({'text_annotation': revised})['text_annotation'] == revised
    old['review_reasons'].append('conflicting_assertions:preparation_state')
    assert anatomy.source_anatomy({'text_annotation': old})['text_annotation'] == old
