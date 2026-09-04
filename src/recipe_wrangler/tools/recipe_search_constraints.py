"""Natural-language constraint extraction for recipe search.

This module deliberately has no Neo4j dependency. Elasticsearch recipe search
uses it to turn a user question into deterministic search constraints.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field


EXTRACT_CONSTRAINTS_SYSTEM_PROMPT = (
    "You classify recipe-search intent and extract structured constraints from "
    "a user question. "
    "Return only fields from the provided schema. "
    "Do not invent constraints that are not stated or strongly implied."
)

EXTRACT_CONSTRAINTS_HUMAN_PROMPT = """Classify the search intent and extract recipe constraints.

Use the schema to align with available recipe-search concepts.

Schema:
{schema}

Rules:
- Use search_intent="title" when the question is just the name of a specific
  dish or recipe, such as "chicken tikka masala".
- Use search_intent="constraints" for descriptive requests, such as "a recipe
  with chicken" or "quick vegan dinners".
- Use search_intent="title_with_constraints" when a named dish also has explicit
  requirements, such as "chicken tikka masala under 30 minutes".
- For title or title_with_constraints, put only the named dish in title_query.
  Otherwise set title_query=null.
- Put requested ingredients into preferred_ingredients. Use the singular
  canonical ingredient name whenever grammatically valid (for example,
  "eggs" becomes "egg" and "tomatoes" becomes "tomato").
- Put ingredients to avoid into excluded_ingredients.
- Put an item into allergens only when the user explicitly asks to avoid it,
  such as "without peanut", "peanut-free", or "allergic to peanut".
  Never treat a requested ingredient as an allergen exclusion merely because
  that ingredient can be an allergen.
- Never put the same canonical food in both preferred_ingredients and
  allergens. For example, "recipes with peanut" means
  preferred_ingredients=["peanut"] and allergens=[], while "peanut-free
  recipes" means preferred_ingredients=[] and allergens=["peanut"].
- Put dietary intents (vegan, keto, gluten free, etc.) into diet.
- Use max_duration_minutes only when a max/prep/cook time limit is explicitly asked.
- Use min_servings only when a lower-bound serving size is explicitly asked.
- If the question is not about recipe retrieval, set unsupported_intent=true and explain why in unsupported_reason.
- Keep limit in [1, 100]. Use 50 when unspecified.
- Return lowercase string values where reasonable.

Question:
{question}
"""

EXTRACT_CONSTRAINTS_JSON_SYSTEM_PROMPT = (
    "You classify recipe-search intent and extract structured constraints from "
    "a user question. "
    "Return one valid JSON object only, no markdown and no extra text."
)

EXTRACT_CONSTRAINTS_JSON_HUMAN_PROMPT = """Extract recipe constraints and return JSON only.

Schema:
{schema}

Question:
{question}

Classify a bare named dish as "title", a descriptive recipe request as
"constraints", and a named dish with explicit requirements as
"title_with_constraints". Put only the named dish in title_query.

Use singular canonical ingredient names whenever grammatically valid. The
allergens array contains only items the user explicitly asks to avoid. Do not
infer an allergen exclusion from a positive ingredient request, and never put
the same canonical food in preferred_ingredients and allergens.

Return exactly one JSON object with these keys:
- search_intent: "title"|"constraints"|"title_with_constraints"
- title_query: string|null
- preferred_ingredients: string[]
- excluded_ingredients: string[]
- allergens: string[]
- diet: string[]
- title_keywords: string[]
- max_duration_minutes: integer|null
- min_servings: integer|null
- limit: integer
- unsupported_intent: boolean
- unsupported_reason: string|null
"""

EXTRACT_CONSTRAINTS_SCHEMA_CONTEXT = """Recipe search fields:
Recipe {title: STRING, duration: FLOAT, serves: FLOAT, ingredients: TEXT, allergens: KEYWORD, tags: KEYWORD, dish_types: KEYWORD}

Allowed diet tag values:
- dairy_free, gluten-free, high-protein, low-carb, low-fat, nut_free, vegan, vegetarian

Allowed dish type values:
- beverages, breakfast, desserts, main-dish, snacks

Allowed allergen values:
- celery, crustacean_shellfish, egg, fish, gluten, lupin, milk, molluscs,
  mustard, peanut, sesame, soy, sulphites, tree_nut, wheat
