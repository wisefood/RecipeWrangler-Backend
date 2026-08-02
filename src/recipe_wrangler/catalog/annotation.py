"""Recipe annotation — the shared core.

Lives in the package rather than in ``scripts/`` because two callers need it:

- ``scripts/catalog/annotate_recipes.py``, annotating the stored corpus in bulk;
- the API, suggesting annotations for a recipe *being created*, before anything
  is persisted, so a person can accept or correct them.

The second case is why suggestions are returned rather than applied. A value a
user confirmed is better evidence than a value a model produced, and the
distinction is recorded: confirmed annotations carry ``method="user_confirmed"``
in ``annotation_evidence``, model ones carry ``method="model"``. Both are
constrained to the same closed vocabulary — confirmation lets a user pick a
different valid value, not invent one.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable

from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog import vocabularies as V

logger = logging.getLogger(__name__)

MODEL_FACETS: tuple[str, ...] = ("course_types", "cuisines", "flavor_profiles", "moods")
DERIVED_FACETS: tuple[str, ...] = ("food_groups",)

DEFAULT_MODEL = os.getenv("ANNOTATION_MODEL", "llama-3.3-70b-versatile")


def vocabulary_block() -> str:
    """The closed vocabularies the model must choose from."""
    lines: list[str] = []
    for facet in MODEL_FACETS:
        if facet == "course_types":
            values, description = S.COURSE_TYPES, "The course the dish is served as."
        else:
            spec = V.FACETS[facet]
            values, description = spec["values"], spec["description"]
        lines.append(f"{facet} — {description}")
        lines.append(f"  allowed values: {', '.join(values)}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You classify recipes into a fixed set of facets.

Rules, in order of importance:
1. Use ONLY values from the allowed lists below. Never invent a value, never
   return a variant spelling, never combine two values with a slash.
2. If the evidence does not clearly support a value, return an empty list for
   that facet. An empty answer is correct and useful; a guess is not.
3. Judge the dish as a whole, from its title and ingredients. Do not infer a
   cuisine from a single ingredient — olive oil does not make a dish Italian,
   and soy sauce does not make it Chinese.
4. Assign at most 2 cuisines, 4 flavor_profiles, 2 moods and 2 course_types.
5. confidence is your own 0-1 estimate that the whole assignment is right.

{vocabulary_block()}

Respond with JSON only, no prose:
{{"course_types": [], "cuisines": [], "flavor_profiles": [], "moods": [],
  "confidence": 0.0}}"""


def build_user_prompt(
    *,
    title: str,
    ingredients: Iterable[str] = (),
    description: str | None = None,
    tags: Iterable[str] = (),
    source: str | None = None,
    existing_course_types: Iterable[str] = (),
    trust_existing_course: bool = True,
) -> str:
    """The per-recipe half of the prompt.

    ``trust_existing_course=False`` omits the stored course type deliberately:
    stating it anchors the model to the value being corrected — a chocolate
    brownie came back as "main-dish" purely because the prompt said it was one.
    """
    parts = [f"Title: {title}"]

    names = [str(i).strip() for i in ingredients if str(i or "").strip()]
    if names:
        parts.append(f"Ingredients: {', '.join(names[:40])}")
    if description:
        parts.append(f"Description: {str(description)[:400]}")

    existing = [c for c in existing_course_types if c]
    if existing and trust_existing_course:
        parts.append(f"Course type already known (do not change): {', '.join(existing)}")

    prior = V.SOURCE_CUISINE_PRIOR.get(str(source or ""))
    if prior:
        parts.append(
            f"Source hint: this recipe comes from a {prior} collection. Treat as"
            " a prior, not a certainty."
        )

    clean_tags = [t for t in tags if t and not str(t).startswith(("source:", "type:"))]
    if clean_tags:
        parts.append(f"Existing tags: {', '.join(map(str, clean_tags[:15]))}")

    return "\n".join(parts)


