# Purpose: Minimal constraint-first Text-to-Cypher LangGraph pipeline for recipe search.

import os
import sys
import json
from dataclasses import dataclass
from operator import add
from typing import Annotated, Any, List, Optional, TypedDict

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_neo4j import Neo4jGraph
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from recipe_wrangler.utils.user_preferences import get_user_preferences


EXTRACT_CONSTRAINTS_SYSTEM_PROMPT = (
    "You extract structured recipe-search constraints from a user question. "
    "Return only fields from the provided schema. "
    "Do not invent constraints that are not stated or strongly implied."
)

EXTRACT_CONSTRAINTS_HUMAN_PROMPT = """Extract recipe constraints from the user question.

Use the graph schema to align with available concepts (Recipe, Ingredient, Allergen, Tag, etc.).

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
- Keep limit in [1, 50]. Use 10 when unspecified.
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

EXTRACT_CONSTRAINTS_SCHEMA_CONTEXT = """Node properties:
Recipe {title: STRING, url: STRING, instructions: LIST, duration: FLOAT, serves: FLOAT, recipe_id: STRING, image_url: STRING}
Ingredient {canonical_id: STRING, name: STRING, miskg_id: STRING}
FoodOnClass {name: STRING, foodon_id: STRING, label: STRING}
FlavorDBIngredient {name: STRING, flavordb_id: INTEGER, is_hub: STRING}
FlavorDBCompound {name: STRING, flavordb_id: INTEGER, is_hub: STRING}
Allergen {name: STRING}
Tag {name: STRING, category: STRING}

Relationship properties:
HAS_INGREDIENT {measurement: STRING, unit: STRING}
HAS_SUBSTITUTION {ingredient: STRING, substitution: STRING, ingredient_original_id: STRING, substitution_original_id: STRING}
PAIRS_WITH {score: FLOAT}
HAS_FLAVOR_COMPOUND {score: FLOAT}
HAS_DRUG_COMPOUND {score: FLOAT}
FLAVORDB_EQUIVALENT {source: STRING, cosine_similarity: FLOAT, similarity: FLOAT, miskg_ingredient: STRING, flavordb_name: STRING}
HAS_ALLERGEN {sources: LIST, keyword_matches: LIST, foodon_ids: LIST, foodon_labels: LIST}

The relationships:
(:Recipe)-[:HAS_INGREDIENT]->(:Ingredient)
(:Recipe)-[:HAS_TAG]->(:Tag)
(:Ingredient)-[:HAS_CLASS]->(:FoodOnClass)
(:Ingredient)-[:FLAVORDB_EQUIVALENT]->(:FlavorDBIngredient)
(:Ingredient)-[:HAS_ALLERGEN]->(:Allergen)
(:Ingredient)-[:HAS_SUBSTITUTION]->(:Ingredient)
(:FoodOnClass)-[:SUBCLASS_OF]->(:FoodOnClass)
(:FlavorDBIngredient)-[:PAIRS_WITH]->(:FlavorDBIngredient)
(:FlavorDBIngredient)-[:HAS_FLAVOR_COMPOUND]->(:FlavorDBCompound)
(:FlavorDBIngredient)-[:HAS_DRUG_COMPOUND]->(:FlavorDBCompound)

Allowed Tag values:
- category=dietary: dairy_free, gluten-free, high-protein, low-carb, low-fat, nut_free, vegan, vegetarian
- category=dish-type: beverages, breakfast, desserts, main-dish, snacks
- category=simplicity: 5_ingredients_or_less
- category=time: 30_minutes_or_less

