"""Reusable, explainable ingredient and recipe cost estimation."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from .constants import TARGET_COUNTRIES
from .recipe_cost_categories import (
    RecipeCostCalibration,
    RecipeCostCategoryConfig,
    classify_recipe_cost_profile,
)


ALIAS_PATH = Path(__file__).with_name("cost_ingredient_aliases.json")
SUPPORTED_COST_REGIONS = ("EU", *TARGET_COUNTRIES)
PRICE_COLUMNS = {
    "EU": "eu_reference_price_eur_kg",
    "IE": "price_ie_eur_kg",
    "HU": "price_hu_eur_kg",
    "SI": "price_si_eur_kg",
}
FOODON_COST_GROUP_CATEGORIES = {
    "cereals": "cereals",
    "dairy_eggs": "dairy and eggs",
    "fish_seafood": "fish and seafood",
    "fruit_nuts": "fruit and nuts",
    "meat": "meat",
    "oils_fats": "oils and fats",
    "sugar_confectionery": "sugar and confectionery",
    "vegetables_pulses": "vegetables and pulses",
}
FOODON_COST_GROUP_ANCHORS = (
    ("cereals", "FOODON_00001709"),
    ("cereals", "FOODON_00001093"),
    ("cereals", "FOODON_00001211"),
    ("dairy_eggs", "FOODON_00001256"),
    ("dairy_eggs", "FOODON_00001274"),
    ("fish_seafood", "FOODON_00001248"),
    ("fish_seafood", "FOODON_00001046"),
    ("fruit_nuts", "FOODON_03315615"),
    ("fruit_nuts", "FOODON_00001172"),
    ("meat", "FOODON_00001006"),
    ("meat", "FOODON_00001131"),
    ("oils_fats", "FOODON_00001087"),
    ("oils_fats", "FOODON_03310719"),
    ("oils_fats", "FOODON_03316694"),
    ("sugar_confectionery", "FOODON_03420108"),
    ("sugar_confectionery", "FOODON_00001149"),
    ("vegetables_pulses", "FOODON_00001261"),
    ("vegetables_pulses", "FOODON_00002683"),
    ("vegetables_pulses", "FOODON_00001209"),
)
UNSAFE_GROUP_CONTEXT_TOKENS = frozenset(
    {
        "bouillon", "broth", "chutney", "dip", "dressing", "extract",
        "juice", "paste", "powder", "puree", "sauce", "seasoning", "soup",
        "spread", "stock", "vinegar",
    }
)

# A base word inside one of these concepts is not enough evidence of economic
# equivalence. Exact catalogue details and reviewed aliases are resolved before
# this guard, so supported forms such as canned tuna still match normally.
NON_EQUIVALENT_CONTEXT_TOKENS = frozenset(
    {
        "almond",
        "broth",
        "bouillon",
        "coconut",
        "extract",
        "flavor",
        "flavour",
        "flour",
        "juice",
        "oat",
        "paste",
        "powder",
        "puree",
        "rice",
        "sauce",
        "seasoning",
        "soup",
        "soy",
        "stock",
        "syrup",
        "vinegar",
    }
)


def normalize_cost_name(value: object) -> str:
    """Normalize a product name without using opaque semantic similarity."""

    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    unaccented = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", unaccented).split())


def _plural_forms(name: str) -> set[str]:
    """Generate conservative English plural keys for exact base matching."""

    words = name.split()
    if not words:
        return set()
    last = words[-1]
    if last.endswith("y") and len(last) > 1 and last[-2] not in "aeiou":
        plural = last[:-1] + "ies"
    elif last.endswith(("s", "x", "z", "ch", "sh")):
        plural = last + "es"
    elif last in {"potato", "tomato"}:
        plural = last + "es"
    else:
        plural = last + "s"
    return {" ".join([*words[:-1], plural])}


def _optional_text(value: Any) -> str | None:
    if pd.isna(value) or str(value).strip() == "":
        return None
    return str(value)


def _row_payload(
    row: pd.Series,
    *,
    requested_name: str,
    resolved_name: str,
    country: str,
    match_method: str,
    match_confidence: str,
) -> dict[str, Any]:
    price = float(row[PRICE_COLUMNS[country]])
    eu_price = float(row["eu_reference_price_eur_kg"])
    level = str(row["product_level"])
    detail = _optional_text(row.get("product_detail"))
    if level == "detail" and detail:
        price_scope = "detailed_product"
    else:
        price_scope = "base_product"
    if match_method.startswith("exact_detail"):
        mapping_explanation = "Matched an explicitly priced detailed product."
    elif match_method.startswith("exact_base"):
        mapping_explanation = "Matched the canonical base product directly."
    elif match_method == "exact_cost_product_id":
        mapping_explanation = "Used an explicit upstream cost-product mapping."
    elif price_scope == "base_product":
        mapping_explanation = (
            "No supported exact detailed form was found; used the canonical "
            "base-product reference, derived as the median of available details."
        )
    else:
        mapping_explanation = "Used a reviewed alias to a supported detailed product."
    return {
        "requested_name": requested_name,
        "resolved_from_name": resolved_name,
        "match_status": "matched",
        "match_method": match_method,
        "cost_match_confidence": match_confidence,
        "mapping_explanation": mapping_explanation,
        "matched_product_id": row["product_id"],
        "matched_canonical_name": row["canonical_name"],
        "matched_product_detail": detail,
        "price_scope": price_scope,
        "economic_reference_price_eur_kg": price,
        "country_price_index": price / eu_price,
        "global_cost_tier": row["global_cost_tier"],
        "within_category_position": _optional_text(
            row.get("within_category_position")
        ),
        "parent_within_category_position": _optional_text(
            row.get("parent_within_category_position")
        ),
        "price_evidence_confidence": row["price_evidence_confidence"],
        "cost_reference_version": row["cost_reference_version"],
    }


class CostCatalogue:
    """In-memory resolver for the versioned classified price catalogue."""

    def __init__(
        self,
        classified: pd.DataFrame,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        required = {
            "product_id",
            "source_ingredient_id",
            "canonical_name",
            "product_detail",
            "product_level",
            "food_category",
            "global_cost_tier",
            "price_evidence_confidence",
            "cost_reference_version",
            *PRICE_COLUMNS.values(),
        }
        missing = required.difference(classified.columns)
        if missing:
            raise ValueError(f"Cost catalogue is missing columns: {sorted(missing)}")
        if classified["product_id"].duplicated().any():
            duplicates = classified.loc[
                classified["product_id"].duplicated(keep=False), "product_id"
            ].unique()
            raise ValueError(f"Duplicate cost product IDs: {sorted(duplicates)}")

        self.frame = classified.copy()
        self.by_product_id = self.frame.set_index("product_id", drop=False)
        bases = self.frame[self.frame["product_level"].eq("base")]
        details = self.frame[
            self.frame["product_level"].eq("detail")
            & self.frame["product_detail"].fillna("").astype(str).str.strip().ne("")
        ]
        self.base_by_name: dict[str, str] = {}
        for row in bases.itertuples(index=False):
            key = normalize_cost_name(row.canonical_name)
            self.base_by_name[key] = row.product_id
            for plural in _plural_forms(key):
                self.base_by_name.setdefault(plural, row.product_id)

        self.detail_by_name: dict[str, str] = {}
        for row in details.itertuples(index=False):
            canonical = normalize_cost_name(row.canonical_name)
            detail = normalize_cost_name(row.product_detail)
            self.detail_by_name[f"{canonical} {detail}"] = row.product_id
            self.detail_by_name[f"{detail} {canonical}"] = row.product_id
            self.detail_by_name[normalize_cost_name(row.source_ingredient_id)] = (
                row.product_id
            )

        self.aliases = {
            normalize_cost_name(alias): product_id
            for alias, product_id in (aliases or {}).items()
        }
        unknown_aliases = set(self.aliases.values()).difference(self.by_product_id.index)
        if unknown_aliases:
            raise ValueError(
                f"Cost aliases reference unknown products: {sorted(unknown_aliases)}"
            )

        # Broad FoodOn fallback values are medians of canonical base products,
        # never of detail rows. They are deliberately general economic proxies,
        # used only when no detail/base product can be resolved.
        self.group_references: dict[str, dict[str, Any]] = {}
        for group, category in FOODON_COST_GROUP_CATEGORIES.items():
            members = bases[bases["food_category"].eq(category)]
            if members.empty:
                continue
            eu_median = float(members[PRICE_COLUMNS["EU"]].median())
            representative = members.loc[
                (members[PRICE_COLUMNS["EU"]] - eu_median).abs().idxmin()
            ]
            self.group_references[group] = {
                "food_category": category,
                "prices": {
                    region: float(members[column].median())
                    for region, column in PRICE_COLUMNS.items()
                },
                "global_cost_tier": representative["global_cost_tier"],
                "cost_reference_version": representative["cost_reference_version"],
            }

    def resolve_group(
        self, ingredient_name: str, country: str, cost_group: str
    ) -> dict[str, Any]:
        """Resolve an approved broad FoodOn group to its base-product median."""

        country = country.strip().upper()
        if country not in SUPPORTED_COST_REGIONS:
            raise ValueError(
                f"country must be one of {SUPPORTED_COST_REGIONS}, got {country!r}"
            )
        group = str(cost_group or "").strip()
        reference = self.group_references.get(group)
        context_tokens = set(normalize_cost_name(ingredient_name).split())
        unsafe_group_context = context_tokens & UNSAFE_GROUP_CONTEXT_TOKENS
        if reference is None or unsafe_group_context:
            return {
                "requested_name": str(ingredient_name or "").strip(),
                "match_status": "unmatched",
                "match_method": None,
                "cost_match_confidence": None,
                "reason": (
                    "unsafe_processed_context_for_foodon_group"
                    if unsafe_group_context
                    else "no_supported_foodon_cost_group"
                ),
            }
        price = reference["prices"][country]
        eu_price = reference["prices"]["EU"]
        label = str(reference["food_category"])
        return {
            "requested_name": str(ingredient_name or "").strip(),
            "resolved_from_name": group,
            "match_status": "matched",
            "match_method": "foodon_group_fallback",
            "cost_match_confidence": "broad_group",
            "mapping_explanation": (
                "No supported detail or base product matched; used the median "
                "of canonical base products in the approved FoodOn economic group."
            ),
            "matched_product_id": f"group__{group}",
            "matched_canonical_name": f"{label} group",
            "matched_product_detail": None,
            "price_scope": "foodon_group",
            "economic_reference_price_eur_kg": price,
            "country_price_index": price / eu_price,
            "global_cost_tier": reference["global_cost_tier"],
            "within_category_position": None,
            "parent_within_category_position": None,
            "price_evidence_confidence": "Broad FoodOn group proxy",
            "cost_reference_version": reference["cost_reference_version"],
        }

    def resolve(
        self,
        ingredient_name: str,
        country: str,
        *,
        canonical_name: str | None = None,
        ingredient_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve detail first, then base, with an auditable fallback trail."""

        country = country.strip().upper()
        if country not in SUPPORTED_COST_REGIONS:
            raise ValueError(
                f"country must be one of {SUPPORTED_COST_REGIONS}, got {country!r}"
            )
        requested = str(ingredient_name or "").strip()
        if ingredient_id and str(ingredient_id) in self.by_product_id.index:
            return _row_payload(
                self.by_product_id.loc[str(ingredient_id)],
                requested_name=requested,
                resolved_name=str(ingredient_id),
                country=country,
                match_method="exact_cost_product_id",
                match_confidence="high",
            )
        candidates: list[tuple[str, str]] = []
        if ingredient_id:
            candidates.append((str(ingredient_id), "upstream_ingredient_id"))
        requested_tokens = set(normalize_cost_name(requested).split())
        if canonical_name:
            canonical_tokens = set(normalize_cost_name(canonical_name).split())
            # A nutrition identity is useful for spelling/variant recovery, but
            # it must not erase an economically meaningful processed context:
            # rice vinegar is not rice and chicken stock is not chicken meat.
            if not (
                requested_tokens.difference(canonical_tokens)
                & NON_EQUIVALENT_CONTEXT_TOKENS
            ):
                candidates.append((str(canonical_name), "upstream_canonical_name"))
        candidates.append((requested, "ingredient_name"))

        seen: set[str] = set()
        normalized_candidates: list[tuple[str, str, str]] = []
        for candidate, source in candidates:
            key = normalize_cost_name(candidate)
            if key and key not in seen:
                normalized_candidates.append((candidate, key, source))
                seen.add(key)

        for candidate, key, source in normalized_candidates:
            product_id = self.detail_by_name.get(key)
            if product_id:
                return _row_payload(
                    self.by_product_id.loc[product_id],
                    requested_name=requested,
                    resolved_name=candidate,
                    country=country,
                    match_method=f"exact_detail:{source}",
                    match_confidence="high",
                )

        for candidate, key, source in normalized_candidates:
            product_id = self.base_by_name.get(key)
            if product_id:
                confidence = "high" if source == "ingredient_name" else "medium"
                return _row_payload(
                    self.by_product_id.loc[product_id],
                    requested_name=requested,
                    resolved_name=candidate,
                    country=country,
                    match_method=f"exact_base:{source}",
                    match_confidence=confidence,
                )

        for candidate, key, source in normalized_candidates:
            product_id = self.aliases.get(key)
            if product_id:
                return _row_payload(
                    self.by_product_id.loc[product_id],
                    requested_name=requested,
                    resolved_name=candidate,
                    country=country,
                    match_method=f"reviewed_alias:{source}",
                    match_confidence="medium",
                )

        # A reviewed multi-word alias may be embedded in a descriptive source
        # phrase (for example, "(1lb) boneless loin pork chops"). Require one
        # unambiguous target and reject known non-equivalent contexts and explicit
        # alternatives. Single-word aliases stay exact-only to avoid accidental
        # matches inside compound dishes.
        for candidate, key, source in normalized_candidates:
            tokens = set(key.split())
            if "or" in tokens:
                continue
            matches = [
                (alias, product_id)
                for alias, product_id in self.aliases.items()
                if len(alias.split()) >= 2
                and re.search(rf"(?:^| ){re.escape(alias)}(?: |$)", key)
                and not (
                    tokens.difference(alias.split())
                    & NON_EQUIVALENT_CONTEXT_TOKENS
                )
            ]
            unique = {product_id for _, product_id in matches}
            if len(unique) == 1:
                product_id = next(iter(unique))
                return _row_payload(
                    self.by_product_id.loc[product_id],
                    requested_name=requested,
                    resolved_name=candidate,
                    country=country,
                    match_method=f"reviewed_alias_phrase:{source}",
                    match_confidence="medium",
                )

        # Conservative phrase fallback enables cases such as "boneless chicken
        # wings" while avoiding economically different products such as stock.
        for candidate, key, source in normalized_candidates:
            tokens = set(key.split())
            if "or" in tokens:
                continue
            matches = [
                (base_key, product_id)
                for base_key, product_id in self.base_by_name.items()
                if re.search(rf"(?:^| ){re.escape(base_key)}(?: |$)", key)
                and not (
                    tokens.difference(base_key.split())
                    & NON_EQUIVALENT_CONTEXT_TOKENS
                )
            ]
            unique = {product_id for _, product_id in matches}
            if len(unique) == 1:
                product_id = next(iter(unique))
                return _row_payload(
                    self.by_product_id.loc[product_id],
                    requested_name=requested,
                    resolved_name=candidate,
                    country=country,
                    match_method=f"base_phrase_fallback:{source}",
                    match_confidence="low",
                )

        return {
            "requested_name": requested,
            "match_status": "unmatched",
            "match_method": None,
            "cost_match_confidence": None,
            "reason": "no_supported_detail_or_base_product",
        }


