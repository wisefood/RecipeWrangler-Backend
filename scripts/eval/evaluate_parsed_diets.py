"""Evaluate real FoodOn/allergen/Cypher diet stages on saved parser outputs.

No generative model calls. Every graph test transaction explicitly rolls back.
References are used only by scoring, never by the matcher/classifier.
"""
from __future__ import annotations
import argparse
import ast
from collections import Counter
import csv
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import uuid

logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)
ROOT=Path(__file__).resolve().parents[2]
OUT=Path(os.getenv('DIET_EVAL_OUTPUT',str(ROOT/'evaluation/allergens_diets/results/parsed-dietary-2026-11-05'))).resolve()
WORK=ROOT/'analysis/diet-end-to-end-2026-11-05'
GROUPS=('vegan','vegetarian')


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def native_allergen_queries():
    tree=ast.parse((ROOT/'src/recipe_wrangler/repositories/neo4j_recipes.py').read_text())
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='upsert_recipe_to_neo4j')
    strings=[n.args[0].value for n in ast.walk(function) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='run' and n.args and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str)]
    keywords=[q for q in strings if 'UNWIND $ingredient_hits AS candidate' in q]
    ancestry=[q for q in strings if 'any(item IN $supported_links' in q]
    assert len(keywords)==len(ancestry)==1
    return keywords[0],ancestry[0]


def metrics(cells):
    eligible=[c for c in cells if c['expected']!='unknown']
    positives=[c for c in eligible if c['expected']=='suitable']
    negatives=[c for c in eligible if c['expected']=='not_suitable']
    correct=sum(c['predicted']==c['expected'] for c in eligible)
    tp=sum(c['predicted']=='suitable' for c in positives)
    fp=sum(c['predicted']=='suitable' for c in negatives)
    tn=sum(c['predicted']=='not_suitable' for c in negatives)
    abstentions=sum(c['predicted']=='unknown' for c in eligible)
    failures=sum(c['predicted']=='error' for c in eligible)
    full_unknown=sum(c['predicted']=='unknown' for c in cells)
    full_failures=sum(c['predicted']=='error' for c in cells)
    def ratio(n,d):return n/d if d else None
    return {'assessable_cells':len(eligible),'correct':correct,'accuracy':ratio(correct,len(eligible)),
            'correct_decision_yield':ratio(correct,len(eligible)),
            'reference_positive':len(positives),'reference_negative':len(negatives),
            'true_positive':tp,'false_positive':fp,'true_negative':tn,
            'positive_precision':ratio(tp,tp+fp),'positive_recall':ratio(tp,len(positives)),
            'unsafe_admission_rate':ratio(fp,len(negatives)),
            'incorrect_rejections':sum(c['predicted']=='not_suitable' for c in positives),
            'abstentions':abstentions,'unknown_rate':ratio(abstentions,len(eligible)),
            'failures':failures,'coverage':ratio(len(eligible)-abstentions-failures,len(eligible)),
            'accuracy_when_decided':ratio(correct,len(eligible)-abstentions-failures),
            'uncertain_references':sum(c['expected']=='unknown' for c in cells),
            'uncertain_reference_predictions':dict(Counter(c['predicted'] for c in cells if c['expected']=='unknown')),
            'full_cohort_cells':len(cells),'full_cohort_unknowns':full_unknown,
            'full_cohort_unknown_rate':ratio(full_unknown,len(cells)),
            'full_cohort_failures':full_failures,
            'full_cohort_decision_coverage':ratio(len(cells)-full_unknown-full_failures,len(cells)),
            'unverified_suitable':sum(c['expected']=='unknown' and c['predicted']=='suitable' for c in cells)}


