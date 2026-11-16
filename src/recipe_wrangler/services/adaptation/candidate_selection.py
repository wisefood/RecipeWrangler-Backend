"""Source-compatible catalogue screening before the bounded culinary batch."""

from collections.abc import Sequence
import math

from pydantic import ValidationError

from .comparison import compare_raw_part
from .models import ComparisonConstraints, ComparisonSnapshot, CompositionRef, QuantityBasisDeclaration
from .comparison_store import ComparisonLoadError, _composition
from .models import AdaptationContext
from .instruction_editor import _allowed_patches
from .objective_approval import InputScoreContext, approve_nutrition_candidate
from .nutrition_benefit import NutritionPolicy, DEFAULT_POLICY, validate_nutrition_policy
from .models import NutrientObjective, RawPartShortlist, ShortlistCandidate
from .impact_objectives import objective_delta
from .production_candidates import make_substitution_edit, production_candidate_stream as production_candidate_records, food_part_candidate_records
from .nutritional_variants import FACETS, nutritional_variant_records


def shortlist_raw_part_candidates(
    snapshot: ComparisonSnapshot, *, target_position: int, objective: NutrientObjective,
    constraints: ComparisonConstraints | None = None, limit: int = 10,
    quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
    allow_general_substitutions: bool = False,
    allow_graph_fallback: bool = True,
    require_score_inputs: bool = False,
    score_context: InputScoreContext | None = None,
    instruction_context: AdaptationContext | None = None,
    nutrition_policy: NutritionPolicy = DEFAULT_POLICY,
) -> RawPartShortlist:
    """Screen retrieved candidates before the ten-candidate limit; never ask an LLM.

    Unknown/conflicting records remain in diagnostics and cannot occupy the
    shortlist. Supplied scoring/text contexts also exclude known policy and
    editing failures; these preflights do not replace final approval.
    """
    validate_nutrition_policy(nutrition_policy)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("Shortlist limit must be an integer between one and ten")
    snapshot = ComparisonSnapshot.model_validate(snapshot.model_dump())
    objective = NutrientObjective.model_validate(objective.model_dump())
    original = next((item for item in snapshot.ingredients if item.position == target_position), None)
    if original is None:
        raise ValueError("Target occurrence not found")
    result = RawPartShortlist(recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
        quantity_input_hash=snapshot.quantity_input_hash, target_position=target_position, status="needs_evidence")
    if original.composition is None:
        result.reasons = ["source_composition"]
        return result
    source = original.composition
    part_rows = []
    facet_search = (allow_general_substitutions and snapshot.input_policy == "production_estimate"
        and objective.metric in FACETS)
    rows = nutritional_variant_records(snapshot, target_position, objective.metric) if facet_search else []
    facet_keys = {(row["release_id"], row["collection"], row["candidate_id"]) for row in rows}
    state = source.properties.get("preparation_state")
    raw = state is not None and state.value == "raw" and state.evidence.status == "stated"
    if raw or not facet_search:
        part_rows, result.reasons = food_part_candidate_records(snapshot, target_position)
        rows.extend(part_rows)
    # Exhaust the preferred mechanisms before fetching graph alternatives.
    def candidate_rows():
        yield from rows
        if allow_general_substitutions and allow_graph_fallback and not any(item.status == "eligible" for item in result.screening):
            graph_rows, graph_reasons = production_candidate_records(snapshot, target_position,
                has_eligible=lambda: any(item.status == "eligible" for item in result.screening))
            for row in graph_rows:
                rows.append(row)
                yield row
            result.reasons.extend(graph_reasons)
    part_keys = {(row["release_id"], row["collection"], row["candidate_id"]) for row in part_rows}
    seen = set()
    for row in candidate_rows():
        ref = CompositionRef(**{key: row[key] for key in ("release_id", "collection", "candidate_id")})
        key = (ref.release_id, ref.collection, ref.candidate_id)
        if ref == source.ref:
            if "unchanged_source_composition_removed" not in result.reasons:
                result.reasons.append("unchanged_source_composition_removed")
            continue
        if key in seen:
            continue
        seen.add(key)
        item = ShortlistCandidate(composition=ref, label=(row.get("candidate") or {}).get("document") or ref.candidate_id,
            status="needs_evidence", discovery_mechanism="same_identity_nutritional_facet_variant" if key in facet_keys
                else "same_identity_food_part_variant" if key in part_keys else "production_graph_substitution")
        result.screening.append(item)
        try:
            target = _composition(row)
            if key in part_keys:
                from .nutritional_variants import stated
                if (any(stated(source, field) != stated(target, field) for field in ("base_identity", "biological_source"))
                        or stated(target, "food_part") in (None, "", "unknown", stated(source, "food_part"))):
                    raise ComparisonLoadError("food_part_candidate_scope_mismatch", blocked=True)
            edit = make_substitution_edit(snapshot, target_position, target, nutritional_metric=objective.metric)
            comparison = compare_raw_part(snapshot, edit, target, constraints, quantity_declarations=quantity_declarations)
        except ComparisonLoadError as error:
            item.status, item.reasons = "rejected" if error.blocked else "needs_evidence", [error.reason]
            continue
        except (ValidationError, KeyError, TypeError):
            item.status, item.reasons = "rejected", ["malformed_matching_record"]
            continue
        item.comparison = comparison
        if comparison.status != "supported_input_comparison":
            item.status = "rejected" if comparison.status == "blocked" else "needs_evidence"
            item.reasons = comparison.blockers + comparison.missing_evidence
            continue
        if require_score_inputs and any(comparison.before["complete_totals"].get(metric) is None
            or comparison.after["complete_totals"].get(metric) is None
            for metric in ("energy_kcal", "sugar_g", "saturated_fat_g", "sodium_mg", "fibre_g", "protein_g")):
            item.reasons = ["complete_comparable_score_nutrients_required"]
            continue
        delta = objective_delta(comparison, objective)
        if delta is None or not math.isfinite(delta):
            item.reasons = ["impact_reference_comparison_incomplete" if objective.impact_context else "objective_nutrient_incomplete_per_serving"]
            continue
        item.objective_improvement_per_serving = -delta if objective.direction == "decrease" else delta
        if item.objective_improvement_per_serving <= 0:
            item.status, item.reasons = "rejected", ["no_objective_improvement"]
            continue
        if score_context is not None:
            approval = approve_nutrition_candidate(snapshot, comparison, objective, score_context,
                constraints=constraints, nutrition_policy=nutrition_policy)
            item.objective_preflight = approval
            if approval["status"] != "approved_input_objective":
                item.status = "needs_evidence" if approval["status"] == "needs_evidence" else "rejected"
                item.reasons = list(approval["reasons"])
                continue
        if instruction_context is not None:
            if (instruction_context.recipe_id, instruction_context.baseline_run_id,
                    instruction_context.quantity_input_hash, instruction_context.target_position) != (
                    snapshot.recipe_id, snapshot.run_id, snapshot.quantity_input_hash, target_position):
                raise ValueError("instruction_context_baseline_mismatch")
            try:
                _allowed_patches(instruction_context, edit)
            except ValueError as error:
                item.reasons = ["instruction_preflight:" + str(error)]
                continue
        item.status = "eligible"
    eligible = sorted((item for item in result.screening if item.status == "eligible"),
        key=lambda item: (-item.objective_improvement_per_serving, item.composition.collection, item.composition.candidate_id))
    for item in eligible[:limit]:
        item.selected = True
        result.candidate_refs.append(item.composition.model_copy(deep=True))
    result.status = "selected" if result.candidate_refs else "needs_evidence" if any(
        item.status == "needs_evidence" for item in result.screening) else "no_supported_candidates"
    if not rows and not result.reasons:
        result.reasons = ["no_exact_source_food_part_variants"]
    if not rows and result.reasons and any(
        any(word in reason for word in ("unresolved", "missing", "no_pinned", "ambiguous")) for reason in result.reasons
    ):
        result.status = "needs_evidence"
    if not result.candidate_refs:
        if not result.screening:
            result.reasons.append("no_candidates_retrieved")
        elif any(item.status == "needs_evidence" for item in result.screening):
            result.reasons.append("candidate_evidence_incomplete")
        elif any(item.objective_improvement_per_serving is not None for item in result.screening):
            result.reasons.append("candidates_numerically_rejected")
        else:
            result.reasons.append("candidates_identity_or_restriction_rejected")
    return result
