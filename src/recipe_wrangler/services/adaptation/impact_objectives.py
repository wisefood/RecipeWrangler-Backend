"""Production cost/emissions policies on pinned, explicitly estimated references."""

import math

from .comparison import supported_quantity

from .models import ComparisonComposition, ComparisonResult, ComparisonSnapshot, Evidence
from .models import ImpactContext, ImpactFactor, NutrientObjective


def _key(ref):
    return ref.release_id, ref.collection, ref.candidate_id


def objective_delta(comparison: ComparisonResult, objective: NutrientObjective) -> float | None:
    if objective.metric not in {"cost_eur", "co2e_kg"}:
        return comparison.recipe_delta_per_serving.get(objective.metric)
    context = objective.impact_context
    if (context is None or comparison.quantity is None or comparison.source_composition is None
            or comparison.target_composition is None or comparison.status != "supported_input_comparison"):
        return None
    if (context.recipe_id, context.baseline_run_id, context.quantity_input_hash) != (
        comparison.edit.recipe_id, comparison.baseline_run_id, comparison.edit.quantity_input_hash):
        return None
    factors = {_key(item.composition): item for item in context.factors}
    if len(factors) != len(context.factors):
        return None
    source = factors.get(_key(comparison.source_composition.ref))
    target = factors.get(_key(comparison.target_composition.ref))
    if source is None or target is None or source.value_per_kg <= 0 or source.product_id == target.product_id:
        return None
    if (source.evidence.status not in {"stated", "proxy"} or target.evidence.status not in {"stated", "proxy"}
            or source.evidence.version != target.evidence.version or source.evidence.method != target.evidence.method):
        return None
    expected_method = "production_cost_reference" if objective.metric == "cost_eur" else "production_sustainability_reference"
    if source.evidence.method != expected_method:
        return None
    if objective.metric == "co2e_kg":
        from recipe_wrangler.repositories import sustainability_references as references
        for factor in (source, target):
            record = factor.source_reference
            if (not record or not references.eligible(record)
                    or record["reference_id"] != factor.product_id
                    or record["metrics"]["climate_change"]["value"] != factor.value_per_kg
                    or factor.evidence.version != references.POLICY_VERSION + ":" + record["source_sha256"]
                    or factor.evidence.reference != record["source_url"] + "#" + record["source_record_id"]
                    or context.region != record["market"]):
                return None
    from .service import COST_MIN_REDUCTION_PCT, SUSTAINABILITY_MIN_REDUCTION_PCT
    threshold = COST_MIN_REDUCTION_PCT if objective.metric == "cost_eur" else SUSTAINABILITY_MIN_REDUCTION_PCT
    if context.minimum_reduction < threshold:
        return None
    if objective.metric == "cost_eur" and (not source.category or source.category != target.category):
        return None
    if (source.value_per_kg - target.value_per_kg) / source.value_per_kg + 1e-12 < context.minimum_reduction:
        return 0.0
    # Same raw edible mass is already checked by the numerical comparison.
    delta = comparison.quantity.weight_g / 1000 * (target.value_per_kg - source.value_per_kg)
    if context.baseline_total + 1e-9 < comparison.quantity.weight_g / 1000 * source.value_per_kg:
        return None
    serves = comparison.serves
    if serves is None or serves <= 0 or not math.isfinite(serves):
        return None
    return delta / serves if math.isfinite(delta) else None


def build_impact_objective(snapshot: ComparisonSnapshot, targets: list[ComparisonComposition], *,
                           mode: str, country: str | None = None) -> tuple[NutrientObjective | None, list[str]]:
    """Resolve production catalogues, allowing only specific supported matches.

    References are estimates. Broad/group price proxies and weak/generic emissions
    mappings cannot establish a benefit between distinct composition variants.
    """
    from .service import COST_MIN_REDUCTION_PCT, SUSTAINABILITY_MIN_REDUCTION_PCT
    if mode not in {"cost", "sustainability"}:
        raise ValueError("Unsupported impact mode")
    factors, reasons = {}, []
    if mode == "cost":
        from recipe_wrangler.pricing.cost_calculator import load_cost_catalogue
        catalogue = load_cost_catalogue()
        if not country:
            return None, ["cost_country_required"]
    else:
        from recipe_wrangler.repositories import sustainability_references as references
        names = [item.name for item in snapshot.ingredients if item.quantity.weight_g > 0]
        names.extend((item.provenance.get("candidate") or {}).get("document") or item.ref.candidate_id for item in targets)
        selected, reference_status = references.lookup_references(list(dict.fromkeys(names)))
        if reference_status != "available":
            return None, [reference_status]

    def resolve(composition, name):
        key = _key(composition.ref)
        if key in factors:
            return factors[key]
        bone = composition.properties.get("bone_status")
        if bone is not None and bone.value in ("bone_in", "present"):
            # Production references are per kg product. An edible mass cannot
            # price/footprint bone-in purchase weight without an edible yield.
            return None
        record = None
        if mode == "cost":
            match = catalogue.resolve(name, country.upper())
            if (match.get("match_status") != "matched" or match.get("price_scope") != "detailed_product"
                    or not str(match.get("match_method", "")).startswith("exact_detail")):
                return None
            value, product, category = match.get("economic_reference_price_eur_kg"), match.get("matched_product_id"), match.get("food_category")
            version = match.get("cost_reference_version")
            if not version:
                return None
        else:
            record = selected.get(name)
            if not record or references.select_reference(name, [record]) is None:
                return None
            value, product = record["metrics"]["climate_change"]["value"], record["reference_id"]
            category, version = None, references.POLICY_VERSION + ":" + record["source_sha256"]
        if (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
                or value < 0 or not product):
            return None
        factor = ImpactFactor(composition=composition.ref, value_per_kg=float(value), product_id=str(product), category=category,
            source_reference=record, evidence=Evidence(reference=record["source_url"] + "#" + record["source_record_id"]
                if record else str(product), method="production_" + mode + "_reference", version=str(version), status="proxy"))
        factors[key] = factor
        return factor

    total = 0.0
    for item in snapshot.ingredients:
        if item.quantity.weight_g == 0:
            continue
        if (item.composition is None or not supported_quantity(snapshot, item)):
            reasons.append(f"occurrence:{item.position}.impact_quantity_or_composition_unknown")
            continue
        factor = resolve(item.composition, item.name)
        if factor is None:
            reasons.append(f"occurrence:{item.position}.specific_impact_reference_required")
        else:
            total += item.quantity.weight_g / 1000 * factor.value_per_kg
    if reasons:
        return None, reasons
    for item in targets:
        resolve(item, (item.provenance.get("candidate") or {}).get("document") or item.ref.candidate_id)
    if len({item.evidence.version for item in factors.values()}) > 1:
        return None, ["impact_reference_versions_differ"]
    return NutrientObjective(metric="cost_eur" if mode == "cost" else "co2e_kg", impact_context=ImpactContext(
        recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id, quantity_input_hash=snapshot.quantity_input_hash,
        baseline_total=total, factors=list(factors.values()), minimum_reduction=COST_MIN_REDUCTION_PCT if mode == "cost"
        else SUSTAINABILITY_MIN_REDUCTION_PCT, region=country.upper() if mode == "cost" else "FR")), []
