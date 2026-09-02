"""Fixed, explainable recipe-cost categories built from internal estimates.

The numeric calculation stays internal.  Consumers receive only the relative
Low/Medium/High category, evidence coverage, and the ingredient cost drivers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

import numpy as np


@dataclass(frozen=True)
class RecipeCostCategoryConfig:
    """Versioned rules for recipe categorisation and explanation."""

    calibration_version: str = "recipe-cost-2026"
    calibration_min_weight_coverage: float = 0.90
    moderate_min_weight_coverage: float = 0.75
    min_calibration_recipes: int = 100
    driver_min_contribution_pct: float = 15.0
    max_reported_drivers: int = 3


DEFAULT_RECIPE_COST_CONFIG = RecipeCostCategoryConfig()


@dataclass(frozen=True)
class RecipeCostCalibration:
    """Frozen EU per-serving reference distribution for recipe categories."""

    calibration_version: str
    q33_cost_per_serving_eur: float
    q67_cost_per_serving_eur: float
    reference_recipe_count: int
    minimum_weight_coverage: float
    quantile_method: str = "linear"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RecipeCostCalibration":
        return cls(
            calibration_version=str(payload["calibration_version"]),
            q33_cost_per_serving_eur=float(payload["q33_cost_per_serving_eur"]),
            q67_cost_per_serving_eur=float(payload["q67_cost_per_serving_eur"]),
            reference_recipe_count=int(payload["reference_recipe_count"]),
            minimum_weight_coverage=float(payload["minimum_weight_coverage"]),
            quantile_method=str(payload.get("quantile_method", "linear")),
        )


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _coverage(profile: Mapping[str, Any]) -> float:
    return _number(profile.get("cost_weight_coverage")) or 0.0


def _internal_cost_per_serving(profile: Mapping[str, Any]) -> float | None:
    """Return the internal cost used consistently for calibration/scoring."""

    return _number(
        profile.get("matched_cost_lower_bound_per_serving_eur")
        or profile.get("estimated_recipe_cost_per_serving_eur")
    )


def _unresolved_weights(profile: Mapping[str, Any]) -> list[str]:
    values = profile.get("unresolved_weight_ingredients") or []
    return [str(value) for value in values if str(value).strip()]


def build_recipe_cost_calibration(
    profiles: Iterable[Mapping[str, Any]],
    *,
    config: RecipeCostCategoryConfig = DEFAULT_RECIPE_COST_CONFIG,
) -> RecipeCostCalibration:
    """Freeze Q33/Q67 from sufficiently covered internal EU recipe estimates."""

    values: list[float] = []
    for profile in profiles:
        cost_per_serving = _internal_cost_per_serving(profile)
        if cost_per_serving is None or cost_per_serving <= 0:
            continue
        if _coverage(profile) < config.calibration_min_weight_coverage:
            continue
        if _unresolved_weights(profile):
            continue
        values.append(cost_per_serving)
    if len(values) < config.min_calibration_recipes:
        raise ValueError(
            "Not enough high-coverage recipes to calibrate cost categories: "
            f"need {config.min_calibration_recipes}, found {len(values)}"
        )
    q33, q67 = np.quantile(
        np.asarray(values, dtype=float), [1 / 3, 2 / 3], method="linear"
    )
    if q33 <= 0 or q67 <= q33:
        raise ValueError("Recipe-cost calibration thresholds are invalid")
    return RecipeCostCalibration(
        calibration_version=config.calibration_version,
        q33_cost_per_serving_eur=float(q33),
        q67_cost_per_serving_eur=float(q67),
        reference_recipe_count=len(values),
        minimum_weight_coverage=config.calibration_min_weight_coverage,
    )


def save_recipe_cost_calibration(
    calibration: RecipeCostCalibration, *, region: str
) -> None:
    """Persist a new active regional calibration, retaining prior rows for audit."""

    from sqlalchemy import text

    from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection

    normalized_region = region.strip().upper()
    if normalized_region not in {"EU", "IE", "HU", "SI"}:
        raise ValueError(f"Unsupported cost-calibration region: {region!r}")
    schema = _get_config()["schema"]
    table = f'"{schema}"."cost_recipe_calibrations"'
    payload = {"region": normalized_region, **calibration.to_dict()}
    with get_connection() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text(f"UPDATE {table} SET is_active = false WHERE region = :region"),
                payload,
            )
            connection.execute(
                text(
                    f"""
                    INSERT INTO {table} (
                        region, calibration_version, q33_cost_per_serving_eur,
                        q67_cost_per_serving_eur, reference_recipe_count,
                        minimum_weight_coverage, quantile_method, is_active
                    ) VALUES (
                        :region, :calibration_version, :q33_cost_per_serving_eur,
                        :q67_cost_per_serving_eur, :reference_recipe_count,
                        :minimum_weight_coverage, :quantile_method, true
                    )
                    """
                ),
                payload,
            )
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise


def load_recipe_cost_calibration(region: str) -> RecipeCostCalibration:
    """Load the active regional calibration from PostgreSQL."""

    from sqlalchemy import text

    from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection

    normalized_region = region.strip().upper()
    if normalized_region not in {"EU", "IE", "HU", "SI"}:
        raise ValueError(f"Unsupported cost-calibration region: {region!r}")
    schema = _get_config()["schema"]
    table = f'"{schema}"."cost_recipe_calibrations"'
    with get_connection() as connection:
        row = connection.execute(
            text(
                f"""
                SELECT calibration_version, q33_cost_per_serving_eur,
                       q67_cost_per_serving_eur, reference_recipe_count,
                       minimum_weight_coverage, quantile_method
                FROM {table}
                WHERE region = :region AND is_active
                """
            ),
            {"region": normalized_region},
        ).mappings().first()
    if row is None:
        raise LookupError(
            f"No active PostgreSQL recipe-cost calibration for {normalized_region}."
        )
    return RecipeCostCalibration.from_dict(dict(row))


def _category(cost_per_serving: float, calibration: RecipeCostCalibration) -> tuple[str, int]:
    if cost_per_serving <= calibration.q33_cost_per_serving_eur:
        return "low", 1
    if cost_per_serving <= calibration.q67_cost_per_serving_eur:
        return "medium", 2
    return "high", 3


def _ingredient_price_class(tier: object) -> str | None:
    return {"€": "lower-cost", "€€": "medium-cost", "€€€": "higher-cost"}.get(
        str(tier)
    )


def _contributors(
    profile: Mapping[str, Any], config: RecipeCostCategoryConfig
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    total = _number(profile.get("matched_cost_lower_bound_eur")) or 0.0
    if total <= 0:
        return [], []
    rows: list[dict[str, Any]] = []
    for item in profile.get("ingredients") or []:
        if not isinstance(item, Mapping) or item.get("cost_status") != "costed":
            continue
        ingredient_cost = _number(item.get("ingredient_cost_eur"))
        if ingredient_cost is None or ingredient_cost <= 0:
            continue
        contribution_pct = 100 * ingredient_cost / total
        rows.append(
            {
                "ingredient": str(item.get("ingredient_name") or "ingredient"),
                "matched_product": item.get("matched_canonical_name"),
                "price_scope": item.get("price_scope"),
                "price_class": _ingredient_price_class(
                    item.get("global_cost_tier")
                ),
                "cost_contribution_pct": round(contribution_pct, 1),
            }
        )
    rows.sort(key=lambda item: item["cost_contribution_pct"], reverse=True)
    main = [
        item
        for index, item in enumerate(rows, start=1)
        if (
            index <= config.max_reported_drivers
            and item["cost_contribution_pct"] >= config.driver_min_contribution_pct
        )
    ]
    return main, rows


def classify_recipe_cost_profile(
    profile: Mapping[str, Any],
    calibration: RecipeCostCalibration,
    *,
    region: str = "EU",
    config: RecipeCostCategoryConfig = DEFAULT_RECIPE_COST_CONFIG,
) -> dict[str, Any]:
    """Return the public, non-monetary cost facet and mandatory explanation."""

    coverage = _coverage(profile)
    ingredient_count = int(profile.get("ingredient_count") or 0)
    priced_count = int(profile.get("matched_ingredient_count") or 0)
    internal_cost = _internal_cost_per_serving(profile)
    main_drivers, contributors = _contributors(profile, config)
    facet: dict[str, Any] = {
        "region": region.strip().upper(),
        "priced_weight_coverage": round(coverage, 4),
        "priced_ingredient_coverage": round(
            priced_count / ingredient_count, 4
        ) if ingredient_count else 0.0,
        "priced_ingredient_count": priced_count,
        "ingredient_count": ingredient_count,
        "contributors": contributors,
    }
    if internal_cost is None or internal_cost <= 0:
        facet.update(
            status="unavailable",
            category=None,
            category_code=None,
            explanation="Cost category unavailable because no ingredients could be priced.",
        )
        return facet
    category, code = _category(internal_cost, calibration)
    label = category.capitalize()
    facet.update(
        status="classified",
        category=category,
        category_code=code,
    )
    if main_drivers:
        names = [str(driver["ingredient"]) for driver in main_drivers]
        contribution = sum(float(driver["cost_contribution_pct"]) for driver in main_drivers)
        facet["explanation"] = (
            f"{label}-cost recipe. "
            f"{', '.join(names)} {'is' if len(names) == 1 else 'are'} the main "
            f"cost driver{'s' if len(names) != 1 else ''}, together accounting "
            f"for {contribution:.1f}% of the estimated ingredient cost."
        )
    elif contributors:
        largest = contributors[0]
        facet["explanation"] = (
            f"{label}-cost recipe. Cost is distributed across its ingredients; "
            f"{largest['ingredient']} is the largest contributor at "
            f"{largest['cost_contribution_pct']:.1f}% of the estimated ingredient cost."
        )
    else:
        facet["explanation"] = (
            f"{label}-cost recipe based on its estimated ingredient cost per serving "
            "relative to the fixed WiseFood reference distribution."
        )
    return facet
