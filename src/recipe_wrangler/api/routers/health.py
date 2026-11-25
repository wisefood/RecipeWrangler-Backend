"""Health check router."""

from fastapi import APIRouter

router = APIRouter(tags=["ops"])


@router.get("/health")
def healthcheck() -> dict[str, str]:
    """Simple readiness probe."""
    return {"status": "ok"}


@router.get('/health/matching')
def matching_health() -> dict:
    from sqlalchemy import text
    from recipe_wrangler.utils.nutrition_postgres import get_engine
    from recipe_wrangler.utils.consumer_suitability import composition_policy_versions
    from recipe_wrangler.matching.projection_worker import PROJECTION_PIPELINE_VERSION, background_enabled
    import os
    with get_engine().connect() as c:
        c.execute(text('SET TRANSACTION READ ONLY'))
        c.execute(text("SET LOCAL statement_timeout='60s'"))
        selected=dict(c.execute(text('SELECT release_id,mode FROM matching_deployment WHERE singleton')).mappings().one())
        pending=c.execute(text('SELECT count(*) FROM matching_projection_jobs WHERE generation>completed_generation')).scalar()
    return {**selected,'runtime_mode':os.getenv('NUTRITION_MATCHER_MODE','legacy'),
            'pending_projections':pending,'composition_policy_versions':composition_policy_versions(),
            'projection_pipeline_version':PROJECTION_PIPELINE_VERSION,
            'background_projection_enabled':background_enabled()}
