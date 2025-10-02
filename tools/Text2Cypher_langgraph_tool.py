# recipe_graph.py
from __future__ import annotations

import copy
import os
import sys
import getpass
from dataclasses import dataclass
from operator import add
from collections import defaultdict
from collections.abc import Mapping
from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict
from langchain_ollama import ChatOllama

# --- Env: OpenAI ---
if "OPENAI_API_KEY" not in os.environ or not os.environ["OPENAI_API_KEY"]:
    # Prompt only if missing, keeps CI/headless happy
    try:
        os.environ["OPENAI_API_KEY"] = getpass.getpass("Enter your OPENAI_API_KEY: ")
    except (EOFError, KeyboardInterrupt):
        pass

# --- Env: Neo4j ---
NEO4J_URI = os.getenv("NEO4J_URI")

# ---- LangChain / LangGraph / Neo4j imports ----
from langchain_neo4j import Neo4jGraph, Neo4jVector
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.example_selectors import SemanticSimilarityExampleSelector

from langgraph.graph import END, START, StateGraph
from langchain_neo4j.chains.graph_qa.cypher_utils import CypherQueryCorrector, Schema
from neo4j.exceptions import ClientError, CypherSyntaxError
from pydantic import BaseModel, Field

from neo4j_graphrag.schema import format_schema

try:
    from tools.fetch_recipe_info import fetch_recipe_info  # type: ignore
except Exception:
    fetch_recipe_info = None  # type: ignore


# ---------------------------
# Typed states
# ---------------------------
class InputState(TypedDict):
    question: str


class OverallState(TypedDict):
    question: str
    next_action: str
    cypher_statement: str
    cypher_errors: List[str]
    database_records: List[dict] | str
    steps: Annotated[List[str], add]


class OutputState(TypedDict):
    results: List[dict] | str
    steps: List[str]
    cypher_statement: str


# ---------------------------
# Config / constants
# ---------------------------
TAGS_TEXT = """Available Tags:
Diet Type: Vegan, Vegetarian, Meat-based, Seafood
Nutritional: High Protein, Low Fat, Low Carb, High Fiber, Low Sugar, Keto Friendly, Balanced
Health: Heart Healthy, Low Cholesterol, Low Sodium, Diabetic Friendly, Weight Loss Friendly, Highly Nutritious, General
Time: 15-minutes-or-less, 30-minutes-or-less, 60-minutes-or-less, 4-hours-or-less, 1-day-or-less
Difficulty: 5-ingredients-or-less, Easy
Dish Type: Appetizer, Main Course, Side Dish, Breakfast, Lunch, Dinner, Desserts, Snacks, Brunch, Salads, Soups & Stews, Beverages and Cocktails
Main Ingredient: Chicken, Beef, etc.
Special Dietary: Dairy Free, Gluten Free
Techniques: Boil, Bake, Grill, Roast, Sauté, Steam, Fry, etc.
Region: Greek, Indian, Italian, etc.
"""


# ---------------------------
# Pydantic models for guardrail + validation
# ---------------------------
class GuardrailsOutput(BaseModel):
    decision: Literal["recipe", "end"] = Field(
        description="Decision on whether the question is related to recipes"
    )


class Property(BaseModel):
    node_label: str
    property_key: str
    property_value: str


class ValidateCypherOutput(BaseModel):
    errors: Optional[List[str]] = Field(
        default=None,
        description="Syntax or semantic errors in the Cypher statement"
    )
    filters: Optional[List[Property]] = Field(
        default=None,
        description="Property filters applied in the Cypher statement"
    )


