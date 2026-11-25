"""Versioned matching persistence; independent of the recipe graph."""
from __future__ import annotations

import hashlib
import json
from sqlalchemy import text
from recipe_wrangler.utils.nutrition_postgres import get_engine


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def deployment() -> dict:
    with get_engine().connect() as c:
        result=c.execute(text('SELECT release_id,mode FROM public.matching_deployment WHERE singleton')).mappings().one()
        return dict(result)


def release(release_id: str) -> dict:
    with get_engine().connect() as c:
        row=c.execute(text('SELECT * FROM public.matching_releases WHERE release_id=:id'),{'id':release_id}).mappings().one()
    if row['status']!='ready':raise ValueError('Matching release is not ready')
    return dict(row)


def compositions(release_id: str) -> list[dict]:
    rows = []
    collection, candidate = '', ''
    with get_engine().connect() as c, c.begin():
        c.execute(text('SET TRANSACTION READ ONLY'))
        c.execute(text("SET LOCAL statement_timeout = '60s'"))
        while True:
            batch = [dict(row) for row in c.execute(text('''SELECT * FROM public.matching_compositions
                WHERE release_id=:id AND (collection,candidate_id)>(:collection,:candidate)
                ORDER BY collection,candidate_id LIMIT 100'''),
                {'id': release_id, 'collection': collection, 'candidate': candidate}).mappings()]
            if not batch:
                return rows
            rows.extend(batch)
            collection, candidate = batch[-1]['collection'], batch[-1]['candidate_id']


def composition_records(release_id: str, collection: str, *,
                        candidate_id: str | None = None, native_id: str | None = None) -> list[dict]:
    """Read exact released keys; native-ID duplicates are returned for adjudication."""
    if (candidate_id is None) == (native_id is None):
        raise ValueError('Specify exactly one candidate_id or native_id')
    field = 'candidate_id' if candidate_id is not None else 'native_id'
    with get_engine().connect() as c:
        return [dict(r) for r in c.execute(text('''SELECT c.* FROM public.matching_compositions c
            JOIN public.matching_releases r USING(release_id)
            WHERE c.release_id=:release AND c.collection=:collection AND r.status='ready'
            AND c.''' + field + '=:identifier ORDER BY c.candidate_id'),
            {'release': release_id, 'collection': collection,
             'identifier': candidate_id if candidate_id is not None else native_id}).mappings()]


def variant_composition_records(release_id: str, region_collection: str, *, dataset: str,
                                source_release: str, identity: str, food_part: str) -> list[dict]:
    """Retrieve exact-source variants before the adaptation shortlist limit.

    Facets generate possibilities only; consumers must validate full records.
    """
    with get_engine().connect() as connection:
        return [dict(row) for row in connection.execute(text('''SELECT c.*
            FROM public.matching_compositions c JOIN public.matching_releases r USING(release_id)
            WHERE c.release_id=:release AND r.status='ready'
            AND c.collection IN ('nutritional_ingredients_eu', :regional)
            AND c.annotation->'source'->>'dataset'=:dataset
            AND c.annotation->'source'->>'release'=:version
            AND COALESCE(c.annotation->'native_annotation'->'facets'->>'base_identity',
                c.annotation->'text_annotation'->'facets'->>'base_identity',
                c.annotation->'text_annotation'->>'main_ingredient')=:identity
            AND COALESCE(c.annotation->'native_annotation'->'facets'->>'food_part',
                c.annotation->'text_annotation'->'facets'->>'food_part')=:part
            ORDER BY c.collection,c.candidate_id'''), {'release': release_id, 'regional': region_collection,
                'dataset': dataset, 'version': source_release, 'identity': identity, 'part': food_part}).mappings()]


