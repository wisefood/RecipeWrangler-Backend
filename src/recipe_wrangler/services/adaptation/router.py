"""FastAPI router for the adaptation service.

Mounted by the main API (`recipe_wrangler.api.main`) and by `app.py` for
standalone development runs.
"""

from __future__ import annotations

from fastapi import APIRouter, Path

from .schemas import (
    SimulateRequest,
    SimulateResponse,
    SuggestionsRequest,
    SuggestionsResponse,
    EnhancementRequest,
    SmallGainRequest,
)
from .service import generate_suggestions, simulate_swap
from .enhancement_response import EnhancementResponse


router = APIRouter(prefix="/api/v1/recipes", tags=["adaptation"])


@router.post("/{recipe_id}/adapt/enhance", response_model=EnhancementResponse)
def enhance(payload: EnhancementRequest, recipe_id: str = Path(...)) -> dict:
    """Run controlled adaptation; return an unsaved draft or explicit evidence gaps."""
    from .enhancement_pipeline import enhance_active_recipe
    from .service import _region_to_source
    return enhance_active_recipe(recipe_id, _region_to_source(payload.region), mode=payload.mode,
        goal_nutrients=payload.goal_nutrients, constraints=payload.constraints, cost_country=payload.region)


@router.post("/{recipe_id}/adapt/small-gain")
def small_gain(payload: SmallGainRequest, recipe_id: str = Path(...)) -> dict:
    """Healthier version by amount cuts, curated food swaps and extra vegetables; unsaved, deterministic, no LLM."""
    from .service import _region_to_source
    from .small_gain import adapt_active_recipe
    return adapt_active_recipe(recipe_id, _region_to_source(payload.region))


@router.post(
    "/{recipe_id}/adapt/suggestions",
    response_model=SuggestionsResponse,
)
def suggestions(
    payload: SuggestionsRequest,
    recipe_id: str = Path(..., description="Recipe ID as stored in PostgreSQL profile."),
) -> SuggestionsResponse:
    result = generate_suggestions(
        recipe_id=recipe_id,
        region=payload.region,
        max_swaps=payload.max_swaps,
        use_llm=payload.use_llm,
        mode=payload.mode,
        goal_nutrients=payload.goal_nutrients,
        target_serves=payload.target_serves,
    )
    return SuggestionsResponse(**result)


@router.post(
    "/{recipe_id}/adapt/simulate",
    response_model=SimulateResponse,
)
def simulate(
    payload: SimulateRequest,
    recipe_id: str = Path(..., description="Recipe ID as stored in PostgreSQL profile."),
) -> SimulateResponse:
    result = simulate_swap(
        recipe_id=recipe_id,
        region=payload.region,
        original_ingredient=payload.swap.original_ingredient,
        substitute_ingredient=payload.swap.substitute_ingredient,
        weight_g=payload.swap.weight_g,
    )
    return SimulateResponse(**result)
