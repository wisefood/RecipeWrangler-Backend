"""Pinned raw substitutions with verified or conditional production input bases."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

from recipe_wrangler.tools.nutrition_completeness import EU, METRICS, REGIONAL, completeness, contribution
from recipe_wrangler.repositories.matching_store import digest
from recipe_wrangler.utils.dietary_ingredient_evidence import ingredient_diet_evidence

from .models import (
    ComparisonComposition, ComparisonConstraints, ComparisonIngredient, ComparisonResult, Evidence,
    ComparisonSnapshot, PropertyAssertion, QuantityBasisDeclaration, RawPartEdit,
)
from .quantity_declarations import QuantityDeclarationError, apply_quantity_declarations


# Explicit absence matters; values come from the actual source, never a food default.
RAW_PRESERVED_PROPERTIES = frozenset({"processes", "preservation_methods", "qualifiers.coating",
    "qualifiers.added_fat", "qualifiers.added_salt", "qualifiers.added_ingredients"})


def _known(assertion: PropertyAssertion | None) -> bool:
    return bool(assertion is not None and assertion.evidence.status == "stated"
                and assertion.value is not None
                and (not isinstance(assertion.value, str) or assertion.value.strip().lower() not in {"", "unknown"}))


def _equal(left: object, right: object) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return set(left) == set(right)
    return type(left) is type(right) and left == right


def _definition(composition: ComparisonComposition, metric: str) -> str | None:
    definition = composition.nutrient_definitions.get(metric)
    return definition if definition and definition.lower() != "unknown" else None


def _compatible_estimate_fields(source: ComparisonComposition, target: ComparisonComposition, metric: str) -> bool:
    """Match known normalized fields across tables, retaining their native provenance.

    This conditional estimate does not harmonize analytical methods. Carbohydrate
    definitions remain native and cannot be equated through wrapper field names.
    """
    if metric == 'carbs_g' or (metric != 'fibre_g' and
            (source.source_dataset, source.source_release) == (target.source_dataset, target.source_release)):
        return False
    fields = []
    for composition in (source, target):
        definition = _definition(composition, metric)
        prefix = f'{composition.source_dataset}:{composition.source_release}:source_field:'
        if definition is None or not definition.startswith(prefix):
            return False
        field = definition[len(prefix):]
        if metric == 'fibre_g' and field in {EU[metric], *REGIONAL[metric]}:
            field = 'Fiber (g)'
        fields.append(field)
    supported = {EU.get(metric), *REGIONAL.get(metric, ())}
    if metric == 'energy_kcal':
        supported.add('Energy')
    if metric == 'sugar_g':
        supported.add('Sugars, total including NLEA')
    if fields[0] == fields[1]:
        return fields[0] in supported
    # These unit-qualified aliases are already normalized by the native readers.
    # Generic sugar labels do not establish the required definition.
    if metric not in {'energy_kcal', 'protein_g', 'fat_g', 'sugar_g', 'saturated_fat_g', 'sodium_mg'}:
        return False
    if metric == 'sugar_g':
        supported.discard('Sugar (g)')
    return all(field in supported for field in fields)


def compare_raw_part(
    snapshot: ComparisonSnapshot,
    edit: RawPartEdit,
    target: ComparisonComposition,
    constraints: ComparisonConstraints | None = None,
    *, quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
) -> ComparisonResult:
    """Compare one pinned occurrence; evidence is supplied by a trusted caller.

    The verified path requires independent stated inputs and preservation facts. The production
    estimate path permits unknown optional properties, reports assumptions and
    preserves known material facts. Cooked substitutions need a separate yield-
    aware mechanism. Declarations never become saved observed properties.
    """
    result = ComparisonResult(
        status="needs_evidence", baseline_run_id=snapshot.run_id, serves=snapshot.serves, edit=edit.model_copy(deep=True)
    )
    result.quantity_basis = nutrition_basis(snapshot)
    if estimated_inputs(snapshot):
        result.comparison_version = "production_input_estimate_v3"
        result.uncertainty.append("Conditional estimate using saved production input masses and reporting bases; edible portions and selected states are assumptions, not verified observations. No cooked yields, retention or added media have been calculated.")
        result.uncertainty.append("Unchanged ingredients retain production's normalized nutrient estimates from their own sources; analytical methods are not fully harmonized. Undefined carbohydrate comparisons remain unknown.")
    constraints = constraints or ComparisonConstraints()
    blocked, missing = result.blockers, result.missing_evidence
    if (edit.recipe_id, edit.baseline_run_id, edit.quantity_input_hash) != (
        snapshot.recipe_id, snapshot.run_id, snapshot.quantity_input_hash
    ):
        blocked.append("baseline_mismatch")
    occurrence = next((i for i in snapshot.ingredients if i.position == edit.position), None)
    if occurrence is None:
        blocked.append("occurrence_not_found")
    if target.ref != edit.to_composition:
        blocked.append("target_composition_mismatch")
    if blocked:
        result.status = "blocked"
        return result

    result.baseline_quantity = occurrence.quantity.model_copy(deep=True)
    try:
        snapshot, declarations = apply_quantity_declarations(snapshot, quantity_declarations)
    except QuantityDeclarationError as error:
        result.status = "blocked"
        blocked.append(error.reason)
        return result
    result.quantity_declarations = declarations
    occurrence = next(item for item in snapshot.ingredients if item.position == edit.position)
    declared_quantity = next((item for item in declarations if item.position == edit.position), None)
    if declarations:
        result.uncertainty.append(
            "Comparison is conditional on the recorded quantity/state declarations; "
            "saved observations and masses are unchanged and the statements are not independently measured."
        )
    source = occurrence.composition
    if source is None:
        missing.append("source_composition")
        return result
    result.quantity = occurrence.quantity.model_copy(deep=True)
    result.observed_properties = {key: value.model_copy(deep=True)
                                  for key, value in occurrence.observed_properties.items()}
    observed = dict(occurrence.observed_properties)
    overlay = occurrence.source_evidence
    if overlay is not None:
        from .occurrence_evidence import selected_source_branch
        result.source_evidence = overlay.model_copy(deep=True)
        if (overlay.recipe_id, overlay.baseline_run_id, overlay.quantity_input_hash, overlay.position, overlay.weight_g) != (
            snapshot.recipe_id, snapshot.run_id, snapshot.quantity_input_hash, occurrence.position, occurrence.quantity.weight_g
        ) or overlay.source_hash != digest(overlay.source_line):
            blocked.append("source_evidence_scope_mismatch")
        if overlay.selected_source_text is not None and overlay.selected_source_text != selected_source_branch(
                occurrence.name, overlay.source_line):
            blocked.append("source_alternative_selection_mismatch")
        blocked.extend("source_evidence_conflict:" + field for field in overlay.conflicts)
        if overlay.deferred_reason == "source_quantity_evidence_invalid":
            blocked.append("source_quantity_evidence_invalid")
        if not overlay.deferred_reason and not blocked:
            for field, assertion in overlay.properties.items():
                if not _known(assertion):
                    continue
                if _known(observed.get(field)) and not _equal(observed[field].value, assertion.value):
                    blocked.append("source_evidence_conflict:" + field)
                else:
                    observed[field] = assertion
    result.source_composition = source.model_copy(deep=True)
    result.target_composition = target.model_copy(deep=True)
    if source.ref != edit.from_composition:
        blocked.append("source_composition_mismatch")
    for label, composition, biological, part in (
        ("source", source, edit.source_biological_source, edit.source_food_part),
        ("target", target, edit.target_biological_source, edit.target_food_part),
    ):
        for field, expected in (("biological_source", biological), ("food_part", part)):
            assertion = composition.properties.get(field)
            if _known(assertion) and assertion.value != expected:
                blocked.append(label + ".property_conflict:" + field)
    allowed = {"nutritional_ingredients_eu", "nutritional_ingredients_" + snapshot.nutrition_source}
    for label, composition in (("source", source), ("target", target)):
        if composition.ref.release_id != snapshot.release_id:
            blocked.append(label + ".release_mismatch")
        if composition.ref.collection not in allowed:
            blocked.append(label + ".region_mismatch")
        if composition.blockers:
            blocked.extend(label + ".source_blocker:" + reason for reason in composition.blockers)
        if composition.basis.amount_g != 100 or composition.basis.portion != "edible":
            blocked.append(label + ".unsupported_reporting_basis")
        if not supported_reporting_basis(snapshot, composition):
            missing.append(label + ".reporting_basis")
    if (source.source_dataset, source.source_release) != (target.source_dataset, target.source_release):
        if estimated_inputs(snapshot):
            result.uncertainty.append("Conditional comparison across released food-composition tables; each source and release remains pinned. Reporting bases and nutrient definitions are checked separately; analytical methods and natural food variation are not fully harmonized.")
        else:
            blocked.append("cross_source_comparison_not_supported")
    if source.ref == target.ref:
        blocked.append("unchanged_composition")
    if occurrence.quantity.weight_g <= 0:
        blocked.append("excluded_occurrence")
    if not supported_quantity(snapshot, occurrence):
        missing.append("occurrence.raw_edible_mass")
    target_bone = target.properties.get("bone_status")
    if (estimated_inputs(snapshot) and occurrence.quantity.evidence.status == "proxy"
            and _known(target_bone) and target_bone.value in ("present", "bone_in")):
        missing.append("target.edible_yield_required")
    # Known meat identities cannot meet these dietary goals.
    if edit.target_identity in {"chicken", "beef", "pork", "lamb", "turkey", "fish"}:
        blocked.extend("consumer_group_conflict:" + group for group in sorted(constraints.required_consumer_groups))
    if edit.operation == "REPLACE_PRODUCT_VARIANT" and (
        edit.source_identity != edit.target_identity or edit.source_biological_source != edit.target_biological_source
    ):
        blocked.append("variant_identity_change_requires_ingredient_operation")

    if edit.nutritional_change is not None:
        result.comparison_version = "nutritional_product_variant_v1"
        from .nutritional_variants import variant_errors, stated
        if not estimated_inputs(snapshot):
            missing.append("nutritional_variant_requires_selected_state_input_policy")
        if (edit.operation != "REPLACE_PRODUCT_VARIANT" or edit.source_identity != edit.target_identity
                or edit.source_food_part != edit.target_food_part):
            blocked.append("nutritional_variant_must_preserve_product_identity_and_part")
        for label, composition, part, biological in (("source", source, edit.source_food_part, edit.source_biological_source),
                ("target", target, edit.target_food_part, edit.target_biological_source)):
            if part != stated(composition, "food_part") or biological != stated(composition, "biological_source"):
                blocked.append(label + ".nutritional_variant_metadata_mismatch")
        for reason in variant_errors(source, target, edit.nutritional_change):
            (missing if reason.endswith("unknown") else blocked).append(reason)
        assertion = observed.get(edit.nutritional_change.facet)
        if _known(assertion) and assertion.value != edit.nutritional_change.from_value:
            blocked.append("observed.nutritional_variant_baseline_facet_conflict")

    if estimated_inputs(snapshot):
        _check_estimated_properties(observed, source, target, edit, result)
    else:
        all_keys = set(observed) | set(source.properties) | set(target.properties)
        required = RAW_PRESERVED_PROPERTIES | (all_keys & {"skin_status", "bone_status"})
        # Required observations cannot be borrowed from selected/proxy source facts.
        for label, properties, part in (
            ("observed", observed, edit.source_food_part),
            ("source", source.properties, edit.source_food_part),
            ("target", target.properties, edit.target_food_part),
        ):
            requirements = {"base_identity": edit.target_identity if label == "target" else edit.source_identity,
                "preparation_state": "raw"}
            biological = edit.target_biological_source if label == "target" else edit.source_biological_source
            if biological is not None or "biological_source" in all_keys:
                requirements["biological_source"] = biological
            if part is not None or "food_part" in all_keys:
                requirements["food_part"] = part
            for key in required:
                assertion = source.properties.get(key)
                requirements[key] = assertion.value if _known(assertion) else next((p[key].value
                    for p in (observed, target.properties) if _known(p.get(key))), None)
            for key, expected in requirements.items():
                assertion = properties.get(key)
                if label == "observed" and key == "preparation_state" and not _known(assertion) and declared_quantity:
                    # Raw edible mass explicitly declares raw state; retain the original observation in the result.
                    assertion = PropertyAssertion(value="raw", evidence=declared_quantity.evidence)
                if not _known(assertion):
                    missing.append(label + ".property:" + key)
                elif not _equal(assertion.value, expected):
                    blocked.append(label + ".property_conflict:" + key)

        # Preserve all other asserted properties, and reject unsupported additions.
        if edit.operation == "REPLACE_INGREDIENT":
            all_keys.discard("nutri_score_fvl_member")
        for key in sorted(all_keys - required - {"base_identity", "biological_source", "food_part", "preparation_state"}):
            assertions = [properties.get(key) for properties in (
                observed, source.properties, target.properties
            )]
            if not all(_known(assertion) for assertion in assertions):
                missing.append("preserved_property:" + key)
            elif not all(_equal(assertions[0].value, a.value) for a in assertions[1:]):
                blocked.append("preserved_property_conflict:" + key)

    # Check the entire proposed ingredient set, including unchanged occurrences.
    for item in snapshot.ingredients:
        if item.quantity.weight_g == 0:
            continue
        composition = target if item.position == edit.position else item.composition
        if item.position != edit.position:
            missing.extend(f"occurrence:{item.position}.source_text_restriction:{reason}" for reason in
                restriction_text_concerns(item.original_text or item.name, constraints))
        for kind, required, assessments, expected in (
            ("allergen", constraints.forbidden_allergens,
             composition.allergen_assessments if composition else {}, "absent"),
            ("consumer", constraints.required_consumer_groups,
             composition.consumer_assessments if composition else {}, "suitable"),
        ):
            for name in sorted(required):
                assertion = assessments.get(name)
                reason = f"occurrence:{item.position}.{kind}:{name}"
                if (not _known(assertion) or not isinstance(assertion.value, str)
                        or assertion.value not in {expected, "present", "not_suitable"}):
                    missing.append(reason)
                elif assertion.value != expected:
                    blocked.append(reason)

    if blocked or missing:
        result.status = "blocked" if blocked else "needs_evidence"
        return result

    comparable = {}
    normalized_fields = []
    for metric in METRICS:
        reasons = []
        if source.values_per_100g.get(metric) is None or target.values_per_100g.get(metric) is None:
            reasons.append("missing_nutrient_value")
        before_definition = _definition(source, metric)
        after_definition = _definition(target, metric)
        if before_definition is None or after_definition is None:
            reasons.append("unknown_nutrient_definition")
        elif before_definition != after_definition:
            if estimated_inputs(snapshot) and _compatible_estimate_fields(source, target, metric):
                normalized_fields.append(metric)
            else:
                reasons.append("incompatible_nutrient_definition")
        comparable[metric] = not reasons
        if reasons:
            result.nutrient_blockers[metric] = reasons

    retained_definitions = [f"{item.position}:{metric}" for item in snapshot.ingredients
        if item.position != edit.position and item.quantity.weight_g > 0 and item.composition is not None
        for metric in METRICS if item.composition.values_per_100g.get(metric) is not None
        and _definition(item.composition, metric) is None]
    if estimated_inputs(snapshot) and retained_definitions:
        result.comparison_version = 'production_input_estimate_v3_unchanged_inputs'
        result.uncertainty.append("Unchanged normalized table values retain their original undefined analytical definitions at "
            + ', '.join(retained_definitions) + ". Their masses and compositions are identical on both sides; "
            "changed-ingredient definitions must still be known and compatible. These are conditional input totals.")

    if normalized_fields:
        result.comparison_version = 'production_input_estimate_v4_source_fields'
        aliases = [metric for metric in normalized_fields
                   if source.nutrient_definitions[metric].split(':source_field:', 1)[-1]
                   != target.nutrient_definitions[metric].split(':source_field:', 1)[-1]]
        if aliases:
            result.comparison_version = 'production_input_estimate_v5_field_aliases'
            result.uncertainty.append('Known native-reader field-name aliases used for: ' + ', '.join(aliases)
                + '. Original field names and values remain recorded.')
            if 'fibre_g' in aliases:
                result.comparison_version = 'production_input_estimate_v6_fibre_fields'
                result.uncertainty.append('Recognized fibre column names are normalized to Fiber (g) for this conditional estimate; original source labels and lookup provenance remain recorded.')
        result.uncertainty.append("Conditional comparison of recognized normalized nutrient fields across pinned tables: "
            + ', '.join(normalized_fields) + ". Native source definitions remain recorded; analytical methods are not fully harmonized. Carbohydrate definitions are not equated by this rule.")

    def totals(replacement: ComparisonComposition | None = None) -> dict:
        rows = []
        reference = replacement or source
        for item in snapshot.ingredients:
            composition = replacement if replacement and item.position == edit.position else item.composition
            values = composition.values_per_100g if composition else {}
            # Unaffected rows with unsupported quantity/source bases stay unknown.
            supported = (composition is not None and not composition.blockers
                         and composition.basis.amount_g == 100 and composition.basis.portion == "edible"
                         and supported_reporting_basis(snapshot, composition)
                         and supported_quantity(snapshot, item)
                         and composition.ref.release_id == snapshot.release_id
                         and composition.ref.collection in allowed
                         and (estimated_inputs(snapshot) or (
                             _known(composition.properties.get("preparation_state"))
                             and composition.properties["preparation_state"].value == "raw")))
            unchanged_estimate = estimated_inputs(snapshot) and item.position != edit.position
            eligible_values = {
                metric: values.get(metric) if supported and (unchanged_estimate or
                    _definition(reference, metric) is not None and (
                        _definition(composition, metric) is not None if estimated_inputs(snapshot)
                        else _definition(composition, metric) == _definition(reference, metric)))
                else None for metric in METRICS
            }
            rows.append({**contribution(eligible_values, item.quantity.weight_g, ingredient=item.name),
                         'position': item.position})
        return completeness(rows, snapshot.serves)

    result.before, result.after = totals(), totals(target)
    source_amounts = contribution(source.values_per_100g, occurrence.quantity.weight_g)
    target_amounts = contribution(target.values_per_100g, occurrence.quantity.weight_g)
    calculations = [source_amounts, target_amounts]
    for totals_result in (result.before, result.after):
        calculations.extend(totals_result[key] for key in (
            "complete_totals", "supported_partial_totals", "complete_totals_per_serving",
            "supported_partial_totals_per_serving",
        ))
    if any(value is not None and not math.isfinite(value)
           for values in calculations for value in values.values()):
        result.status = "blocked"
        blocked.append("nonfinite_nutrient_calculation")
        result.before = result.after = None
        return result
    for metric in METRICS:
        delta = target_amounts[metric] - source_amounts[metric] if comparable[metric] else None
        result.ingredient_delta[metric] = delta
        whole_recipe_known = (result.before["complete_totals"][metric] is not None
                              and result.after["complete_totals"][metric] is not None)
        result.recipe_delta[metric] = delta if whole_recipe_known else None
        result.recipe_delta_per_serving[metric] = (
            result.recipe_delta[metric] / snapshot.serves
            if result.recipe_delta[metric] is not None and snapshot.serves is not None else None
        )
    if any(comparable.values()):
        result.status = "supported_input_comparison"
    else:
        missing.append("no_comparable_nutrients")
    return result


def _check_estimated_properties(observed, source, target, edit, result):
    """Check material assertions without inventing absence or anatomical defaults."""
    missing, blocked = result.missing_evidence, result.blockers
    from recipe_wrangler.tools.nutrition_match import food_class
    family = food_class(edit.source_identity)
    as_described = (edit.nutritional_change is None and family in {"dairy", "oil_fat", "sweetener"}
        and food_class(edit.target_identity) == family
        and not any(_known(properties.get("preparation_state"))
                    for properties in (observed, source.properties, target.properties)))
    if as_described:
        result.uncertainty.append("Both source states are unspecified for as-described " + family +
            " inputs. Compare the released per-100-g edible compositions conditionally; no raw/cooked equivalence, yield or finished-dish measurement is claimed.")
    for label, properties, expected in (("observed", observed, edit.source_biological_source),
        ("source", source.properties, edit.source_biological_source),
        ("target", target.properties, edit.target_biological_source)):
        fact = properties.get("biological_source")
        if expected is not None and _known(fact) and fact.value != expected:
            blocked.append(label + ".property_conflict:biological_source")
    if _known(observed.get("biological_source")) and not _known(source.properties.get("biological_source")):
        missing.append("source.property:biological_source")
    for label, properties, identity in (
        ("observed", observed, edit.source_identity),
        ("source", source.properties, edit.source_identity),
        ("target", target.properties, edit.target_identity),
    ):
        fact = properties.get("base_identity")
        if not _known(fact):
            missing.append(label + ".property:base_identity")
        elif fact.value != identity:
            blocked.append(label + ".property_conflict:base_identity")
        state = properties.get("preparation_state")
        if edit.nutritional_change is not None:
            source_state = source.properties.get("preparation_state")
            if _known(state) and _known(source_state) and state.value != source_state.value:
                blocked.append(label + ".property_conflict:preparation_state")
            elif _known(state) != _known(source_state) and label != "observed":
                missing.append(label + ".nutritional_variant_state_equivalence")
            elif label == "observed" and _known(state) and not _known(source_state):
                missing.append("observed.nutritional_variant_state_equivalence")
        elif _known(state) and _known(source.properties.get("preparation_state")) and state.value != source.properties["preparation_state"].value:
            blocked.append(label + ".property_conflict:preparation_state")
        elif not _known(state) and not as_described:
            if label != "observed":
                missing.append(label + ".property:preparation_state")
            else:
                result.uncertainty.append("Occurrence preparation state is not stated; this draft is conditional on the selected source composition representing the input ingredient.")
    if edit.source_identity == edit.target_identity and edit.source_food_part != edit.target_food_part:
        for label, composition in (("source", source), ("target", target)):
            if not _known(composition.properties.get("biological_source")):
                missing.append(label + ".property:biological_source")
        for label, properties, expected in (("observed", observed, edit.source_food_part),
            ("source", source.properties, edit.source_food_part), ("target", target.properties, edit.target_food_part)):
            fact = properties.get("food_part")
            if not _known(fact) or expected is None:
                missing.append(label + ".property:food_part")
            elif fact.value != expected:
                blocked.append(label + ".property_conflict:food_part")
    exempt = {"base_identity", "biological_source", "food_part", "preparation_state", "nutri_score_fvl_member"}
    if edit.nutritional_change is not None:
        exempt.add(edit.nutritional_change.facet)
        if edit.nutritional_change.facet == "fat_level":
            exempt.add("fat_content_description")
        if edit.nutritional_change.to_value == "no_added_salt":
            exempt.add("qualifiers.added_salt")
        result.uncertainty.append("Same-product nutritional variant on the saved as-described input basis; no raw/cooked conversion or cooked-portion equivalence is claimed. Unspecified product states and replacement volume-to-mass equivalence remain assumptions.")
    keys = (set(observed) | set(source.properties) | set(target.properties)) - exempt
    for key in sorted(keys):
        facts = [p.get(key) for p in (observed, source.properties, target.properties)]
        known = [fact for fact in facts if _known(fact)]
        if (key in {"fat_level", "fat_content_description", "meat_fat_description"}
                and not _known(observed.get(key))
                and (edit.operation == "REPLACE_INGREDIENT" or edit.source_food_part != edit.target_food_part)):
            before_fat, after_fat = source.values_per_100g.get("saturated_fat_g"), target.values_per_100g.get("saturated_fat_g")
            if before_fat is not None and after_fat is not None and after_fat < before_fat:
                result.uncertainty.append("The recipe does not specify " + key +
                    "; this ingredient/cut replacement has lower source-table saturated fat. Fat-description equivalence is not claimed; culinary review remains required.")
                continue
        if key == "fortification" and not _known(observed.get(key)):
            result.uncertainty.append("Fortification is not specified by the recipe; replacement fortification may differ. Only supported nutrient values are compared.")
            continue
        if key == "processes" and all(isinstance(fact.value, list) and set(fact.value) <= {
            "diced", "chopped", "sliced", "cubed", "shredded", "grated"} for fact in known):
            # Raw cutting is retained in recipe text; a composition table need
            # not contain a separately measured row for every knife cut.
            continue
        if any(not _equal(known[0].value, fact.value) for fact in known[1:]):
            blocked.append("preserved_property_conflict:" + key)
        elif known and any(not _known(fact) for fact in facts[1:]):
            # Known requirements need support on both compositions. Unknown is
            # never silently interpreted as the absence of a coating/allergen.
            missing.append("preserved_property:" + key)
    if any(not _known(source.properties.get(key)) or not _known(target.properties.get(key))
           for key in ("qualifiers.coating", "qualifiers.added_fat", "qualifiers.added_salt", "qualifiers.added_ingredients")):
        result.uncertainty.append("Unspecified coating/addition properties remain unknown; no absence or full property-equivalence claim is made. Review the candidate's composition description before accepting the draft.")


# ---- formerly input_basis.py ----

def estimated_inputs(snapshot: ComparisonSnapshot) -> bool:
    return snapshot.input_policy == "production_estimate"


def supported_quantity(snapshot: ComparisonSnapshot, ingredient: ComparisonIngredient) -> bool:
    quantity = ingredient.quantity
    properties = [ingredient.observed_properties]
    if ingredient.source_evidence is not None:
        properties.append(ingredient.source_evidence.properties)
    if ingredient.composition is not None:
        properties.append(ingredient.composition.properties)
    bone_in = any(p.get("bone_status") is not None and p["bone_status"].evidence.status == "stated"
        and p["bone_status"].value in ("present", "bone_in") for p in properties)
    return (quantity.basis == "raw_edible" and quantity.evidence.status == "stated") or (
        estimated_inputs(snapshot) and quantity.basis == "selected_state_edible"
        and not bone_in
        and quantity.evidence.status == "proxy"
        and quantity.evidence.method == "saved_production_input_estimate")


def supported_reporting_basis(snapshot: ComparisonSnapshot, composition: ComparisonComposition | None) -> bool:
    if composition is None or composition.blockers:
        return False
    basis = composition.basis
    return basis.amount_g == 100 and basis.portion == "edible" and (
        basis.evidence.status == "stated" or (
            estimated_inputs(snapshot) and basis.evidence.status == "proxy"
            and basis.evidence.method == "legacy_contract_assumption"))


def nutrition_basis(snapshot: ComparisonSnapshot) -> Literal["selected_state_input_estimate", "raw_edible_input"]:
    return "selected_state_input_estimate" if estimated_inputs(snapshot) else "raw_edible_input"


# ---- formerly restriction_evidence.py ----

def composition_restriction_evidence(name: str) -> tuple[dict, dict]:
    """Retain attributed diet properties and uncertain lexical allergen findings."""
    from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names

    consumers = {}
    for group in ("vegan", "vegetarian"):
        result = ingredient_diet_evidence(name, group)
        if result is None:
            continue
        source = result["source_provenance"]
        consumers[group] = PropertyAssertion(value=result["status"], evidence=Evidence(
            reference=source["url"] + "#" + result["ingredient_id"] + "/" + group
                + "/" + ",".join(result["property_sources"]),
            method="open_food_facts_ingredient_property", version=source["sha256"], status="stated"))
    allergens = {}
    for result in detect_allergen_evidence_from_names([name]):
        allergens[result["allergen"]] = PropertyAssertion(
            value="present" if result["presence"] == "contains" else "unknown",
            evidence=Evidence(reference="ingredient_label:" + name, method="allergen_keyword_detection",
                version=result["classification_version"], status="inferred"))
    return allergens, consumers


def restriction_text_concerns(text: str, constraints: ComparisonConstraints) -> list[str]:
    """A selected composition cannot erase conflicting original or instruction text."""
    if not constraints.forbidden_allergens and not constraints.required_consumer_groups:
        return []
    from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names
    from recipe_wrangler.utils.consumer_suitability import dietary_keyword_evidence
    from recipe_wrangler.utils.es_recipe_evidence import allergen_filter_groups

    reasons = []
    for group in sorted(constraints.required_consumer_groups):
        hits, _ = dietary_keyword_evidence(text, group)
        if hits:
            reasons.append("consumer:" + group)
    if constraints.forbidden_allergens:
        forbidden = allergen_filter_groups(list(constraints.forbidden_allergens))
        for item in detect_allergen_evidence_from_names([text]):
            if allergen_filter_groups([item["allergen"]]) & forbidden:
                reasons.append("allergen:" + item["allergen"])
    return sorted(set(reasons))
