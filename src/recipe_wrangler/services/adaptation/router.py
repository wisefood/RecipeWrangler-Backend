"""FastAPI router for the adaptation service.

Mounted by the main API (`recipe_wrangler.api.main`) and by `app.py` for
standalone development runs.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query

from .schemas import (
    AdaptationResponse,
    EnhancementRequest,
    SmallGainRequest,
)
from .enhancement_response import EnhancementResponse


router = APIRouter(prefix="/api/v1/recipes", tags=["adaptation"])


@router.get("/{recipe_id}/adaptations", response_model=AdaptationResponse)
def stored_adaptation(
    recipe_id: str = Path(..., description="Recipe ID as stored in PostgreSQL profile."),
    mode: Literal["sustainability", "nutrition", "cost", "time"] = Query("sustainability"),
    region: Literal["IE", "HU", "SI", "EU"] = Query(..., description="Region of the adaptation."),
    allergens: list[str] = Query([], description="User's allergens; no swap may contain them."),
    diet: Literal["vegan", "vegetarian", "pescatarian"] | None = Query(None, description="User's diet; no swap may break it."),
) -> dict:
    """The adaptation of a recipe for one mode and region, in the same shape for every mode: the stored row, or the engine's answer when
    nothing is stored (nutrition: minutes) or the mode recomputes for restrictions (sustainability). Restricted nutrition checks the stored swap."""
    from .adaptation_row import adaptation_row
    row = adaptation_row(recipe_id, mode, region, allergens, diet)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No {mode} adaptation for recipe {recipe_id} in region {region}.")
    return row


@router.post("/{recipe_id}/adapt/enhance", response_model=EnhancementResponse)
def enhance(payload: EnhancementRequest, recipe_id: str = Path(...)) -> dict:
    """Run controlled adaptation; return an unsaved draft or explicit evidence gaps."""
    from .enhancement_pipeline import enhance_active_recipe
    from .service import _region_to_source
    return enhance_active_recipe(recipe_id, _region_to_source(payload.region), mode=payload.mode,
        goal_nutrients=payload.goal_nutrients, constraints=payload.constraints, cost_country=payload.region)


def nutrition_adaptation(payload: SmallGainRequest, recipe_id: str = Path(...)) -> dict:
    """Healthier version by amount cuts, curated food swaps and extra vegetables; unsaved, deterministic, no LLM."""
    from .service import _region_to_source
    from .nutrition_adaptation import adapt_active_recipe
    return adapt_active_recipe(recipe_id, _region_to_source(payload.region), allergens=payload.allergens, diet=payload.diet)
