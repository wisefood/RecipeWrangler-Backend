import pytest

from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator, HybridMatcher, assess_compatibility
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary


@pytest.fixture
def annotator():
    names = ['milk', 'yogurt', 'bread', 'rice', 'chocolate', 'vanilla', 'strawberry',
             'almond', 'peanut', 'chocolate milk', 'milk chocolate', 'almond milk']
    terms = [{'term_id': name, 'preferred_label': name, 'definition': '', 'curation_status': '',
              'labels': [{'text': name, 'type': 'preferred'}]} for name in names]
    parser = ContextualParser(IngredientDictionary({'schema_version': 1, 'provenance': {}, 'terms': terms}),
                              {'milk': {'milk'}})
    return HybridAnnotator(parser)


@pytest.mark.parametrize('base,flavour', [('milk','chocolate'), ('yogurt','strawberry'),
                                        ('rice','vanilla'), ('bread','almond')])
def test_flavour_grammar_applies_across_foods(annotator, base, flavour):
    a = annotator.annotate(f'{base}, {flavour} flavour')
    assert a['main_ingredient'] == base
    assert a['qualifiers']['flavours'] == [flavour]
    assert a['qualifiers']['flavour_status'] == ['flavoured']
    event = next(m for m in a['modifiers'] if m['field'] == 'qualifiers.flavours')
    assert event['target_food_id'] == 'food:0'
    assert a['parsed_text'][event['start']:event['end']] == event['quote']
    assert event['concept_candidates'][0]['term_id'] == flavour


@pytest.mark.parametrize('text', ['strawberry flavoured yogurt', 'yogurt, strawberry flavor',
                                 'yogurt flavoured with strawberry'])
def test_prefix_and_suffix_flavours_keep_the_product_head(annotator, text):
    a = annotator.annotate(text)
    assert a['main_ingredient'] == 'yogurt'
    assert len(a['foods']) == 1
    assert a['qualifiers']['flavours'] == ['strawberry']


@pytest.mark.parametrize('base,flavour', [('milk','chocolate'), ('yogurt','strawberry'), ('rice','vanilla')])
def test_generic_identity_ranks_above_matching_claim_on_flavoured_product(annotator, base, flavour):
    matcher = HybridMatcher(annotator)
    result = matcher.match(f'{base}, no added sugar', [
        {'key': 'flavoured', 'label': f'{base}, {flavour} flavour, no added sugar', 'cosine': .99},
        {'key': 'generic', 'label': base, 'cosine': .4}])
    assert result['review_candidate_key'] == 'generic'
    assert result['candidates'][0]['key'] == 'generic'
    assert result['status'] == 'needs_review'
    assert result['proposed_candidate_key'] is None
    assert result['candidates'][1]['ranking']['additional_identity_properties']


def test_requested_flavour_is_preserved_and_different_flavour_rejects(annotator):
    query = annotator.annotate('yogurt, vanilla flavour')
    assert assess_compatibility(query, annotator.annotate('yogurt, strawberry flavour')).relation == 'reject'
    assert assess_compatibility(query, annotator.annotate('yogurt')).relation == 'needs_review'
    result = HybridMatcher(annotator).match('yogurt, vanilla flavour', [
        {'key':'generic', 'label':'yogurt'}, {'key':'vanilla', 'label':'yogurt, vanilla flavour'}])
    assert result['proposed_candidate_key'] == 'vanilla'


def test_explicit_unflavoured_is_different_from_missing_information(annotator):
    generic = annotator.annotate('yogurt')
    plain = annotator.annotate('yogurt, unflavoured')
    flavoured = annotator.annotate('yogurt, vanilla flavour')
    assert assess_compatibility(generic, flavoured).relation == 'needs_review'
    assert assess_compatibility(plain, flavoured).relation == 'reject'


def test_flavour_exclusion_is_not_positive_presence(annotator):
    a = annotator.annotate('milk, without vanilla flavour')
    assert a['qualifiers'].get('flavours') is None
    assert a['qualifiers']['excluded_flavours'] == ['vanilla']
    assert assess_compatibility(a, annotator.annotate('milk, vanilla flavour')).relation == 'reject'


def test_no_added_flavour_is_not_unflavoured(annotator):
    a = annotator.annotate('milk, no added flavour')
    assert a['qualifiers']['flavour_status'] == ['no_added_flavour']
    assert assess_compatibility(a, annotator.annotate('milk, vanilla flavour')).relation == 'needs_review'


@pytest.mark.parametrize('text,polarity', [('milk for vanilla flavour', 'intended'),
    ('milk, vanilla or strawberry flavour', 'alternative')])
def test_flavour_scope_does_not_invent_current_product(annotator, text, polarity):
    a = annotator.annotate(text)
    assert not a['qualifiers'].get('flavours')
    assert any(m['field'] == 'qualifiers.flavours' and m['polarity'] == polarity for m in a['modifiers'])


def test_different_positive_ingredient_mentions_do_not_prove_absence(annotator):
    a = annotator.annotate('bread, contains almond')
    b = annotator.annotate('bread, contains peanut')
    assert assess_compatibility(a, b).relation == 'needs_review'


@pytest.mark.parametrize('text', ['milk, not flavoured with vanilla', 'milk, without flavour of vanilla'])
def test_postposed_flavour_exclusion_is_not_presence(annotator, text):
    a = annotator.annotate(text)
    assert not a['qualifiers'].get('flavours')
    assert a['qualifiers']['excluded_flavours'] == ['vanilla']


def test_negative_ingredient_marker_is_not_presence(annotator):
    a = annotator.annotate('bread, does not contain almond')
    assert not a['qualifiers'].get('added_ingredients')
    assert a['qualifiers']['excluded_ingredients'] == ['almond']
    assert assess_compatibility(a, annotator.annotate('bread')).relation == 'needs_review'
    assert assess_compatibility(a, annotator.annotate('bread, contains almond')).relation == 'reject'


def test_alternative_addition_is_not_asserted(annotator):
    a = annotator.annotate('bread, contains almond or peanut')
    assert not a['qualifiers'].get('added_ingredients')
    assert any(m['polarity'] == 'alternative' for m in a['modifiers'])


def test_dictionary_plural_normalization_applies_to_exclusions(annotator):
    a = annotator.annotate('bread, contains no almonds')
    b = annotator.annotate('bread, contains almond')
    assert a['qualifiers']['excluded_ingredients'] == ['almond']
    assert assess_compatibility(a, b).relation == 'reject'


@pytest.mark.parametrize('base,addition', [('bread','almond'), ('rice','peanut'), ('yogurt','vanilla')])
def test_explicit_additions_use_dictionary_names(annotator, base, addition):
    a = annotator.annotate(f'{base}, contains {addition}')
    assert a['qualifiers']['added_ingredients'] == [addition]
    assert a['product_identity']['refinements'][0]['concept_candidates']


def test_named_products_are_not_collapsed_to_a_shared_word(annotator):
    a = annotator.annotate('chocolate milk')
    b = annotator.annotate('milk chocolate')
    c = annotator.annotate('almond milk')
    assert a['main_ingredient'] == 'chocolate milk'
    assert b['main_ingredient'] == 'milk chocolate'
    assert c['main_ingredient'] == 'almond milk'
    assert assess_compatibility(a, b).relation != 'compatible_proposal'


def test_multiple_foods_are_not_rewritten_as_flavour(annotator):
    a = annotator.annotate('milk with bread')
    assert len(a['foods']) == 2
    assert a['main_ingredient'] is None
    assert not a['qualifiers'].get('flavours')
