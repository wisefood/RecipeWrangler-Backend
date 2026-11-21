"""Public enhancement audit: candidate origins, stage decisions and final outcome."""

from typing import Any, Literal

from pydantic import Field, model_validator

from recipe_wrangler.repositories.matching_store import digest

from .models import ComparisonModel, CompositionRef, RawPartEdit
from .models import CandidateAssessment
from .models import AdaptationPreview, NutrientObjective
from .models import RecipeAdaptationResponse


class StageDecision(ComparisonModel):
    status: str
    reasons: list[str] = Field(default_factory=list)


class EnhancementCandidate(ComparisonModel):
    candidate_id: str
    target_position: int = Field(ge=0)
    label: str
    composition: CompositionRef
    mechanism: Literal["same_identity_food_part_variant", "production_graph_substitution", "same_identity_nutritional_facet_variant"]
    operation: str
    proposed_edit: RawPartEdit | None = None
    shortlisted: bool = False
    objective_metric: str | None = None
    objective_improvement_per_serving: float | None = Field(default=None, allow_inf_nan=False)
    screening_decision: StageDecision
    objective_preflight: StageDecision = Field(default_factory=lambda: StageDecision(status="not_evaluated"))
    culinary_status: Literal["not_evaluated", "unavailable", "suitable", "suitable_with_changes", "unsuitable", "uncertain"] = "not_evaluated"
    culinary_assessment: CandidateAssessment | None = None
    culinary_evidence_status: Literal["inferred"] | None = None
    objective_decision: StageDecision = Field(default_factory=lambda: StageDecision(status="not_evaluated"))
    instruction_decision: StageDecision = Field(default_factory=lambda: StageDecision(status="not_evaluated"))
    system_decision: Literal["selected", "rejected", "needs_evidence", "requires_changes", "not_selected", "not_evaluated"]
    decision_reasons: list[str] = Field(default_factory=list)


class EnhancementDecision(ComparisonModel):
    outcome: Literal["adapted_recipe_draft", "no_adaptation", "needs_evidence", "changes_required", "blocked"]
    stage: str
    selected_candidate_id: str | None = None
    target_position: int | None = Field(default=None, ge=0)
    reasons: list[str] = Field(default_factory=list)
    selection_policy: Literal["highest_objective_gain_among_context_suitable_candidates_passing_final_checks"] = "highest_objective_gain_among_context_suitable_candidates_passing_final_checks"


class EnhancementResponse(RecipeAdaptationResponse):
    response_schema_version: Literal["enhancement_response_v2"] = "enhancement_response_v2"
    pipeline_version: Literal["recipe_enhancement_v1"] = "recipe_enhancement_v1"
    mode: Literal["nutrition", "cost", "sustainability"]
    explanation: str = ""
    status: Literal["draft_requires_review", "blocked", "needs_evidence", "changes_required", "no_supported_candidates",
                    "instruction_pending", "unavailable", "no_target"]
    baseline_run_id: str | None = None
    quantity_input_hash: str | None = None
    objective: NutrientObjective | None = None
    candidates: list[EnhancementCandidate] = Field(default_factory=list)
    final_decision: EnhancementDecision
    screening: dict[str, Any] | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    stages: list[str] = Field(default_factory=list)
    preview: AdaptationPreview | None = None
    objective_decisions: list[dict[str, Any]] = Field(default_factory=list)
    final_objective_approval: dict[str, Any] | None = None
    recipe_revision: str | None = None
    search_policy_version: Literal["deterministic_eligibility_before_shortlist_v1"] = "deterministic_eligibility_before_shortlist_v1"
    model_call_limit: Literal[3] = 3
    model_calls: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    instruction_decisions: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_audit(self) -> "EnhancementResponse":
        keys = [(item.candidate_id, item.target_position) for item in self.candidates]
        if len(keys) != len(set(keys)):
            raise ValueError("Candidate identities must be unique per target occurrence")
        selected = [item for item in self.candidates if item.system_decision == "selected"]
        if self.status == "draft_requires_review":
            decision = self.final_decision
            if (len(selected) != 1 or decision.outcome != "adapted_recipe_draft"
                    or selected[0].candidate_id != decision.selected_candidate_id
                    or selected[0].target_position != decision.target_position
                    or self.selected_candidate_id != decision.selected_candidate_id
                    or self.comparison.edit.position != decision.target_position
                    or self.comparison.edit.to_composition != selected[0].composition
                    or not self.final_objective_approval
                    or self.final_objective_approval.get("status") != "approved_input_objective"):
                raise ValueError("Recipe draft must identify one checked, approved selected candidate")
        elif selected or self.selected_candidate_id is not None or self.final_decision.selected_candidate_id is not None or self.final_decision.target_position is not None:
            raise ValueError("An unsuccessful request cannot select a candidate")
        expected = {"draft_requires_review": "adapted_recipe_draft", "blocked": "blocked", "needs_evidence": "needs_evidence",
            "changes_required": "changes_required"}.get(self.status, "no_adaptation")
        if self.final_decision.outcome != expected:
            raise ValueError("Final decision must agree with the response status")
        return self


