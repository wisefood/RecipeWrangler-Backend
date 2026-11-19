"""Publish a source-preserving matching catalogue to PostgreSQL and a new ES index."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import requests
from sqlalchemy import text

from recipe_wrangler.matching.runtime import ROOT,ASSETS,build_annotator,VERSION
from recipe_wrangler.matching.foodex2 import FoodEx2Catalogue
from recipe_wrangler.matching.nutrition_policy import SourceNutrientPolicy
from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS,native_candidate_id
from recipe_wrangler.repositories.elasticsearch_vectors import elastic_vector_url
from recipe_wrangler.repositories.matching_store import digest
from recipe_wrangler.tools.nutrition_completeness import native_values
from recipe_wrangler.utils.nutrition_postgres import get_engine

SNAPSHOT=ROOT/'analysis/nutrition_inventory_2026-10-26_catalogue_baseline'
EXPANDED=ROOT/'analysis/irish_full_catalogue_baseline_2026-10-26'


def read_lines(path):
    with path.open() as f:return [json.loads(line) for line in f]


def concept_id(field,value):return 'urn:recipe-wrangler:facet:'+digest([field,value])[:24]


def publish(release_id,apply=False):
    candidates=read_lines(SNAPSHOT/'search_candidates.jsonl')+read_lines(EXPANDED/'additional_candidates.jsonl')
    nutrients={(r['source_key'],r['native_id']):r['record'] for r in
        read_lines(SNAPSHOT/'nutrient_rows.jsonl')+read_lines(EXPANDED/'additional_nutrient_rows.jsonl')}
    records={r['catalogue_key']:r for r in read_lines(ROOT/'data/nutrition/matching/catalogue/composition_catalogue_2026-10-22/records.jsonl')}
    profiles=json.loads((EXPANDED/'source_nutrients.json').read_text())
    aliases=json.loads((SNAPSHOT/'aliases.json').read_text())
    snapshot=json.loads((SNAPSHOT/'manifest.json').read_text())
    print('Candidate rows',len(candidates),'nutrient records',len(nutrients),flush=True)
    if not apply:return
    search_index='matching-'+release_id.lower().replace('_','-')
    paths=[SNAPSHOT/'search_candidates.jsonl',EXPANDED/'additional_candidates.jsonl',
        SNAPSHOT/'nutrient_rows.jsonl',EXPANDED/'additional_nutrient_rows.jsonl',
        EXPANDED/'source_nutrients.json',SNAPSHOT/'aliases.json',*[ROOT/p for p in ASSETS.values()]]
    manifest={'release_id':release_id,'search_index':search_index,'legacy_index':snapshot['physical_index'],
        'aliases':aliases,'embedding_model':snapshot['embedding_model'],
        'embedding_revision':snapshot['embedding_revision'],'mechanism_version':VERSION,
        'input_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    with get_engine().begin() as c:
        if c.execute(text('SELECT 1 FROM matching_releases WHERE release_id=:id'),{'id':release_id}).first():
            raise ValueError('Release already exists; choose a new immutable release ID')
        c.execute(text('INSERT INTO matching_releases(release_id,manifest,status) VALUES(:id,CAST(:m AS jsonb),\'loading\')'),
            {'id':release_id,'m':json.dumps(manifest)})
    session=requests.Session();base=elastic_vector_url()
    response=session.get(base+'/'+snapshot['physical_index'],timeout=30);response.raise_for_status()
    original=response.json()[snapshot['physical_index']]
    if original['settings']['index']['uuid']!=snapshot['index_uuid']:raise ValueError('Legacy index UUID changed')
    settings={k:v for k,v in original['settings']['index'].items() if k in {'number_of_shards','number_of_replicas','analysis','similarity'}}
    mapping=original['mappings'];mapping.setdefault('properties',{})['matching_facets']={'type':'flattened'}
    mapping['properties']['catalogue_release']={'type':'keyword'}
    response=session.put(base+'/'+search_index,json={'settings':settings,'mappings':mapping},timeout=30);response.raise_for_status()
    response=session.post(base+'/_reindex',json={'source':{'index':snapshot['physical_index']},'dest':{'index':search_index}},timeout=180)
    response.raise_for_status()
    if response.json().get('failures'):raise ValueError('Legacy index copy failed')
    annotator=build_annotator();foodex=FoodEx2Catalogue(ROOT/ASSETS['foodex'])
    class Provider:
        def nutrient(self,source,identifier):return nutrients.get((source,str(identifier)))
    policy=SourceNutrientPolicy(Provider(),annotator,records,profiles,foodex,native_values)
    vectors=np.concatenate([np.load(SNAPSHOT/'search_vectors.npy'),np.load(EXPANDED/'additional_vectors.npy')])
    assert len(vectors)==len(candidates)
    concepts={};seen=set()
    statement=text('''INSERT INTO matching_compositions(release_id,collection,candidate_id,native_id,
        candidate,nutrient_row,source_record,source_profile,annotation)
        VALUES(:release,:collection,:id,:native,CAST(:candidate AS jsonb),CAST(:row AS jsonb),
        CAST(:record AS jsonb),CAST(:profile AS jsonb),CAST(:annotation AS jsonb))''')
    for start in range(0,len(candidates),200):
        rows=[];bulk=[]
        for candidate,vector in zip(candidates[start:start+200],vectors[start:start+200],strict=True):
            collection=candidate['collection'];key=(collection,str(candidate['id']))
            if key in seen:continue
            seen.add(key)
            source=next(s for s,v in COLLECTIONS.items() if v==collection)
            native=native_candidate_id(candidate,source);row=nutrients.get((source,native)) or {}
            facts=policy.source(collection,candidate)
            catalogue_key=(candidate.get('metadata') or {}).get('catalogue_key')
            facets=(facts.get('native_annotation') or facts['text_annotation'])['facets']
            clean={k:candidate[k] for k in ['id','document','metadata']}
            rows.append({'release':release_id,'collection':collection,'id':str(candidate['id']),'native':native,
                'candidate':json.dumps(clean),'row':json.dumps(row),'record':json.dumps(records.get(catalogue_key)),
                'profile':json.dumps(profiles.get(catalogue_key)),'annotation':json.dumps(facts)})
            doc={'collection':collection,'source_id':candidate['id'],'document':candidate['document'],
                'metadata':candidate.get('metadata') or {},'embedding':vector.tolist(),
                'catalogue_release':release_id,'matching_facets':facets}
            bulk.extend([json.dumps({'index':{'_id':candidate['es_id']}}),json.dumps(doc)])
            for field,value in facets.items():
                if value is None or isinstance(value,(bool,dict)):continue
                for term in value if isinstance(value,list) else [value]:
                    if isinstance(term,str):concepts[field,term]={'id':concept_id(field,term),'field':field,'value':term}
        with get_engine().begin() as c:
            if rows:c.execute(statement,rows)
        response=session.post(base+'/'+search_index+'/_bulk',data='\n'.join(bulk)+'\n',
            headers={'Content-Type':'application/x-ndjson'},timeout=60);response.raise_for_status()
        if response.json().get('errors'):raise ValueError('Composition index bulk failed; release remains unpublished')
        if start%1000==0:print('Published',start,'/',len(candidates),flush=True)
    with get_engine().begin() as c:
        c.execute(text('''INSERT INTO matching_concepts(concept_id,facet_kind,value,label,provenance)
            VALUES(:id,:field,:value,:value,CAST(:provenance AS jsonb)) ON CONFLICT(facet_kind,value) DO NOTHING'''),
            [{**r,'provenance':json.dumps({'method':'local_controlled_facet','release_id':release_id,
                'ontology_equivalence':'not_asserted'})} for r in concepts.values()])
        count=c.execute(text('SELECT count(*) FROM matching_compositions WHERE release_id=:id'),{'id':release_id}).scalar()
        assert count==len(seen)
    response=session.post(base+'/'+search_index+'/_refresh',timeout=30);response.raise_for_status()
    response=session.post(base+'/'+search_index+'/_count',json={'query':{'term':{'catalogue_release':release_id}}},timeout=30);response.raise_for_status()
    assert response.json()['count']==len(seen)
    with get_engine().begin() as c:
        c.execute(text("UPDATE matching_releases SET status='ready' WHERE release_id=:id"),{'id':release_id})
    output=ROOT/'analysis'/('matching_release_'+release_id);output.mkdir(exist_ok=False)
    (output/'manifest.json').write_text(json.dumps({**manifest,'composition_records':len(seen),'concepts':len(concepts)},indent=2))
    print('Ready',release_id,len(seen),'compositions',len(concepts),'concepts',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--release-id',required=True)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args();publish(args.release_id,args.apply)
