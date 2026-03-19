"""Pydantic models for API payloads and internal pipeline state."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union, Literal

from pydantic import BaseModel, ConfigDict, Field


class IngredientProfile(BaseModel):
    name: Optional[str] = None
    measurement: Optional[str] = None
    weight_g: float = 0.0

    source: Optional[str] = None
    canonical_food_id: Optional[str] = None
    matched_nutritional_ingredient: Optional[str] = None
    protein_per_100g: Optional[float] = None
    carbs_per_100g: Optional[float] = None
    fat_per_100g: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    distance: Optional[float] = None
    sustainability_ingredient: Optional[str] = None
    matched_sustainability_ingredient: Optional[str] = None
    sustainability_weight_g: Optional[float] = None
    cf_val: Optional[float] = Field(default=None, description="Carbon footprint value")
    sustainability_distance: Optional[float] = None
    contribution: Optional[float] = None


class RecipeState(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    raw_recipe: Optional[str] = None
    title: Optional[str] = None
    region: Optional[str] = None

    # DEPRECATED (kept optional for upstream nodes)
    ingredient_names: List[str] = Field(default_factory=list)
    measurements: List[str] = Field(default_factory=list)
    weights: Union[List[float], Dict[str, Any]] = Field(default_factory=list)

    # NEW canonical container
    ingredients: List[IngredientProfile] = Field(default_factory=list)

    debug: bool = False
    directions: List[str] = Field(default_factory=list)
    total_time: Optional[float] = None
    tags: List[str] = Field(default_factory=list)
    allergens: List[str] = Field(default_factory=list)

    sustainability_per_kg: Optional[float] = None
    total_protein_g: Optional[float] = None
    total_fat_g: Optional[float] = None
    total_carbohydrate_g: Optional[float] = None
    total_energy_kcal: Optional[float] = None

    profiling_totals: Dict[str, float] = Field(default_factory=dict)
    full_profile: Dict[str, Any] = Field(default_factory=dict)
    pipeline_trace: Dict[str, Any] = Field(default_factory=dict)
    nutri_score: Optional[Dict[str, Any]] = None
    nutri_score_color: Optional[str] = None
    nutri_score_source: Optional[str] = None

    # optional inputs if available
    serves: Optional[float] = None
    serving_size_g: Optional[float] = None
    min_similarity: Optional[float] = None

    similar_recipes: List[Dict[str, Any]] = Field(default_factory=list)
    agent_decision: Optional[str] = None
    query: Optional[str] = None
    cypher: Optional[str] = None
    tag_list: List[str] = Field(default_factory=list)


class RecipeSearchRequest(BaseModel):
    """Incoming payload for the recipe search endpoint."""

    question: str = Field(
        default="",
        description="Natural language recipe question. Empty means unconstrained random search.",
    )
    exclude_allergens: List[str] = Field(
        default_factory=list,
        description="Allergen names to exclude (e.g., ['peanut', 'tree_nut'])",
    )


class RecipeSearchResponse(BaseModel):
    """Normalized response envelope for recipe search results."""

    results: Union[List[Dict[str, Any]], str]
    steps: List[str]
    cypher_statement: str


class RecipeSearchFilters(BaseModel):
    """Explicit parameterized filters for deterministic recipe search."""

    include_ingredients: List[str] = Field(default_factory=list)
    exclude_ingredients: List[str] = Field(default_factory=list)
    exclude_allergens: List[str] = Field(default_factory=list)
    diet_tags: List[str] = Field(default_factory=list)
    max_duration_minutes: Optional[int] = None
    limit: int = Field(default=10, ge=1, le=100)


class ParseRecipeRequest(BaseModel):
    """Incoming payload carrying raw recipe text to parse."""

    raw_recipe: str = Field(..., min_length=1, description="Unstructured recipe text")


class ParseRecipeResponse(BaseModel):
    """Structured representation returned by the parse tool."""

    title: str
    ingredient_names: List[str]
    measurements: List[str]
    directions: List[str]
    total_time: Optional[float] = None


class RecipeProfileRequest(BaseModel):
    """Incoming payload for the recipe profiling endpoint."""

    raw_recipe: str = Field(
        ...,
        min_length=1,
        description="Unstructured recipe text to analyze",
    )
    region: Literal["IE", "US"] = Field(
        default="IE",
        description="Country/region code used to select nutrition source (supports 'IE' and 'US').",
    )
    persist_trace: bool = Field(
        default=True,
        description="Whether to persist profiling trace metadata into Postgres.",
    )


class RecipeProfileResponse(RecipeState):
    """Response payload returned by the profiling endpoint."""

    message: str = "Success"


class RecipeDetailResponse(BaseModel):
    """Detailed recipe representation fetched directly from Neo4j."""

    recipe_id: Optional[str]
    title: Optional[str]
    source: Optional[str] = None
    image_url: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    ingredients: List[Dict[str, Any]]
    instructions: List[str]
    duration: Optional[float]
    serves: Optional[float]
    total_kcal_per_serving: Optional[float] = None
    total_protein_g_per_serving: Optional[float] = None
    total_carbs_g_per_serving: Optional[float] = None
    total_fat_g_per_serving: Optional[float] = None
    total_fiber_g_per_serving: Optional[float] = None
    total_sugar_g_per_serving: Optional[float] = None
    total_sodium_mg_per_serving: Optional[float] = None
    total_cholesterol_mg_per_serving: Optional[float] = None
    nutri_score: Optional[float] = None
    total_nutrients: Optional[Dict[str, Any]] = None
    total_nutrients_per_serving: Optional[Dict[str, Any]] = None
    nutri_score_raw: Optional[Any] = None
    nutri_score_breakdown: Optional[Dict[str, Any]] = None
    nutrition_source: Optional[str] = None
    nutrients: Optional[List[Dict[str, Any]]] = None
    nutrition_profiling_details: Optional[List[Dict[str, Any]]] = None
    nutrition_profiling_debug: Optional[Dict[str, Any]] = None
