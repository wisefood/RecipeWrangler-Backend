"""Models and metrics for matching evaluation."""

from recipe_wrangler.matching.evaluation.benchmark import (
    BenchmarkItem,
    BenchmarkTarget,
)
from recipe_wrangler.matching.evaluation.io import (
    read_benchmark_jsonl,
    write_benchmark_jsonl,
)
from recipe_wrangler.matching.evaluation.scoring import (
    BenchmarkPrediction,
    EvaluationReport,
    evaluate_predictions,
)
from recipe_wrangler.matching.evaluation.summary import (
    BenchmarkSummary,
    summarize_benchmark,
)

__all__ = [
    "BenchmarkItem",
    "BenchmarkPrediction",
    "BenchmarkSummary",
    "BenchmarkTarget",
    "EvaluationReport",
    "evaluate_predictions",
    "read_benchmark_jsonl",
    "summarize_benchmark",
    "write_benchmark_jsonl",
]
