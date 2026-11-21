"""Reusable ingredient-to-food-composition matching primitives."""

from recipe_wrangler.matching.models import (
    EquivalencePrediction,
    FoodCandidate,
    FoodFacets,
    FoodMention,
    MatchDecision,
    MatchProvenance,
    RecipeContext,
    RetrievalEvidence,
)

__all__ = [
    "FoodCandidate",
    "FoodFacets",
    "FoodMention",
    "EquivalencePrediction",
    "MatchDecision",
    "MatchProvenance",
    "RecipeContext",
    "RetrievalEvidence",
]
