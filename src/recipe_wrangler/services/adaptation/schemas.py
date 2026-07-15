"""Pydantic request/response models for the adaptation endpoints."""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


Region = Literal["IE", "US", "HU"]
Mode = Literal["nutrition", "sustainability", "reduce_quantity"]


class SuggestionsRequest(BaseModel):
    region: Region = Field(..., description="Nutrition region: IE / US / HU.")
    mode: Mode = Field(
        "nutrition",
        description="Optimisation target: 'nutrition' (swap to improve Nutri-Score), "
                    "'sustainability' (swap to cut CO2e), or 'reduce_quantity' (use less of "
                    "the worst nutrient contributor when no swap helps).",
    )
    max_swaps: int = Field(
        1,
        ge=1,
        le=3,
        description="Number of top-ranked substitute suggestions to return (after LLM filter, if enabled).",
    )
    use_llm: bool = Field(
        False,
        description="If true, run an LLM judge over the deterministic candidate set to drop "
                    "culinary-nonsense swaps and rerank by recipe-aware sense. The judge cannot "
                    "invent substitutes; on any failure the deterministic ranking is returned.",
    )
    goal_nutrients: List[str] = Field(
        default_factory=list,
        description="Member dietary goals biasing which Nutri-Score nutrient is targeted "
                    "(nutrition mode only). Accepts goal slugs (reduce_fat, reduce_sugar, "
                    "reduce_salt, reduce_calories) or nutrient keys (saturated_fats, sugar, "
                    "sodium, energy). A goal wins only if the recipe actually scores badly on it.",
    )


class Explanation(BaseModel):
    headline: str
    reason: str
    warning: Optional[str] = None


class Suggestion(BaseModel):
    rank: int
    action: Literal["swap", "reduce"] = Field(
        "swap",
        description="'swap' replaces the ingredient with a substitute; 'reduce' keeps the "
                    "ingredient but uses less of it (reduce_quantity mode).",
    )
    original_ingredient: str = Field(
        ...,
        description="The ingredient being replaced or reduced (same across all suggestions in a single response).",
    )
    # Swap-only fields (null for action='reduce').
    substitute_name: Optional[str] = None
    source: Optional[Literal["miskg", "foodon"]] = None
    category_distance: Optional[Literal["low", "medium", "high"]] = None
    flavor_similarity: Optional[float] = Field(
        None,
        description="FlavorDB flavor-compound Jaccard between original and substitute, in [0,1]. "
                    "Used only as a tiebreak among culinarily-sane candidates; null when either "
                    "ingredient's FlavorDB coverage is too sparse to trust.",
    )
    introduces_allergen: bool = False
    new_allergens: list[str] = Field(default_factory=list)
    explanation: Explanation
    llm_justification: Optional[str] = Field(
        None,
        description="LLM-generated recipe-aware rationale. Present only when use_llm=true and "
                    "the judge accepted this candidate; null otherwise.",
    )

    # ---- reduce_quantity-mode metrics (populated when action='reduce') ----
    reduced_from_weight_g: Optional[float] = None
    reduced_to_weight_g: Optional[float] = None
    reduction_pct: Optional[float] = Field(
        None, description="Fraction of the original weight removed (0.5 = halved)."
    )

    # ---- nutrition-mode metrics (populated when mode=nutrition) ----
    simulated_nutri_score: Optional[str] = None
    nutri_score_points_saved: Optional[int] = None
    relative_improvement: Optional[float] = None
    target_nutrient_per_100g: Optional[float] = None
    original_per_100g: Optional[float] = None
    nutrient_delta_per_serving: Optional[dict[str, float]] = None

    # ---- sustainability-mode metrics (populated when mode=sustainability) ----
    simulated_co2e_per_serving_kg: Optional[float] = None
    co2e_reduction_per_serving_kg: Optional[float] = None
    co2e_reduction_pct: Optional[float] = None
    original_cf_kg_co2e_per_kg: Optional[float] = Field(
        None,
        description="Carbon footprint of the original ingredient (kg CO2e / kg).",
    )
    candidate_cf_kg_co2e_per_kg: Optional[float] = Field(
        None,
        description="Carbon footprint of the substitute (kg CO2e / kg).",
    )


class LLMRejection(BaseModel):
    substitute_name: str
    reason: Optional[str] = None


class SuggestionsResponse(BaseModel):
    recipe_id: str
    region: Region
    mode: Mode = Field(..., description="Which optimisation target produced this response.")

    # ---- always-populated context ----
    status: Literal["ok", "already_optimal"] = Field(
        "ok",
        description="'already_optimal' when the recipe scores below the target threshold on "
                    "every negative Nutri-Score nutrient — nothing to adapt, suggestions empty.",
    )
    message: Optional[str] = Field(
        None,
        description="Human-readable note accompanying non-'ok' statuses.",
    )
    offending_ingredient: str = ""
    offending_ingredient_contribution_pct: float = Field(
        0.0,
        description="Share of the target metric attributable to the offending ingredient. "
                    "Nutrition mode: % of recipe's target-nutrient total. "
                    "Sustainability mode: % of recipe's total CO2e.",
    )

    # ---- nutrition-mode context (populated when mode=nutrition) ----
    current_nutri_score: Optional[str] = None
    target_nutrient: Optional[str] = None
    target_nutrient_label: Optional[str] = None
    target_nutrient_points: Optional[int] = None
    target_nutrient_points_max: Optional[int] = None

    # ---- sustainability-mode context (populated when mode=sustainability) ----
    current_co2e_per_serving_kg: Optional[float] = None
    current_co2e_total_kg: Optional[float] = None

    suggestions: list[Suggestion]

    # ---- LLM metadata (populated when use_llm=true) ----
    llm_used: bool = Field(False, description="Whether the LLM judge ran and produced this ranking.")
    llm_model: Optional[str] = Field(None, description="Model id that produced the ranking (e.g. 'qwen3-32b').")
    llm_source: Optional[Literal["vllm", "groq"]] = None
    llm_rejected: list[LLMRejection] = Field(
        default_factory=list,
        description="Candidates the LLM dropped as culinary-nonsense, with reasons.",
    )


class SwapInput(BaseModel):
    original_ingredient: str
    substitute_ingredient: str
    weight_g: Optional[float] = Field(
        None,
        gt=0,
        description="Override for the substitute weight. Defaults to the original ingredient's weight.",
    )


class SimulateRequest(BaseModel):
    region: Region
    swap: SwapInput


class NutrientDelta(BaseModel):
    per_serving: dict[str, float]
    per_100g: dict[str, float]


class SimulateResponse(BaseModel):
    recipe_id: str
    region: Region
    original_nutri_score: str
    simulated_nutri_score: str
    nutri_score_points_delta: int
    original_total_nutrients_per_100g: dict[str, float]
    simulated_total_nutrients_per_100g: dict[str, float]
    original_total_nutrients_per_serving: dict[str, float]
    simulated_total_nutrients_per_serving: dict[str, float]
    nutrient_delta: NutrientDelta
    simulated_nutri_score_breakdown: dict[str, Any]
    original_co2e_per_serving_kg: Optional[float] = None
    simulated_co2e_per_serving_kg: Optional[float] = None
    co2e_reduction_per_serving_kg: Optional[float] = None
