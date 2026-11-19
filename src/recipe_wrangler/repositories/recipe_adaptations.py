"""Read stored adaptations (table recipe_adaptations: one row per recipe, mode and region)."""

from sqlalchemy import text

from recipe_wrangler.utils.nutrition_postgres import get_engine

_SQL = text(
    "SELECT recipe_id, mode, region, version, status, is_adapted, reason_code, original, adapted, swap, benefit, provenance "
    "FROM recipe_adaptations WHERE recipe_id = :recipe_id AND mode = :mode AND region = :region"
)


def get_adaptation(recipe_id: str, mode: str, region: str) -> dict | None:
    with get_engine().connect() as conn:
        row = conn.execute(_SQL, {"recipe_id": recipe_id, "mode": mode, "region": region}).mappings().first()
    return dict(row) if row else None