def food_part_composition_records(release_id: str, region_collection: str, *, dataset: str,
                                 source_release: str, identity: str, biological_source: str,
                                 source_food_part: str) -> list[dict]:
    """Discover other raw parts of the same released product and biological source.

    Return one sentinel beyond the 500-record scan budget so callers can report
    truncation. Missing cached anatomy can be recovered from quoted label wording;
    candidates with conflicting or still-missing facts fail consumer validation.
    Full evidence and compatibility checks precede the final shortlist.
    """
    with get_engine().connect() as connection:
        return [dict(row) for row in connection.execute(text('''SELECT c.*
            FROM public.matching_compositions c JOIN public.matching_releases r USING(release_id)
            WHERE c.release_id=:release AND r.status='ready'
            AND c.collection IN ('nutritional_ingredients_eu', :regional)
            AND c.annotation->'source'->>'dataset'=:dataset
            AND c.annotation->'source'->>'release'=:version
            AND c.annotation->'text_annotation'->'facets'->>'base_identity'=:identity
            AND (c.annotation->'text_annotation'->'facets'->>'biological_source'=:biological
                OR COALESCE(c.annotation->'text_annotation'->'facets'->>'biological_source','') IN ('','unknown'))
            AND c.annotation->'text_annotation'->'facets'->>'preparation_state'='raw'
            AND (c.annotation->'text_annotation'->'facets'->>'food_part' NOT IN (:part, '', 'unknown')
                OR COALESCE(c.annotation->'text_annotation'->'facets'->>'food_part','') IN ('','unknown'))
            ORDER BY c.collection,c.candidate_id LIMIT 501'''), {
                'release': release_id, 'regional': region_collection, 'dataset': dataset,
                'version': source_release, 'identity': identity, 'biological': biological_source,
                'part': source_food_part}).mappings()]


def identity_composition_records(release_id: str, region_collection: str, *, dataset: str,
                                 source_release: str, identity: str, state: str | None, include_other_sources: bool = False) -> list[dict]:
    """Discover pinned graph identities, preferring native and baseline-table records.

    Twenty candidates plus a truncation sentinel; all still require comparison.
    """
    with get_engine().connect() as connection, connection.begin():
        connection.execute(text('SET TRANSACTION READ ONLY'))
        connection.execute(text("SET LOCAL statement_timeout = '60s'"))
        return [dict(row) for row in connection.execute(text('''SELECT c.*
            FROM public.matching_compositions c JOIN public.matching_releases r USING(release_id)
            WHERE c.release_id=:release AND r.status='ready'
            AND c.collection IN ('nutritional_ingredients_eu', :regional)
            AND (CAST(:other_sources AS boolean) OR (c.annotation->'source'->>'dataset'=:dataset
                AND c.annotation->'source'->>'release'=:version))
            AND COALESCE(c.annotation->'text_annotation'->'facets'->>'base_identity',
                c.annotation->'text_annotation'->>'main_ingredient')=:identity
            AND (CAST(:state AS text) IS NULL OR c.annotation->'text_annotation'->'facets'->>'preparation_state'=:state)
            ORDER BY (c.collection=:regional) DESC,
                (c.annotation->'source'->>'dataset'=:dataset AND c.annotation->'source'->>'release'=:version) DESC,
                c.collection,c.candidate_id LIMIT 21'''),
            {'release': release_id, 'regional': region_collection, 'dataset': dataset,
             'version': source_release, 'identity': identity, 'state': state, 'other_sources': include_other_sources}).mappings()]


def save_run(recipe_id: str, source: str, release_id: str | None, version: str,
             inputs: dict, profile: dict, *, activate: bool=False) -> str:
    with get_engine().begin() as c:
        return insert_run(c,recipe_id,source,release_id,version,inputs,profile,activate=activate)


def insert_run(c,recipe_id: str, source: str, release_id: str | None, version: str,
               inputs: dict, profile: dict, *, activate: bool=False) -> str:
    """Insert within a caller's transaction so recipe writes and runs stay atomic."""
    if activate and (profile.get('source_context_rematch_positions') or any(
        d.get('source_context_status')=='requires_rematching' for d in profile.get('details',[])
    )):
        raise ValueError('Cannot activate changed ingredient context before rematching')
    input_hash=digest(inputs)
    run_id=digest([recipe_id,source,release_id,version,input_hash,profile])
    c.execute(text('''INSERT INTO public.matching_profile_runs
            (run_id,recipe_id,nutrition_source,release_id,mechanism_version,input_hash,profile)
            VALUES(:id,:recipe,:source,:release,:version,:hash,CAST(:profile AS jsonb))
            ON CONFLICT(run_id) DO NOTHING'''),{'id':run_id,'recipe':recipe_id,'source':source,
            'release':release_id,'version':version,'hash':input_hash,
            'profile':json.dumps(profile,allow_nan=False)})
    if activate:
        c.execute(text('''INSERT INTO public.matching_active_profiles(recipe_id,nutrition_source,run_id)
                VALUES(:recipe,:source,:id) ON CONFLICT(recipe_id,nutrition_source)
                DO UPDATE SET run_id=EXCLUDED.run_id'''),{'recipe':recipe_id,'source':source,'id':run_id})
    return run_id