"""


class ExtractConstraintsOutput(BaseModel):
    search_intent: Literal[
        "title", "constraints", "title_with_constraints"
    ] = Field(
        description=(
            "Whether the user supplied a named recipe title, descriptive "
            "constraints, or a named title plus explicit constraints."
        )
    )
    title_query: str | None = Field(
        description=(
            "The named dish only for title/title_with_constraints; otherwise null."
        )
    )
    preferred_ingredients: list[str] = Field(
        default_factory=list,
        description=(
            "Requested ingredients, using singular canonical names whenever "
            "grammatically valid."
        ),
    )
    excluded_ingredients: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(
        default_factory=list,
        description=(
            "Only allergens the user explicitly asked to avoid; never allergens "
            "merely mentioned as requested ingredients."
        ),
    )
    diet: list[str] = Field(default_factory=list)
    title_keywords: list[str] = Field(default_factory=list)
    max_duration_minutes: int | None = None
    min_servings: int | None = None
    limit: int = 50
    unsupported_intent: bool = False
    unsupported_reason: str | None = None


def _empty_constraints() -> dict[str, Any]:
    return ExtractConstraintsOutput(
        search_intent="constraints",
        title_query=None,
    ).model_dump()


def _constraint_key(value: object) -> str:
    """Normalize an extracted food phrase for include/exclude comparison."""

    words = re.findall(
        r"[a-z0-9]+",
        str(value or "").strip().casefold().replace("_", " "),
    )
    if not words:
        return ""
    last = words[-1]
    if len(last) > 3 and last.endswith("ies"):
        words[-1] = f"{last[:-3]}y"
    elif len(last) > 3 and last.endswith(("ches", "shes", "xes", "zes")):
        words[-1] = last[:-2]
    elif (
        len(last) > 3
        and last.endswith("s")
        and not last.endswith(("ss", "us", "is"))
    ):
        words[-1] = last[:-1]
    return " ".join(words)


def _food_phrase_pattern(value: object) -> str:
    """Build a phrase pattern accepting a regular singular/plural ending."""

    words = re.findall(
        r"[a-z0-9]+",
        str(value or "").strip().casefold().replace("_", " "),
    )
    if not words:
        return r"(?!x)x"
    last = words[-1]
    singular = _constraint_key(last)
    variants = {last, singular}
    if singular.endswith("y") and len(singular) > 2:
        variants.add(f"{singular[:-1]}ies")
    elif singular.endswith(("ch", "sh", "x", "z")):
        variants.add(f"{singular}es")
    elif not singular.endswith("s"):
        variants.add(f"{singular}s")
    last_pattern = "(?:" + "|".join(
        sorted((re.escape(item) for item in variants), key=len, reverse=True)
    ) + ")"
    return r"[\s_-]+".join(
        [*(re.escape(word) for word in words[:-1]), last_pattern]
    )


def _question_explicitly_excludes(question: str, food: object) -> bool:
    """Return whether the original question explicitly avoids ``food``."""

    food_pattern = _food_phrase_pattern(food)
    left_cues = (
        r"(?:without|avoid(?:ing)?|exclude(?:d|ing)?|no|"
        r"free[\s-]+(?:from|of)|allergic[\s-]+to|allergy[\s-]+to)"
    )
    left_pattern = (
        rf"\b{left_cues}\s+(?:any\s+)?(?:{food_pattern})\b"
    )
    right_pattern = (
        rf"\b(?:{food_pattern})(?:[\s-]+free|\s+allerg(?:y|ic))\b"
    )
    normalized_question = str(question or "").casefold()
    return bool(
        re.search(left_pattern, normalized_question)
        or re.search(right_pattern, normalized_question)
    )


def resolve_ingredient_allergen_conflicts(
    question: str,
    requested_ingredients: list[str],
    inferred_allergens: list[str],
    explicit_allergens: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Remove contradictory ingredient/allergen filters before search.

    Explicit request-payload exclusions always win. For contradictions created
    by natural-language extraction, an explicit avoidance phrase keeps the
    exclusion; otherwise the positive ingredient request wins.
    """

    requested = list(dict.fromkeys(requested_ingredients or []))
    inferred = list(dict.fromkeys(inferred_allergens or []))
    explicit = list(dict.fromkeys(explicit_allergens or []))
    allergens = list(dict.fromkeys([*inferred, *explicit]))
    explicit_keys = {
        _constraint_key(item) for item in explicit if _constraint_key(item)
    }
    requested_by_key = {
        _constraint_key(item): item
        for item in requested
        if _constraint_key(item)
    }
    allergens_by_key = {
        _constraint_key(item): item
        for item in allergens
        if _constraint_key(item)
    }

    for key in requested_by_key.keys() & allergens_by_key.keys():
        requested_item = requested_by_key[key]
        if (
            key in explicit_keys
            or _question_explicitly_excludes(question, requested_item)
        ):
            requested = [
                item for item in requested if _constraint_key(item) != key
            ]
        else:
            allergens = [
                item for item in allergens if _constraint_key(item) != key
            ]

    return requested, allergens