@lru_cache(maxsize=1)
def load_cost_catalogue() -> CostCatalogue:
    """Load the runtime cost catalogue from PostgreSQL.

    The processed CSV/Parquet files are reproducible import artefacts, not a
    runtime source of truth. Run ``import_cost_catalogue_to_postgres.py`` after
    regenerating them.
    """

    from sqlalchemy import text

    from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection

    cfg = _get_config()
    schema = cfg["schema"]
    products = f'"{schema}"."cost_products"'
    prices = f'"{schema}"."cost_prices"'
    aliases = f'"{schema}"."cost_aliases"'
    query = text(
        f"""
        SELECT
            p.product_id,
            p.source_ingredient_id,
            p.canonical_name,
            COALESCE(p.product_detail, '') AS product_detail,
            p.product_level,
            p.food_category,
            p.global_cost_tier,
            p.price_evidence_confidence,
            p.cost_reference_version,
            MAX(cp.price_eur_kg) FILTER (WHERE cp.region = 'EU') AS eu_reference_price_eur_kg,
            MAX(cp.price_eur_kg) FILTER (WHERE cp.region = 'IE') AS price_ie_eur_kg,
            MAX(cp.price_eur_kg) FILTER (WHERE cp.region = 'HU') AS price_hu_eur_kg,
            MAX(cp.price_eur_kg) FILTER (WHERE cp.region = 'SI') AS price_si_eur_kg
        FROM {products} p
        JOIN {prices} cp ON cp.product_id = p.product_id
        GROUP BY
            p.product_id, p.source_ingredient_id, p.canonical_name,
            p.product_detail, p.product_level, p.food_category,
            p.global_cost_tier, p.price_evidence_confidence, p.cost_reference_version
        ORDER BY p.product_id
        """
    )
    with get_connection() as connection:
        frame = pd.read_sql(query, connection)
        alias_rows = connection.execute(
            text(f"SELECT alias_normalized, product_id FROM {aliases}")
        ).mappings().all()
    if frame.empty:
        raise RuntimeError(
            "PostgreSQL cost catalogue is empty. Run the cost catalogue import first."
        )
    if frame[list(PRICE_COLUMNS.values())].isna().any().any():
        raise RuntimeError("PostgreSQL cost catalogue has missing regional prices.")
    return CostCatalogue(
        frame,
        aliases={row["alias_normalized"]: row["product_id"] for row in alias_rows},
    )


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if pd.notna(parsed) else None


