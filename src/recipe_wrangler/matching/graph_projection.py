"""Evidence-preserving shapes shared by matching graph projections."""
from __future__ import annotations

from recipe_wrangler.repositories.matching_store import digest
from recipe_wrangler.matching.composition_catalogue import COOKING


def facet_properties(facets: dict) -> list[dict]:
    properties=[]
    for field,value in facets.items():
        if field in {'identity','base_identity','compound'} or value is None:continue
        for term in value if isinstance(value,(list,set,frozenset)) else [value]:
            if not isinstance(term,str) or term=='unknown':continue
            kind='cooking_method' if field=='processes' and term in COOKING else field
            properties.append({'kind':kind,'value':term,'field':field,
                'concept_id':'urn:recipe-wrangler:facet:'+digest([field,term])[:24]})
    return properties


def ingredient_frame(name: str, annotation: dict, release_id: str) -> dict | None:
    """Attach full single-food identity and qualifiers without choosing preparation."""
    # Several ontology classes may share the same culinary label. The frame
    # retains only the grounded food identity/facets, never chooses an ontology ID.
    blockers=set(annotation.get('review_reasons',[]))-{'ambiguous_ontology_candidates'}
    if len(annotation.get('foods',[]))!=1 or blockers:
        return None
    facets=annotation['facets'];identity=facets.get('base_identity') or annotation.get('main_ingredient')
    if not identity:return None
    return {'frame_id':digest([release_id,name,facets]),'ingredient':name,
        'identity':identity,'identity_id':digest(['food_identity',identity]),
        'properties':facet_properties(facets),'release':release_id,
        'method':'grounded_single_food_annotation','annotation':annotation}


def occurrence_projection(run: dict, detail: dict, candidate: dict | None) -> dict:
    position=detail['position'];facts=detail.get('observed_facts') or {}
    facets=facts.get('facets') or {}
    # Observed properties never inherit a proxy record's specific properties.
    return {'id':digest([run['run_id'],position]),'run_id':run['run_id'],
        'recipe_id':run['recipe_id'],'source':run['nutrition_source'],'position':position,
        'ingredient':detail.get('ingredient') or detail.get('name'),
        'original_id':f"{run['recipe_id']}:{position}",
        'original_text':detail.get('original_ingredient_text') or '',
        'weight_g':detail.get('weight_g'),'outcome':detail.get('outcome'),
        'quantity_input_hash':detail.get('quantity_input_hash'),
        'source_recipe_revision':run['profile'].get('source_recipe_revision') if run.get('profile') else None,
        'quantity_version':detail.get('quantity_version'),
        'quantity_status':detail.get('quantity_status'),
        'original_weight_g':detail.get('original_weight_g'),
        'release':run['release_id'],'properties':facet_properties(facets),
        'variant_id':digest([run['release_id'],candidate['collection'],candidate['candidate_id']]) if candidate else None}
