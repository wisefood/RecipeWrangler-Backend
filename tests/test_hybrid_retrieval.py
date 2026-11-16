from dataclasses import asdict

import pytest

from recipe_wrangler.matching.composition_catalogue import exact_description_key
from recipe_wrangler.matching.hybrid_retrieval import HybridRetrievalIndex, lexical_tokens
from recipe_wrangler.matching.models import FoodFacets


def annotation(**values):
    return {'main_ingredient': 'chicken', 'foods': [{'food_id': 'food:0'}],
            'facets': asdict(FoodFacets(**values))}


def build_index(records):
    groups, annotations = {}, {}
    for key, label, values in records:
        record = {'catalogue_key': key, 'label': label,
                  'source': {'dataset': 'test', 'release': '1', 'record_id': key}}
        groups.setdefault(exact_description_key(label), []).append(record)
        a = annotation(**values)
        annotations[key] = {'facets': a['facets'], 'text_annotation': a}
    return HybridRetrievalIndex(groups, annotations)


@pytest.mark.parametrize('text', ['skinless chicken breast, raw', 'Chicken, breast, without skin, raw',
                                 'CHICKEN BREAST RAW W/O SKIN'])
def test_normalized_words_preserve_skin_state(text):
    assert sorted(lexical_tokens(text)) == sorted(lexical_tokens('Chicken, breast, without skin, raw'))
    assert sorted(lexical_tokens(text)) != sorted(lexical_tokens('chicken breast with skin raw'))


def test_facet_and_lexical_paths_recover_record_below_vector_budget():
    values = dict(biological_source='chicken', food_part='breast', preparation_state='raw', skin_status='absent')
    index = build_index([('good', 'Chicken, breast, without skin, raw', values),
                         ('other', 'Chicken, roasted', {'preparation_state': 'cooked'})])
    scores = [0.1 if index.groups[k][0]['catalogue_key'] == 'good' else .99 for k in index.keys]
    hits = index.retrieve('skinless chicken breast, raw', annotation(**values), scores, limit=1)
    good = next(h for h in hits if h['records'][0]['catalogue_key'] == 'good')
    assert {p['method'] for p in good['retrieval_paths']} >= {'normalized_exact', 'lexical', 'facets'}
    assert 'vector' not in {p['method'] for p in good['retrieval_paths']}
    assert len(hits) == 2


def test_missing_facets_do_not_remove_vector_candidate():
    index = build_index([('unknown', 'Chicken fillet', {}),
                        ('good', 'Chicken breast without skin raw', {'skin_status': 'absent'})])
    scores = [.99 if index.groups[k][0]['catalogue_key'] == 'unknown' else .1 for k in index.keys]
    hits = index.retrieve('skinless chicken breast raw', annotation(skin_status='absent'), scores, limit=1)
    unknown = next(h for h in hits if h['records'][0]['catalogue_key'] == 'unknown')
    assert 'vector' in {p['method'] for p in unknown['retrieval_paths']}


def test_facets_are_not_combined_across_source_variants():
    label = 'Chicken breast'
    index = build_index([('one', label, {'preparation_state': 'raw'}),
                         ('two', label, {'skin_status': 'absent'})])
    hits = index.retrieve('skinless chicken breast raw',
        annotation(preparation_state='raw', skin_status='absent'), [.5])
    assert len(hits) == 1
    assert 'facets' not in {p['method'] for p in hits[0]['retrieval_paths']}
    assert {r['catalogue_key'] for r in hits[0]['records']} == {'one', 'two'}


def test_qualified_names_and_sources_are_not_collapsed():
    index = build_index([('plain', 'Chicken breast raw', {}),
                         ('breaded', 'Chicken breast breaded raw', {})])
    assert len(index.keys) == 2
    hits = index.retrieve('chicken breast raw', annotation(), [.5, .5])
    assert hits[0]['label'] == 'Chicken breast raw'
    assert any(h['label'] == 'Chicken breast breaded raw' for h in hits)


def test_wrong_vector_alignment_is_rejected():
    index = build_index([('one', 'Chicken breast', {})])
    with pytest.raises(ValueError):
        index.retrieve('chicken', annotation(), [])
