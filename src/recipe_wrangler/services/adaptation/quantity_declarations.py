"""Apply explicit attestations to a copied comparison view, never saved facts."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from .models import ComparisonQuantity, ComparisonSnapshot, QuantityBasisDeclaration


class QuantityDeclarationError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def apply_quantity_declarations(
    snapshot: ComparisonSnapshot, declarations: Sequence[QuantityBasisDeclaration],
) -> tuple[ComparisonSnapshot, list[QuantityBasisDeclaration]]:
    """Preserve mass and scope; caller must authenticate the declaration source."""
    if not declarations:
        return snapshot, []
    try:
        validated = [QuantityBasisDeclaration.model_validate(item.model_dump()) for item in declarations]
    except ValidationError as error:
        raise QuantityDeclarationError("invalid_quantity_declaration") from error
    ingredients = {item.position: item for item in snapshot.ingredients}
    seen = set()
    for declaration in validated:
        if (declaration.recipe_id, declaration.baseline_run_id, declaration.quantity_input_hash) != (
            snapshot.recipe_id, snapshot.run_id, snapshot.quantity_input_hash,
        ):
            raise QuantityDeclarationError("quantity_declaration_baseline_mismatch")
        if declaration.position in seen:
            raise QuantityDeclarationError("duplicate_quantity_declaration")
        seen.add(declaration.position)
        ingredient = ingredients.get(declaration.position)
        if ingredient is None:
            raise QuantityDeclarationError("quantity_declaration_occurrence_not_found")
        if ingredient.quantity.weight_g != declaration.weight_g:
            raise QuantityDeclarationError("quantity_declaration_mass_mismatch")
        refinable_estimate = (snapshot.input_policy == "production_estimate"
            and ingredient.quantity.basis == "selected_state_edible"
            and ingredient.quantity.evidence.status == "proxy"
            and ingredient.quantity.evidence.method == "saved_production_input_estimate")
        if ingredient.quantity.basis not in {"unknown", declaration.basis} and not refinable_estimate:
            raise QuantityDeclarationError("quantity_declaration_requires_conversion")
        state = ingredient.observed_properties.get("preparation_state")
        if state and state.evidence.status == "stated" and state.value == "cooked":
            raise QuantityDeclarationError("quantity_declaration_state_conflict")
    prepared = snapshot.model_copy(deep=True)
    by_position = {item.position: item for item in prepared.ingredients}
    for declaration in validated:
        ingredient = by_position[declaration.position]
        ingredient.quantity = ComparisonQuantity(
            weight_g=ingredient.quantity.weight_g, basis=declaration.basis,
            evidence=declaration.evidence.model_copy(deep=True),
        )
    return prepared, validated