# ---------------------------
# RecipeGraphApp
# ---------------------------
@dataclass
class RecipeSearchApp:
    neo4j_uri: str
    openai_model: str = "gpt-4o"
    temperature: float = 0.0
    strict_value_mapping: bool = True  # if True, mapping misses short-circuit to 'end'

    def __post_init__(self):
        # Neo4j connections
        self.base_graph = Neo4jGraph(
            url=self.neo4j_uri,
            refresh_schema=False,
        )
        self.enhanced_graph = Neo4jGraph(
            url=self.neo4j_uri,
            refresh_schema=False,
            enhanced_schema=True,
        )

        if not self._try_refresh_schema_with_apoc():
            fallback_schema = self._build_schema_without_apoc(self.base_graph)
            self.base_graph.structured_schema = fallback_schema
            self.base_graph.schema = format_schema(fallback_schema, is_enhanced=False)

            enhanced_schema = copy.deepcopy(fallback_schema)
            self.enhanced_graph.structured_schema = enhanced_schema
            self.enhanced_graph.schema = format_schema(
                enhanced_schema, is_enhanced=True
            )

        # LLM
        #self.llm = ChatOpenAI(model=self.openai_model, temperature=self.temperature)
        self.llm = ChatOllama(model="gpt-oss:20b", temperature=self.temperature) # or qwen3:8b gpt-oss:20b

        # Example selector (requires embeddings + vector backend)
        self.example_selector = SemanticSimilarityExampleSelector.from_examples(
            self._fewshot_examples(),
            OpenAIEmbeddings(),
            Neo4jVector,
            k=5,
            input_keys=["question"],
        )

        # Build chains + compiled graph
        self._build_chains()
        self.langgraph = self._build_state_graph().compile()

    # ---------- Public API ----------
    def invoke(self, question: str) -> OutputState:
        return self.langgraph.invoke({"question": question})

    # ---------- Public Stage Runners (for testing each node) ----------
    def run_guardrails(self, question: str) -> OverallState:
        """Run only the guardrails node and return its partial state."""
        return self._guardrails({"question": question})

    def run_generate_cypher(self, question: str) -> OverallState:
        """Run only the text-to-cypher node and return cypher + next action."""
        return self._generate_cypher({"question": question})

    def run_validate_cypher(self, question: str, cypher: str) -> OverallState:
        """Run only the validate-cypher node for a given question/cypher."""
        return self._validate_cypher({
            "question": question,
            "cypher_statement": cypher,
        })

    def run_correct_cypher(self, question: str, cypher: str, errors: List[str]) -> OverallState:
        """Run only the correct-cypher node, providing errors and prior cypher."""
        return self._correct_cypher({
            "question": question,
            "cypher_statement": cypher,
            "cypher_errors": errors,
        })

    def run_execute_cypher(self, cypher: str) -> OverallState:
        """Run only the execute-cypher node using the provided cypher."""
        return self._execute_cypher({
            "cypher_statement": cypher,
        })

    def run_generate_final_answer(self, question: str, results: List[dict] | str, cypher: str = "") -> OutputState:
        """Run only the final-answer node with provided results (and optional cypher)."""
        return self._generate_final_answer({
            "question": question,
            "database_records": results,
            "cypher_statement": cypher,
            "steps": [],
        })

    def save_graph_png(self, output_path: str = "recipe_langgraph.png") -> None:
        """
        Saves a PNG of the graph if the installed langgraph build supports it.
        """
        try:
            png_bytes = self.langgraph.get_graph().draw_mermaid_png()
            with open(output_path, "wb") as f:
                f.write(png_bytes)
            print(f"Graph saved to: {output_path}")
        except Exception as e:
            print("Unable to render graph PNG with this environment:", repr(e))

    # ---------- Schema helpers ----------
    def _try_refresh_schema_with_apoc(self) -> bool:
        """Attempt to populate schema data using APOC-powered helpers."""
        try:
            self.base_graph.refresh_schema()
            self.enhanced_graph.refresh_schema()
            return True
        except (ValueError, ClientError) as exc:
            if self._is_missing_apoc_error(exc):
                return False
            raise

    @staticmethod
    def _is_missing_apoc_error(exc: Exception) -> bool:
        """Detect whether an exception stems from missing APOC procedures."""
        message = str(exc).lower()
        if "apoc" in message and "procedure" in message:
            return True
        if isinstance(exc, ClientError) and exc.code == "Neo.ClientError.Procedure.ProcedureNotFound":
            return True
        cause = getattr(exc, "__cause__", None)
        return RecipeSearchApp._is_missing_apoc_error(cause) if cause else False

    def _build_schema_without_apoc(self, graph: Neo4jGraph) -> Dict[str, Any]:
        """Construct a minimal Neo4j schema without relying on APOC procedures."""
        node_props: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for row in graph.query("CALL db.schema.nodeTypeProperties()"):
            labels = row.get("nodeLabels") or []
            property_name = row.get("propertyName")
            if not labels or not property_name:
                continue
            prop_type = self._normalize_property_type(row.get("propertyTypes"))
            for label in labels:
                existing = node_props[label]
                if not any(prop["property"] == property_name for prop in existing):
                    existing.append({"property": property_name, "type": prop_type})

        for row in graph.query("CALL db.labels()"):
            label = row.get("label") or row.get("name")
            if label:
                node_props.setdefault(label, [])

        rel_props: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        relationships_lookup = set()
        relationships: List[Dict[str, str]] = []
        for row in graph.query("CALL db.schema.relTypeProperties()"):
            rel_type = row.get("relType")
            if not rel_type:
                continue

            property_name = row.get("propertyName")
            if property_name:
                prop_type = self._normalize_property_type(row.get("propertyTypes"))
                existing = rel_props[rel_type]
                if not any(prop["property"] == property_name for prop in existing):
                    existing.append({"property": property_name, "type": prop_type})

            sources = row.get("sourceNodeLabels") or ["*"]
            targets = row.get("targetNodeLabels") or ["*"]
            for start in sources:
                for end in targets:
                    identifier = (start, rel_type, end)
                    if identifier not in relationships_lookup:
                        relationships_lookup.add(identifier)
                        relationships.append(
                            {"start": start, "type": rel_type, "end": end}
                        )

        for row in graph.query("CALL db.relationshipTypes()"):
            rel_type = row.get("relationshipType") or row.get("name")
            if not rel_type:
                continue
            rel_props.setdefault(rel_type, [])
            if not any(rel["type"] == rel_type for rel in relationships):
                relationships.append({"start": "*", "type": rel_type, "end": "*"})

        relationships.sort(key=lambda rel: (rel["type"], rel["start"], rel["end"]))

        try:
            constraints = graph.query("SHOW CONSTRAINTS")
        except ClientError:
            constraints = []
        try:
            indexes = graph.query("SHOW INDEXES")
        except ClientError:
            indexes = []

        return {
            "node_props": dict(sorted(node_props.items())),
            "rel_props": dict(sorted(rel_props.items())),
            "relationships": relationships,
            "metadata": {"constraint": constraints, "index": indexes},
        }

    @staticmethod
    def _normalize_property_type(property_types: Optional[List[str]]) -> str:
        if not property_types:
            return "UNKNOWN"
        if len(property_types) == 1:
            return property_types[0]
        base, *rest = property_types
        if base == "LIST" and rest:
            return f"LIST<{','.join(rest)}>"
        return " | ".join(property_types)

    @staticmethod
    def _normalize_cypher_statement(cypher: Optional[str]) -> str:
        """Trim whitespace and trailing semicolons so validators recognize the query."""
        if not cypher:
            return ""
        cleaned = cypher.strip()
        while cleaned.endswith(";"):
            cleaned = cleaned[:-1].rstrip()
        return cleaned

    # ---------- Internals ----------
    def _fewshot_examples(self):
        return [
            {
                "question": "Tell me a recipe with chicken under 30 minutes",
                "query": """MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
WHERE toLower(i.name) CONTAINS 'chicken' AND r.Duration < 30
RETURN DISTINCT r.title""",
            },
            {
                "question": "Tell me a recipe with rice and beef",
                "query": """MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i1:Ingredient), (r)-[:HAS_INGREDIENT]->(i2:Ingredient)
WHERE toLower(i1.name) CONTAINS 'rice'
  AND toLower(i2.name) CONTAINS 'beef'
RETURN DISTINCT r.title""",
            },
            {
                "question": "Tell me a Greek salad",
                "query": """MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
WITH r, collect(t.name) AS tags
WHERE 'Greek' IN tags AND 'Salads' IN tags
RETURN DISTINCT r.title""",
            },
            {
                "question": "Tell me a high-protein dairy free recipe with chicken under 1 hour.",
                "query": """MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
MATCH (r)-[:HAS_TAG]->(t:Tag)
WHERE toLower(i.name) CONTAINS 'chicken'
WITH r, collect(t.name) AS tags
WHERE 'High Protein' IN tags AND 'Dairy Free' IN tags AND r.Duration < 60
RETURN DISTINCT r.title""",
            },
            {
                "question": "Tell me a dairy free salad with seafood",
                "query": """MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
WITH r, collect(t.name) AS tags
WHERE 'Seafood' IN tags AND 'Salads' IN tags AND 'Dairy Free' IN tags
RETURN DISTINCT r.title""",
            },
            {
                "question": "Give me a high-protein vegan recipe that takes less than 30 minutes.",
                "query": """MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
WITH r, collect(t.name) AS tags
WHERE 'High Protein' IN tags AND 'Vegan' IN tags AND r.Duration < 30
RETURN DISTINCT r.title""",
            },
            {
                "question": "Which Indian dessert has the highest WhoScore?",
                "query": """MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
WITH r, collect(t.name) AS tags
WHERE 'Indian' IN tags AND 'Desserts' IN tags
RETURN r.title
ORDER BY r.WhoScore DESC
LIMIT 1""",
            },
            {
                "question": "Find me a gluten-free seafood main course for dinner.",
                "query": """MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
WITH r, collect(t.name) AS tags
WHERE 'Gluten Free' IN tags AND 'Seafood' IN tags AND 'Main Course' IN tags AND 'Dinner' IN tags
RETURN DISTINCT r.title""",
            },
            {
                "question": "Find recipes with at least 100 grams of chicken and tagged as low carb.",
                "query": """MATCH (r:Recipe)-[rel:HAS_INGREDIENT]->(i:Ingredient)
MATCH (r)-[:HAS_TAG]->(t:Tag)
WHERE toLower(i.name) CONTAINS 'chicken' AND rel.weight >= 100
WITH r, collect(t.name) AS tags
WHERE 'Low Carb' IN tags
RETURN DISTINCT r.title""",
            },
            {
                "question": "Tell me a high protein recipe with chicken",
                "query": """MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag),
      (r)-[:HAS_INGREDIENT]->(i:Ingredient)
WHERE t.name = 'High Protein' AND toLower(i.name) CONTAINS 'chicken'
RETURN r
LIMIT 5""",
            },
            {
                "question": "pizza",
                "query": """MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
WHERE toLower(r.title) CONTAINS 'pizza'
RETURN DISTINCT r.title""",
            },
            {
                "question": "Show me quick vegan breakfast recipes",
                "query": """MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
WITH r, collect(t.name) AS tags
WHERE 'Vegan' IN tags AND 'Breakfast' IN tags AND r.Duration <= 20
RETURN DISTINCT r.title""",
            },
            {
                "question": "Tell me a low-fat recipe with chicken and rice",
                "query": """WITH ['chicken','rice'] AS required
MATCH (r:Recipe)
WHERE ALL(ing IN required WHERE
  EXISTS {
    MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
    WHERE toLower(i.name) CONTAINS toLower(ing)
  }
)
MATCH (r)-[:HAS_TAG]->(t:Tag)
WHERE t.name = 'Low Fat'
RETURN DISTINCT r.title
LIMIT 25""",
            },
            {
                "question": "Show me low-carb mains that use beef or chicken",
                "query": """MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
WHERE ANY(x IN ['beef','chicken'] WHERE toLower(i.name) CONTAINS x)
MATCH (r)-[:HAS_TAG]->(t:Tag)
WITH r, collect(DISTINCT t.name) AS tags
WHERE 'Low Carb' IN tags AND 'Main Course' IN tags
RETURN DISTINCT r.title
LIMIT 25""",
            },
        ]

    def _build_chains(self) -> None:
        # ---- Guardrails ----
        guardrails_system = """
As an intelligent assistant, your primary objective is to decide whether a given question is related to food recipes or not. 
If the question is related to food recipes, output "recipe". Otherwise, output "end".
To make this decision, assess the content of the question and determine if it refers to any recipe, ingredient or diet type.
Provide only the specified output: "recipe" or "end".
"""
        guardrails_prompt = ChatPromptTemplate.from_messages(
            [("system", guardrails_system), ("human", "{question}")]
        )
        self.guardrails_chain = guardrails_prompt | self.llm | StrOutputParser()

        # ---- Text2Cypher ----
        text2cypher_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Given an input question, convert it to a Cypher query. No pre-amble."
                    "Do not wrap the response in any backticks or anything else. Respond with a Cypher statement only!"
                    "Property names are case-sensitive; copy them exactly as they appear in the schema.",
                ),
                (
                    "human",
                """You are a Neo4j expert. Given an input question, create a syntactically correct Cypher query to run.
                Do not wrap the response in any backticks or anything else. Respond with a Cypher statement only!
                Property names are case-sensitive. Use the exact casing from the schema (e.g., use Duration, not duration).
                Here is the schema information
                {schema}

                And all the available tags:
                {tags}

                Below are a number of examples of questions and their corresponding Cypher queries.

                {fewshot_examples}

                User input: {question}
                Cypher query:""",
                ),
            ]
        )
        self.text2cypher_chain = text2cypher_prompt | self.llm | StrOutputParser()

        # ---- Validate Cypher ----
        validate_cypher_system = "You are a Cypher expert reviewing a statement written by a junior developer."
        validate_cypher_user = """You must check the following:
* Are there any syntax errors or misspelings in the Cypher statement?
* Are there any missing or undefined variables in the Cypher statement?
* Are any node labels missing from the schema?
* Are any relationship types missing from the schema?
* Are any of the properties not included in the schema?
* Does the Cypher statement include enough information to answer the question?

Examples of good errors:
* Label (:Foo) does not exist, did you mean (:Bar)?
* Property bar does not exist for label Foo, did you mean baz?
* Relationship FOO does not exist, did you mean FOO_BAR?

If there are errors, explain them. 

Schema:
{schema}

And all the available tags:
{tags}

The question is:
{question}

The Cypher statement is:
{cypher}

Make sure you don't make any mistakes!"""
        validate_cypher_prompt = ChatPromptTemplate.from_messages(
            [("system", validate_cypher_system), ("human", validate_cypher_user)]
        )
        self.validate_cypher_chain = validate_cypher_prompt | self.llm.with_structured_output(
            ValidateCypherOutput
        )

        # ---- Corrector (directional) ----
        corrector_schema = [
            Schema(el["start"], el["type"], el["end"])
            for el in self.enhanced_graph.structured_schema.get("relationships")
        ]
        self.cypher_query_corrector = CypherQueryCorrector(corrector_schema)

        # ---- Final Answer ----
        generate_final_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "You are a helpful assistant"),
                (
                    "human",
                    """Use the following results retrieved from a database to provide
a succinct, definitive answer to the user's question.

Respond as if you are answering the question directly.

Results: {results}
Question: {question}""",
                ),
            ]
        )
        self.generate_final_chain = generate_final_prompt | self.llm | StrOutputParser()

    # ---------------------------
    # Node functions
    # ---------------------------
    def _guardrails(self, state: InputState) -> OverallState:
        raw = (self.guardrails_chain.invoke({"question": state.get("question")}) or "").strip().lower()
        # Prefer exact word matches to avoid substring hits like "recipes" -> "recipe"
        import re
        has_recipe = bool(re.search(r"\brecipe\b", raw))
        has_end = bool(re.search(r"\bend\b", raw))
        decision = "recipe" if has_recipe else ("end" if has_end else None)
        if decision is None:
            q = (state.get("question") or "").lower()
            recipe_keywords = [
                "recipe","ingredient","cook","bake","grill","dish","salad","soup",
                "vegan","keto","gluten","dairy","low carb","high protein",
                "main course","dessert","breakfast","dinner",
            ]
            decision = "recipe" if any(k in q for k in recipe_keywords) else "end"

        db = None
        if decision == "end":
            db = "This questions is not about recipes or their ingredients. Therefore I cannot answer this question."
        return {
            "next_action": decision,
            "database_records": db,
            "steps": ["guardrail"],
        }


    def _generate_cypher(self, state: OverallState) -> OverallState:
        NL = "\n"
        examples = self._fewshot_examples()
        q_tokens = (state.get("question") or "").lower().split()
        def score(ex):
            return len(set(q_tokens) & set(ex["question"].lower().split()))
        top = sorted(examples, key=score, reverse=True)[:5]
        fewshot_examples = (NL * 2).join(
            [f"Question: {el['question']}{NL}Cypher:{el['query']}" for el in top]
        )

        cypher = self.text2cypher_chain.invoke(
            {
                "question": state.get("question"),
                "fewshot_examples": fewshot_examples,
                "schema": self.enhanced_graph.schema,
                "tags": TAGS_TEXT,
            }
        )
        normalized = self._normalize_cypher_statement(cypher)
        return {"cypher_statement": normalized, "steps": ["generate_cypher"]}

    def _validate_cypher(self, state: OverallState) -> OverallState:
        errors: List[str] = []
        mapping_errors: List[str] = []

        # Normalize statement so downstream validators handle trailing semicolons
        incoming_cypher = self._normalize_cypher_statement(state.get("cypher_statement"))

        # Syntax check
        try:
            self.enhanced_graph.query(f"EXPLAIN {incoming_cypher}")
        except CypherSyntaxError as e:
            errors.append(e.message)

        # Direction correction (experimental)
        corrected = None
        try:
            corrected = self.cypher_query_corrector(incoming_cypher)
        except Exception as exc:
            print("Cypher direction corrector failed:", repr(exc))

        if corrected and corrected != incoming_cypher:
            print("Relationship direction was corrected")
        cypher_to_use = corrected or incoming_cypher

        # Static schema checks for clearer error messages (labels/relationships)
        try:
            import re, difflib
            # Only count labels outside relationship brackets, e.g., (:Label) not [:REL]
            used_labels = set(re.findall(r"(?<!\[):`?([A-Za-z_][A-Za-z0-9_]*)`?", cypher_to_use or ""))
            used_rels = set(re.findall(r"\[:`?([A-Za-z_][A-Za-z0-9_]*)`?\]", cypher_to_use or ""))
            known_labels = set(self.enhanced_graph.structured_schema.get("node_props", {}).keys())
            known_rels = set(rel.get("type") for rel in self.enhanced_graph.structured_schema.get("relationships", []))
            for lbl in sorted(used_labels):
                if lbl not in known_labels:
                    suggestion = next(iter(difflib.get_close_matches(lbl, list(known_labels), n=1, cutoff=0.6)), None)
                    msg = f"Label :{lbl} does not exist" + (f", did you mean :{suggestion}?" if suggestion else "")
                    errors.append(msg)
            for rt in sorted(used_rels):
                if rt not in known_rels:
                    suggestion = next(iter(difflib.get_close_matches(rt, list(known_rels), n=1, cutoff=0.6)), None)
                    msg = f"Relationship type {rt} does not exist" + (f", did you mean {suggestion}?" if suggestion else "")
                    errors.append(msg)
        except Exception:
            pass

        # LLM validation (and optional value mapping)
        # Wrap the structured-output call in try/except because some models
        # (e.g., local ChatOllama) may not produce strict JSON reliably.
        try:
            llm_out = self.validate_cypher_chain.invoke({
                "question": state.get("question"),
                "schema": self.enhanced_graph.schema,
                "tags": TAGS_TEXT,
                "cypher": cypher_to_use,
            })
        except Exception:
            llm_out = ValidateCypherOutput(errors=None, filters=None)

        
        if llm_out.errors:
            errors.extend(llm_out.errors)

        if self.strict_value_mapping and llm_out.filters:
            for f in llm_out.filters:
                # Map only string properties
                node_props = self.enhanced_graph.structured_schema["node_props"].get(f.node_label, [])
                prop_meta = [p for p in node_props if p["property"] == f.property_key]
                if not prop_meta or prop_meta[0].get("type") != "STRING":
                    continue

                # Exact case-insensitive equality (strict)
                mapping = self.enhanced_graph.query(
                    f"""
                    MATCH (n:{f.node_label})
                    WHERE toLower(n.`{f.property_key}`) = toLower($value)
                    RETURN 'yes' LIMIT 1
                    """,
                    {"value": f.property_value},
                )
                if not mapping:
                    msg = f"Missing value mapping for {f.node_label}.{f.property_key} == {f.property_value}"
                    print(msg)
                    mapping_errors.append(msg)

        # Decide next action
        if mapping_errors:
            next_action = "end"
        elif errors:
            next_action = "correct_cypher"
        else:
            next_action = "execute_cypher"

        return {
            "next_action": next_action,
            "cypher_statement": cypher_to_use,
            "cypher_errors": errors,
            "steps": ["validate_cypher"],
        }

    def _correct_cypher(self, state: OverallState) -> OverallState:
        correct_cypher_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a Cypher expert reviewing a statement written by a junior developer. "
                    "You need to correct the Cypher statement based on the provided errors. No pre-amble."
                    "Do not wrap the response in any backticks or anything else. Respond with a Cypher statement only!",
                ),
                (
                    "human",
                    """Check for invalid syntax or semantics and return a corrected Cypher statement.

Schema:
{schema}

Note: Do not include any explanations or apologies in your responses.
Do not wrap the response in any backticks or anything else.
Respond with a Cypher statement only!

Do not respond to any questions that might ask anything else than for you to construct a Cypher statement.

The question is:
{question}

The Cypher statement is:
{cypher}

The errors are:
{errors}

Corrected Cypher statement: """,
                ),
            ]
        )
        correct_cypher_chain = correct_cypher_prompt | self.llm | StrOutputParser()

        corrected = correct_cypher_chain.invoke(
            {
                "question": state.get("question"),
                "errors": state.get("cypher_errors"),
                "cypher": state.get("cypher_statement"),
                "schema": self.enhanced_graph.schema,
            }
        )
        return {
            "next_action": "validate_cypher",
            "cypher_statement": self._normalize_cypher_statement(corrected),
            "steps": ["correct_cypher"],
        }

    def _execute_cypher(self, state: OverallState) -> OverallState:
        cypher = self._normalize_cypher_statement(state.get("cypher_statement"))
        records = self.enhanced_graph.query(cypher)
        no_results = "I couldn't find any relevant information in the database"
        return {
            "database_records": records if records else no_results,
            "next_action": "end",
            "steps": ["execute_cypher"],
        }

    def _enrich_records_with_recipe_info(self, records: List[dict]) -> List[dict]:
        if not fetch_recipe_info:
            return records

        info_cache: Dict[str, Dict[str, Any]] = {}
        enriched: List[dict] = []

        for record in records:
            if not isinstance(record, dict):
                enriched.append(record)
                continue

            title = self._extract_title_from_record(record)
            if not title:
                enriched.append(record)
                continue

            cached_info = info_cache.get(title)
            if cached_info is None:
                try:
                    cached_info = fetch_recipe_info(title) or {}
                except Exception:
                    cached_info = {}
                info_cache[title] = cached_info

            enriched.append(self._merge_recipe_info(record, title, cached_info))

        return enriched

    def _extract_title_from_record(self, record: Mapping[str, Any]) -> Optional[str]:
        for key, value in record.items():
            if isinstance(value, str) and "title" in key.lower():
                return value

        direct_title = record.get("title")
        if isinstance(direct_title, str):
            return direct_title

        for value in record.values():
            if isinstance(value, Mapping):
                nested_title = value.get("title")
                if isinstance(nested_title, str):
                    return nested_title
            else:
                try:
                    value_dict = dict(value)
                except Exception:
                    continue
                nested_title = value_dict.get("title")
                if isinstance(nested_title, str):
                    return nested_title

        return None

    def _merge_recipe_info(self, record: Mapping[str, Any], title: str, info: Mapping[str, Any]) -> dict:
        merged = dict(record)
        merged.setdefault("title", title)

        nutri_value = self._get_case_insensitive(info, ["nutri_score", "nutriscore", "NutriScore"])
        sustain_value = self._get_case_insensitive(info, ["sustainability_score", "sustainabilityperkg", "Sustainability_per_kg"])
        duration_value = self._get_case_insensitive(info, ["duration", "Duration"])

        if nutri_value is not None:
            merged["nutri_score"] = nutri_value
        if sustain_value is not None:
            merged["sustainability_score"] = sustain_value
        if duration_value is not None:
            merged["duration"] = duration_value

        return merged

    @staticmethod
    def _get_case_insensitive(data: Mapping[str, Any], candidates: List[str]) -> Any:
        if not isinstance(data, Mapping):
            return None

        lower_map = {str(key).lower(): value for key, value in data.items()}
        for candidate in candidates:
            if candidate in data:
                return data[candidate]
            lowered_candidate = candidate.lower()
            if lowered_candidate in lower_map:
                return lower_map[lowered_candidate]
        return None

    def _generate_final_answer(self, state: OverallState) -> OutputState:
        # For now, skip LLM summarization and return raw results directly
        steps = (state.get("steps") or []) + ["generate_final_answer"]
        database_records = state.get("database_records")
        if isinstance(database_records, list):
            database_records = self._enrich_records_with_recipe_info(database_records)
        return {
            "results": database_records,
            "steps": steps,
            "cypher_statement": self._normalize_cypher_statement(state.get("cypher_statement", "")),
        }


