"""Pydantic models for API payloads and internal pipeline state."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


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
    total_sustainability: Optional[float] = None
    total_sustainability_per_serving: Optional[float] = None
    sustainability_details: List[Dict[str, Any]] = Field(default_factory=list)
    sustainability_serves: Optional[float] = None
    sustainability_profiling_details: Optional[List[Dict[str, Any]]] = None
    sustainability_profiling_debug: Optional[Dict[str, Any]] = None
    total_protein_g: Optional[float] = None
    total_fat_g: Optional[float] = None
    total_carbohydrate_g: Optional[float] = None
    total_energy_kcal: Optional[float] = None

    profiling_totals: Dict[str, float] = Field(default_factory=dict)
    full_profile: Dict[str, Any] = Field(default_factory=dict)
    pipeline_trace: Dict[str, Any] = Field(default_factory=dict)
    nutri_score: Optional[Dict[str, Any]] = None
    nutri_score_breakdown: Optional[Dict[str, Any]] = None
    nutri_score_color: Optional[str] = None
    nutri_score_source: Optional[str] = None

    # optional inputs if available
    serves: Optional[float] = None
    serving_size_g: Optional[float] = None
    min_similarity: Optional[float] = None

    # set by the profiling nodes (declared so they survive the LangGraph merge)
    nutrition_source: Optional[str] = None
    nutritional_source: Optional[str] = None
    nutrition_source_key: Optional[str] = None
    nutrition_serves: Optional[float] = None
    nutritional_totals: Optional[Dict[str, Any]] = None
    nutritional_details: Optional[List[Dict[str, Any]]] = None

    # profiling quality flags (set by Recipe_Profiling_Node)
    serves_source: Optional[str] = None            # "given" | "estimated"
    weights_capped: Optional[bool] = None          # True if implausibly-inflated weights were trimmed
    nutrition_coverage: Optional[float] = None     # fraction of recipe weight that got a nutrition match
    nutrition_low_coverage: Optional[bool] = None  # True if nutrition_coverage < ~0.8
    sustainability_coverage: Optional[float] = None
    sustainability_low_coverage: Optional[bool] = None
    profiling_quality: Dict[str, Any] = Field(default_factory=dict)

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
    sources: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("sources", "source"),
    )
    dish_types: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("dish_types", "dish_type"),
    )
    max_duration_minutes: Optional[int] = None
    limit: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    sort_by: Optional[Literal["title_asc", "title_desc", "time_asc", "time_desc", "random"]] = None
    include_facets: bool = Field(default=False)
    include_total: bool = Field(default=True)
    include_disabled: bool = Field(
        default=False,
        description="When true, disabled (soft-deleted) recipes appear in results — console/admin use only.",
    )

    @field_validator("dish_types", mode="before")
    @classmethod
    def _coerce_dish_types(cls, value):  # noqa: N805
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("sources", mode="before")
    @classmethod
    def _coerce_sources(cls, value):  # noqa: N805
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [value]
        return value


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
    region: Literal["IE", "US", "HU"] = Field(
        default="IE",
        description="Country/region code used to select nutrition source (supports 'IE', 'US', and 'HU').",
    )
    persist_trace: bool = Field(
        default=True,
        description="Whether to persist profiling trace metadata into Postgres.",
    )
    serves: Optional[float] = Field(
        default=None,
        gt=0,
        description="Trusted serving count to use instead of the parser's inferred serving count.",
    )
    parse_only: bool = Field(
        default=False,
        description=(
            "When true, skip weight estimation and nutrition profiling. "
            "Returns only the parsed title, ingredient_names, measurements, directions, total_time, and serves."
        ),
    )


class RecipeProfileResponse(RecipeState):
    """Response payload returned by the profiling endpoint."""

    message: str = "Success"


class RecipeUpdateRequest(BaseModel):
    """Payload for patching an existing recipe. All fields are optional."""

    instructions: Optional[List[str]] = None
    image_url: Optional[str] = None
    source_id: Optional[str] = None
    expert_recipe: Optional[bool] = None
    title: Optional[str] = None
    allergens: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    duration: Optional[float] = Field(default=None, gt=0)


class RecipeUpdateResponse(BaseModel):
    recipe_id: str
    updated_fields: List[str]
    tags: List[str] = Field(default_factory=list)
    allergens: List[str] = Field(default_factory=list)
    message: str = "Recipe updated successfully"


class RecipeDisableRequest(BaseModel):
    """Payload for disabling a single recipe (soft delete)."""

    reason: Optional[str] = Field(default=None, max_length=500)


class RecipeBulkStatusRequest(BaseModel):
    """Payload for bulk disable/enable by explicit recipe IDs."""

    recipe_ids: List[str] = Field(..., min_length=1, max_length=100000)
    reason: Optional[str] = Field(default=None, max_length=500)


class RecipeDisableByQueryRequest(RecipeSearchFilters):
    """Bulk disable every recipe matching the given search filters.

    Refuses an unconstrained query unless ``allow_unfiltered`` is set — a
    typo'd empty body must not disable the whole corpus.
    """

    reason: Optional[str] = Field(default=None, max_length=500)
    allow_unfiltered: bool = Field(default=False)


class RecipeStatusResponse(BaseModel):
    """Result of a disable/enable operation."""

    status: Literal["active", "disabled"]
    requested: int
    updated: int
    recipe_ids: List[str] = Field(
        default_factory=list,
        description="Resolved canonical IDs that changed (omitted beyond 1000 for bulk ops).",
    )
    es_sync: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    message: str = "Recipe status updated"


class RecipeCreateRequest(BaseModel):
    """Payload for creating a new user recipe."""

    title: str = Field(..., min_length=1)
    ingredients: List[str] = Field(..., min_length=1, description="Raw ingredient strings e.g. '1 cup flour'")
    instructions: List[str] = Field(default_factory=list)
    duration: float = Field(..., gt=0, description="Total cooking time in minutes")
    serves: float = Field(..., gt=0)
    region: Literal["IE", "US", "HU"] = Field(
        default="IE",
        description="Nutrition source region — IE (Irish), US (USDA), HU (Hungarian)",
    )
    image_url: Optional[str] = None
    source_id: Optional[str] = Field(default=None, description="UUID of the source from the sources microservice")
    expert_recipe: bool = Field(default=False, description="Whether this recipe has been reviewed and annotated by a nutrition expert")
    tags: List[str] = Field(default_factory=list, description="User-supplied diet/category tags")
    allergens: List[str] = Field(default_factory=list, description="User-supplied allergen labels")
    protein_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional manual total protein (grams) for the whole recipe.",
    )
    carbohydrate_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional manual total carbohydrate (grams) for the whole recipe.",
    )
    fat_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional manual total fat (grams) for the whole recipe.",
    )
    energy_kcal: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional manual total energy (kcal) for the whole recipe.",
    )
    sugar_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional manual total sugar (grams) for the whole recipe.",
    )
    saturated_fat_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional manual total saturated fat (grams) for the whole recipe.",
    )
    sodium_mg: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional manual total sodium (milligrams) for the whole recipe.",
    )
    fibre_g: Optional[float] = Field(
        default=None,
        ge=0,
        description="Optional manual total fibre (grams) for the whole recipe.",
    )


class RecipeCreateResponse(BaseModel):
    """Confirmation returned after a new recipe is created."""

    recipe_id: str
    message: str = "Recipe created successfully"


class RecipeSubstituteRequest(BaseModel):
    """Payload for the ingredient substitution endpoint."""

    ingredient: str = Field(..., min_length=1, description="Name of the ingredient to substitute")
    region: Literal["IE", "US", "HU"] = Field(
        default="IE",
        description="Nutrition source region for the re-profiling step",
    )


class RecipeSubstituteResponse(BaseModel):
    """Response from the ingredient substitution endpoint."""

    original_ingredient: str
    substitute: str
    substitution_source: Literal["graph_direct", "foodon_taxonomy"]
    candidates: List[str]
    modified_recipe_profile: Dict[str, Any]


class RecipeCardResponse(BaseModel):
    """Slim recipe representation for card/list rendering — no nutrition data."""

    recipe_id: Optional[str]
    title: Optional[str]
    url: Optional[str] = None
    source: Optional[str] = None
    source_id: Optional[str] = None
    expert_recipe: bool = False
    image_url: Optional[str] = None
    duration: Optional[float] = None
    serves: Optional[float] = None
    cost_category: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    nutri_score_label: Optional[str] = None
    nutri_score_color: Optional[str] = None
    status: str = "active"


class RecipeDetailResponse(BaseModel):
    """Detailed recipe representation fetched directly from Neo4j."""

    recipe_id: Optional[str]
    title: Optional[str]
    url: Optional[str] = None
    source: Optional[str] = None
    source_id: Optional[str] = None
    expert_recipe: bool = False
    image_url: Optional[str] = None
    edited: Optional[bool] = None
    status: str = "active"
    disabled_reason: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    ingredients: List[Dict[str, Any]]
    instructions: List[str]
    duration: Optional[float]
    serves: Optional[float]
    cost_category: Optional[str] = None
    total_kcal_per_serving: Optional[float] = None
    total_protein_g_per_serving: Optional[float] = None
    total_carbs_g_per_serving: Optional[float] = None
    total_fat_g_per_serving: Optional[float] = None
    total_fiber_g_per_serving: Optional[float] = None
    total_sugar_g_per_serving: Optional[float] = None
    total_sodium_mg_per_serving: Optional[float] = None
    total_cholesterol_mg_per_serving: Optional[float] = None
    nutri_score: Optional[float] = None
    nutri_score_label: Optional[str] = None
    nutri_score_color: Optional[str] = None
    total_nutrients: Optional[Dict[str, Any]] = None
    total_nutrients_per_serving: Optional[Dict[str, Any]] = None
    nutri_score_breakdown: Optional[Dict[str, Any]] = None
    nutrition_source: Optional[str] = None
    has_ground_truth_nutrition: bool = False
    ground_truth_nutrition_source: Optional[str] = None
    ground_truth_nutrition: Optional[Dict[str, Any]] = None
    nutrition_profiling_details: Optional[List[Dict[str, Any]]] = None
    nutrition_profiling_debug: Optional[Dict[str, Any]] = None
    total_sustainability: Optional[float] = None
    total_sustainability_per_serving: Optional[float] = None
    sustainability_per_kg: Optional[float] = None
    sustainability_profiling_details: Optional[List[Dict[str, Any]]] = None


class RecipeCardNutrition(BaseModel):
    """Slim recipe card enriched with per-serving macros.

    Consumed by FoodChat plan enrichment and edit-verification predicates.
    Macro fields are per serving and come from the nutrition store when a
    profile exists; they are ``null`` otherwise.
    """

    recipe_id: str
    title: Optional[str] = None
    image_url: Optional[str] = None
    duration: Optional[float] = None
    tags: List[str] = Field(default_factory=list)
    dish_types: List[str] = Field(default_factory=list)
    allergens: List[str] = Field(default_factory=list)
    kcal_per_serving: Optional[float] = None
    protein_g_per_serving: Optional[float] = None
    carbs_g_per_serving: Optional[float] = None
    fat_g_per_serving: Optional[float] = None
    nutri_score_label: Optional[str] = None


class RecipeDetailsBatchRequest(BaseModel):
    """Batch recipe-details lookup payload for FoodChat plan enrichment."""

    recipe_ids: List[str] = Field(
        ...,
        min_length=1,
        max_length=30,
        description="Recipe ids to resolve (1-30 per call).",
    )
    region: Optional[str] = Field(
        default=None,
        description="Optional nutrition region selector: US, IE, or HU.",
    )

    @field_validator("recipe_ids")
    @classmethod
    def _validate_recipe_ids(cls, value):  # noqa: N805
        cleaned = [str(rid).strip() for rid in value]
        if any(not rid for rid in cleaned):
            raise ValueError("recipe_ids entries must be non-empty strings")
        return cleaned


class RecipeDetailsBatchResponse(BaseModel):
    """Batch details response. Unknown recipe ids are simply absent from ``results``."""

    results: Dict[str, RecipeCardNutrition] = Field(default_factory=dict)


class FoodChatUserProfile(BaseModel):
    allergies: List[str] = Field(default_factory=list)
    diet: List[str] = Field(default_factory=list)


class NutritionProfile(BaseModel):
    """Macro target ranges per serving. All fields optional — only provided ranges are applied."""
    min_calories: Optional[float] = None
    max_calories: Optional[float] = None
    min_protein_g: Optional[float] = None
    max_protein_g: Optional[float] = None
    min_carbs_g: Optional[float] = None
    max_carbs_g: Optional[float] = None
    min_fat_g: Optional[float] = None
    max_fat_g: Optional[float] = None


class FoodChatConstraints(BaseModel):
    include_ingredients: List[str] = Field(default_factory=list)
    exclude_ingredients: List[str] = Field(default_factory=list)
    exclude_recipe_ids: List[str] = Field(default_factory=list)
    favorite_recipe_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Recipe IDs the user has favorited. Soft ranking boost only — favorites "
            "float to the top of their meal slot but are never hard-filtered in; "
            "diet/allergen/exclusion filters still apply to them."
        ),
    )
    nutrition_profile: Optional[NutritionProfile] = None


class FoodChatRequest(BaseModel):
    user_profile: FoodChatUserProfile = Field(default_factory=FoodChatUserProfile)
    constraints: FoodChatConstraints = Field(default_factory=FoodChatConstraints)
    quotas: Dict[str, int] = Field(default_factory=dict, description="e.g., {'breakfast': 5, 'lunch': 5, 'dinner': 5}")
    randomize: bool = Field(default=True, description="When true, sort by rand() for diversity across plan iterations")


class FoodChatNutrition(BaseModel):
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None


class FoodChatRecipeItem(BaseModel):
    recipe_id: str
    title: str
    ingredients: str
    directions: str
    dish_type: Optional[str] = None
    nutrition: Optional[FoodChatNutrition] = None


class FoodChatResponse(BaseModel):
    results: Dict[str, List[FoodChatRecipeItem]]
