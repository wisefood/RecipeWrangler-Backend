from dataclasses import asdict

from recipe_wrangler.matching.hybrid import HybridMatcher
from recipe_wrangler.matching.reranking import rerank
from recipe_wrangler.matching.models import FoodFacets


def annotation(identity='egg', **facets):
    values = asdict(FoodFacets(base_identity=identity, **facets))
    values['processes'] = sorted(values['processes'])
    values['preservation_methods'] = sorted(values['preservation_methods'])
    return {'main_ingredient': identity, 'facets': values,
        'foods': [{'food_id': 'food:0', 'identity': identity, 'family': 'egg' if identity == 'egg' else 'generic'}] if identity else [],
        'modifiers': [], 'qualifiers': {}, 'review_reasons': [], 'unresolved': []}


def hit(key, a, relation='needs_review'):
    return {'key': key, 'label': key, 'cosine': .99 if key == 'fried' else .1,
        'annotation': a, 'compatibility': {'relation': relation, 'rules': []}}


def test_generic_hen_egg_beats_unsupported_fried_and_raw_states():
    query = annotation(biological_source='chicken')
    candidates = [hit('fried', annotation(biological_source='chicken', preparation_state='cooked', processes=frozenset({'fried'}))),
        hit('raw', annotation(biological_source='chicken', preparation_state='raw')),
        hit('generic', annotation(biological_source='chicken'))]
    ranked = rerank(query, candidates)
    assert [c['key'] for c in ranked] == ['generic', 'raw', 'fried']
    assert ranked[0]['retrieval_rank'] == 3
    assert ranked[0]['rerank_position'] == 1
    assert ranked[0]['ranking']['additional_candidate_properties'] == []


def test_supported_identity_and_source_beat_less_complete_candidate():
    query = annotation(biological_source='chicken')
    ranked = rerank(query, [hit('unspecified species', annotation()),
                           hit('known species', annotation(biological_source='chicken'))])
    assert ranked[0]['key'] == 'known species'


def test_rejection_cannot_be_overridden_by_high_similarity_or_low_specificity():
    query = annotation(biological_source='chicken')
    ranked = rerank(query, [hit('wrong species', annotation(biological_source='duck'), 'reject'),
        hit('fried', annotation(biological_source='chicken', processes=frozenset({'fried'})))])
    assert ranked[-1]['key'] == 'wrong species'


def test_unknown_query_preserves_retrieval_order():
    ranked = rerank(annotation(None), [hit('fried', annotation(processes=frozenset({'fried'}))),
                                      hit('generic', annotation())])
    assert [c['key'] for c in ranked] == ['fried', 'generic']


def test_unparsed_processing_word_does_not_make_candidate_look_generic():
    query = annotation('milk', reconstitution_state='reconstituted')
    query['qualifiers']['starting_physical_form'] = 'powder'
    unknown_process = annotation('milk')
    unknown_process['unresolved'] = [{'text': 'evaporated'}]
    powder = annotation('milk', physical_form='powder', fat_level='skimmed')
    ranked = rerank(query, [hit('evaporated', unknown_process), hit('powder', powder)])
    assert ranked[0]['key'] == 'powder'
    assert ranked[1]['ranking']['unresolved_candidate_words'] == ['evaporated']


def test_stated_fat_percentage_does_not_promote_unstated_powder_form():
    query = annotation('milk', fat_content_description='1% fat')
    pasteurised = annotation('milk', fat_content_description='1% fat')
    pasteurised['unresolved'] = [{'text': 'pasteurised'}]
    powder = annotation('milk', fat_content_description='1% fat', physical_form='powder')
    ranked = rerank(query, [hit('powder', powder), hit('pasteurised', pasteurised)])
    assert ranked[0]['key'] == 'pasteurised'
    assert ranked[1]['ranking']['additional_product_properties'] == [('physical_form', 'powder')]


class Annotator:
    def __init__(self, values):
        self.values = values

    def annotate(self, text):
        return self.values[text]


def test_source_variants_are_reranked_separately():
    query = annotation(biological_source='chicken')
    base = annotation(biological_source='chicken')
    cooked = annotation(biological_source='chicken', preparation_state='cooked', processes=frozenset({'fried'}))
    sources = {key: {'text_annotation': value, 'facets': value['facets'], 'issues': [],
                     'metadata': {}, 'native_evidence': []} for key, value in [('fried', cooked), ('generic', base)]}
    matcher = HybridMatcher(Annotator({'hen eggs': query, 'group': base}), sources)
    result = matcher.match('hen eggs', [{'key': 'group', 'label': 'group',
        'records': [{'catalogue_key': 'fried'}, {'catalogue_key': 'generic'}]}])
    assert result['candidates'][0]['annotation']['facets']['preparation_state'] is None
    assert len(result['candidates'][0]['source_assessments']) == 2