# ---------------------------
# Build the graph
# ---------------------------
    def _build_state_graph(self) -> StateGraph:
        g = StateGraph(OverallState, input=InputState, output=OutputState)

        # Register nodes with string keys
        g.add_node("guardrails", self._guardrails)
        g.add_node("generate_cypher", self._generate_cypher)
        g.add_node("validate_cypher", self._validate_cypher)
        g.add_node("correct_cypher", self._correct_cypher)
        g.add_node("execute_cypher", self._execute_cypher)
        g.add_node("generate_final_answer", self._generate_final_answer)

        # Edges must use the string keys
        g.add_edge(START, "guardrails")
        g.add_conditional_edges("guardrails", self._guardrails_condition)
        g.add_edge("generate_cypher", "validate_cypher")
        g.add_conditional_edges("validate_cypher", self._validate_cypher_condition)
        g.add_edge("execute_cypher", "generate_final_answer")
        g.add_edge("correct_cypher", "validate_cypher")
        g.add_edge("generate_final_answer", END)

        return g


    # ---------------------------
    # Routing conditions
    # ---------------------------
    def _guardrails_condition(
        self, state: OverallState
    ) -> Literal["generate_cypher", "generate_final_answer"]:
        if state.get("next_action") == "end":
            return "generate_final_answer"
        elif state.get("next_action") == "recipe":
            return "generate_cypher"

    def _validate_cypher_condition(
        self, state: OverallState
    ) -> Literal["generate_final_answer", "correct_cypher", "execute_cypher"]:
        if state.get("next_action") == "end":
            return "generate_final_answer"
        elif state.get("next_action") == "correct_cypher":
            return "correct_cypher"
        elif state.get("next_action") == "execute_cypher":
            return "execute_cypher"


