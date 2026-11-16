"""Offline adapters must preserve IDs, nutrients and retrieval semantics."""

import pytest

from recipe_wrangler.matching.evaluation.production_catalogue import (
    COLLECTIONS, FrozenProductionCatalogue, lucene_length, native_candidate_id, phrase_frequency,
)


def catalogue(queries=None):
    documents = [{'es_id': str(i), 'document': label, 'collection': COLLECTIONS['eu'],
                  'tokens': [[word, j] for j,word in enumerate(label.split())]}
                 for i,label in enumerate(['stock chicken cube', 'stock cube', 'milk'])]
    candidates = [{'es_id': d['es_id'], 'id': 'eu:'+d['es_id'], 'collection': d['collection'],
                   'document':d['document'], 'metadata':{'eu_id':'eu:'+d['es_id']}} for d in documents]
    queries = queries or {'stock cube': {'embedding':[1,0], 'tokens':[['stock',0],['cube',1]]}}
    return FrozenProductionCatalogue(documents,candidates,[[1,0],[1,0],[0,1]],queries,
        [{'source_key':'eu','native_id':'eu:1','record':{'nutrients':{'Protein':{'value':None,'unit':'g'}}}}])


def test_phrase_boost_and_rrf_are_separate_from_cosine():
    hits = catalogue().query(COLLECTIONS['eu'],'stock cube')
    exact = next(h for h in hits if h['id']=='eu:1')
    other = next(h for h in hits if h['id']=='eu:0')
    assert exact['lexical_rank'] == 1
    assert other['lexical_rank'] == 2
    assert exact['distance'] == pytest.approx(0)
    assert exact['rrf_score'] == pytest.approx(1/62+1/61)


def test_native_ids_and_missing_nutrients_are_not_relabelled_or_zeroed():
    c = catalogue()
    row = c.by_id(COLLECTIONS['eu'],'eu:1')
    assert row['metadata']['eu_id']=='eu:1' and 'distance' not in row
    assert c.nutrient('eu','eu:1')['nutrients']['Protein']['value'] is None
    assert c.nutrient('eu','missing') is None
    assert native_candidate_id({'id':'uuid','metadata':{'canonical_food_id':'IE123'}},'irish')=='IE123'
    row['metadata']['eu_id']='changed'
    assert c.by_id(COLLECTIONS['eu'],'eu:1')['metadata']['eu_id']=='eu:1'


def test_uncaptured_queries_are_fatal_even_if_a_caller_swallows_the_exception():
    c = catalogue()
    with pytest.raises(ValueError,match='Uncaptured query'):
        c.query(COLLECTIONS['eu'],'new query')
    assert c.errors[0]['query']=='new query'


def test_invalid_vectors_do_not_produce_search_hits():
    with pytest.raises(ValueError,match='Invalid query'):
        catalogue({'stock cube':{'embedding':[0,0],'tokens':[]}}).query(COLLECTIONS['eu'],'stock cube')


def test_lucene_norm_and_phrase_positions():
    assert lucene_length(25)==25
    assert lucene_length(41)==40
    assert phrase_frequency([['low',0],['fat',1],['low',2],['fat',3]],[['low',0],['fat',1]])==2
    assert phrase_frequency([['low',0],['fat',2]],[['low',0],['fat',1]])==0


def test_deleted_document_statistics_and_indexed_vector_ties_are_replayed():
    base = catalogue()
    reference = [{'collection':COLLECTIONS['eu'], 'query':'stock cube', 'hits':[
        {'id':'eu:1','vector_rank':1,'distance':.00000004},
        {'id':'eu:0','vector_rank':2,'distance':.00000012},
        {'id':'eu:2','vector_rank':3,'distance':1}]}]
    statistics = {'doc_count':10, 'sum_ttf':40, 'live_document_count':3,
                  'doc_freq':{'stock':4,'cube':4}}
    replay = FrozenProductionCatalogue(base.documents,base.candidates,base.raw_vectors,
        base.queries,[],statistics=statistics,captured_vectors=reference)
    hits=replay.query(COLLECTIONS['eu'],'stock cube')
    assert hits[0]['id']=='eu:1' and hits[0]['vector_rank']==1
    assert hits[0]['distance']==pytest.approx(.00000004,abs=1e-15)
    assert replay.doc_count==10 and replay.avg_length==4
    assert replay.df['stock']==4


def test_added_foods_compete_with_frozen_vector_stream_and_update_global_statistics():
    base=catalogue()
    doc={'es_id':'new','collection':COLLECTIONS['eu'],'document':'stock cube',
         'tokens':[['stock',0],['cube',1]]}
    candidate=doc | {'id':'new','metadata':{'eu_id':'new'}}
    reference=[{'collection':COLLECTIONS['eu'],'query':'stock cube','hits':[
        {'id':'eu:1','vector_rank':1,'distance':.2}]}]
    replay=FrozenProductionCatalogue(base.documents+[doc],base.candidates+[candidate],
        [*base.raw_vectors,[1,0]],base.queries,[],baseline_candidates=3,captured_vectors=reference,
        statistics={'doc_count':10,'sum_ttf':40,'live_document_count':3,'doc_freq':{'stock':4,'cube':4}})
    hit=next(h for h in replay.query(COLLECTIONS['eu'],'stock cube') if h['id']=='new')
    assert hit['vector_rank']==1
    assert replay.doc_count==11 and replay.df['stock']==5


