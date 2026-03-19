"""Consistent dependency error mapping for API endpoints."""

from __future__ import annotations

from typing import Any

from recipe_wrangler.api.exceptions import (
    APIException,
    GatewayTimeoutError,
    ServiceUnavailableError,
)


def map_dependency_error(dependency: str, exc: Exception) -> APIException:
    """Map backend exceptions into stable APIException types/codes."""
    message = str(exc or "").strip()
    lowered = message.lower()

    if "timeout" in lowered or "timed out" in lowered:
        return GatewayTimeoutError(
            detail=f"{dependency} timeout.",
            extra={"title": "DependencyTimeout", "dependency": dependency},
        )

    if any(
        token in lowered
        for token in (
            "connection refused",
            "could not connect",
            "connection reset",
            "name or service not known",
            "temporary failure in name resolution",
            "service unavailable",
            "is unavailable",
            "failed to fetch",
            "failed to upsert",
            "operationalerror",
            "undefinedtable",
        )
    ):
        return ServiceUnavailableError(
            detail=f"{dependency} unavailable.",
            extra={"title": "DependencyUnavailable", "dependency": dependency},
        )

    return ServiceUnavailableError(
        detail=f"{dependency} request failed.",
        extra={"title": "DependencyError", "dependency": dependency},
    )