# Backwards compatibility: expose expected class name
RecipeGraphApp = RecipeSearchApp

# ---------------------------
# CLI entry point
# ---------------------------
def _main(argv: list[str]) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Recipe LangGraph runner")
    parser.add_argument("--question", "-q", type=str, help="Question to ask the graph or stage")
    parser.add_argument("--print-graph", "-p", action="store_true", help="Save the graph PNG")
    parser.add_argument("--graph-path", type=str, default="recipe_langgraph.png", help="Output path for PNG")
    parser.add_argument("--non-strict-mapping", action="store_true", help="Do not block on missing value mappings")
    parser.add_argument("--stage", choices=[
        "guardrails",
        "generate_cypher",
        "validate_cypher",
        "correct_cypher",
        "execute_cypher",
        "final_answer",
    ], help="Run a specific stage instead of the full graph")
    parser.add_argument("--cypher", type=str, help="Cypher to use for validate/correct/execute/final stages")
    parser.add_argument("--errors", type=str, help="Comma-separated or JSON list of errors for correct_cypher stage")
    parser.add_argument("--results", type=str, help="JSON list of records or a string for final_answer stage")

    args = parser.parse_args(argv)

    if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
        print("Please set NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD in your environment.", file=sys.stderr)
        return 2

    app = RecipeGraphApp(
        neo4j_uri=NEO4J_URI,
        strict_value_mapping=not args.non_strict_mapping,
    )

    if args.print_graph:
        app.save_graph_png(args.graph_path)

    # Stage-specific execution
    if args.stage:
        from pprint import pprint
        if args.stage == "guardrails":
            if not args.question:
                parser.error("--stage guardrails requires --question")
            pprint(app.run_guardrails(args.question))
        elif args.stage == "generate_cypher":
            if not args.question:
                parser.error("--stage generate_cypher requires --question")
            pprint(app.run_generate_cypher(args.question))
        elif args.stage == "validate_cypher":
            if not (args.question and args.cypher):
                parser.error("--stage validate_cypher requires --question and --cypher")
            pprint(app.run_validate_cypher(args.question, args.cypher))
        elif args.stage == "correct_cypher":
            if not (args.question and args.cypher and args.errors is not None):
                parser.error("--stage correct_cypher requires --question, --cypher and --errors")
            try:
                # Allow JSON array or comma-separated string
                errs = json.loads(args.errors) if args.errors.strip().startswith("[") else [e for e in args.errors.split(",") if e]
            except Exception:
                errs = [e for e in (args.errors or "").split(",") if e]
            pprint(app.run_correct_cypher(args.question, args.cypher, errs))
        elif args.stage == "execute_cypher":
            if not args.cypher:
                parser.error("--stage execute_cypher requires --cypher")
            pprint(app.run_execute_cypher(args.cypher))
        elif args.stage == "final_answer":
            if not (args.question and args.results is not None):
                parser.error("--stage final_answer requires --question and --results (JSON or string)")
            try:
                results = json.loads(args.results)
            except Exception:
                results = args.results
            pprint(app.run_generate_final_answer(args.question, results, args.cypher or ""))
        return 0

    # Full-graph execution
    if args.question:
        out = app.invoke(args.question)
        from pprint import pprint
        pprint(out)
    else:
        if not args.print_graph:
            parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
