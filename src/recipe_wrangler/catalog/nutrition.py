"""Nutrition profiles as they appear on a catalog document.

One definition, two callers. The corpus rebuild loads every profile in a single
Postgres pass; the per-recipe commit loads one. They must produce byte-identical
documents or a rebuild would silently rewrite what a create had just written —
so the shaping lives here and the loading strategy lives with each caller.

The flat ``nutri_score_<region>`` fields matter more than they look. Unfiltered
browse filters on ``exists: nutri_score_eu`` as its "has been profiled" marker,
so a document without them is not merely missing a score: it cannot appear in
browse at all. A recipe created through the API had exactly that shape, because
the per-recipe projection never loaded profiles — it was profiled in Postgres
and unprofiled everywhere anyone could see.
"""

from __future__ import annotations

from typing import Any

from recipe_wrangler.catalog import sources as S

# Nutrition sources that get their own flat score fields on the document.
# Keyed by the Postgres `nutrition_source` value, valued by the field suffix.
FLAT_REGIONS: dict[str, str] = {
    "eu": "eu",
    "irish": "ie",
    "hungarian": "hu",
    "usda": "us",
    "slovenian": "si",
}


def _clean(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def profile_summary(row: Any, *, nutri_label) -> dict[str, Any]:
    """Shape one Postgres profile row into a `profiles[]` entry."""
    score = row["nutri_score"] if isinstance(row["nutri_score"], dict) else {}
    label = nutri_label(score.get("nutri_score") or score.get("label"))
    source = S.resolve(row["source"])
    nutrition_source = _clean(row["nutrition_source"])
    return {
        "nutrition_source": nutrition_source,
        "region": nutrition_source,
        "nutri_score": label,
        "nutri_color": _clean(score.get("color")) or None,
        "nutri_points": _float(score.get("score")),
        "sust_per_serving": _float(row["total_sustainability_per_serving"]),
        "pipeline_version": _clean(row["pipeline_version"]) or None,
        "computed_at": (
            row["computed_at"].isoformat() if row["computed_at"] else None
        ),
        "is_ground_truth": bool(
            source and nutrition_source in source.ground_truth_nutrition_sources
        ),
    }


def apply_profiles(doc: dict[str, Any], profiles: list[dict[str, Any]]) -> None:
    """Attach profiles and their derived fields to a document, in place.

    A no-op for a recipe with no profiles, which is a legitimate state — an
    unprofiled recipe is still a recipe. It simply will not surface in browse
    until profiling runs, which is the behaviour browse intends.
    """
    if not profiles:
        return

    from recipe_wrangler.catalog.entities import NUTRI_RANKS

    doc["profiles"] = profiles
    doc["nutrition_sources"] = sorted(
        {p["nutrition_source"] for p in profiles if p["nutrition_source"]}
    )
    doc["regions_available"] = doc["nutrition_sources"]

    ranks = [p["nutri_score"] for p in profiles if p["nutri_score"]]
    if ranks:
        doc["best_nutri_rank"] = min(NUTRI_RANKS[label] for label in ranks)

    for profile in profiles:
        region = FLAT_REGIONS.get(profile["nutrition_source"])
        if region and profile["nutri_score"]:
            doc[f"nutri_score_{region}"] = profile["nutri_score"]
            if profile["nutri_color"]:
                doc[f"nutri_color_{region}"] = profile["nutri_color"]
            if profile["nutri_points"] is not None:
                doc[f"nutri_points_{region}"] = profile["nutri_points"]
        sust = profile.get("sust_per_serving")
        if sust is not None:
            doc.setdefault("sust_per_serving", sust)

    doc["has_ground_truth_nutrition"] = any(p["is_ground_truth"] for p in profiles)

    newest = max((p["computed_at"] for p in profiles if p["computed_at"]), default=None)
    if newest:
        doc["profiled_at"] = newest


PROFILE_COLUMNS = (
    "recipe_id, nutrition_source, source, nutri_score, "
    "total_sustainability_per_serving, pipeline_version, computed_at"
)


def load_profiles_for(recipe_id: str, *, nutri_label) -> list[dict[str, Any]]:
    """Every profile Postgres holds for one recipe.

    The single-recipe counterpart to the rebuild's full-table scan. Failure is
    swallowed and logged by the caller rather than raised: a projection that
    fails because nutrition is briefly unavailable would leave the recipe out
    of search entirely, which is strictly worse than one without a score.
    """
    from sqlalchemy import text

    from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection

    table = _get_config()["profiles_table"]
    with get_connection() as conn:
        rows = conn.execute(
            text(
                f'SELECT {PROFILE_COLUMNS} FROM "{table}" WHERE recipe_id = :rid'
            ),
            {"rid": recipe_id},
        ).mappings()
        return [profile_summary(row, nutri_label=nutri_label) for row in rows]


# --------------------------------------------------------------------------- #
# Per-serving macros.
#
# Elasticsearch carries a Nutri-Score but not the macros, so these stay a
# Postgres read whichever store supplied the recipe. Public and living here
# because three callers need them — the FoodChat candidate path, the meal
# planner, and the plan-nutrition attachment — and two of them were reaching
# across packages for a private name, which is the usual sign that something
# is in the wrong module rather than that the underscore should be ignored.
# --------------------------------------------------------------------------- #
# The same nutrient may be keyed either way depending on which pipeline wrote
# the trace. Trying both is what stops a macro filter silently passing every
# recipe because it read `None` for all of them.
NUTRIENT_KEYS: dict[str, tuple[str, ...]] = {
    "calories": ("energy_kcal", "calories"),
    "protein_g": ("protein_g",),
    "carbs_g": ("carbohydrate_g", "carbs_g"),
    "fat_g": ("fat_g",),
    # Carried because the client's macro breakdown shows four segments. Without
    # it every meal card had to fetch the whole recipe again just to fill in one
    # number — 21 extra requests to render a weekly plan, for data the plan
    # response was already most of the way to containing.
    "fiber_g": ("fiber_g", "fibre_g", "dietary_fiber_g"),
}


def per_serving(raw: dict[str, Any] | None) -> dict[str, float | None] | None:
    """Per-serving macros from a Postgres nutrition row."""
    if not raw:
        return None
    nutrients = (
        raw.get("total_nutrients_per_serving") or raw.get("total_nutrients") or {}
    )
    if not isinstance(nutrients, dict):
        return None

    def pick(*keys: str) -> float | None:
        for key in keys:
            value = nutrients.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
        return None

    return {name: pick(*keys) for name, keys in NUTRIENT_KEYS.items()}


def within_targets(macros: dict[str, float | None] | None, profile: Any) -> bool:
    """Whether a recipe meets the macro targets.

    An unprofiled recipe passes. Excluding it would silently restrict the plan
    to profiled recipes whenever any target is set, which is a different filter
    from the one the member asked for.

    A missing individual macro also passes, for the same reason: "we do not know
    this recipe's protein" is not "this recipe fails the protein target".
    """
    if not profile or macros is None:
        return True

    bounds = (
        ("calories", profile.min_calories, profile.max_calories),
        ("protein_g", profile.min_protein_g, profile.max_protein_g),
        ("carbs_g", profile.min_carbs_g, profile.max_carbs_g),
        ("fat_g", profile.min_fat_g, profile.max_fat_g),
    )
    for name, low, high in bounds:
        value = macros.get(name)
        if value is None:
            continue
        if low is not None and value < low:
            return False
        if high is not None and value > high:
            return False
    return True


