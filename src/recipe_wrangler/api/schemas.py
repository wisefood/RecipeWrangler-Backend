"""Request/response models for the web API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


class RecipeSearchRequest(BaseModel):
    """Incoming payload for the recipe search endpoint."""

    question: str = Field(..., min_length=1, description="Natural language recipe question")


class RecipeSearchResponse(BaseModel):
    """Normalized response envelope for recipe search results."""

    results: Union[List[Dict[str, Any]], str]
    steps: List[str]
    cypher_statement: str


class ParseRecipeRequest(BaseModel):
    """Incoming payload carrying raw recipe text to parse."""

    raw_recipe: str = Field(..., min_length=1, description="Unstructured recipe text")


class ParseRecipeResponse(BaseModel):
    """Structured representation returned by the parse tool."""

    title: str
    ingredient_names: List[str]
    measurements: List[str]
    directions: List[str]
    total_time: Optional[int] = None


class RecipeProfileRequest(BaseModel):
    """Incoming payload for the recipe profiling endpoint."""

    raw_recipe: str = Field(
        ...,
        min_length=1,
        description="Unstructured recipe text to analyze",
    )


class RecipeProfileResponse(BaseModel):
    """Response payload returned by the profiling endpoint."""

    title: Optional[str]
    serves: Optional[float]
    duration_min: Optional[float]
    ingredients_grams: Dict[str, Any]
    directions: List[str]
    profiling_totals: Dict[str, Any]
    tags: List[str] = Field(default_factory=list)


class RecipeDetailResponse(BaseModel):
    """Detailed recipe representation fetched directly from Neo4j."""

    id: str
    title: Optional[str]
    ingredients: List[Dict[str, Any]]
    instructions: List[str]
    duration: Optional[float]
    serves: Optional[float]
    total_carbs_g_per_serving: Optional[float]
    nutri_score: Optional[float]
    total_protein_g_per_serving: Optional[float]
    total_sustainability_per_serving: Optional[float]
    total_kcal_per_serving: Optional[float]
    total_fat_g_per_serving: Optional[float]
    total_fiber_g_per_serving: Optional[float]
    total_sugar_g_per_serving: Optional[float]
    total_cholesterol_mg_per_serving: Optional[float]
    tags: List[str]
    allergens: List[str]