def call_model(
    user_prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    attempts: int = 4,
) -> dict[str, Any]:
    """Ask Groq for one recipe's annotation, retrying on transient failures.

    Concurrency pushes Groq into rate-limiting (429) and the occasional
    truncated response; without retries those became permanent losses — a run
    at 8 workers silently dropped 569 of 7,221 recipes.
    """
    from langchain_groq import ChatGroq

    llm = ChatGroq(model=model, temperature=temperature)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = llm.invoke([("system", SYSTEM_PROMPT), ("human", user_prompt)])
            text = str(getattr(response, "content", response)).strip()
            if text.startswith("```"):
                text = text.split("```")[1].removeprefix("json").strip()
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == attempts - 1:
                break
            time.sleep(2**attempt)
    raise last if last else RuntimeError("model call failed")


def validate_facets(
    raw: dict[str, Any], *, facets: Iterable[str] = MODEL_FACETS
) -> tuple[dict[str, list[str]], float | None]:
    """Keep only in-vocabulary values; return them with the model's confidence.

    Anything outside the vocabulary is discarded rather than coerced — that is
    what keeps the facet browsable.
    """
    confidence = raw.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = None

    out: dict[str, list[str]] = {}
    for facet in facets:
        proposed = raw.get(facet) or []
        values = (
            S.canonical_course_types(proposed)
            if facet == "course_types"
            else V.validate_values(facet, proposed)
        )
        if values:
            out[facet] = values
    return out, confidence


def suggest(
    *,
    title: str,
    ingredients: Iterable[str] = (),
    description: str | None = None,
    tags: Iterable[str] = (),
    source: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
) -> tuple[dict[str, list[str]], float | None]:
    """Annotate an unsaved draft. Returns (facet values, confidence)."""
    prompt = build_user_prompt(
        title=title,
        ingredients=ingredients,
        description=description,
        tags=tags,
        source=source,
        trust_existing_course=False,
    )
    return validate_facets(call_model(prompt, model=model, temperature=temperature))



def derive_food_groups(doc: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Food groups from FoodOn ancestry — deterministic, no model.

    Lives here rather than in the annotation script because the commit path
    needs it too: a recipe created through the API is annotated at write time,
    and a derivation that only ran in an offline backfill left every new recipe
    without food groups until someone remembered to run it.

    Each evidence entry records the exact ontology classes that produced the
    group, so any assignment can be traced back to the ingredient that caused
    it rather than taken on trust. That traceability is why this facet is
    derived and never model-assigned: a guessed food group is indistinguishable
    from a known one once it is in the index.

    Returns empty when the ingredients carry no FoodOn ancestry — which is the
    normal state for a just-created recipe, whose ingredients have not been
    linked into the taxonomy yet. An empty result is a gap to be filled later,
    not a classification of "no food groups".
    """
    ancestors = doc.get("ingredient_class_ancestors") or []
    groups = V.food_groups_from_foodon(ancestors)

    by_group: dict[str, list[str]] = {}
    for raw in ancestors:
        normalized = V._normalize_foodon_id(raw)
        group = V.FOODON_ID_TO_FOOD_GROUP.get(normalized)
        if group:
            by_group.setdefault(group, []).append(normalized)

    evidence = [
        V.evidence_entry(
            "food_groups",
            group,
            method="foodon_derived",
            confidence=1.0,
            sources=["foodon"],
            foodon_ids=sorted(set(by_group.get(group, []))),
        )
        for group in groups
    ]
    return groups, evidence

def evidence_for(
    values: dict[str, list[str]],
    *,
    method: str,
    confidence: float | None = None,
) -> list[dict[str, Any]]:
    """Provenance records for a set of accepted facet values.

    ``method`` distinguishes how a value was arrived at — ``"model"`` for an
    unreviewed suggestion, ``"user_confirmed"`` where a person accepted or
    chose it. The second is stronger evidence and later passes must not
    overwrite it.
    """
    return [
        V.evidence_entry(facet, value, method=method, confidence=confidence)
        for facet, vals in values.items()
        for value in vals
    ]