class GraphHarness:
    def __init__(self):
        os.environ['EMBED_DEVICE']='cpu'
        os.environ['EMBED_BATCH_SIZE']='1'
        os.environ['EMBED_WARM_ON_IMPORT']='0'
        os.environ['TOKENIZERS_PARALLELISM']='false'
        os.environ['HF_HUB_OFFLINE']='1'
        from recipe_wrangler.utils.env_loader import load_runtime_env
        load_runtime_env()
        from recipe_wrangler.utils import foodon_matching as fm
        from recipe_wrangler.utils.neo4j_utils import driver
        self.driver=driver;self.fm=fm
        self.cache_path=OUT/'link-cache.json'
        self.budget_path=OUT/'vector-query-budget.json'
        self.links=json.loads(self.cache_path.read_text()) if self.cache_path.exists() else {}
        self.vector_queries=json.loads(self.budget_path.read_text())['used'] if self.budget_path.exists() else 0
        rows=json.loads((WORK/'foodon-labels.json').read_text())
        class Rows:
            def run(self,query):return iter(rows)
        self.index=fm.fetch_label_index(Rows())
        original=fm.query_vector_collection
        def bounded_vector(*args,**kwargs):
            if self.vector_queries>=300:raise RuntimeError('Vector-query budget of 300 exhausted')
            self.vector_queries+=1
            self.budget_path.write_text(json.dumps({'used':self.vector_queries,'limit':300})+'\n')
            return original(*args,**kwargs)
        fm.query_vector_collection=bounded_vector
        self.keyword_query,self.foodon_query=native_allergen_queries()

    def classify(self,title,names):
        from neo4j import Query
        from recipe_wrangler.repositories import neo4j_recipes as repo
        from recipe_wrangler.utils import dietary_classification as dc
        from recipe_wrangler.utils.food_ontology import ALLERGEN_DETECTION_RULES,ALLERGEN_EXCLUSION_REGEXES
        from recipe_wrangler.utils.non_food_ingredients import is_unambiguous_non_food_ingredient
        names=list(dict.fromkeys(n for n in names if not is_unambiguous_non_food_ingredient(n)))
        if len(names)>32:raise ValueError('Parsed ingredient bound exceeded')
        for name in names:
            if name not in self.links:self.links[name]=self.fm.match_ingredient_to_foodon(name,self.index)
        self.cache_path.write_text(json.dumps(self.links,ensure_ascii=False)+'\n')
        rid='diet-eval-'+uuid.uuid4().hex
        ingredients=[{'name':name,'canonical_id':rid+':'+str(i)} for i,name in enumerate(names)]
        start=time.monotonic()
        with self.driver.session(fetch_size=100) as session:
            tx=session.begin_transaction(timeout=60)
            try:
                tx.run('CREATE (r:Recipe {recipe_id:$rid,title:$title})',rid=rid,title=title).consume()
                records=list(tx.run('MATCH (r:Recipe {recipe_id:$rid}) UNWIND $items AS item CREATE (i:Ingredient {name:item.name,canonical_id:item.canonical_id}) CREATE (r)-[:HAS_INGREDIENT]->(i) RETURN elementId(i) AS id,i.name AS name',rid=rid,items=ingredients))
                ids=[r['id'] for r in records]
                for item in ingredients:
                    hit=self.links[item['name']]
                    if hit:
                        fid,method,confidence,approximate=hit
                        self.fm.write_link(tx,item['canonical_id'],fid,method=method,confidence=confidence,approximate=approximate,ancestor_level=method=='ancestor_embedding')
                for allergen in ALLERGEN_DETECTION_RULES:
                    hits=[{'name':n,'hits':e[0],'presence':e[1]} for n in names if (e:=repo._allergen_keyword_evidence(n,allergen))[0]]
                    if hits:tx.run(self.keyword_query,recipe_id=rid,allergen=allergen,ingredient_hits=hits,**repo._allergen_ontology_params(allergen)).consume()
                supported=[item for batch in self.fm.ingredient_link_batches(tx,ingredient_ids=ids) for item in batch]
                for allergen,rule in ALLERGEN_DETECTION_RULES.items():
                    if rule['roots']:
                        tx.run(self.foodon_query,recipe_id=rid,allergen=allergen,roots=rule['roots'],supported_links=supported,name_exclusions=ALLERGEN_EXCLUSION_REGEXES.get(allergen,[]),**repo._allergen_ontology_params(allergen)).consume()
                for group in GROUPS:
                    assert dc._initialize_unknown(tx,group,ids)==len(names)
                    dc._classify_negative(tx,group,ids)
                    dc._classify_positive(tx,group,ids)
                    assert dc._aggregate_recipe_batch(tx,[rid],group)==1
                assessments=[dict(r) for r in tx.run('MATCH (:Recipe {recipe_id:$rid})-[s:SUITABILITY_FOR]->(g:ConsumerGroup) WHERE g.name IN $groups RETURN g.name AS group,s.status AS status,s.reason_codes AS reasons,s.blocking_ingredients AS blockers,s.unknown_ingredients AS unknowns',rid=rid,groups=list(GROUPS))]
                evidence=[dict(r) for r in tx.run('MATCH (:Recipe {recipe_id:$rid})-[:HAS_INGREDIENT]->(i) RETURN i.name AS name,[(i)-[s:SUITABILITY_FOR]->(g) WHERE g.name IN $groups | {group:g.name,status:s.status,reasons:s.reason_codes}] AS suitability,[(i)-[:HAS_DECLARATION]->(d)-[:CONCERNS]->(a) | {allergen:a.name,presence:d.presence}] AS allergens',rid=rid,groups=list(GROUPS))]
                assert len(assessments)==2
            finally:
                tx.rollback()
            remaining=session.run(Query('MATCH (r:Recipe {recipe_id:$rid}) RETURN count(r) AS count',timeout=60),rid=rid).single()['count']
            assert remaining==0,'Rollback verification failed'
        return {'assessments':assessments,'ingredients':evidence,'links':{n:self.links[n] for n in names},'rollback_verified':True,'graph_elapsed_seconds':round(time.monotonic()-start,3)}


