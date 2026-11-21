"""Retryable PostgreSQL outbox for matching graph, catalogue and cache projections."""
from __future__ import annotations

import logging
import os
import threading

from sqlalchemy import text

from recipe_wrangler.repositories import matching_store as store
from recipe_wrangler.utils.nutrition_postgres import get_engine
from recipe_wrangler.utils.neo4j_utils import run_query

logger = logging.getLogger(__name__)
PROJECTION_PIPELINE_VERSION = 'allergen-reconciliation-v1'
_stop = threading.Event()
_thread = None


def reconcile_new_recipes() -> None:
    """Recover missed notifications, including imports using the graph directly."""
    rows = run_query('''MATCH (r:Recipe)
        WHERE r.recipe_id IS NOT NULL AND (r.matching_projection_release IS NULL OR
            coalesce(r.matching_input_revision,'')<>coalesce(r.matching_projected_revision,''))
        RETURN r.recipe_id AS id LIMIT 100''')
    with get_engine().begin() as c:
        for row in rows:
            if row['id']:
                # Reconciliation must not keep incrementing an already pending job.
                c.execute(text('''INSERT INTO matching_projection_jobs(recipe_id) VALUES(:id)
                    ON CONFLICT(recipe_id) DO UPDATE SET
                    generation=matching_projection_jobs.generation+1,requested_at=now()
                    WHERE matching_projection_jobs.generation=matching_projection_jobs.completed_generation'''),
                    {'id': row['id']})


def drain_once(recipe_ids: list[str] | None = None) -> int:
    deployment = store.deployment()
    if deployment['mode'] != 'new':
        return 0
    with get_engine().connect() as lock:
        if not lock.execute(text("SELECT pg_try_advisory_lock(hashtextextended('matching_projection_worker',0))")).scalar():
            return 0
        try:
            if recipe_ids is None:reconcile_new_recipes()
            elif not recipe_ids:return 0
            recipe_filter=' AND recipe_id=ANY(:ids)' if recipe_ids is not None else ''
            jobs = [dict(r) for r in lock.execute(text('''SELECT recipe_id,generation FROM matching_projection_jobs
                WHERE generation>completed_generation'''+recipe_filter+''' ORDER BY requested_at LIMIT 20'''),
                {'ids':recipe_ids} if recipe_ids is not None else {}).mappings()]
            for job in jobs:
                recipe_id = job['recipe_id']
                try:
                    revision = run_query('''MATCH (r:Recipe {recipe_id:$id})
                        RETURN r.matching_input_revision AS revision''', {'id': recipe_id})
                    from recipe_wrangler.matching.graph_sync import bridge
                    report = bridge(deployment['release_id'], True, [recipe_id],
                                    active_only=True, write_report=False)
                    from recipe_wrangler.matching.claims import refresh_claims
                    refresh_claims([recipe_id])
                    from recipe_wrangler.utils.dietary_classification import refresh_recipe_suitability
                    refresh_recipe_suitability([recipe_id])
                    from recipe_wrangler.catalog.projection import project
                    project(recipe_id)
                    from recipe_wrangler.utils.recipe_cache import cache_delete
                    cache_delete(recipe_id, strict=True)
                    run_query('''MATCH (r:Recipe {recipe_id:$id})
                        SET r.matching_projection_release=$release,
                            r.matching_projection_status=$status,
                            r.matching_projected_revision=$revision''',
                        {'id': recipe_id, 'release': deployment['release_id'],
                         'status': 'projected' if report['projected_runs'] else 'awaiting_profile',
                         'revision': revision[0]['revision'] if revision else None})
                    with get_engine().begin() as c:
                        c.execute(text('''UPDATE matching_projection_jobs SET
                            completed_generation=GREATEST(completed_generation,:generation),
                            completed_at=now(),last_error=NULL,attempts=attempts+1 WHERE recipe_id=:recipe_id'''), job)
                except Exception as exc:
                    logger.exception('Matching projection failed for %s', recipe_id)
                    with get_engine().begin() as c:
                        c.execute(text('''UPDATE matching_projection_jobs SET last_error=:error,
                            attempts=attempts+1,requested_at=now() WHERE recipe_id=:id'''),
                            {'id': recipe_id, 'error': type(exc).__name__ + ': ' + str(exc)[:500]})
            return len(jobs)
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtextextended('matching_projection_worker',0))"))


def background_enabled() -> bool:
    """Startup-only control; manual bounded drains remain available."""
    value = os.getenv('MATCHING_PROJECTION_WORKER_ENABLED', '1')
    if value not in {'0', '1'}:
        raise ValueError('MATCHING_PROJECTION_WORKER_ENABLED must be 0 or 1')
    return value == '1'


def start() -> None:
    global _thread
    if not background_enabled():
        logger.info('Background matching projection disabled by startup configuration')
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    def work():
        while not _stop.is_set():
            try:
                count = drain_once()
            except Exception:
                logger.exception('Matching projection worker failed; retrying')
                count = 0
            _stop.wait(1 if count else 15)
    _thread = threading.Thread(target=work, name='matching-projections', daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=30)
