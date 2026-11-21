"""Adaptation data models: comparison, contextual assessment, preview, instruction drafts and recipe response."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


# ---- from comparison_models.py ----

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Mass = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
PositiveNumber = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]
Metric = Literal[
    "energy_kcal", "protein_g", "carbs_g", "fat_g", "sugar_g",
    "saturated_fat_g", "sodium_mg", "fibre_g",
]
NutritionSource = Literal["eu", "irish", "hungarian", "slovenian"]


class ComparisonModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(ComparisonModel):
    reference: Text
    method: Text
    version: Text
    status: Literal["stated", "inferred", "proxy"]


class PropertyAssertion(ComparisonModel):
    value: str | bool | list[str] | None
    evidence: Evidence


class CompositionRef(ComparisonModel):
    release_id: Text
    collection: Text
    candidate_id: Text


class ComparisonQuantity(ComparisonModel):
    weight_g: Mass
    basis: Literal[
        "raw_edible", "cooked_edible", "as_purchased", "drained",
        "reconstituted", "selected_state_edible", "unknown",
    ]
    evidence: Evidence


class QuantityDeclarationEvidence(Evidence):
    method: Literal["user_confirmation", "source_review"]
    status: Literal["stated"] = "stated"


class QuantityBasisDeclaration(ComparisonModel):
    """An internal attestation, supplied by a caller that verifies its origin.

    This records a statement, not independent measurement or an LLM inference.
    It cannot authorize a mass edit or a cooking/edible-yield conversion.
    """

    recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    position: Annotated[int, Field(ge=0, strict=True)]
    weight_g: PositiveNumber
    basis: Literal["raw_edible"]
    declared_by: Text
    statement: Text
    evidence: QuantityDeclarationEvidence


class CompositionBasis(ComparisonModel):
    amount_g: PositiveNumber
    portion: Literal["edible", "as_purchased", "unknown"]
    evidence: Evidence


class ComparisonComposition(ComparisonModel):
    """Normalized source data; the caller must verify exact released records.

    Values use the canonical metric units (kcal, g, and mg for sodium), per
    ``basis.amount_g``. Missing keys and nulls are unknown, never zero.
    """

    ref: CompositionRef
    source_dataset: Text
    source_release: Text
    basis: CompositionBasis
    properties: dict[str, PropertyAssertion]
    values_per_100g: dict[Metric, Mass | None]
    nutrient_definitions: dict[Metric, Text] = Field(default_factory=dict)
    allergen_assessments: dict[str, PropertyAssertion] = Field(default_factory=dict)
    consumer_assessments: dict[str, PropertyAssertion] = Field(default_factory=dict)
    blockers: list[Text] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class OccurrenceSourceEvidence(ComparisonModel):
    """Read-only source claims, distinct from saved observations and quantities."""

    recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    position: Annotated[int, Field(ge=0, strict=True)]
    weight_g: Mass
    source_line: Annotated[str, Field(min_length=1)]
    source_hash: Text
    selected_source_text: Text | None = None
    properties: dict[str, PropertyAssertion] = Field(default_factory=dict)
    property_support: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    conflicts: list[Text] = Field(default_factory=list)
    deferred_reason: str | None = None
    recovery_version: Literal["adaptation_source_overlay_v1"] = "adaptation_source_overlay_v1"


class ComparisonIngredient(ComparisonModel):
    position: Annotated[int, Field(ge=0, strict=True)]
    name: Text
    original_text: str = ""
    quantity: ComparisonQuantity
    observed_properties: dict[str, PropertyAssertion] = Field(default_factory=dict)
    source_evidence: OccurrenceSourceEvidence | None = None
    composition: ComparisonComposition | None = None


class ComparisonSnapshot(ComparisonModel):
    """Pinned baseline supplied by a trusted loader, without rematching names."""

    recipe_id: Text
    run_id: Text
    nutrition_source: NutritionSource
    release_id: Text
    quantity_input_hash: Text
    serves: PositiveNumber | None = None
    ingredients: list[ComparisonIngredient] = Field(min_length=1)
    input_policy: Literal["verified", "production_estimate"] = "verified"
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_positions(self) -> ComparisonSnapshot:
        positions = [item.position for item in self.ingredients]
        if len(positions) != len(set(positions)):
            raise ValueError("Ingredient positions must be unique within a run")
        return self


class NutritionalFacetChange(ComparisonModel):
    facet: Literal["fat_level", "sodium_level", "sugar_level"]
    from_value: Text | None = None
    to_value: Text
    metric: Metric


class RawPartEdit(ComparisonModel):
    """Pinned raw substitution with explicit food identities and optional parts."""

    operation: Literal["REPLACE_PRODUCT_VARIANT", "REPLACE_INGREDIENT"] = "REPLACE_PRODUCT_VARIANT"
    recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    position: Annotated[int, Field(ge=0, strict=True)]
    from_composition: CompositionRef
    to_composition: CompositionRef
    source_identity: Text
    target_identity: Text
    source_biological_source: Text | None = None
    target_biological_source: Text | None = None
    source_food_part: Text | None = None
    target_food_part: Text | None = None
    quantity_policy: Literal["preserve_raw_edible_mass", "preserve_saved_input_mass"] = "preserve_raw_edible_mass"
    nutritional_change: NutritionalFacetChange | None = None


class ComparisonConstraints(ComparisonModel):
    forbidden_allergens: set[Text] = Field(default_factory=set)
    required_consumer_groups: set[Literal["vegan", "vegetarian"]] = Field(default_factory=set)


class NumberRange(ComparisonModel):
    lower: Mass
    upper: Mass

    @model_validator(mode="after")
    def ordered(self):
        if self.upper < self.lower:
            raise ValueError("Reversed range")
        return self


class ComparisonResult(ComparisonModel):
    status: Literal["blocked", "needs_evidence", "supported_input_comparison"]
    baseline_run_id: str
    serves: PositiveNumber | None = None
    edit: RawPartEdit
    comparison_version: Literal["raw_chicken_part_v2", "raw_chicken_part_v3", "raw_substitution_v1", "production_input_estimate_v1", "production_input_estimate_v2", "production_input_estimate_v3", "production_input_estimate_v4_source_fields", "production_input_estimate_v5_field_aliases", "production_input_estimate_v6_fibre_fields", "production_input_estimate_v3_unchanged_inputs", "production_input_estimate_v2_unchanged_inputs", "nutritional_product_variant_v1"] = "raw_substitution_v1"
    baseline_quantity: ComparisonQuantity | None = None
    quantity: ComparisonQuantity | None = None
    quantity_declarations: list[QuantityBasisDeclaration] = Field(default_factory=list)
    observed_properties: dict[str, PropertyAssertion] = Field(default_factory=dict)
    source_evidence: OccurrenceSourceEvidence | None = None
    source_composition: ComparisonComposition | None = None
    target_composition: ComparisonComposition | None = None
    blockers: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    nutrient_blockers: dict[Metric, list[str]] = Field(default_factory=dict)
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    ingredient_delta: dict[Metric, float | None] = Field(default_factory=dict)
    recipe_delta: dict[Metric, float | None] = Field(default_factory=dict)
    recipe_delta_per_serving: dict[Metric, float | None] = Field(default_factory=dict)
    quantity_basis: Literal["raw_edible_input", "selected_state_input_estimate"] = "raw_edible_input"
    culinary_status: Literal["not_evaluated"] = "not_evaluated"
    instruction_status: Literal["pending"] = "pending"
    uncertainty: list[str] = Field(default_factory=lambda: [
        "Input-ingredient estimates; cooked yield, retention and consumed media are not evaluated.",
        "Equivalent raw edible mass does not establish equivalent cooked portions or culinary suitability.",
    ])


# ---- from contextual_models.py ----

Position = Annotated[int, Field(ge=0, strict=True)]
# Project-specific pilot vocabulary, not a universal taxonomy of ingredient functions.
CulinaryRole = Literal[
    "main_component", "binding", "thickening", "emulsifying", "aerating", "setting",
    "structure", "texture", "moisture", "flavour", "cooking_medium",
]


class ContextIngredient(ComparisonModel):
    position: Position
    name: Text
    original_text: str = ""
    quantity: ComparisonQuantity
    observed_properties: dict[str, PropertyAssertion] = Field(default_factory=dict)
    source_evidence: OccurrenceSourceEvidence | None = None


class ContextStep(ComparisonModel):
    step_id: Text
    text: Text


class AdaptationContext(ComparisonModel):
    recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    title: Text
    objective: Text
    target_position: Position
    ingredients: list[ContextIngredient] = Field(min_length=1)
    steps: list[ContextStep]
    constraints: ComparisonConstraints = Field(default_factory=ComparisonConstraints)

    @model_validator(mode="after")
    def check_references(self) -> AdaptationContext:
        positions = [item.position for item in self.ingredients]
        step_ids = [item.step_id for item in self.steps]
        if len(positions) != len(set(positions)) or len(step_ids) != len(set(step_ids)):
            raise ValueError("Context ingredient positions and step IDs must be unique")
        if self.target_position not in positions:
            raise ValueError("Target occurrence must belong to the context")
        return self


class ContextQuote(ComparisonModel):
    kind: Literal["ingredient", "instruction"]
    reference_id: Text  # ingredient:<position>, or an exact supplied step ID
    quote: Text


class RoleHypothesis(ComparisonModel):
    role: CulinaryRole
    reason: Text
    evidence: list[ContextQuote] = Field(min_length=1)


class RoleAssessment(ComparisonModel):
    status: Literal["inferred", "uncertain"]
    roles: list[RoleHypothesis]
    uncertainties: list[Text]

    @model_validator(mode="after")
    def check_status(self) -> RoleAssessment:
        roles = [item.role for item in self.roles]
        if len(roles) != len(set(roles)):
            raise ValueError("Roles must be unique")
        if self.status == "inferred" and (not roles or self.uncertainties):
            raise ValueError("Inferred roles need evidence and no unresolved role uncertainty")
        if self.status == "uncertain" and not self.uncertainties:
            raise ValueError("Uncertain roles must explain the missing information")
        return self


class ContextCandidate(ComparisonModel):
    candidate_id: Text
    label: Text
    composition: CompositionRef
    properties: dict[str, PropertyAssertion] = Field(default_factory=dict)
    planned_edit: RawPartEdit | None = None
    constraint_status: Literal["passed", "blocked", "unknown"] = "unknown"
    constraint_reasons: list[Text] = Field(default_factory=list)
    constraint_evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_constraints(self) -> ContextCandidate:
        if self.planned_edit is not None and self.planned_edit.to_composition != self.composition:
            raise ValueError("Planned edit must target this exact candidate composition")
        if self.constraint_status == "passed":
            if self.constraint_reasons or not self.constraint_evidence:
                raise ValueError("Passed constraints require deterministic evidence and no blockers")
            if any(item.status != "stated" for item in self.constraint_evidence):
                raise ValueError("Inferred or proxy evidence cannot approve hard constraints")
        elif not self.constraint_reasons:
            raise ValueError("Blocked/unknown constraints must give their reasons")
        return self


class RoleFit(ComparisonModel):
    role: CulinaryRole
    status: Literal["preserved", "needs_changes", "not_preserved", "uncertain"]


class RequiredContextChange(ComparisonModel):
    """A conditional request; only supported, checked operations can enter a draft."""

    kind: Literal["quantity", "ingredient", "cooking_method", "instruction"]
    ingredient_positions: list[Position]
    step_ids: list[Text] = Field(min_length=1)
    description: Text
    status: Literal["proposed_requires_validation"] = "proposed_requires_validation"

    @model_validator(mode="after")
    def check_targets(self) -> RequiredContextChange:
        if self.kind == "quantity" and not self.ingredient_positions:
            raise ValueError("Quantity changes must target existing ingredient occurrences")
        if (len(self.ingredient_positions) != len(set(self.ingredient_positions))
                or len(self.step_ids) != len(set(self.step_ids))):
            raise ValueError("Required change targets must be unique")
        return self


class CandidateAssessment(ComparisonModel):
    candidate_id: Text
    verdict: Literal["suitable", "suitable_with_changes", "unsuitable", "uncertain"]
    reason: Text
    role_fit: list[RoleFit] = Field(min_length=1)
    evidence: list[ContextQuote] = Field(min_length=1)
    required_changes: list[RequiredContextChange]
    uncertainties: list[Text]

    @model_validator(mode="after")
    def check_verdict(self) -> CandidateAssessment:
        if self.verdict == "suitable" and (
            self.required_changes or self.uncertainties or any(item.status != "preserved" for item in self.role_fit)
        ):
            raise ValueError("Suitable candidates must preserve roles without unresolved changes")
        if self.verdict == "suitable_with_changes" and (
            not self.required_changes or self.uncertainties
            or any(item.status in {"not_preserved", "uncertain"} for item in self.role_fit)
        ):
            raise ValueError("Conditional suitability needs explicit feasible changes")
        if self.verdict == "uncertain" and not self.uncertainties:
            raise ValueError("Uncertain candidates must explain uncertainty")
        return self


class CandidateBatch(ComparisonModel):
    assessments: list[CandidateAssessment]


class ContextModelResult(ComparisonModel):
    context_hash: Text
    target_position: Position
    model: str | None = None
    source: str | None = None
    failure_reason: str | None = None
    prompt_version: Literal["culinary_context_v1", "culinary_context_v2", "culinary_context_v3"] = "culinary_context_v3"
    vocabulary_version: Literal["pilot_roles_v1"] = "pilot_roles_v1"
    evidence_status: Literal["inferred"] = "inferred"
    numerical_approval: Literal[False] = False
    instruction_status: Literal["pending"] = "pending"


class RoleInferenceResult(ContextModelResult):
    status: Literal["assessed", "uncertain", "unavailable"]
    assessment: RoleAssessment | None = None
    assessment_hash: str | None = None


class CandidateExclusion(ComparisonModel):
    candidate_id: Text
    constraint_status: Literal["blocked", "unknown"]
    reasons: list[Text] = Field(min_length=1)


class CandidateBatchResult(ContextModelResult):
    status: Literal["assessed", "uncertain", "unavailable"]
    candidate_batch_hash: Text
    role_assessment_hash: str | None = None
    assessments: list[CandidateAssessment] = Field(default_factory=list)
    excluded_candidates: list[CandidateExclusion] = Field(default_factory=list)


# ---- from preview_models.py ----

class ImpactFactor(ComparisonModel):
    composition: CompositionRef
    value_per_kg: Mass
    product_id: Text
    category: str | None = None
    evidence: Evidence
    source_reference: dict[str, Any] | None = None


class ImpactContext(ComparisonModel):
    recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    baseline_total: Mass
    factors: list[ImpactFactor]
    minimum_reduction: Annotated[float, Field(ge=0, le=1)]
    region: Text


class NutrientObjective(ComparisonModel):
    metric: Literal["energy_kcal", "protein_g", "carbs_g", "fat_g", "sugar_g", "saturated_fat_g",
                    "sodium_mg", "fibre_g", "cost_eur", "co2e_kg"]
    direction: Literal["decrease", "increase"] = "decrease"
    basis: Literal["per_serving"] = "per_serving"
    impact_context: ImpactContext | None = None


class PreviewCandidate(ComparisonModel):
    candidate_id: Text
    composition: CompositionRef
    label: Text
    status: Literal["rejected", "needs_evidence", "needs_changes", "ready_for_instruction_editing"]
    reasons: list[str] = Field(default_factory=list)
    comparison: ComparisonResult | None = None
    culinary: CandidateAssessment | None = None
    objective_improvement_per_serving: float | None = None
    rank: Annotated[int, Field(gt=0, strict=True)] | None = None
    numerical_scope: Literal["single_variant_edit_only"] = "single_variant_edit_only"


class ShortlistCandidate(ComparisonModel):
    composition: CompositionRef
    label: Text
    status: Literal["eligible", "needs_evidence", "rejected"]
    reasons: list[str] = Field(default_factory=list)
    comparison: ComparisonResult | None = None
    objective_preflight: dict[str, Any] | None = None
    objective_improvement_per_serving: float | None = None
    selected: bool = False
    discovery_mechanism: Literal["same_identity_nutritional_facet_variant", "same_identity_food_part_variant", "production_graph_substitution"] | None = None


class RawPartShortlist(ComparisonModel):
    recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    target_position: Annotated[int, Field(ge=0, strict=True)]
    status: Literal["selected", "needs_evidence", "no_supported_candidates"]
    reasons: list[str] = Field(default_factory=list)
    candidate_refs: list[CompositionRef] = Field(default_factory=list)
    screening: list[ShortlistCandidate] = Field(default_factory=list)
    selection_version: Literal["raw_part_shortlist_v1"] = "raw_part_shortlist_v1"


class AdaptationPreview(ComparisonModel):
    recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    target_position: Annotated[int, Field(ge=0, strict=True)]
    objective: NutrientObjective
    status: Literal["has_supported_candidates", "changes_required", "needs_evidence", "no_supported_candidates", "blocked"]
    reasons: list[str] = Field(default_factory=list)
    context: AdaptationContext | None = None
    roles: RoleInferenceResult | None = None
    culinary_batch: CandidateBatchResult | None = None
    candidates: list[PreviewCandidate] = Field(default_factory=list)
    candidate_selection: RawPartShortlist | None = None
    preview_version: Literal["raw_part_preview_v1"] = "raw_part_preview_v1"
    recipe_ready: Literal[False] = False
    instruction_status: Literal["pending"] = "pending"


# ---- from instruction_models.py ----

class InstructionPatch(ComparisonModel):
    kind: Literal["title", "ingredient_name", "ingredient_text", "instruction"]
    reference_id: Text
    start: Annotated[int, Field(ge=0, strict=True)]
    end: Annotated[int, Field(gt=0, strict=True)]
    before: Annotated[str, Field(min_length=1)]
    # Empty means deletion, accepted only when present in an operation's exact allowlist.
    after: str


class CookingAdjustment(ComparisonModel):
    change_index: Annotated[int, Field(ge=0, strict=True)]
    parameter: Literal["duration", "temperature"]
    patch: InstructionPatch
    reason: Text


class InstructionPatchPlan(ComparisonModel):
    status: Literal["proposed", "uncertain"]
    patches: list[InstructionPatch]
    uncertainties: list[Text]
    cooking_adjustments: list[CookingAdjustment] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_status(self) -> "InstructionPatchPlan":
        if self.status == "proposed" and (not self.patches or self.uncertainties):
            raise ValueError("A proposed plan needs patches and no unresolved uncertainty")
        if self.status == "uncertain" and (self.patches or self.cooking_adjustments or not self.uncertainties):
            raise ValueError("Uncertain plans must abstain and explain why")
        return self


class DraftIngredient(ComparisonModel):
    """Proposed display text; original observed properties remain in the baseline."""

    position: Position
    name: Text
    original_text: str
    quantity: ComparisonQuantity


class InstructionDraft(ComparisonModel):
    status: Literal["draft_requires_review", "needs_evidence", "unavailable"]
    candidate_id: Text
    recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    context_hash: str | None = None
    preview_hash: str | None = None
    model: str | None = None
    source: str | None = None
    reasons: list[str] = Field(default_factory=list)
    patches: list[InstructionPatch] = Field(default_factory=list)
    cooking_adjustments: list[CookingAdjustment] = Field(default_factory=list)
    title: str | None = None
    ingredients: list[DraftIngredient] = Field(default_factory=list)
    instructions: list[ContextStep] = Field(default_factory=list)
    comparison: ComparisonResult | None = None
    editor_version: Literal["raw_part_exact_patches_v1", "raw_substitution_exact_patches_v1", "raw_substitution_cooking_parameters_v1", "reviewed_overlap_exact_patches_v1", "reviewed_overlap_exact_patches_v2", "reviewed_overlap_exact_patches_v3", "reviewed_overlap_exact_patches_v4"] = "raw_part_exact_patches_v1"
    evidence_status: Literal["inferred"] = "inferred"
    recipe_ready: Literal[False] = False
    uncertainties: list[str] = Field(default_factory=list)


# ---- from recipe_response_models.py ----

class RecipeNutrition(ComparisonModel):
    basis: Literal["raw_edible_input", "selected_state_input_estimate"] = "raw_edible_input"
    totals: dict[Metric, Mass | None]
    per_serving: dict[Metric, Mass | None]
    supported_partial_totals: dict[Metric, Mass]
    supported_partial_per_serving: dict[Metric, Mass | None]
    completeness: dict[str, Any]
    nutri_score: None = None
    nutri_score_status: Literal["not_calculated"] = "not_calculated"
    input_score_estimate: dict[str, Any] | None = None


class AdaptedRecipeIngredient(ComparisonModel):
    position: Position
    name: Text
    original_text: str
    quantity: ComparisonQuantity
    baseline_quantity: ComparisonQuantity
    composition: CompositionRef | None
    matched_label: str | None = None
    quantity_assumption: dict[str, Any] | None = None
    baseline_observed_properties: dict[str, PropertyAssertion]
    proposed_composition_properties: dict[str, PropertyAssertion]


class AdaptedRecipe(ComparisonModel):
    title: Text
    serves: PositiveNumber
    ingredients: list[AdaptedRecipeIngredient] = Field(min_length=1)
    instructions: list[Text] = Field(min_length=1)
    nutrition: RecipeNutrition
    impact_estimate: dict[str, Any] | None = None


class RecipeIngredientChange(ComparisonModel):
    from_ingredient: Text
    to_ingredient: Text
    edit: RawPartEdit


class RecipeAdaptationResponse(ComparisonModel):
    status: Literal["draft_requires_review", "blocked", "needs_evidence", "changes_required",
                    "no_supported_candidates", "instruction_pending", "unavailable"]
    original_recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    objective: NutrientObjective
    selected_candidate_id: str | None = None
    adapted_recipe: AdaptedRecipe | None = None
    changes: list[RecipeIngredientChange] = Field(default_factory=list)
    comparison: ComparisonResult | None = None
    constraints: ComparisonConstraints = Field(default_factory=ComparisonConstraints)
    constraint_status: Literal["not_evaluated", "passed_input_comparison"] = "not_evaluated"
    reasons: list[str] = Field(default_factory=list)
    candidate_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list)
    recipe_ready: Literal[False] = False
    saved: Literal[False] = False
    response_version: Literal["controlled_recipe_v1"] = "controlled_recipe_v1"

    @model_validator(mode="after")
    def check_outcome(self) -> "RecipeAdaptationResponse":
        if self.status == "draft_requires_review":
            if self.adapted_recipe is None or self.comparison is None or not self.changes:
                raise ValueError("A successful draft requires recipe, comparison and changes")
        elif self.adapted_recipe is not None or self.changes:
            raise ValueError("Unsuccessful adaptation cannot return an adapted recipe or applied changes")
        return self
