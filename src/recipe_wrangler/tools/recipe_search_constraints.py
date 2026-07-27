"""Natural-language constraint extraction for recipe search.

This module deliberately has no Neo4j dependency. Elasticsearch recipe search
uses it to turn a user question into deterministic search constraints.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field


EXTRACT_CONSTRAINTS_SYSTEM_PROMPT = (
    "You extract structured recipe-search constraints from a user question. "
    "Return only fields from the provided schema. "
    "Do not invent constraints that are not stated or strongly implied."
)

EXTRACT_CONSTRAINTS_HUMAN_PROMPT = """Extract recipe constraints from the user question.

Use the schema to align with available recipe-search concepts.

Schema:
{schema}

Rules:
- Put requested ingredients into preferred_ingredients.
- Put ingredients to avoid into excluded_ingredients.
- Put allergen exclusions into allergens.
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
    "You extract structured recipe-search constraints from a user question. "
    "Return one valid JSON object only, no markdown and no extra text."
)

EXTRACT_CONSTRAINTS_JSON_HUMAN_PROMPT = """Extract recipe constraints and return JSON only.

Schema:
{schema}

Question:
{question}

Return exactly one JSON object with these keys:
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
    preferred_ingredients: list[str] = Field(default_factory=list)
    excluded_ingredients: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)
    diet: list[str] = Field(default_factory=list)
    title_keywords: list[str] = Field(default_factory=list)
    max_duration_minutes: int | None = None
    min_servings: int | None = None
    limit: int = 50
    unsupported_intent: bool = False
    unsupported_reason: str | None = None


@dataclass
class RecipeConstraintExtractor:
    """Extract recipe-search constraints without initializing Neo4j."""

    model: str = "llama-3.1-8b-instant"
    temperature: float = 0.0
    structured_output_method: str = "function_calling"

    def __post_init__(self) -> None:
        from langchain_groq import ChatGroq

        self.llm = ChatGroq(
            model=self.model,
            temperature=self.temperature,
            max_retries=2,
        )
        self._structured_extraction_enabled = True
        self._build_chains()

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
        output = ExtractConstraintsOutput().model_dump()

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
        return ExtractConstraintsOutput().model_dump()

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
            return ExtractConstraintsOutput().model_dump()
        return self._parse_constraints_json_text(raw)

    def extract_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Return the state fragment consumed by both recipe-search backends."""

        schema_text = EXTRACT_CONSTRAINTS_SCHEMA_CONTEXT
        question = str(state.get("question") or "")
        constraints = ExtractConstraintsOutput().model_dump()

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
        return {
            "query_constraints": constraints,
            "exclude_allergens": state.get("exclude_allergens"),
            "steps": ["extract_constraints"],
        }
