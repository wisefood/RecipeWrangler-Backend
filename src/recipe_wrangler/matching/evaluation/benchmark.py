"""Annotation schema for multi-source ingredient-matching benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from recipe_wrangler.matching.models import FoodMention


TargetRelation = Literal["exact", "acceptable_equivalent", "acceptable_parent"]


@dataclass(frozen=True)
class BenchmarkTarget:
    """A reviewed composition record that is valid for one benchmark item."""

    source_id: str
    record_id: str
    relation: TargetRelation
    preference: int | None = None

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("source_id is required")
        if not self.record_id:
            raise ValueError("record_id is required")
        if self.preference is not None and self.preference < 1:
            raise ValueError("preference must be positive")


@dataclass(frozen=True)
class BenchmarkItem:
    """One adjudicated mention, supporting multiple valid composition records."""

    item_id: str
    mention: FoodMention
    acceptable_targets: tuple[BenchmarkTarget, ...]
    abstain_is_valid: bool
    risk_categories: frozenset[str]
    rationale: str

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id is required")
        if self.abstain_is_valid and self.acceptable_targets:
            raise ValueError("an abstention item cannot have acceptable targets")
        if not self.abstain_is_valid and not self.acceptable_targets:
            raise ValueError("a matchable item requires at least one acceptable target")
        if not isinstance(self.risk_categories, frozenset):
            raise TypeError("risk_categories must be a frozenset")
