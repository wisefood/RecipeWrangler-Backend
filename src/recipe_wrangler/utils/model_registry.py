"""Groq model ids the provider has retired, and what to use instead.

Every LLM-backed tool here reaches for a model id through an env var with a
hardcoded fallback. When Groq shut down `llama-3.1-8b-instant` and
`llama-3.3-70b-versatile` on 2026-08-16, six of those fallbacks went dead at
once, and nothing in this codebase noticed: the calls simply started failing
at the provider with a 404. The visible symptom was
`POST /api/v1/recipes/profile` returning 503 "Profiling pipeline request
failed." on every request, which reads as an outage rather than as a
configuration problem.

The failure was worse where a caller swallowed it. `rerank_with_llm` catches
every exception and falls back to deterministic ranking, so the nutrition-aware
substitution judge stopped running without ever surfacing an error — the
substitutions just got worse.

`resolve()` substitutes a retired id rather than honouring it, because a
retired id cannot succeed: passing it through only converts a recoverable
misconfiguration into a hard failure. The warning names the shutdown date and
the replacement, in the log of the process that would otherwise have failed.

FoodChat and FoodScholar carry the same table in their own `model_profiles`
modules. Keep the three in step when a provider retires an id.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: Retired id -> (shutdown date, replacement this project should use).
RETIRED: dict[str, tuple[str, str]] = {
    # `ParsedRecipe` demands seven fields with min_length=1 and the 8b model
    # routinely omitted `directions`, so the parser's replacement is the 20b
    # model rather than the one Groq names generically.
    "llama-3.1-8b-instant": ("2026-08-16", "openai/gpt-oss-20b"),
    "llama-3.3-70b-versatile": ("2026-08-16", "openai/gpt-oss-20b"),
    # Not part of the 2026-08-16 batch — found dead separately, while checking
    # every id this service still names. It backed SEARCH_MAIN_MODEL, so
    # LLM-backed recipe search had been failing on its own schedule.
    "meta-llama/llama-4-scout-17b-16e-instruct": ("unknown", "openai/gpt-oss-20b"),
}

_warned: set[str] = set()


def resolve(model: Optional[str]) -> Optional[str]:
    """Return ``model``, or its replacement if the provider has retired it."""
    if not model:
        return model

    name = model.strip()
    entry = RETIRED.get(name.lower())
    if entry is None:
        return name

    shutdown, replacement = entry
    if name.lower() not in _warned:
        _warned.add(name.lower())
        when = f" on {shutdown}" if shutdown != "unknown" else ""
        logger.warning(
            "Model '%s' was withdrawn by the provider%s and cannot serve "
            "requests; using '%s' instead. Set the relevant *_LLM / *_MODEL "
            "environment variable to choose a different replacement.",
            name, when, replacement,
        )
    return replacement


def from_env(*names: str, default: str) -> str:
    """First non-empty value among ``names``, else ``default``, resolved.

    Reading the variables here rather than at each call site keeps the
    retirement check on the path every model id travels — a fallback that
    bypassed it is exactly how the dead ids survived.
    """
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return resolve(value)
    return resolve(default)