def persist_profile_record(connection,record: dict) -> str | None:
    """Activate saved analyses only after an explicit production deployment."""
    details=record.get('nutrition_profiling_details') or []
    source=record.get('nutrition_source')
    if source not in {'eu','irish','hungarian','slovenian'} or not details:return None
    releases={d.get('catalogue_release') for d in details}
    versions={d.get('mechanism_version') for d in details}
    if len(releases)!=1 or None in releases or len(versions)!=1 or None in versions:return None
    release_id=next(iter(releases));version=next(iter(versions));recipe_id=record['recipe_id']
    old=connection.execute(text('''SELECT row_to_json(p) FROM "nutrients-recipe-profiles" p
        WHERE recipe_id=:recipe AND nutrition_source=:source'''),{'recipe':recipe_id,'source':source}).scalar()
    if old:
        insert_run(connection,recipe_id,source,None,'legacy_snapshot_complete_v2',{'profile':old},old)
    profile={'title':record.get('title'),'details':details,'catalogue_release':release_id,
        'mechanism_version':version,'clean_totals':record.get('total_nutrients'),
        'clean_totals_per_serving':record.get('total_nutrients_per_serving'),
        'nutri_score':record.get('nutri_score'),'profiling_quality':record.get('profiling_quality')}
    for key in ('quantity_input_hash','quantity_version'):
        values={d.get(key) for d in details}
        if len(values)==1 and None not in values:profile[key]=next(iter(values))
    inputs={'names':[d.get('ingredient') or d.get('name') for d in details],
        'weights':[d.get('weight_g') for d in details],
        'originals':[d.get('original_ingredient_text') for d in details]}
    active = connection.execute(text('''SELECT mode='new' AND release_id=:release
        FROM matching_deployment WHERE singleton'''), {'release': release_id}).scalar() is True
    profile['nutri_score_breakdown'] = record.get('nutri_score_breakdown')
    profile['nutrition_quality'] = (record.get('profiling_quality') or {}).get('nutrition_quality')
    quality = record.get('profiling_quality') or {}
    profile.update(quality.get('nutrition_completeness') or {})
    profile['serves'] = quality.get('serves') or (record.get('trace') or {}).get('serves')
    profile['source_key'] = source
    run_id = insert_run(connection,recipe_id,source,release_id,version,inputs,profile,activate=active)
    if active:
        enqueue_projection(connection, recipe_id)
    return run_id


def enqueue_projection(connection, recipe_id: str) -> None:
    """Called inside the recipe/profile transaction so jobs cannot be lost."""
    connection.execute(text('''INSERT INTO matching_projection_jobs(recipe_id) VALUES(:recipe)
        ON CONFLICT(recipe_id) DO UPDATE SET generation=matching_projection_jobs.generation+1,
        requested_at=now(),last_error=NULL'''), {'recipe': recipe_id})


def active_profile(recipe_id: str, source: str, *, include_provenance: bool = False) -> dict | None:
    columns = 'r.run_id,r.profile,r.mechanism_version,r.release_id'
    if include_provenance:
        columns += ',r.recipe_id,r.nutrition_source,r.input_hash'
    with get_engine().connect() as c:
        row=c.execute(text('SELECT ' + columns + '''
            FROM public.matching_active_profiles a JOIN public.matching_profile_runs r USING(run_id)
            WHERE a.recipe_id=:id AND a.nutrition_source=:source
            AND r.recipe_id=a.recipe_id AND r.nutrition_source=a.nutrition_source'''),
            {'id':recipe_id,'source':source}).mappings().first()
    return dict(row) if row else None


def latest_profile(recipe_id: str, source: str) -> dict | None:
    """Read the latest proposed run, including staged runs before activation."""
    with get_engine().connect() as c:
        row=c.execute(text('''SELECT run_id,profile,mechanism_version,release_id,created_at
            FROM public.matching_profile_runs WHERE recipe_id=:id AND nutrition_source=:source
            AND release_id IS NOT NULL ORDER BY created_at DESC,run_id DESC LIMIT 1'''),
            {'id':recipe_id,'source':source}).mappings().first()
    return dict(row) if row else None
