"""Bridge existing ingredients and versioned recipe occurrences to food variants."""
from __future__ import annotations
import json
from pathlib import Path
from functools import lru_cache
from sqlalchemy import text
from recipe_wrangler.matching.runtime import build_annotator
from recipe_wrangler.matching.graph_projection import ingredient_frame,occurrence_projection
from recipe_wrangler.repositories import matching_store as store
from recipe_wrangler.utils.nutrition_postgres import get_engine
from recipe_wrangler.utils.neo4j_utils import run_query


@lru_cache(maxsize=2)
def _annotator(release_id):
    return build_annotator()


def bridge(release_id,apply=False,recipe_ids=None,*,active_only=False,write_report=True,project_frames=True):
    store.release(release_id)
    ingredients=[r['name'] for r in run_query('MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient) WHERE $ids IS NULL OR r.recipe_id IN $ids RETURN DISTINCT i.name AS name ORDER BY name', {'ids':recipe_ids}) if r['name']] if project_frames else []
    annotate=_annotator(release_id).annotate if ingredients else None
    frames=[];unresolved=[]
    for name in ingredients:
        if not str(name).strip():
            unresolved.append({'name':name,'reasons':['empty_ingredient_name']});continue
        try:annotation=annotate(name)
        except ValueError as exc:
            unresolved.append({'name':name,'reasons':['invalid_ingredient_text:'+str(exc)]});continue
        frame=ingredient_frame(name,annotation,release_id)
        if frame:frames.append(frame)
        else:unresolved.append({'name':name,'reasons':annotation.get('review_reasons') or ['identity_not_established']})
    print('Grounded ingredient frames',len(frames),'unresolved',len(unresolved),flush=True)
    if not apply:return
    run_query('CREATE INDEX matching_recipe_id_lookup IF NOT EXISTS FOR (r:Recipe) ON (r.recipe_id)')
    concepts={p['concept_id']:p for frame in frames for p in frame['properties']}
    with get_engine().begin() as c:
        if frames:c.execute(text('''INSERT INTO matching_ingredient_frames(frame_id,release_id,input_name,annotation,projection)
            VALUES(:id,:release,:name,CAST(:annotation AS jsonb),CAST(:projection AS jsonb))
            ON CONFLICT(frame_id) DO NOTHING'''),[{'id':f['frame_id'],'release':release_id,'name':f['ingredient'],
            'annotation':json.dumps(f['annotation']),'projection':json.dumps({k:v for k,v in f.items() if k!='annotation'})} for f in frames])
        if concepts:c.execute(text('''INSERT INTO matching_concepts(concept_id,facet_kind,value,label,provenance)
            VALUES(:concept_id,:field,:value,:value,CAST(:provenance AS jsonb))
            ON CONFLICT(facet_kind,value) DO NOTHING'''),[{**p,'provenance':json.dumps({
                'method':'grounded_single_food_annotation','release_id':release_id,
                'ontology_equivalence':'not_asserted'})} for p in concepts.values()])
    for label,key in [('MatchingIngredientFrame','frame_id'),('MatchingIngredientOccurrence','occurrence_id')]:
        run_query(f'CREATE CONSTRAINT {label.lower()}_unique IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE')
    for start in range(0,len(frames),200):
        run_query('''UNWIND $rows AS row
            MATCH (i:Ingredient {name:row.ingredient})
            MERGE (f:MatchingIngredientFrame {frame_id:row.frame_id})
            SET f.release_id=row.release,f.method=row.method,f.input_name=row.ingredient
            MERGE (i)-[:HAS_MATCHING_FRAME]->(f)
            MERGE (identity:MatchingFoodIdentity {identity_id:row.identity_id}) SET identity.name=row.identity
            MERGE (f)-[:HAS_IDENTITY]->(identity)
            MERGE (i)-[link:HAS_MATCHING_IDENTITY]->(identity)
            SET link.method=row.method,link.release_id=row.release
            FOREACH (property IN row.properties |
                MERGE (p:MatchingFacet {facet_id:property.kind+':'+property.value})
                SET p.kind=property.kind,p.value=property.value,p.postgres_concept_id=property.concept_id
                MERGE (f)-[:HAS_OBSERVED_FACET]->(p))''',{'rows':frames[start:start+200]})
    with get_engine().connect() as c:
        inconsistent=c.execute(text('''SELECT count(*) FROM (SELECT collection,native_id
            FROM matching_compositions WHERE release_id=:release GROUP BY collection,native_id
            HAVING count(DISTINCT nutrient_row)>1 OR count(DISTINCT annotation)>1) AS conflicting'''),
            {'release':release_id}).scalar()
        if inconsistent:raise ValueError('Native source IDs have inconsistent duplicate records; do not link arbitrarily')
        candidates={(r['collection'],r['native_id']):dict(r) for r in c.execute(text('''SELECT collection,native_id,candidate_id
            FROM matching_compositions WHERE release_id=:release ORDER BY candidate_id'''),{'release':release_id}).mappings()}
        recipe_filter=' AND recipe_id=ANY(:recipe_ids)' if recipe_ids else ''
        if active_only:recipe_filter+=' AND run_id IN (SELECT run_id FROM matching_active_profiles)'
        runs=[dict(r) for r in c.execute(text('''SELECT DISTINCT ON (recipe_id,nutrition_source)
            run_id,recipe_id,nutrition_source,release_id,profile FROM matching_profile_runs
            WHERE release_id=:release'''+recipe_filter+'''
            ORDER BY recipe_id,nutrition_source,created_at DESC,run_id DESC'''),
            {'release':release_id,'recipe_ids':recipe_ids}).mappings()]
    pending=[];count=0;unlinked=[];reviewed_outside=[]
    from recipe_wrangler.matching.reviewed_aliases import reviewed_public_record
    from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS
    def write(rows):
        run_query('''UNWIND $rows AS row
            MATCH (recipe:Recipe {recipe_id:row.recipe_id})
            MERGE (o:MatchingIngredientOccurrence {occurrence_id:row.id})
            SET o.recipe_id=row.recipe_id,o.run_id=row.run_id,o.nutrition_source=row.source,
                o.position=row.position,o.ingredient=row.ingredient,o.original_text=row.original_text,
                o.weight_g=row.weight_g,o.outcome=row.outcome,o.release_id=row.release,o.staged=true
            SET o.quantity_input_hash=row.quantity_input_hash,o.quantity_version=row.quantity_version,
                o.quantity_status=row.quantity_status,o.original_weight_g=row.original_weight_g
            SET o.source_recipe_revision=row.source_recipe_revision
            MERGE (recipe)-[:HAS_MATCHING_OCCURRENCE]->(o)
            FOREACH (property IN row.properties |
                MERGE (p:MatchingFacet {facet_id:property.kind+':'+property.value})
                SET p.kind=property.kind,p.value=property.value,p.postgres_concept_id=property.concept_id
                MERGE (o)-[:HAS_OBSERVED_FACET]->(p))
            WITH o,row,recipe OPTIONAL MATCH (v:MatchingFoodVariant {variant_id:row.variant_id})
            FOREACH (_ IN CASE WHEN v IS NULL THEN [] ELSE [1] END |
                MERGE (o)-[:MATCHED_TO_VARIANT]->(v))
            WITH o,row,recipe
            OPTIONAL MATCH (o)-[old:DERIVED_FROM]->()
            DELETE old
            WITH DISTINCT o,row,recipe
            OPTIONAL MATCH (recipe)-[:HAS_INGREDIENT_ORIGINAL]->(original:Ingredients_original)
            WHERE coalesce(original.original_text,original.name)=row.original_text AND row.original_text<>''
            WITH o,row,collect(DISTINCT original) AS originals
            SET o.source_link_status=CASE WHEN row.original_text='' THEN 'missing_original_text'
                WHEN size(originals)=1 THEN 'exact_recipe_source_text'
                WHEN size(originals)>1 THEN 'ambiguous_source_nodes' ELSE 'source_text_not_projected' END
            FOREACH (original IN CASE WHEN size(originals)=1 THEN originals ELSE [] END |
                MERGE (o)-[:DERIVED_FROM]->(original))''',{'rows':rows})
    for run in runs:
        for detail in run['profile'].get('details',[]):
            facts=detail.get('selected_composition_facts')
            candidate=None
            if facts and detail.get('outcome')!='unresolved':
                collection=COLLECTIONS[facts['source_key']]
                candidate=candidates.get((collection,str(facts['native_id'])))
                if candidate and detail.get('selected_candidate_id'):
                    candidate={**candidate,'candidate_id':detail['selected_candidate_id']}
                if candidate is None and reviewed_public_record(str(detail.get('selected_candidate_id'))) is not None:
                    # A reviewed public value outside the release has no variant node; project the occurrence unlinked.
                    reviewed_outside.append({'run_id':run['run_id'],'position':detail['position'],'candidate_id':detail['selected_candidate_id']})
                elif candidate is None:unlinked.append({'run_id':run['run_id'],'position':detail['position'],'reason':'missing_exact_composition'})
            pending.append(occurrence_projection(run,detail,candidate));count+=1
            if len(pending)>=250:write(pending);pending=[]
    if pending:write(pending)
    # Occurrence-only assertions can introduce values absent from source foods.
    # Register those values too; graph IDs must resolve in the authoritative registry.
    graph_concepts=[dict(r) for r in run_query('''MATCH (f:MatchingFacet)
        WHERE f.postgres_concept_id IS NOT NULL
        RETURN DISTINCT f.postgres_concept_id AS id,f.kind AS kind,f.value AS value''')]
    with get_engine().begin() as c:
        if graph_concepts:c.execute(text('''INSERT INTO matching_concepts(concept_id,facet_kind,value,label,provenance)
            VALUES(:id,:field,:value,:value,CAST(:provenance AS jsonb))
            ON CONFLICT(facet_kind,value) DO NOTHING'''),[{'id':r['id'],
                'field':'processes' if r['kind']=='cooking_method' else r['kind'],'value':r['value'],
                'provenance':json.dumps({'method':'grounded_occurrence_or_source_facet',
                    'release_id':release_id,'ontology_equivalence':'not_asserted'})} for r in graph_concepts])
    if active_only:
        run_query("""MATCH (r:Recipe)-[:HAS_MATCHING_OCCURRENCE]->(o:MatchingIngredientOccurrence)
            WHERE r.recipe_id IN $ids
            SET o.current=o.run_id IN $run_ids,o.staged=NOT o.run_id IN $run_ids""",
            {'ids':recipe_ids or sorted({r['recipe_id'] for r in runs}),
             'run_ids':[r['run_id'] for r in runs]})
    report={'release_id':release_id,'ingredient_frames':len(frames),'unresolved_ingredients':unresolved,
        'projected_runs':len(runs),'occurrences_submitted':count,'unlinked_selected_compositions':unlinked,
        'reviewed_outside_release':reviewed_outside,
        'recipe_filter':recipe_ids}
    if unlinked:raise ValueError('Missing selected graph compositions: '+str(unlinked[:3]))
    if write_report:
        folder=Path('data/analysis')/('matching_graph_audit_'+release_id);folder.mkdir(exist_ok=True)
        name='bridge_audit_subset_'+store.digest(sorted(recipe_ids))[:12]+'.json' if recipe_ids else 'bridge_active_audit.json' if active_only else 'bridge_audit.json'
        (folder/name).write_text(json.dumps(report,indent=2))
    print('Projected recipe runs',len(runs),'occurrences',count,'unlinked selections',len(unlinked),flush=True)
    return report


