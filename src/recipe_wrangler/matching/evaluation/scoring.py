"""Database-independent scoring for reviewed matching benchmarks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from recipe_wrangler.matching.evaluation.benchmark import BenchmarkItem


@dataclass(frozen=True)
class BenchmarkPrediction:
    """One selected composition record, or an explicit abstention."""

    item_id: str
    source_id: str | None = None
    record_id: str | None = None

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id is required")
        if (self.source_id is None) != (self.record_id is None):
            raise ValueError("source_id and record_id must both be set or both be null")

    @property
    def abstained(self) -> bool:
        return self.source_id is None


@dataclass(frozen=True)
class EvaluationReport:
    item_count: int
    accepted_target_count: int
    exact_target_count: int
    correct_abstention_count: int
    accuracy: float
    abstention_precision: float
    abstention_recall: float
    abstention_f1: float
    risk_category_accuracy: dict[str, float]


def evaluate_predictions(
    items: Iterable[BenchmarkItem],
    predictions: Iterable[BenchmarkPrediction],
) -> EvaluationReport:
    """Score one prediction per benchmark item against all accepted targets."""
    benchmark = tuple(items)
    expected_ids = {item.item_id for item in benchmark}
    if len(expected_ids) != len(benchmark):
        raise ValueError("benchmark item_id values must be unique")

    by_id: dict[str, BenchmarkPrediction] = {}
    for prediction in predictions:
        if prediction.item_id in by_id:
            raise ValueError(f"duplicate prediction for {prediction.item_id!r}")
        by_id[prediction.item_id] = prediction

    missing = expected_ids - by_id.keys()
    unexpected = by_id.keys() - expected_ids
    if missing or unexpected:
        raise ValueError(
            f"prediction IDs do not match benchmark; missing={sorted(missing)!r}, "
            f"unexpected={sorted(unexpected)!r}"
        )

    accepted = exact = correct_abstentions = 0
    predicted_abstentions = gold_abstentions = 0
    risk_results: dict[str, list[bool]] = defaultdict(list)

    for item in benchmark:
        prediction = by_id[item.item_id]
        if prediction.abstained:
            predicted_abstentions += 1
            correct = item.abstain_is_valid
            if correct:
                correct_abstentions += 1
        else:
            target = next(
                (
                    target
                    for target in item.acceptable_targets
                    if target.source_id == prediction.source_id
                    and target.record_id == prediction.record_id
                ),
                None,
            )
            correct = target is not None
            if target is not None:
                accepted += 1
                exact += target.relation == "exact"

        gold_abstentions += item.abstain_is_valid
        for risk in item.risk_categories:
            risk_results[risk].append(correct)

    correct_count = accepted + correct_abstentions
    false_abstentions = predicted_abstentions - correct_abstentions
    missed_abstentions = gold_abstentions - correct_abstentions
    precision = _safe_ratio(correct_abstentions, correct_abstentions + false_abstentions)
    recall = _safe_ratio(correct_abstentions, correct_abstentions + missed_abstentions)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    return EvaluationReport(
        item_count=len(benchmark),
        accepted_target_count=accepted,
        exact_target_count=exact,
        correct_abstention_count=correct_abstentions,
        accuracy=_safe_ratio(correct_count, len(benchmark)),
        abstention_precision=precision,
        abstention_recall=recall,
        abstention_f1=f1,
        risk_category_accuracy={
            risk: _safe_ratio(sum(results), len(results))
            for risk, results in sorted(risk_results.items())
        },
    )


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0
