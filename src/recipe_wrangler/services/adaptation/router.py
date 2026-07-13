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
)
from .service import generate_suggestions, simulate_swap


router = APIRouter(prefix="/api/v1/recipes", tags=["adaptation"])


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
