"""Read-only PostgreSQL boundary for controlled comparisons; no name rematching."""

from __future__ import annotations

from copy import deepcopy
import math
from collections.abc import Sequence

from pydantic import ValidationError

from recipe_wrangler.repositories import matching_store
from recipe_wrangler.matching.reviewed_aliases import RECORD_FILLS
from recipe_wrangler.matching.anatomy import source_anatomy
from recipe_wrangler.matching.quantities import valid_quantity_adjustment, resolve_quantity, effective_quantity
from recipe_wrangler.tools.nutrition_completeness import EU, METRICS, REGIONAL, native_values

from .comparison import compare_raw_part
from .occurrence_evidence import recover_source_evidence
from .comparison import composition_restriction_evidence
from .models import (
    ComparisonComposition, ComparisonConstraints, ComparisonIngredient, ComparisonQuantity,
    ComparisonResult, ComparisonSnapshot, CompositionBasis, CompositionRef, Evidence,
    NutritionSource, PropertyAssertion, QuantityBasisDeclaration, RawPartEdit,
)


VERSION = "comparison_postgres_v1"
SOURCES = {"eu", "irish", "hungarian", "slovenian"}


class ComparisonLoadError(ValueError):
    """A missing or contradictory source, distinct from a database outage."""

    def __init__(self, reason: str, *, blocked: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.blocked = blocked


def _evidence(reference: str, method: str, *, stated: bool = False, proxy: bool = False) -> Evidence:
    return Evidence(reference=reference, method=method, version=VERSION,
                    status="stated" if stated else "proxy" if proxy else "inferred")


def _assertions(annotation: dict, reference: str) -> dict[str, PropertyAssertion]:
    """Retain saved assertions; default empty lists do not establish absence."""
    values = dict(annotation.get("facets") or {})
    values.update({"qualifiers." + key: value for key, value in (annotation.get("qualifiers") or {}).items()})
    recovery = annotation.get("occurrence_recovery") or {}
    assertions = {}
    for field, value in values.items():
        if value is None:
            continue
        evidence_field = "main_ingredient" if field == "base_identity" else field if field.startswith("qualifiers.") else "facets." + field
        support = [item for item in annotation.get("evidence", [])
                   if item.get("field") == evidence_field and item.get("polarity") == "asserted"
                   and item.get("quote") and item["quote"] in (annotation.get("parsed_text") or annotation.get("input_text") or "")]
        recovered = [item for item in recovery.get("recovered", [])
                     if item.get("field") == field and item.get("value") == value]
        stated = bool(support) and all(item.get("status", "stated") == "stated" for item in support)
        if recovered:
            stated = all(item.get("evidence") and all(
                e.get("status") == "stated" and e.get("quote")
                and e["quote"] in (recovery.get("original_text") or "") for e in item["evidence"])
                         for item in recovered)
        if any(field in item.get("field", "") for item in recovery.get("conflicts", [])):
            stated = False
        if any(field in reason and any(word in reason for word in ("conflict", "disagreement"))
               for reason in annotation.get("review_reasons", [])):
            stated = False
        # A list needs support for its members, not just one matching modifier.
        if isinstance(value, list) and value:
            supported = {item.get("value") for item in annotation.get("modifiers", [])
                         if item.get("field") in {field, evidence_field} and item.get("polarity") == "asserted"
                         and isinstance(item.get("value"), str)}
            stated = stated and (bool(recovered) or set(value) <= supported)
        if field in {"skin_status", "bone_status"} and value == "absent":
            value = "skinless" if field == "skin_status" else "boneless"
        recovery_version = next((item["version"] for item in annotation.get("label_evidence_recovery", [])
                                 if item["field"] == field), None)
        assertions[field] = PropertyAssertion(value=value, evidence=_evidence(
            reference + "/" + evidence_field, recovery_version or annotation.get("annotation_version") or "saved_annotation",
            stated=stated,
        ))
    return assertions


def _same_number(left: object, right: object) -> bool:
    return (left is None and right is None) or (
        isinstance(left, (int, float)) and not isinstance(left, bool)
        and isinstance(right, (int, float)) and not isinstance(right, bool)
        and math.isfinite(left) and math.isfinite(right)
        and math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    )


def _same_values(left: dict, right: dict) -> bool:
    return all(_same_number(left.get(metric), right.get(metric)) for metric in METRICS)


def _native_carbohydrate_definition(profile: dict, source: dict, value: object) -> str | None:
    """The pinned EFSA reader maps this component before creating the EU wrapper."""
    observations = (profile.get('native_values') or {}).get('carbs_g') or []
    if (source.get('release') != 'EFSA EU FCDB 2026'
            or profile.get('file') != 'data/EFSA_EU_FCDB_2026/composition_values.csv'
            or value is None or not observations):
        return None
    for observation in observations:
        if not isinstance(observation, dict):
            return None
        unit = observation.get('raw_unit')
        if (unit not in {'Gram/100 gram', 'Milligram/100 gram'}
                or not _same_number(observation.get('value'), value)
                or not isinstance(observation.get('source_csv_row'), int)
                or isinstance(observation['source_csv_row'], bool) or observation['source_csv_row'] < 2):
            return None
        try:
            raw = float(str(observation['raw_value']).replace(',', '.'))
        except (ValueError, TypeError, KeyError):
            return None
        if not _same_number(raw / (1000 if unit == 'Milligram/100 gram' else 1), value):
            return None
    return f"{source['dataset']}:{source['release']}:native_component:carbohydrate"


def _legacy_efsa_carbohydrate_definition(row: dict, source: dict, value: object) -> str | None:
    """Compare retained native codes, never the importer's by-difference wrapper label."""
    nutrient = (row.get('nutrients') or {}).get('Carbohydrate, by difference') or {}
    code = nutrient.get('source_code')
    parts = code.split(':') if isinstance(code, str) else []
    dataset = source.get('dataset')
    if (source.get('release') != 'frozen_legacy_inventory' or not dataset
            or row.get('source') != dataset or row.get('id') != source.get('record_id')
            or not str(row.get('id', '')).startswith(dataset + ':')
            or row.get('source_url') != 'https://www.efsa.europa.eu/en/microstrategy/food-composition-data-2026'
            or len(parts) != 3 or parts[0] != 'EFSA' or not all(parts)
            or nutrient.get('unit') != 'g' or value is None
            or not _same_number(nutrient.get('value'), value)):
        return None
    return f"{dataset}:frozen_legacy_inventory:native_component:{parts[1]}"


def _composition(row: dict) -> ComparisonComposition:
    facts = source_anatomy(row["annotation"])
    ref = CompositionRef(release_id=row["release_id"], collection=row["collection"], candidate_id=row["candidate_id"])
    reference = f"matching_compositions/{ref.release_id}/{ref.collection}/{ref.candidate_id}"
    source_key = facts.get("source_key")
    if source_key not in SOURCES or ref.collection != "nutritional_ingredients_" + source_key:
        raise ComparisonLoadError("composition_collection_mismatch", blocked=True)
    if str(facts.get("native_id")) != row["native_id"] or str(row["candidate"].get("id")) != ref.candidate_id:
        raise ComparisonLoadError("composition_identity_mismatch", blocked=True)
    if facts.get("label") != row["candidate"].get("document"):
        raise ComparisonLoadError("composition_label_mismatch", blocked=True)
    nutrient_row = row["nutrient_row"]
    native_id = nutrient_row.get("canonical_food_id") if source_key in {"irish", "hungarian"} else nutrient_row.get("id")
    if native_id is not None and str(native_id) != row["native_id"]:
        raise ComparisonLoadError("native_nutrient_identity_mismatch", blocked=True)
    values = native_values(nutrient_row)
    if not _same_values(values, facts.get("values_per_100g") or {}):
        raise ComparisonLoadError("released_nutrient_values_mismatch", blocked=True)
    source = facts["source"]
    for name in ("source_record", "source_profile"):
        item = row.get(name)
        if item and (item.get("source") != source or item.get("label") != facts["label"]):
            raise ComparisonLoadError("released_source_identity_mismatch", blocked=True)
    if row.get("source_profile") and not _same_values(values, row["source_profile"].get("values_per_100g") or {}):
        raise ComparisonLoadError("native_profile_values_mismatch", blocked=True)
    basis = facts.get("basis") or {}
    if basis.get("unit") != "g" or basis.get("amount") != 100 or basis.get("part") != "edible_portion":
        raise ComparisonLoadError("unsupported_source_reporting_basis", blocked=True)
    text = facts.get("text_annotation") or {}
    properties = _assertions(text, reference + "/annotation/text_annotation")
    native = facts.get("native_annotation") or {}
    # Unreviewed native mappings stay distinct from text-supported statements.
    for field, value in (native.get("facets") or {}).items():
        if value != (text.get("facets") or {}).get(field):
            normalized = "skinless" if field == "skin_status" and value == "absent" else "boneless" if field == "bone_status" and value == "absent" else value
            properties[field] = PropertyAssertion(value=normalized, evidence=_evidence(
                reference + "/annotation/native_annotation/facets/" + field, "native_facet_mapping",
            ))
    from .nutritional_variants import recover_product_claims
    properties = recover_product_claims(properties, facts['label'], reference)
    definitions = {}
    carbohydrate_definition = _native_carbohydrate_definition(row.get('source_profile') or {}, source, values.get('carbs_g'))
    definition_method = 'pinned_native_reader_observations'
    definition_field = 'source_profile/native_values/carbs_g'
    if carbohydrate_definition is None:
        carbohydrate_definition = _legacy_efsa_carbohydrate_definition(nutrient_row, source, values.get('carbs_g'))
        definition_method = 'retained_efsa_component_code'
        definition_field = 'nutrient_row/nutrients/Carbohydrate, by difference/source_code'
    if carbohydrate_definition:
        definitions['carbs_g'] = carbohydrate_definition
    for metric in METRICS:
        # The EU wrapper's carbohydrate label does not establish its native definition.
        if metric == "carbs_g" or values.get(metric) is None:
            continue
        if "nutrients" in nutrient_row:
            label = EU[metric]
            if label not in nutrient_row["nutrients"]:
                label = "Energy" if metric == "energy_kcal" else "Sugars, total including NLEA" if metric == "sugar_g" else label
        else:
            label = next((key for key in REGIONAL[metric] if nutrient_row.get(key) is not None), None)
        if label:
            definitions[metric] = f"{source['dataset']}:{source['release']}:source_field:{label}"
    allergens, consumers = composition_restriction_evidence(facts["label"])
    return ComparisonComposition(
        ref=ref, source_dataset=source["dataset"], source_release=source["release"],
        basis=CompositionBasis(amount_g=100, portion="edible", evidence=_evidence(
            reference + "/annotation/basis", basis.get("evidence") or "unknown_basis",
            stated=basis.get("evidence") == "native_reader" and bool(row.get("source_profile") and (
                row["source_profile"].get("native_values")
                or row["source_profile"].get("basis") == "per 100 g edible food in selected state")),
            proxy=basis.get("evidence") == "legacy_contract_assumption",
        )), properties=properties, values_per_100g=values, nutrient_definitions=definitions,
        allergen_assessments=allergens, consumer_assessments=consumers,
        blockers=facts.get("blockers") or [], provenance={**deepcopy(row),
            **({'nutrient_definition_evidence': {'carbs_g': {
                'method': definition_method, 'definition': carbohydrate_definition,
                'source_field': definition_field}}} if carbohydrate_definition else {}),
            **({"label_evidence_recovery": deepcopy(text["label_evidence_recovery"])}
               if text.get("label_evidence_recovery") else {})},
    )


def load_active_snapshot(recipe_id: str, nutrition_source: NutritionSource, *,
                         allow_input_estimates: bool = False) -> ComparisonSnapshot:
    """Load an active run; opt-in production assumptions remain explicit proxies."""
    if nutrition_source not in SOURCES:
        raise ComparisonLoadError("unsupported_nutrition_source", blocked=True)
    active = matching_store.active_profile(recipe_id, nutrition_source, include_provenance=True)
    if active is None:
        raise ComparisonLoadError("active_profile_not_found")
    if active.get("recipe_id") != recipe_id or active.get("nutrition_source") != nutrition_source:
        raise ComparisonLoadError("active_profile_ownership_mismatch", blocked=True)
    profile = active["profile"]
    release = active["release_id"]
    if not release or profile.get("catalogue_release") != release:
        raise ComparisonLoadError("profile_release_mismatch", blocked=True)
    if profile.get("source_key") not in (None, nutrition_source):
        raise ComparisonLoadError("profile_region_mismatch", blocked=True)
    quantity_hash = profile.get("quantity_input_hash")
    if not quantity_hash:
        raise ComparisonLoadError("missing_quantity_input_hash")
    ingredients, issues, cache = [], [], {}
    for detail in profile.get("details") or []:
        position = detail["position"]
        reference = f"matching_profile_runs/{active['run_id']}/profile/details/{position}"
        if detail.get("quantity_input_hash") not in (None, quantity_hash):
            raise ComparisonLoadError("occurrence_quantity_hash_mismatch", blocked=True)
        if detail.get("catalogue_release") not in (None, release):
            raise ComparisonLoadError("occurrence_release_mismatch", blocked=True)
        facts = detail.get("selected_composition_facts")
        composition = None
        if facts and detail.get("outcome") != "unresolved":
            if (detail.get("canonical_food_id") not in (None, facts["native_id"])
                    or detail.get("source_nutrition") not in (None, facts["source_key"])):
                raise ComparisonLoadError("saved_selection_identity_mismatch", blocked=True)
            selected_id = detail.get("selected_candidate_id")
            collection = "nutritional_ingredients_" + facts["source_key"]
            key = (collection, selected_id, str(facts["native_id"]))
            if key not in cache:
                rows = matching_store.composition_records(release, collection, **(
                    {"candidate_id": str(selected_id)} if selected_id is not None else {"native_id": str(facts["native_id"])}
                ))
                if len(rows) > 1:
                    raise ComparisonLoadError("ambiguous_selected_native_id")
                cache[key] = rows[0] if rows else None
            row = cache[key]
            if row is None:
                issues.append(f"occurrence:{position}.selected_composition_not_found")
            else:
                if matching_store.digest(source_anatomy(row["annotation"])) != matching_store.digest(source_anatomy(facts)):
                    raise ComparisonLoadError("saved_selection_source_mismatch", blocked=True)
                composition = _composition(row)
                saved_values = detail.get("values_per_100g") or {}
                fills = {}
                for metric in METRICS:
                    saved, native = saved_values.get(metric), composition.values_per_100g.get(metric)
                    if _same_number(saved, native):
                        continue
                    fill = RECORD_FILLS.get(facts["native_id"], {}).get(metric)
                    if (not allow_input_estimates or native is not None or not fill
                            or (detail.get("nutrients_filled") or {}).get(metric) != fill["source"]
                            or not _same_number(saved, fill["value"])):
                        raise ComparisonLoadError("saved_selection_source_mismatch", blocked=True)
                    composition.values_per_100g[metric] = saved
                    fills[metric] = deepcopy(fill)
                if fills:
                    # Reviewed values do not establish a missing analytical definition.
                    composition.provenance["reviewed_nutrient_fills"] = fills
                if selected_id is None:
                    composition.provenance["selection_resolution"] = "unique_exact_native_id"
                composition.blockers.extend(detail.get("blockers") or [])
        quantity_evidence = detail.get("quantity_evidence") or {}
        if (detail.get('quantity_adjustment') is not None or detail.get('quantity_status') == 'adjusted_input_quantity_estimate') and not valid_quantity_adjustment(detail):
            raise ComparisonLoadError('saved_quantity_adjustment_mismatch', blocked=True)
        if quantity_evidence.get("kind") in {"explicit_source_mass", "source_volume_estimate"} and not _same_number(
            quantity_evidence.get("weight_g"), detail["weight_g"]
        ):
            raise ComparisonLoadError("saved_quantity_evidence_mismatch", blocked=True)
        original = detail.get("original_ingredient_text") or ""
        resolved = resolve_quantity(detail.get("ingredient") or detail.get("name"), detail["weight_g"], original)
        if (resolved.get("quantity_evidence") or {}).get("kind") == "explicit_source_mass":
            current = effective_quantity(resolved, original)
            if not _same_number(current["weight_g"], detail["weight_g"]):
                raise ComparisonLoadError("source_mass_requires_baseline_recalculation")
        # Explicit source mass does not establish an edible/raw/cooked state basis.
        quantity = ComparisonQuantity(weight_g=detail["weight_g"], basis="unknown", evidence=_evidence(
            reference + "/quantity_evidence", detail.get("quantity_status") or "unverified_quantity",
            stated=quantity_evidence.get("kind") == "explicit_source_mass",
        ))
        if allow_input_estimates and detail.get("outcome") != "unresolved" and composition is not None:
            quantity = ComparisonQuantity(weight_g=detail["weight_g"], basis="selected_state_edible",
                evidence=_evidence(reference + "/quantity_evidence", "saved_production_input_estimate", proxy=True))
        ingredients.append(ComparisonIngredient(
            position=position, name=detail.get("ingredient") or detail.get("name"),
            original_text=detail.get("original_ingredient_text") or "", quantity=quantity,
            observed_properties=_assertions(detail.get("observed_facts") or {}, reference + "/observed_facts"),
            source_evidence=recover_source_evidence(detail, recipe_id=recipe_id, run_id=active["run_id"],
                                                   quantity_hash=quantity_hash),
            composition=composition,
        ))
    return ComparisonSnapshot(
        recipe_id=recipe_id, run_id=active["run_id"], nutrition_source=nutrition_source,
        release_id=release, quantity_input_hash=quantity_hash, serves=profile.get("serves"),
        input_policy="production_estimate" if allow_input_estimates else "verified",
        ingredients=ingredients, provenance={"active_run": deepcopy(active), "loading_issues": issues},
    )


def preview_raw_part(nutrition_source: NutritionSource, edit: RawPartEdit,
                     constraints: ComparisonConstraints | None = None, *,
                     quantity_declarations: Sequence[QuantityBasisDeclaration] = ()) -> ComparisonResult:
    """Internal preview using exact source keys, with a final active-run recheck."""
    try:
        snapshot = load_active_snapshot(edit.recipe_id, nutrition_source)
        if snapshot.run_id != edit.baseline_run_id or snapshot.quantity_input_hash != edit.quantity_input_hash:
            raise ComparisonLoadError("baseline_mismatch", blocked=True)
        ref = edit.to_composition
        if ref.release_id != snapshot.release_id:
            raise ComparisonLoadError("target_release_mismatch", blocked=True)
        if ref.collection not in {"nutritional_ingredients_eu", "nutritional_ingredients_" + nutrition_source}:
            raise ComparisonLoadError("target_region_mismatch", blocked=True)
        rows = matching_store.composition_records(ref.release_id, ref.collection, candidate_id=ref.candidate_id)
        if not rows:
            raise ComparisonLoadError("target_composition_not_found")
        result = compare_raw_part(snapshot, edit, _composition(rows[0]), constraints,
                                  quantity_declarations=quantity_declarations)
        current = matching_store.active_profile(edit.recipe_id, nutrition_source, include_provenance=True)
        if current is None or current["run_id"] != snapshot.run_id:
            raise ComparisonLoadError("active_profile_changed", blocked=True)
        result.missing_evidence.extend(snapshot.provenance["loading_issues"])
        result.uncertainty.append("Loaded from the active PostgreSQL run; saved quantities do not record an edible/state basis.")
        return result
    except ComparisonLoadError as error:
        return ComparisonResult(status="blocked" if error.blocked else "needs_evidence",
                                baseline_run_id=edit.baseline_run_id, edit=edit.model_copy(deep=True),
                                blockers=[error.reason] if error.blocked else [],
                                missing_evidence=[] if error.blocked else [error.reason])
    except (ValidationError, KeyError, TypeError):
        return ComparisonResult(status="blocked", baseline_run_id=edit.baseline_run_id,
                                edit=edit.model_copy(deep=True), blockers=["malformed_matching_record"])
