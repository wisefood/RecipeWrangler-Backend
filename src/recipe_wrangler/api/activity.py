"""Reporting what only this service knows.

The gateway records that a search happened, by whom, and how long the round
trip took. It cannot see the things that decide whether the search was any
good: the normalised constraints the extractor produced, whether the first pass
matched nothing, whether the title requirement had to be demoted to a ranking
signal. Those are reported from here.

A thin wrapper over the vendored ``wf_telemetry`` rather than calls to it
directly, so the handlers stay readable and the mapping from :class:`Caller` to
an analytics identity lives in one place.

Everything is a no-op unless ``ANALYTICS_ENABLED`` is true and an ingest secret
is configured. Nothing here raises.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional

from . import wf_telemetry
from .identity import Caller

logger = logging.getLogger(__name__)

APP = "recipewrangler"


def _identity(caller: Optional[Caller]) -> dict:
    return {"user_id": getattr(caller, "sub", None)}


def _elapsed_ms(started: Optional[float], latency_ms: Optional[float]) -> Optional[float]:
    if latency_ms is not None:
        return latency_ms
    if started is None:
        return None
    return (time.perf_counter() - started) * 1000.0


#: Filter keys worth reporting: what the *query* asked for. Everything else is
#: dropped, and the default is to drop.
#:
#: The list exists because `base_constraints` and the filter payload carry the
#: caller's own profile alongside the query — `exclude_allergens` is their
#: allergies, `boost_tags` their dietary groups, `boost_ingredients` the foods
#: they like. Reporting those keyed to a user id would put health data in an
#: analytics table, which is exactly what the platform's own tracing policy
#: forbids ("never allergies, dietary profiles"). `rank_query` is dropped too:
#: it duplicates the raw query, which has its own consent-gated column.
_REPORTABLE_FILTERS = frozenset({
    "dish_types",
    "cuisines",
    "moods",
    "flavor_profiles",
    "food_groups",
    "convenience",
    "nutrition_claims",
    "nutri_scores",
    "sources",
    "region",
    "max_duration_minutes",
    "sort_by",
    "search_intent",
    "title_query",
    "title_keywords",
    "require_diet_tags",
    "course_types",
    "fq",
    "sort",
    "rejected_options",
    # Why a search produced nothing, when it produced nothing because it broke.
    "error",
})


def reportable_filters(filters: Optional[Mapping[str, Any]]) -> dict:
    """The filters safe to record, allowlisted rather than blocklisted.

    Allowlisted on purpose: a blocklist means the next field somebody adds to
    the search payload is reported by default, and the fields being guarded
    against here are medical.
    """
    if not filters:
        return {}
    return {
        key: value
        for key, value in filters.items()
        if key in _REPORTABLE_FILTERS and value not in (None, [], "", {})
    }


def report_search(
    *,
    surface: str,
    raw_query: Optional[str],
    filters: Optional[Mapping[str, Any]] = None,
    first_pass: Optional[int] = None,
    final: Optional[int] = None,
    relaxed: bool = False,
    lexical_fallback: bool = False,
    started: Optional[float] = None,
    latency_ms: Optional[float] = None,
    caller: Optional[Caller] = None,
) -> None:
    """Report one search on any of this service's six search surfaces."""
    try:
        wf_telemetry.TELEMETRY.search(
            surface=surface,
            raw_query=raw_query or None,
            filters=reportable_filters(filters),
            result_count_first_pass=first_pass,
            result_count_final=final,
            relaxed=relaxed,
            lexical_fallback=lexical_fallback,
            latency_ms=_elapsed_ms(started, latency_ms),
            app=APP,
            **_identity(caller),
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("activity.report_search_failed", exc_info=True)


def report_llm_usage(
    *,
    model: Optional[str],
    feature: str,
    provider: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    started: Optional[float] = None,
    latency_ms: Optional[float] = None,
    caller: Optional[Caller] = None,
) -> None:
    """Report the cost of one model call.

    The constraint extractor runs on every natural-language search and has a
    silent fallback chain, so its usage is the difference between "search is
    slow" and "search is slow because the JSON chain keeps failing over".
    """
    try:
        wf_telemetry.TELEMETRY.llm_usage(
            model=model,
            feature=feature,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_ms=_elapsed_ms(started, latency_ms),
            app=APP,
            **_identity(caller),
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("activity.report_llm_usage_failed", exc_info=True)


def report_event(
    event_type: str,
    *,
    props: Optional[Mapping[str, Any]] = None,
    caller: Optional[Caller] = None,
) -> None:
    try:
        wf_telemetry.TELEMETRY.event(
            event_type, props=dict(props or {}), app=APP, **_identity(caller)
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("activity.report_event_failed", exc_info=True)


# --------------------------------------------------------------- LLM usage --
# Token extraction and the LangChain callback live in the vendored client, so
# all four services read the providers' differing shapes the same way.
usage_from_llm_result = wf_telemetry.usage_from_llm_result


def usage_callback(feature: str, *, provider: Optional[str] = None):
    """Report each model call's cost under this service's name.

    The constraint extractor runs on every natural-language search and has a
    silent fallback chain, so its usage is the difference between "search is
    slow" and "the structured-output chain is failing over on every call".
    """
    return wf_telemetry.usage_callback(feature, provider=provider, app=APP)
