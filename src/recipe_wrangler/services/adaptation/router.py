"""FastAPI router for the adaptation service.

Mounted by the main API (`recipe_wrangler.api.main`) and by `app.py` for
standalone development runs.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query

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


@router.get("/{recipe_id}/adaptations")
def stored_adaptation(
    recipe_id: str = Path(..., description="Recipe ID as stored in PostgreSQL profile."),
    mode: Literal["sustainability", "nutrition", "cost"] = Query("sustainability"),
    region: Literal["IE", "HU", "SI", "EU"] = Query(..., description="Region of the adaptation."),
) -> dict:
    """The stored adaptation of a recipe for one mode and region, or the reason code and sentence when there is none."""
    from recipe_wrangler.repositories.recipe_adaptations import get_adaptation
    row = get_adaptation(recipe_id, mode, region)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No stored {mode} adaptation row for recipe {recipe_id} in region {region}.")
    if row["status"] != "adapted":
        from .sustainability_reasons import WHY
        row["reason"] = WHY.get(row["reason_code"]) if mode == "sustainability" else None
    return row


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
