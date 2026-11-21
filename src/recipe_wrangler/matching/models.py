"""Database-independent domain models for food matching."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


PreparationState = Literal["raw", "cooked", "unknown"]
SodiumLevel = Literal[
    "regular",
    "reduced",
    "low",
    "no_added_salt",
    "unsalted",
    "unknown",
]
FatLevel = Literal["fat_free", "skimmed", "semi_skimmed", "low", "reduced", "whole", "full_fat", "unknown"]
SugarLevel = Literal["sugar_free", "no_added_sugar", "low", "reduced", "unknown"]
GrainRefinement = Literal["wholegrain", "refined", "unknown"]
MatchStatus = Literal["exact", "acceptable", "parent", "abstain"]
EquivalenceStatus = Literal[
    "exact",
    "acceptable",
    "parent",
    "incompatible",
    "insufficient_information",
]

_PREPARATION_STATES = frozenset({"raw", "cooked", "unknown"})
_SODIUM_LEVELS = frozenset(
    {"regular", "reduced", "low", "no_added_salt", "unsalted", "unknown"}
)


@dataclass(frozen=True)
class FoodFacets:
    """Structured, nutritionally relevant properties of a food mention or row.

    ``None`` means that a facet was not stated or could not be established. It
    must not be interpreted as a default such as raw, fresh or regular-sodium.
    """

    identity: str | None = None
    biological_source: str | None = None
    food_part: str | None = None
    preparation_state: PreparationState | None = None
    processes: frozenset[str] = field(default_factory=frozenset)
    preservation_methods: frozenset[str] = field(default_factory=frozenset)
    physical_form: str | None = None
    colour: str | None = None
    sodium_level: SodiumLevel | None = None
    fat_level: FatLevel | None = None
    sugar_level: SugarLevel | None = None
    grain_refinement: GrainRefinement | None = None
    skin_status: str | None = None
    bone_status: str | None = None
    packing_medium: str | None = None
    gluten_free: bool | None = None
    compound: bool | None = None
    base_identity: str | None = None
    # Annotation fields only until extraction and comparison policies are validated.
    size: str | None = None
    size_target: Literal["food", "piece", "package", "unknown"] | None = None
    thickness: str | None = None
    variety: str | None = None
    ripeness: str | None = None
    cooking_extent: str | None = None
    texture: str | None = None
    particle_size: str | None = None
    drainage_status: str | None = None
    reconstitution_state: str | None = None
    meat_fat_description: str | None = None
    fat_content_description: str | None = None
    fortification: str | None = None
    seed_status: str | None = None
    derived_substance: str | None = None

    def __post_init__(self) -> None:
        if self.size_target not in {None, "food", "piece", "package", "unknown"}:
            raise ValueError(f"Unsupported size_target: {self.size_target!r}")
        if (
            self.preparation_state is not None
            and self.preparation_state not in _PREPARATION_STATES
        ):
            raise ValueError(
                f"Unsupported preparation_state: {self.preparation_state!r}"
            )
        if self.sodium_level is not None and self.sodium_level not in _SODIUM_LEVELS:
            raise ValueError(f"Unsupported sodium_level: {self.sodium_level!r}")
        if not isinstance(self.processes, frozenset):
            raise TypeError("processes must be a frozenset")
        if not isinstance(self.preservation_methods, frozenset):
            raise TypeError("preservation_methods must be a frozenset")


@dataclass(frozen=True)
class RecipeContext:
    """Optional context that can disambiguate an otherwise short mention."""

    title: str | None = None
    cuisine: str | None = None
    neighbouring_ingredients: tuple[str, ...] = ()
    amount_g: float | None = None
    unit: str | None = None
    ingredient_role: str | None = None
    optional: bool | None = None
    quantity_qualifier: str | None = None
    usage_note: str | None = None


@dataclass(frozen=True)
class FoodMention:
    """A food phrase to resolve, independent of its parser or caller."""

    text: str
    identity: str
    facets: FoodFacets
    context: RecipeContext | None = None
    target_region: str | None = None


@dataclass(frozen=True)
class RetrievalEvidence:
    """Scores and rank produced by a candidate source before final selection."""

    rank: int | None = None
    lexical_score: float | None = None
    dense_score: float | None = None
    fused_score: float | None = None
    queries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.rank is not None and self.rank < 1:
            raise ValueError("rank must be positive")


@dataclass(frozen=True)
class FoodCandidate:
    """A composition-table record exposed by a retrieval adapter."""

    record_id: str
    source_id: str
    label: str
    facets: FoodFacets
    retrieval: RetrievalEvidence
    annotation_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class EquivalencePrediction:
    """Whether a candidate can represent a mention before source selection."""

    status: EquivalenceStatus
    confidence: float | None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class MatchProvenance:
    """Reproducibility metadata for a matching decision."""

    matcher_version: str
    policy_id: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchDecision:
    """The final equivalence decision, before downstream nutrient calculation."""

    status: MatchStatus
    candidate: FoodCandidate | None
    confidence: float | None
    reasons: tuple[str, ...]
    provenance: MatchProvenance

    def __post_init__(self) -> None:
        if self.status == "abstain" and self.candidate is not None:
            raise ValueError("an abstention cannot select a candidate")
        if self.status != "abstain" and self.candidate is None:
            raise ValueError("a non-abstention decision requires a candidate")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