Allowed Allergen values:
- crustacean_shellfish, egg, fish, milk, peanut, sesame, soy, tree_nut, wheat
"""


class InputState(TypedDict):
    question: str
    exclude_allergens: Optional[List[str]]


class OverallState(TypedDict):
    question: str
    query_constraints: dict
    platform_preferences: dict
    constraints: dict
    cypher_statement: str
    results: List[dict] | str
    steps: Annotated[List[str], add]
    exclude_allergens: Optional[List[str]]


class OutputState(TypedDict):
    query_constraints: dict
    platform_preferences: dict
    constraints: dict
    cypher_statement: str
    results: List[dict] | str
    steps: List[str]


class ExtractConstraintsOutput(BaseModel):
    preferred_ingredients: List[str] = Field(default_factory=list)
    excluded_ingredients: List[str] = Field(default_factory=list)
    allergens: List[str] = Field(default_factory=list)
    diet: List[str] = Field(default_factory=list)
    title_keywords: List[str] = Field(default_factory=list)
    max_duration_minutes: Optional[int] = None
    min_servings: Optional[int] = None
    limit: int = 10
    unsupported_intent: bool = False
    unsupported_reason: Optional[str] = None


@dataclass
class RecipeSearchAppV2:
    neo4j_uri: str
    # Defaults are only used when this class is instantiated directly.
    # API wiring can override these via env settings.
    model: str = "llama-3.1-8b-instant"
    temperature: float = 0.0
    structured_output_method: str = "function_calling"

    def __post_init__(self):
        self.enhanced_graph = Neo4jGraph(
            url=self.neo4j_uri,
            refresh_schema=True,
            enhanced_schema=True,
        )

        from langchain_groq import ChatGroq

        self.llm = ChatGroq(
            model=self.model,
            temperature=self.temperature,
            max_retries=2,
        )
        self._structured_extraction_enabled = True
        self._build_chains()
        self.langgraph = self._build_state_graph().compile()

    def invoke(self, question: str, exclude_allergens: Optional[List[str]] = None) -> OutputState:
        return self.langgraph.invoke(
            {"question": question, "exclude_allergens": exclude_allergens}
        )

    def run_extract_constraints(self, question: str) -> OverallState:
        return self._extract_constraints({"question": question})

    def run_load_user_preferences(self) -> OverallState:
        return self._load_user_preferences({})

    def run_compose_cypher(
        self,
        question: str,
        query_constraints: dict,
        platform_preferences: Optional[dict] = None,
        exclude_allergens: Optional[List[str]] = None,
    ) -> OverallState:
        return self._compose_cypher(
            {
                "question": question,
                "query_constraints": query_constraints,
                "platform_preferences": platform_preferences or {},
                "exclude_allergens": exclude_allergens,
            }
        )

    def run_execute_cypher(self, cypher: str) -> OverallState:
        return self._execute_cypher({"cypher_statement": cypher})

    def save_graph_png(self, output_path: str = "recipe_langgraph_v2.png") -> None:
        try:
            png_bytes = self.langgraph.get_graph().draw_mermaid_png()
            with open(output_path, "wb") as f:
                f.write(png_bytes)
            print(f"Graph saved to: {output_path}")
        except Exception as e:
            print("Unable to render graph PNG with this environment:", repr(e))

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

    @staticmethod
    def _normalize_list(items: Optional[List[Any]]) -> List[str]:
        return [str(x).strip().casefold() for x in (items or []) if str(x).strip()]

    @staticmethod
    def _clamp_limit(value: Any, default: int = 10) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = default
        return max(1, min(50, n))

    def _get_recipe_property_key(self, *candidates: str) -> Optional[str]:
        node_props = (self.enhanced_graph.structured_schema or {}).get("node_props", {}).get("Recipe", [])
        lookup = {str(p.get("property", "")).casefold(): p.get("property") for p in node_props}
        for c in candidates:
            found = lookup.get(c.casefold())
            if found:
                return found
        return None

    @staticmethod
    def _format_property_access(variable: str, prop: Optional[str]) -> str:
        import re

        if not prop:
            return f"{variable}.title"
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", prop):
            return f"{variable}.{prop}"
        return f"{variable}.`{prop}`"

    @staticmethod
    def _looks_empty_constraints(data: dict) -> bool:
        if data.get("unsupported_intent"):
            return False
        list_keys = [
            "preferred_ingredients",
            "excluded_ingredients",
            "allergens",
            "diet",
            "title_keywords",
        ]
        has_any_list_value = any(data.get(k) for k in list_keys)
        has_any_numeric = data.get("max_duration_minutes") is not None or data.get("min_servings") is not None
        return not has_any_list_value and not has_any_numeric

    def _heuristic_extract_constraints(self, question: str) -> dict:
        import re

        q = str(question or "")
        ql = q.casefold()
        out = ExtractConstraintsOutput().model_dump()

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
        out["diet"] = sorted({d.replace("-", " ") for d in diet_keywords if d in ql})

        time_match = re.search(r"\b(?:under|less than|max(?:imum)?|within)\s+(\d{1,3})\s*(?:minutes|minute|mins|min)\b", ql)
        if time_match:
            out["max_duration_minutes"] = int(time_match.group(1))

        serves_match = re.search(r"\bfor\s+(\d{1,2})\s*(?:people|persons|servings|serves)\b", ql)
        if serves_match:
            out["min_servings"] = int(serves_match.group(1))

        include_matches = re.findall(
            r"\b(?:with|using|containing|contains)\s+([a-z][a-z\s-]{1,60}?)(?=\b(?:under|less than|within|for|without|excluding|that|which|recipe|recipes)\b|$)",
            ql,
        )
        include_matches += re.findall(
            r"\b(?:a|an)\s+([a-z][a-z\s-]{1,60}?)\s+recipes?\b",
            ql,
        )
        excludes = re.findall(
            r"\b(?:without|excluding|exclude|no)\s+([a-z][a-z\s-]{1,60}?)(?=\b(?:under|less than|within|for|with|that|which|recipe|recipes)\b|$)",
            ql,
        )

        def split_items(chunks: List[str]) -> List[str]:
            items: List[str] = []
            for chunk in chunks:
                for part in re.split(r"\s*(?:,| and )\s*", chunk):
                    token = part.strip(" .,!?:;-'\"")
                    if token:
                        items.append(token)
            return items

        out["preferred_ingredients"] = sorted(set(split_items(include_matches)))
        out["excluded_ingredients"] = sorted(set(split_items(excludes)))
        return out

    @staticmethod
    def _is_unsupported_response_format_error(exc: Exception) -> bool:
        msg = str(exc).casefold()
        return "response format" in msg or "response_format" in msg or "json_schema" in msg

    def _parse_constraints_json_text(self, raw_text: str) -> dict:
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
                obj = json.loads(candidate)
                return ExtractConstraintsOutput.model_validate(obj).model_dump()
            except Exception:
                continue
        return ExtractConstraintsOutput().model_dump()

    def _extract_constraints_with_json_text(self, question: str, schema_text: str) -> dict:
        try:
            raw = self.extract_constraints_json_chain.invoke(
                {
                    "question": question,
                    "schema": schema_text,
                }
            )
        except Exception:
            return ExtractConstraintsOutput().model_dump()
        return self._parse_constraints_json_text(raw)

    def _extract_constraints(self, state: InputState) -> OverallState:
        schema_text = EXTRACT_CONSTRAINTS_SCHEMA_CONTEXT
        question = state.get("question") or ""

        constraints = ExtractConstraintsOutput().model_dump()
        if self._structured_extraction_enabled:
            try:
                extracted = self.extract_constraints_chain.invoke(
                    {
                        "question": question,
                        "schema": schema_text,
                    }
                )
                constraints = extracted.model_dump()
            except Exception as exc:
                if self._is_unsupported_response_format_error(exc):
                    self._structured_extraction_enabled = False
                constraints = self._extract_constraints_with_json_text(question, schema_text)
        else:
            constraints = self._extract_constraints_with_json_text(question, schema_text)

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
            if constraints.get("max_duration_minutes") is None and heuristics.get("max_duration_minutes") is not None:
                constraints["max_duration_minutes"] = heuristics["max_duration_minutes"]
            if constraints.get("min_servings") is None and heuristics.get("min_servings") is not None:
                constraints["min_servings"] = heuristics["min_servings"]

        constraints["limit"] = self._clamp_limit(constraints.get("limit"), default=10)
        return {
            "query_constraints": constraints,
            "exclude_allergens": state.get("exclude_allergens"),
            "steps": ["extract_constraints"],
        }

    def _load_user_preferences(self, _: OverallState) -> OverallState:
        prefs = get_user_preferences() or {}
        return {
            "platform_preferences": prefs,
            "steps": ["load_user_preferences"],
        }

    def _merge_constraints(self, query_constraints: dict, platform_preferences: dict, exclude_allergens: Optional[List[str]]) -> dict:
        merged = dict(query_constraints or {})
        prefs = platform_preferences or {}

        merged["preferred_ingredients"] = sorted(
            set(
                self._normalize_list(query_constraints.get("preferred_ingredients"))
                + self._normalize_list(
                    prefs.get("preferred_ingredients") or prefs.get("prefered_ingredients")
                )
            )
        )
        merged["excluded_ingredients"] = sorted(set(self._normalize_list(query_constraints.get("excluded_ingredients"))))
        merged["allergens"] = sorted(
            set(
                self._normalize_list(query_constraints.get("allergens"))
                + self._normalize_list(exclude_allergens)
                + self._normalize_list(prefs.get("allergens"))
            )
        )
        merged["diet"] = sorted(
            set(self._normalize_list(query_constraints.get("diet")) + self._normalize_list(prefs.get("diet")))
        )
        merged["title_keywords"] = sorted(set(self._normalize_list(query_constraints.get("title_keywords"))))
        merged["limit"] = self._clamp_limit(query_constraints.get("limit"), default=10)
        return merged

    def _compose_cypher(self, state: OverallState) -> OverallState:
        import json

        query_constraints = dict(state.get("query_constraints") or {})
        platform_preferences = dict(state.get("platform_preferences") or {})
        constraints = self._merge_constraints(
            query_constraints=query_constraints,
            platform_preferences=platform_preferences,
            exclude_allergens=state.get("exclude_allergens"),
        )

        preferred_ingredients = constraints["preferred_ingredients"]
        excluded_ingredients = constraints["excluded_ingredients"]
        allergens = constraints["allergens"]
        diets = constraints["diet"]
        title_keywords = constraints["title_keywords"]
        max_duration = query_constraints.get("max_duration_minutes")
        min_servings = query_constraints.get("min_servings")
        limit = constraints["limit"]
        unsupported_intent = bool(query_constraints.get("unsupported_intent"))
        unsupported_reason = str(query_constraints.get("unsupported_reason") or "").strip()

        title_prop = self._get_recipe_property_key("title", "name")
        id_prop = self._get_recipe_property_key("id", "recipe_id")
        duration_prop = self._get_recipe_property_key("Duration", "duration", "total_minutes")
        serves_prop = self._get_recipe_property_key("Serves", "serves", "yield")
        title_access = self._format_property_access("r", title_prop)
        id_access = self._format_property_access("r", id_prop) if id_prop else "elementId(r)"
        duration_access = self._format_property_access("r", duration_prop) if duration_prop else None
        serves_access = self._format_property_access("r", serves_prop) if serves_prop else None

        if unsupported_intent:
            return {
                "query_constraints": query_constraints,
                "platform_preferences": platform_preferences,
                "constraints": constraints,
                "cypher_statement": "",
                "results": unsupported_reason or "This question is not about recipe retrieval.",
                "steps": ["compose_cypher"],
            }

        predicates: List[str] = []
        if preferred_ingredients:
            if len(preferred_ingredients) == 1:
                ing = json.dumps(preferred_ingredients[0])
                predicates.append(
                    "EXISTS { "
                    "MATCH (r)-[:HAS_INGREDIENT]->(i_pref:Ingredient) "
                    f"WHERE toLower(i_pref.name) CONTAINS {ing} "
                    "}"
                )
            else:
                preferred_list = json.dumps(preferred_ingredients)
                predicates.append(
                    "ALL(ing IN "
                    f"{preferred_list} "
                    "WHERE EXISTS { "
                    "MATCH (r)-[:HAS_INGREDIENT]->(i_pref:Ingredient) "
                    "WHERE toLower(i_pref.name) CONTAINS ing "
                    "})"
                )
        if excluded_ingredients:
            if len(excluded_ingredients) == 1:
                ing = json.dumps(excluded_ingredients[0])
                predicates.append(
                    "NOT EXISTS { "
                    "MATCH (r)-[:HAS_INGREDIENT]->(i_ex:Ingredient) "
                    f"WHERE toLower(i_ex.name) CONTAINS {ing} "
                    "}"
                )
            else:
                excluded_list = json.dumps(excluded_ingredients)
                predicates.append(
                    "ALL(ing IN "
                    f"{excluded_list} "
                    "WHERE NOT EXISTS { "
                    "MATCH (r)-[:HAS_INGREDIENT]->(i_ex:Ingredient) "
                    "WHERE toLower(i_ex.name) CONTAINS ing "
                    "})"
                )
        if allergens:
            if len(allergens) == 1:
                allergen = json.dumps(allergens[0])
                predicates.append(
                    "NOT EXISTS { "
                    "MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(al_pref:Allergen) "
                    f"WHERE toLower(al_pref.name) = {allergen} "
                    "}"
                )
            else:
                allergen_list = json.dumps(allergens)
                predicates.append(
                    "NOT EXISTS { "
                    "MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(al_pref:Allergen) "
                    f"WHERE toLower(al_pref.name) IN {allergen_list} "
                    "}"
                )
        if diets:
            if len(diets) == 1:
                diet = json.dumps(diets[0])
                predicates.append(
                    "EXISTS { "
                    "MATCH (r)-[:HAS_TAG]->(t_pref:Tag) "
                    f"WHERE toLower(t_pref.name) = {diet} "
                    "AND toLower(t_pref.category) = 'dietary' "
                    "}"
                )
            else:
                diet_list = json.dumps(diets)
                predicates.append(
                    "EXISTS { "
                    "MATCH (r)-[:HAS_TAG]->(t_pref:Tag) "
                    f"WHERE toLower(t_pref.name) IN {diet_list} "
                    "AND toLower(t_pref.category) = 'dietary' "
                    "}"
                )
        if duration_access and isinstance(max_duration, int) and max_duration > 0:
            predicates.append(f"{duration_access} <= {max_duration}")
        if serves_access and isinstance(min_servings, int) and min_servings > 0:
            predicates.append(f"{serves_access} >= {min_servings}")
        if title_prop and title_keywords:
            if len(title_keywords) == 1:
                word = json.dumps(title_keywords[0])
                predicates.append(f"toLower({title_access}) CONTAINS {word}")
            else:
                title_words = json.dumps(title_keywords)
                predicates.append(f"ALL(word IN {title_words} WHERE toLower({title_access}) CONTAINS word)")

        where_clause = f"WHERE {' AND '.join(predicates)}" if predicates else ""

        query_lines = ["MATCH (r:Recipe)"]
        if where_clause:
            query_lines.append(where_clause)
        query_lines.append(
            f"RETURN DISTINCT coalesce(toString({id_access}), toString(r.id), toString(r.recipe_id)) AS recipe_id, {title_access} AS title, coalesce(r.source, '') AS source"
            if title_prop
            else "RETURN DISTINCT coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id, coalesce(r.title, r.name) AS title, coalesce(r.source, '') AS source"
        )
        query_lines.append(
            "ORDER BY CASE WHEN toLower(source) = 'myplate' THEN 0 ELSE 1 END, title"
        )
        query_lines.append(f"LIMIT {limit}")
        cypher = "\n".join(query_lines)

        return {
            "query_constraints": query_constraints,
            "platform_preferences": platform_preferences,
            "constraints": constraints,
            "cypher_statement": cypher,
            "steps": ["compose_cypher"],
        }

    def _execute_cypher(self, state: OverallState) -> OverallState:
        cypher = str(state.get("cypher_statement") or "").strip()
        if not cypher:
            return {
                "results": state.get("results") or "No query to execute.",
                "cypher_statement": "",
                "steps": ["execute_cypher"],
            }
        records = self.enhanced_graph.query(cypher)
        no_results = "I couldn't find any relevant information in the database"
        return {
            "results": records if records else no_results,
            "cypher_statement": cypher,
            "steps": ["execute_cypher"],
        }

    def _build_state_graph(self) -> StateGraph:
        g = StateGraph(OverallState, input_schema=InputState, output_schema=OutputState)
        g.add_node("extract_constraints", self._extract_constraints)
        g.add_node("load_user_preferences", self._load_user_preferences)
        g.add_node("compose_cypher", self._compose_cypher)
        g.add_node("execute_cypher", self._execute_cypher)
        g.add_edge(START, "extract_constraints")
        g.add_edge("extract_constraints", "load_user_preferences")
        g.add_edge("load_user_preferences", "compose_cypher")
        g.add_edge("compose_cypher", "execute_cypher")
        g.add_edge("execute_cypher", END)
        return g


def _main(argv: list[str]) -> int:
    import argparse
    import json
    from pprint import pprint

    parser = argparse.ArgumentParser(description="Recipe LangGraph v2 runner")
    parser.add_argument("--question", "-q", type=str, help="Question to ask the graph or stage")
    parser.add_argument("--print-graph", "-p", action="store_true", help="Save the graph PNG")
    parser.add_argument("--graph-path", type=str, default="recipe_langgraph_v2.png", help="Output path for PNG")
    parser.add_argument(
        "--stage",
        choices=["extract_constraints", "load_user_preferences", "compose_cypher", "execute_cypher"],
        help="Run a specific stage instead of the full graph",
    )
    parser.add_argument("--constraints", type=str, help="JSON object for compose_cypher stage (query constraints)")
    parser.add_argument("--platform-preferences", type=str, help="JSON object for compose_cypher stage")
    parser.add_argument("--cypher", type=str, help="Cypher to use for execute_cypher stage")
    parser.add_argument(
        "--exclude-allergens",
        type=str,
        help="JSON array of allergens to exclude at runtime, e.g. '[\"peanut\"]'",
    )
    args = parser.parse_args(argv)

    if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
        print("Please set NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD in your environment.", file=sys.stderr)
        return 2

    app = RecipeSearchAppV2(neo4j_uri=NEO4J_URI)

    if args.print_graph:
        app.save_graph_png(args.graph_path)

    exclude_allergens = None
    if args.exclude_allergens:
        try:
            parsed = json.loads(args.exclude_allergens)
            exclude_allergens = parsed if isinstance(parsed, list) else None
        except Exception:
            exclude_allergens = None

    if args.stage:
        if args.stage == "extract_constraints":
            if not args.question:
                parser.error("--stage extract_constraints requires --question")
            pprint(app.run_extract_constraints(args.question))
        elif args.stage == "load_user_preferences":
            pprint(app.run_load_user_preferences())
        elif args.stage == "compose_cypher":
            if not (args.question and args.constraints):
                parser.error("--stage compose_cypher requires --question and --constraints")
            try:
                query_constraints = json.loads(args.constraints)
            except Exception:
                parser.error("--constraints must be a JSON object")
            if not isinstance(query_constraints, dict):
                parser.error("--constraints must be a JSON object")
            platform_preferences = {}
            if args.platform_preferences:
                try:
                    platform_preferences = json.loads(args.platform_preferences)
                except Exception:
                    parser.error("--platform-preferences must be a JSON object")
                if not isinstance(platform_preferences, dict):
                    parser.error("--platform-preferences must be a JSON object")
            pprint(
                app.run_compose_cypher(
                    args.question,
                    query_constraints,
                    platform_preferences=platform_preferences,
                    exclude_allergens=exclude_allergens,
                )
            )
        elif args.stage == "execute_cypher":
            if not args.cypher:
                parser.error("--stage execute_cypher requires --cypher")
            pprint(app.run_execute_cypher(args.cypher))
        return 0

    if args.question:
        out = app.invoke(args.question, exclude_allergens=exclude_allergens)
        pprint(out)
    else:
        if not args.print_graph:
            parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