def _ingredient_weight_g(ingredient: Mapping[str, Any]) -> float | None:
    for key in ("weight_g", "weight_grams"):
        if key in ingredient:
            return _number(ingredient.get(key))
    if "ingredient_quantity_kg" in ingredient:
        quantity = _number(ingredient.get("ingredient_quantity_kg"))
        return None if quantity is None else 1000 * quantity
    return None


def calculate_recipe_cost_profile(
    ingredients: Sequence[Mapping[str, Any]],
    servings: float,
    country: str,
    *,
    catalogue: CostCatalogue | None = None,
    calibration: RecipeCostCalibration | Mapping[str, Any] | None = None,
    category_config: RecipeCostCategoryConfig | None = None,
) -> dict[str, Any]:
    """Calculate an explainable recipe estimate from normalized gram weights.

    A complete total is returned only when every ingredient has a positive
    weight and a supported price. Partial results are explicitly lower bounds.
    No recipe-level euro tier is assigned.
    """

    servings_value = _number(servings)
    if servings_value is None or servings_value <= 0:
        raise ValueError("servings must be positive")
    country = country.strip().upper()
    resolver = catalogue or load_cost_catalogue()

    rows: list[dict[str, Any]] = []
    matched_cost = 0.0
    total_positive_weight = 0.0
    costed_weight = 0.0
    unmatched: list[str] = []
    unresolved_weights: list[str] = []
    fallback_names: list[str] = []
    for position, ingredient in enumerate(ingredients):
        name = str(
            ingredient.get("name")
            or ingredient.get("ingredient")
            or ingredient.get("ingredient_name")
            or ""
        ).strip()
        weight_g = _ingredient_weight_g(ingredient)
        if weight_g is not None and weight_g < 0:
            raise ValueError(f"Ingredient weight cannot be negative: {name!r}")
        if weight_g is not None and weight_g > 0:
            total_positive_weight += weight_g
        resolved = resolver.resolve(
            name,
            country,
            canonical_name=_optional_text(
                ingredient.get("canonical_name")
                or ingredient.get("canonical_ingredient")
                or ingredient.get("cost_canonical_name")
            ),
            ingredient_id=_optional_text(
                ingredient.get("cost_ingredient_id")
                or ingredient.get("ingredient_id")
            ),
        )
        cost_group = _optional_text(
            ingredient.get("cost_group") or ingredient.get("foodon_cost_group")
        )
        if resolved["match_status"] != "matched" and cost_group:
            resolved = resolver.resolve_group(name, country, cost_group)
        row = {"position": position, "ingredient_name": name, "weight_g": weight_g}
        row.update(resolved)
        if weight_g is None or weight_g <= 0:
            row["cost_status"] = "unresolved_weight"
            row["ingredient_cost_eur"] = None
            unresolved_weights.append(name or f"ingredient_{position}")
        elif resolved["match_status"] != "matched":
            row["cost_status"] = "unmatched_product"
            row["ingredient_cost_eur"] = None
            unmatched.append(name or f"ingredient_{position}")
        else:
            cost = (weight_g / 1000.0) * float(
                resolved["economic_reference_price_eur_kg"]
            )
            row["cost_status"] = "costed"
            row["ingredient_cost_eur"] = cost
            matched_cost += cost
            costed_weight += weight_g
            if resolved.get("price_scope") == "base_product" and (
                "fallback" in str(resolved.get("match_method"))
                or str(resolved.get("match_method")).startswith("reviewed_alias")
            ):
                fallback_names.append(name)
        rows.append(row)

    complete = bool(rows) and not unmatched and not unresolved_weights
    weight_coverage = (
        costed_weight / total_positive_weight if total_positive_weight > 0 else 0.0
    )
    result: dict[str, Any] = {
        "status": "complete" if complete else "partial",
        "country": country,
        "servings": servings_value,
        "currency": "EUR",
        "pricing_basis": "economic reference price estimates",
        "cost_reference_version": next(
            (
                row.get("cost_reference_version")
                for row in rows
                if row.get("cost_reference_version")
            ),
            None,
        ),
        "matched_ingredient_count": sum(
            row.get("cost_status") == "costed" for row in rows
        ),
        "ingredient_count": len(rows),
        "costed_weight_g": costed_weight,
        "positive_weight_total_g": total_positive_weight,
        "cost_weight_coverage": weight_coverage,
        "matched_cost_lower_bound_eur": matched_cost,
        "matched_cost_lower_bound_per_serving_eur": matched_cost / servings_value,
        "estimated_recipe_cost_total_eur": matched_cost if complete else None,
        "estimated_recipe_cost_per_serving_eur": (
            matched_cost / servings_value if complete else None
        ),
        "recipe_cost_tier": None,
        "unmatched_ingredients": unmatched,
        "unresolved_weight_ingredients": unresolved_weights,
        "base_fallback_ingredients": fallback_names,
        "group_fallback_ingredients": [
            row["ingredient_name"]
            for row in rows
            if row.get("match_method") == "foodon_group_fallback"
        ],
        "ingredients": rows,
    }
    if complete:
        calculation_explanation = (
            f"Estimated economic reference cost in {country}: €{matched_cost:.2f} "
            f"total, or €{matched_cost / servings_value:.2f} per serving. "
            f"All {len(rows)} ingredients were costed."
        )
    else:
        calculation_explanation = (
            f"Partial economic reference cost in {country}: at least "
            f"€{matched_cost:.2f} (€{matched_cost / servings_value:.2f} per "
            f"serving) from {result['matched_ingredient_count']}/{len(rows)} "
            "ingredients. A complete recipe cost is withheld until every "
            "ingredient has a supported product and positive weight."
        )
    if fallback_names:
        calculation_explanation += (
            " General base-product prices were used for: "
            + ", ".join(fallback_names)
            + "."
        )
    result["calculation_explanation"] = calculation_explanation
    result["explanation"] = calculation_explanation + " No recipe-level category has been calibrated yet."
    if calibration is not None:
        resolved_calibration = (
            calibration
            if isinstance(calibration, RecipeCostCalibration)
            else RecipeCostCalibration.from_dict(calibration)
        )
        facet = classify_recipe_cost_profile(
            result,
            resolved_calibration,
            region=country,
            config=category_config or RecipeCostCategoryConfig(
                calibration_version=resolved_calibration.calibration_version
            ),
        )
        result["cost_facet"] = facet
        result["recipe_cost_category"] = facet["category"]
        result["recipe_cost_category_code"] = facet["category_code"]
        result["recipe_cost_tier"] = facet["category_code"]
        result["explanation"] = facet["explanation"]
    return result


