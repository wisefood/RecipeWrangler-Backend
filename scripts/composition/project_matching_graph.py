"""Project published food variants for adaptation without altering recipe graph links."""
from __future__ import annotations
import argparse
from recipe_wrangler.repositories import matching_store as store
from recipe_wrangler.utils.neo4j_utils import run_query
from recipe_wrangler.matching.composition_catalogue import COOKING
from recipe_wrangler.matching.graph_projection import facet_properties


def variants(rows):
    for row in rows:
        facts=row['annotation']
        annotation=facts.get('native_annotation') or facts['text_annotation']
        facets=annotation['facets'];identity=facets.get('base_identity') or annotation.get('main_ingredient')
        properties=facet_properties(facets)
        yield {'id':store.digest([row['release_id'],row['collection'],row['candidate_id']]),
            'release':row['release_id'],'collection':row['collection'],'composition_id':row['candidate_id'],
            'label':row['candidate']['document'],'identity':identity,
            'identity_id':store.digest(['food_identity',identity]) if identity else None,'properties':properties,
            'blocked':bool(facts['blockers'])}


def project(release_id,apply=False):
    store.release(release_id)
    rows=list(variants(store.compositions(release_id)))
    print('Adaptation variant projections',len(rows),flush=True)
    if not apply:return
    for label,key in [('MatchingFoodVariant','variant_id'),('MatchingFoodIdentity','identity_id'),('MatchingFacet','facet_id')]:
        run_query(f'CREATE CONSTRAINT {label.lower()}_unique IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE')
    for start in range(0,len(rows),250):
        run_query('''UNWIND $rows AS row
            MERGE (v:MatchingFoodVariant {variant_id:row.id})
            SET v.label=row.label,v.release_id=row.release,v.collection=row.collection,
                v.composition_id=row.composition_id,v.source_blocked=row.blocked
            FOREACH (_ IN CASE WHEN row.identity_id IS NULL THEN [] ELSE [1] END |
                MERGE (i:MatchingFoodIdentity {identity_id:row.identity_id}) SET i.name=row.identity
                MERGE (v)-[:VARIANT_OF]->(i))
            FOREACH (property IN row.properties |
                MERGE (f:MatchingFacet {facet_id:property.kind+':'+property.value})
                SET f.kind=property.kind,f.value=property.value,f.postgres_concept_id=property.concept_id
                MERGE (v)-[:HAS_FACET]->(f))''',{'rows':rows[start:start+250]})
        if start%5000==0:print('Projected',start,flush=True)
    run_query('''MATCH (i:Ingredient),(f:MatchingFoodIdentity)
        WHERE toLower(trim(i.name))=toLower(f.name)
        MERGE (i)-[r:HAS_MATCHING_IDENTITY]->(f)
        SET r.method='exact_normalized_label',r.release_id=$release''',{'release':release_id})
    print('Projection complete; existing Recipe and Ingredient links preserved; exact identity links added.',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-id',required=True)
    p.add_argument('--apply',action='store_true');a=p.parse_args();project(a.release_id,a.apply)