def _candidate_key(item: dict) -> tuple[str, str, str]:
    ref = item["composition"]
    return ref["release_id"], ref["collection"], ref["candidate_id"]


def finalize_enhancement_response(result: dict) -> dict:
    """Build an audit from recorded stages only; never invent unrun decisions."""
    screening, preview = result.get("screening") or {}, result.get("preview") or {}
    viewed = {_candidate_key(item): item for item in preview.get("candidates", [])}
    approvals = {item["candidate_id"]: item for item in result.get("objective_decisions", [])}
    edits = {item["candidate_id"]: item for item in result.get("instruction_decisions", [])}
    success = result["status"] == "draft_requires_review" and result.get("adapted_recipe") is not None
    selected_id = result.get("selected_candidate_id") if success else None
    position = preview.get("target_position")
    candidates = []
    for attempt in screening.get("attempts", []):
        for item in (attempt.get("shortlist") or {}).get("screening", []):
            identifier = digest(item["composition"])
            contextual = viewed.get(_candidate_key(item), {}) if attempt["position"] == position else {}
            culinary = contextual.get("culinary")
            approval = approvals.get(identifier) if attempt["position"] == position else None
            instruction = edits.get(identifier) if attempt["position"] == position else None
            state, reasons = "not_evaluated", list(item.get("reasons", []))
            if item["status"] == "rejected":
                state = "rejected"
            elif item["status"] == "needs_evidence":
                state = "needs_evidence"
            elif not item["selected"]:
                state, reasons = "not_selected", ["outside_shortlist_limit"]
            elif culinary and culinary["verdict"] == "unsuitable":
                state, reasons = "rejected", ["culinarily_unsuitable"]
            elif (culinary and culinary["verdict"] == "suitable_with_changes"
                  and contextual.get("status") != "ready_for_instruction_editing"):
                state, reasons = "requires_changes", ["additional_operations_require_validation_and_recalculation"]
            elif culinary and culinary["verdict"] == "uncertain":
                state, reasons = "needs_evidence", ["culinary_uncertainty"]
            elif contextual and not culinary:
                state, reasons = "needs_evidence", list(contextual.get("reasons", []))
            elif approval and approval["status"] != "approved_input_objective":
                state = "rejected" if approval["status"] == "rejected" else "needs_evidence"
                reasons = list(approval["reasons"])
            elif instruction and instruction["status"] != "draft_requires_review":
                state, reasons = "needs_evidence", list(instruction["reasons"])
            elif identifier == selected_id and attempt["position"] == position:
                state, reasons = "selected", ["context_and_final_objective_approved_and_instructions_validated"]
            elif success:
                state, reasons = "not_selected", ["another_candidate_selected"]
            elif approval and approval["status"] == "approved_input_objective":
                state, reasons = "not_selected", list(result.get("reasons", []))
            if result["status"] == "blocked":
                state, reasons = "not_selected", list(result.get("reasons", []))
            candidates.append(EnhancementCandidate(candidate_id=identifier, target_position=attempt["position"],
                label=item["label"], composition=item["composition"], operation={
                    "same_identity_nutritional_facet_variant": "nutritional_product_variant",
                    "same_identity_food_part_variant": "food_part_variant",
                    "production_graph_substitution": "raw_ingredient_substitution",
                }.get(item.get("discovery_mechanism"), attempt["operation"]),
                mechanism=item.get("discovery_mechanism") or ("same_identity_nutritional_facet_variant"
                    if (item.get("comparison") or {}).get("edit", {}).get("nutritional_change")
                    else "same_identity_food_part_variant" if attempt["operation"] == "food_part_variant" else "production_graph_substitution"),
                proposed_edit=(item.get("comparison") or {}).get("edit"), shortlisted=item["selected"],
                objective_metric=(screening.get("numerical_objective") or {}).get("metric"),
                objective_improvement_per_serving=item.get("objective_improvement_per_serving"),
                screening_decision=StageDecision(status=item["status"], reasons=item.get("reasons", [])),
                objective_preflight=StageDecision(status=item["objective_preflight"]["status"],
                    reasons=item["objective_preflight"]["reasons"]) if item.get("objective_preflight") else StageDecision(status="not_evaluated"),
                culinary_status=culinary["verdict"] if culinary else "unavailable" if contextual else "not_evaluated",
                culinary_assessment=culinary, culinary_evidence_status="inferred" if culinary else None,
                objective_decision=StageDecision(status=approval["status"], reasons=approval["reasons"]) if approval else StageDecision(status="not_evaluated"),
                instruction_decision=StageDecision(status=instruction["status"], reasons=instruction["reasons"]) if instruction else StageDecision(status="not_evaluated"),
                system_decision=state, decision_reasons=reasons))
    outcome = {"draft_requires_review": "adapted_recipe_draft", "blocked": "blocked", "needs_evidence": "needs_evidence",
        "changes_required": "changes_required"}.get(result["status"], "no_adaptation")
    explanations = {
        'hosted_provider_rate_limited': 'The model provider is temporarily rate-limiting requests. No adapted recipe was approved.',
        'hosted_provider_timeout': 'The model provider did not answer within the time limit. No adapted recipe was approved.',
        'hosted_provider_authentication_failed': 'The configured model provider rejected its credentials.',
        'hosted_provider_unreachable': 'The configured model provider could not be reached.',
        'hosted_budget_exhausted': 'The approved model-call budget has been reached.',
        'hosted_generation_disabled_cache_miss': 'This prototype has no matching saved model response for the current inputs. Fresh generation is disabled.',
        'hosted_case_not_approved': 'This recipe needs approval for its hosted-model case before culinary assessment and instruction editing can run.',
        'source_recipe_repeated_lines_require_verification': 'The source ingredient list repeats lines that are not repeated in the nutrition profile. Verify the source before changing this recipe.',
        'source_recipe_occurrence_alignment_required': 'The source ingredient list and nutrition profile contain different ingredient occurrences. They must be aligned before adaptation.',
        'source_mass_requires_baseline_recalculation': 'The source states a mass that differs from the saved nutrition profile. Recalculate the baseline before adapting this recipe.',
        'search_budget_reached': 'The bounded candidate search reached its limit without an approved replacement. Other changes may exist.',
        'no_supported_adaptation_after_implemented_search': 'No replacement passed the implemented searches and current evidence, restriction and nutrition checks. See the screening attempts for the reasons.',
        'no_screened_candidates_within_production_search_limit': 'No replacement passed the current identity, evidence and nutrition checks within the candidate search limit. See the screening attempts for details.',
    }
    explanation = next((explanations[reason] for reason in result.get('reasons', []) if reason in explanations), None)
    if success:
        explanation = 'An unsaved adapted recipe is ready for review, with updated instructions and recalculated input nutrition estimates.'
    packet = {**result, 'explanation': explanation or 'The adaptation checks did not approve a complete recipe. See the recorded reasons and candidate decisions.',
        "objective": result.get("objective") or screening.get("numerical_objective"),
        "candidates": candidates, "final_decision": EnhancementDecision(outcome=outcome,
        stage=(result.get("stages") or ["preflight"])[-1], selected_candidate_id=selected_id,
        target_position=position if selected_id else None, reasons=list(result.get("reasons", [])))}
    return EnhancementResponse.model_validate(packet).model_dump(mode="json")