@dataclass
class RecipeConstraintExtractor:
    """Extract recipe-search constraints without initializing Neo4j."""

    model: str = "llama-3.1-8b-instant"
    temperature: float = 0.0
    source: str = "groq"
    structured_output_method: str = "function_calling"

    def __post_init__(self) -> None:
        source = self.source.strip().lower()
        if source == "openrouter":
            from langchain_openai import ChatOpenAI

            self.llm = ChatOpenAI(
                model=self.model,
                temperature=self.temperature,
                max_retries=2,
                base_url=os.getenv(
                    "OPENROUTER_BASE_URL",
                    "https://openrouter.ai/api/v1",
                ),
                api_key=os.getenv("OPENROUTER_API_KEY", ""),
                callbacks=self._usage_callbacks(source),
            )
        elif source == "groq":
            from langchain_groq import ChatGroq

            self.llm = ChatGroq(
                model=self.model,
                temperature=self.temperature,
                max_retries=2,
                callbacks=self._usage_callbacks(source),
            )
        else:
            raise ValueError("source must be 'groq' or 'openrouter'")
        self._structured_extraction_enabled = True
        self._build_chains()

    @staticmethod
    def _usage_callbacks(source: str) -> list:
        """Report what this extractor costs.

        It runs on every natural-language recipe search, and it has a silent
        fallback chain — so without this, "search got slow and expensive" and
        "the structured-output chain is failing over on every call" look
        identical from the outside.
        """
        try:
            from recipe_wrangler.api.activity import usage_callback

            handler = usage_callback("recipe_constraint_extraction", provider=source)
            return [handler] if handler else []
        except Exception:  # pragma: no cover - never block client construction
            return []

    def _build_chains(self) -> None:
        extract_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", EXTRACT_CONSTRAINTS_SYSTEM_PROMPT),
                ("human", EXTRACT_CONSTRAINTS_HUMAN_PROMPT),
            ]
        )
        self.extract_constraints_chain = extract_prompt | self.llm.with_structured_output(
            ExtractConstraintsOutput,
            method=self.structured_output_method,
        )
        extract_json_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", EXTRACT_CONSTRAINTS_JSON_SYSTEM_PROMPT),
                ("human", EXTRACT_CONSTRAINTS_JSON_HUMAN_PROMPT),
            ]
        )
        self.extract_constraints_json_chain = extract_json_prompt | self.llm | StrOutputParser()

    def run_extract_constraints(self, question: str) -> dict[str, Any]:
        return self.extract_state({"question": question})

    @staticmethod
    def _clamp_limit(value: Any, default: int = 50) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            limit = default
        return max(1, min(100, limit))

    @staticmethod
    def _looks_empty_constraints(data: dict[str, Any]) -> bool:
        if data.get("unsupported_intent"):
            return False
        if data.get("search_intent") != "constraints" and data.get("title_query"):
            return False
        list_keys = [
            "preferred_ingredients",
            "excluded_ingredients",
            "allergens",
            "diet",
            "title_keywords",
        ]
        has_list_value = any(data.get(key) for key in list_keys)
        has_numeric_value = (
            data.get("max_duration_minutes") is not None
            or data.get("min_servings") is not None
        )
        return not has_list_value and not has_numeric_value

    @staticmethod
    def _heuristic_extract_constraints(question: str) -> dict[str, Any]:
        query = str(question or "")
        normalized_query = query.casefold()
        output = _empty_constraints()

        diet_keywords = [
            "vegan",
            "vegetarian",
            "keto",
            "paleo",
            "gluten free",
            "gluten-free",
            "dairy free",
            "dairy-free",
            "low carb",
            "high protein",
        ]
        output["diet"] = sorted(
            {
                diet.replace("-", " ")
                for diet in diet_keywords
                if diet in normalized_query
            }
        )

        time_match = re.search(
            r"\b(?:under|less than|max(?:imum)?|within)\s+(\d{1,3})\s*"
            r"(?:minutes|minute|mins|min)\b",
            normalized_query,
        )
        if time_match:
            output["max_duration_minutes"] = int(time_match.group(1))

        serves_match = re.search(
            r"\bfor\s+(\d{1,2})\s*(?:people|persons|servings|serves)\b",
            normalized_query,
        )
        if serves_match:
            output["min_servings"] = int(serves_match.group(1))

        include_matches = re.findall(
            r"\b(?:with|using|containing|contains)\s+([a-z][a-z\s-]{1,60}?)"
            r"(?=\b(?:under|less than|within|for|without|excluding|that|which|recipe|recipes)\b|$)",
            normalized_query,
        )
        include_matches += re.findall(
            r"\b(?:a|an)\s+([a-z][a-z\s-]{1,60}?)\s+recipes?\b",
            normalized_query,
        )
        exclude_matches = re.findall(
            r"\b(?:without|excluding|exclude|no)\s+([a-z][a-z\s-]{1,60}?)"
            r"(?=\b(?:under|less than|within|for|with|that|which|recipe|recipes)\b|$)",
            normalized_query,
        )

        def split_items(chunks: list[str]) -> list[str]:
            items: list[str] = []
            for chunk in chunks:
                for part in re.split(r"\s*(?:,| and )\s*", chunk):
                    token = part.strip(" .,!?:;-'\"")
                    if token:
                        items.append(token)
            return items

        output["preferred_ingredients"] = sorted(set(split_items(include_matches)))
        output["excluded_ingredients"] = sorted(set(split_items(exclude_matches)))
        return output

    @staticmethod
    def _is_unsupported_response_format_error(exc: Exception) -> bool:
        message = str(exc).casefold()
        return (
            "response format" in message
            or "response_format" in message
            or "json_schema" in message
        )

    @staticmethod
    def _parse_constraints_json_text(raw_text: str) -> dict[str, Any]:
        candidates = [str(raw_text or "").strip()]
        text = candidates[0]
        if text:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidates.insert(0, text[start : end + 1])

        for candidate in candidates:
            if not candidate:
                continue
            try:
                payload = json.loads(candidate)
                return ExtractConstraintsOutput.model_validate(payload).model_dump()
            except Exception:
                continue
        return _empty_constraints()

    def _extract_constraints_with_json_text(
        self,
        question: str,
        schema_text: str,
    ) -> dict[str, Any]:
        try:
            raw = self.extract_constraints_json_chain.invoke(
                {"question": question, "schema": schema_text}
            )
        except Exception:
            return _empty_constraints()
        return self._parse_constraints_json_text(raw)

    def extract_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Return the state fragment consumed by both recipe-search backends."""

        schema_text = EXTRACT_CONSTRAINTS_SCHEMA_CONTEXT
        question = str(state.get("question") or "")
        constraints = _empty_constraints()

        if self._structured_extraction_enabled:
            try:
                extracted = self.extract_constraints_chain.invoke(
                    {"question": question, "schema": schema_text}
                )
                constraints = extracted.model_dump()
            except Exception as exc:
                if self._is_unsupported_response_format_error(exc):
                    self._structured_extraction_enabled = False
                constraints = self._extract_constraints_with_json_text(
                    question,
                    schema_text,
                )
        else:
            constraints = self._extract_constraints_with_json_text(
                question,
                schema_text,
            )

        if self._looks_empty_constraints(constraints):
            heuristics = self._heuristic_extract_constraints(question)
            for key in [
                "preferred_ingredients",
                "excluded_ingredients",
                "allergens",
                "diet",
                "title_keywords",
            ]:
                if not constraints.get(key) and heuristics.get(key):
                    constraints[key] = heuristics[key]
            if (
                constraints.get("max_duration_minutes") is None
                and heuristics.get("max_duration_minutes") is not None
            ):
                constraints["max_duration_minutes"] = heuristics[
                    "max_duration_minutes"
                ]
            if (
                constraints.get("min_servings") is None
                and heuristics.get("min_servings") is not None
            ):
                constraints["min_servings"] = heuristics["min_servings"]

        constraints["limit"] = self._clamp_limit(
            constraints.get("limit"),
            default=50,
        )
        if constraints.get("search_intent") in {"title", "title_with_constraints"}:
            title_query = str(constraints.get("title_query") or "").strip()
            constraints["title_query"] = title_query or question.strip() or None
        else:
            constraints["search_intent"] = "constraints"
            constraints["title_query"] = None
        return {
            "query_constraints": constraints,
            "exclude_allergens": state.get("exclude_allergens"),
            "steps": ["extract_constraints"],
        }
