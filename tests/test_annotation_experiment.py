import pytest

from recipe_wrangler.matching.evaluation.annotation_experiment import (
    compare_annotations, prepare_phrase, validate_annotation,
)


def annotation():
    return {
        'main_ingredient': 'egg',
        'facets': {'biological_source': 'chicken', 'processes': ['boiled']},
        'context': {}, 'unresolved': [],
        'evidence': [
            {'field': 'main_ingredient', 'quote': 'eggs', 'polarity': 'asserted'},
            {'field': 'facets.biological_source', 'quote': 'chicken', 'polarity': 'asserted'},
            {'field': 'facets.processes', 'quote': 'boiled', 'polarity': 'asserted'},
        ],
    }


def test_span_validation_and_unknown_fields():
    text = 'eggs, chicken, boiled'
    result = validate_annotation(annotation(), text)
    assert result['evidence'][1]['spans'] == [{'start': 6, 'end': 13}]
    bad = annotation()
    bad['evidence'][1]['quote'] = 'duck'
    with pytest.raises(ValueError, match='substring'):
        validate_annotation(bad, text)
    bad = annotation()
    bad['facets']['invented_facet'] = 'something'
    with pytest.raises(ValueError, match='Unknown facets'):
        validate_annotation(bad, text)


def test_claims_need_asserted_evidence():
    raw = annotation()
    raw['evidence'][1]['polarity'] = 'negated'
    with pytest.raises(ValueError, match='lack supporting spans'):
        validate_annotation(raw, 'eggs, chicken, boiled')


def test_field_comparison_distinguishes_missing_wrong_and_unsupported():
    cases = [{'case_id': 'a', 'kind': 'controlled', 'expected': {
        'main_ingredient': 'egg', 'facets.biological_source': None,
        'facets.size': 'large', 'facets.preparation_state': 'cooked',
    }}]
    predictions = [{'case_id': 'a', 'annotation': {
        'main_ingredient': 'eggs', 'facets': {'biological_source': 'chicken', 'preparation_state': 'raw'},
    }}]
    stats, checks = compare_annotations(cases, predictions)
    assert [c['outcome'] for c in checks] == ['agreed', 'unsupported_assertion', 'missed', 'wrong_value']
    assert stats['facets.size']['missed'] == 1
    with pytest.raises(ValueError, match='Missing'):
        compare_annotations(cases, [])


def test_quantity_adapter_retains_size_and_handling():
    assert prepare_phrase('4 medium ripe bananas, sliced') == 'medium ripe bananas, sliced'
    assert prepare_phrase('70 g Eggs, chicken, whole, boiled') == 'Eggs, chicken, whole, boiled'
    assert prepare_phrase('75% fat spread') == '75% fat spread'


def test_null_collection_adapter_preserves_semantics_and_raw_response():
    from recipe_wrangler.matching.evaluation.annotation_experiment import normalize_collection_nulls

    raw = annotation()
    raw['facets']['preservation_methods'] = None
    raw['facets']['cooking_extent'] = 'hard_boiled'
    adapted, changed = normalize_collection_nulls(raw)
    assert raw['facets']['preservation_methods'] is None
    assert adapted['facets']['preservation_methods'] == []
    assert adapted['facets']['cooking_extent'] == 'hard_boiled'
    assert changed == ['facets.preservation_methods']