def freeze():
    files=[Path(__file__),WORK/'foodon-labels.json',OUT/'references.json',OUT/'reference-manifest.json']
    files.extend(ROOT/'src/recipe_wrangler'/n for n in ['repositories/neo4j_recipes.py','utils/foodon_matching.py','utils/food_ontology.py','utils/consumer_suitability.py','utils/ingredient_context.py','utils/dietary_classification.py'])
    files.extend(ROOT/'src/recipe_wrangler'/n for n in ['utils/dietary_ingredient_evidence.py','utils/data/dietary_ingredients.json','utils/non_food_ingredients.py'])
    (OUT/'run-manifest.json').write_text(json.dumps({str(p.relative_to(ROOT)):digest(p) for p in files},indent=2)+'\n')


def run(smoke=False):
    for name,sha in json.loads((OUT/'run-manifest.json').read_text()).items():assert digest(ROOT/name)==sha,name
    harness=GraphHarness()
    if smoke:
        cases=[('Plain rice',['rice','water'],{'vegan':'suitable','vegetarian':'suitable'}),('Milk',['milk'],{'vegan':'not_suitable','vegetarian':'suitable'}),('Chicken',['chicken'],{'vegan':'not_suitable','vegetarian':'not_suitable'}),('Nut milk',['nut milk'],{'vegan':'suitable','vegetarian':'suitable'}),('Coconut cream',['cream of coconut'],{'vegan':'suitable','vegetarian':'suitable'}),('Whole milk',['whole milk'],{'vegan':'not_suitable','vegetarian':'suitable'}),('Greek yogurt',['plain greek yogurt'],{'vegan':'not_suitable','vegetarian':'suitable'}),('Egg yolks',['egg yolks'],{'vegan':'not_suitable','vegetarian':'suitable'})]
        results=[]
        for title,names,expected in cases:
            value=harness.classify(title,names)
            assert {r['group']:r['status'] for r in value['assessments']}==expected,value
            results.append(value)
        (OUT/'graph-smoke.json').write_text(json.dumps(results,indent=2)+'\n')
        print(json.dumps({'smoke_cases':len(results),'vector_queries':harness.vector_queries}));return
    references=json.loads((OUT/'references.json').read_text())
    directory=OUT/'cases';directory.mkdir(exist_ok=True)
    for i,record in enumerate(references):
        for name,sha in json.loads((OUT/'run-manifest.json').read_text()).items():assert digest(ROOT/name)==sha,name
        path=directory/(record['case_id'].replace(':','-')+'.json')
        if path.exists():continue
        # Only runtime data, never source annotations or expected diet labels.
        names=[e['name'] for e in record['parsed_ingredients']]
        result=harness.classify(record['title'],names)
        path.write_text(json.dumps({'case_id':record['case_id'],**result},indent=2,ensure_ascii=False)+'\n')
        print(json.dumps({'completed':i+1,'case_id':record['case_id'],'statuses':{r['group']:r['status'] for r in result['assessments']},'vector_queries':harness.vector_queries}),flush=True)
    (OUT/'execution.json').write_text(json.dumps({'cases':len(references),'vector_queries_this_process':harness.vector_queries,'rollback_only':True,'new_generative_calls':0},indent=2)+'\n')


def score():
    refs=json.loads((OUT/'references.json').read_text());cells=[]
    for r in refs:
        path=OUT/'cases'/(r['case_id'].replace(':','-')+'.json')
        p=json.loads(path.read_text()) if path.exists() else {'assessments':[]}
        states={s['group']:s['status'] for s in p['assessments']}
        for g in GROUPS:
            cells.append({'case_id':r['case_id'],'diet':g,'expected':r['expected'][g],'predicted':states.get(g,'error'),'reference_reason':r['reference_reason'],'parsed_names':'; '.join(e['name'] for e in r['parsed_ingredients'])})
    with (OUT/'expected_vs_pipeline.csv').open('w',encoding='utf-8-sig',newline='') as h:
        writer=csv.DictWriter(h,fieldnames=list(cells[0]));writer.writeheader();writer.writerows(cells)
    summary={g:metrics([c for c in cells if c['diet']==g]) for g in GROUPS}
    summary['pooled']=metrics(cells)
    (OUT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['freeze','smoke','run','score']);args=parser.parse_args()
    {'freeze':freeze,'smoke':lambda:run(True),'run':run,'score':score}[args.action]()