def calculate_cost_from_profile(
    profile: Mapping[str, Any],
    country: str | None = None,
    *,
    catalogue: CostCatalogue | None = None,
    calibration: RecipeCostCalibration | Mapping[str, Any] | None = None,
    category_config: RecipeCostCategoryConfig | None = None,
) -> dict[str, Any]:
    """Adapter for RecipeState/profile dictionaries produced during imports."""

    ingredients = (
        profile.get("ingredients")
        or profile.get("nutrition_profiling_details")
        or []
    )
    if not isinstance(ingredients, Sequence) or isinstance(ingredients, (str, bytes)):
        raise ValueError("profile ingredients must be a sequence")
    normalized = [
        item if isinstance(item, Mapping) else {"name": str(item)}
        for item in ingredients
    ]
    raw_country = str(
        country
        or profile.get("region")
        or profile.get("nutrition_source")
        or "EU"
    ).strip().upper()
    resolved_country = {
        "IRISH": "IE",
        "IRELAND": "IE",
        "HUNGARIAN": "HU",
        "HUNGARY": "HU",
        "SLOVENIAN": "SI",
        "SLOVENIA": "SI",
        "EU27": "EU",
    }.get(raw_country, raw_country)
    servings = profile.get("serves")
    if servings is None and isinstance(profile.get("profiling_quality"), Mapping):
        servings = profile["profiling_quality"].get("serves")
    return calculate_recipe_cost_profile(
        normalized,
        servings,
        resolved_country,
        catalogue=catalogue,
        calibration=calibration,
        category_config=category_config,
    )


def calculate_recipe_batch(
    recipes: Iterable[Mapping[str, Any]],
    country: str,
    *,
    catalogue: CostCatalogue | None = None,
    calibration: RecipeCostCalibration | Mapping[str, Any] | None = None,
    category_config: RecipeCostCategoryConfig | None = None,
) -> list[dict[str, Any]]:
    """Cost already-normalized recipes without database or graph dependencies."""

    resolver = catalogue or load_cost_catalogue()
    results: list[dict[str, Any]] = []
    for recipe in recipes:
        estimate = calculate_cost_from_profile(
            recipe,
            country,
            catalogue=resolver,
            calibration=calibration,
            category_config=category_config,
        )
        results.append(
            {
                "recipe_id": recipe.get("recipe_id") or recipe.get("id"),
                "title": recipe.get("title"),
                **estimate,
            }
        )
    return results
