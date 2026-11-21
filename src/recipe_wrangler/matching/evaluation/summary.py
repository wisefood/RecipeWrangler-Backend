"""Coverage checks for a reviewed ingredient-matching benchmark."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from recipe_wrangler.matching.evaluation.benchmark import BenchmarkItem


@dataclass(frozen=True)
class BenchmarkSummary:
    item_count: int
    abstain_count: int
    source_target_counts: dict[str, int]
    risk_category_counts: dict[str, int]


def summarize_benchmark(items: Iterable[BenchmarkItem]) -> BenchmarkSummary:
    """Return simple annotation-coverage counts without judging model quality."""
    collected = tuple(items)
    sources = Counter(
        target.source_id
        for item in collected
        for target in item.acceptable_targets
    )
    risks = Counter(risk for item in collected for risk in item.risk_categories)
    return BenchmarkSummary(
        item_count=len(collected),
        abstain_count=sum(item.abstain_is_valid for item in collected),
        source_target_counts=dict(sorted(sources.items())),
        risk_category_counts=dict(sorted(risks.items())),
    )
