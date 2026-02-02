# Purpose: Text-to-Cypher LangGraph pipeline for recipe search.

import os
import sys
from dataclasses import dataclass
from operator import add
from typing import Annotated, List, Literal, Optional, TypedDict
from dotenv import load_dotenv


load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

# ---- LangChain / LangGraph / Neo4j imports ----
from langchain_neo4j import Neo4jGraph
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from langgraph.graph import END, START, StateGraph
from langchain_neo4j.chains.graph_qa.cypher_utils import CypherQueryCorrector, Schema
from neo4j.exceptions import CypherSyntaxError
from pydantic import BaseModel, Field

from recipe_wrangler.utils.examples import TEXT2CYPHER_FEWSHOT_EXAMPLES
from recipe_wrangler.utils.prompts import (
    CORRECT_CYPHER_HUMAN_PROMPT,
    CORRECT_CYPHER_SYSTEM_PROMPT,
    GUARDRAILS_SYSTEM_PROMPT,
    TEXT2CYPHER_HUMAN_PROMPT,
    TEXT2CYPHER_SYSTEM_PROMPT,
    VALIDATE_CYPHER_HUMAN_PROMPT,
    VALIDATE_CYPHER_SYSTEM_PROMPT,
)


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
    results: List[dict] | str
    steps: Annotated[List[str], add]


class OutputState(TypedDict):
    results: List[dict] | str
    steps: List[str]
    cypher_statement: str


# ---------------------------
# Pydantic models for guardrail + validation
# ---------------------------

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
# RecipeSearchApp
# ---------------------------

@dataclass
class RecipeSearchApp:
    neo4j_uri: str
    # Defaults are only used when this class is instantiated directly.
    # API wiring (api/config.py + api/dependencies.py) overrides these via env settings.
    main_model: str = "openai/gpt-oss-20b"
    guardrails_model: str = "llama-3.1-8b-instant"
    temperature: float = 0.0
    strict_value_mapping: bool = True  # if True, mapping misses short-circuit to 'end'

    def __post_init__(self):
        # Neo4j connections
        self.enhanced_graph = Neo4jGraph(
            url=self.neo4j_uri,
            refresh_schema=True,
            enhanced_schema=True,
        )
        
        from langchain_groq import ChatGroq

        self.llm = ChatGroq(
            model=self.main_model,
            temperature=self.temperature,
            max_retries=2,
        )

        self.guardrails_llm = ChatGroq(
            model=self.guardrails_model,
            temperature=self.temperature,
            max_retries=2,
        )
        
        # Build chains + compiled graph
        self._build_chains()
        self.langgraph = self._build_state_graph().compile()

    # ---------- Public API ----------
    def invoke(self, question: str) -> OutputState:
        return self.langgraph.invoke({"question": question})

    def run_guardrails(self, question: str) -> OverallState:
        return self._guardrails({"question": question})

    def run_generate_cypher(self, question: str) -> OverallState:
        return self._generate_cypher({"question": question})

    def run_validate_cypher(self, question: str, cypher: str) -> OverallState:
        return self._validate_cypher({"question": question, "cypher_statement": cypher})

    def run_correct_cypher(self, question: str, cypher: str, errors: List[str]) -> OverallState:
        return self._correct_cypher(
            {"question": question, "cypher_statement": cypher, "cypher_errors": errors}
        )

    def run_execute_cypher(self, cypher: str) -> OverallState:
        return self._execute_cypher({"cypher_statement": cypher})

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
    def _build_chains(self) -> None:
        # ---- Guardrails ----
        guardrails_prompt = ChatPromptTemplate.from_messages(
            [("system", GUARDRAILS_SYSTEM_PROMPT), ("human", "{question}")]
        )
        self.guardrails_chain = guardrails_prompt | self.guardrails_llm | StrOutputParser()

        # ---- Text2Cypher ----
        text2cypher_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", TEXT2CYPHER_SYSTEM_PROMPT),
                ("human", TEXT2CYPHER_HUMAN_PROMPT),
            ]
        )
        self.text2cypher_chain = text2cypher_prompt | self.llm | StrOutputParser()

        # ---- Validate Cypher ----
        validate_cypher_prompt = ChatPromptTemplate.from_messages(
            [("system", VALIDATE_CYPHER_SYSTEM_PROMPT), ("human", VALIDATE_CYPHER_HUMAN_PROMPT)]
        )
        self.validate_cypher_chain = validate_cypher_prompt | self.llm.with_structured_output(
            ValidateCypherOutput,
            method="json_schema",
        )

        # ---- Corrector (directional) ----
        relationships = (self.enhanced_graph.structured_schema or {}).get("relationships") or []
        corrector_schema = [Schema(el["start"], el["type"], el["end"]) for el in relationships]
        self.cypher_query_corrector = CypherQueryCorrector(corrector_schema)

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

        db = ""
        if decision == "end":
            db = "This questions is not about recipes or their ingredients. Therefore I cannot answer this question."
        return {
            "next_action": decision,
            "results": db,
            "cypher_statement": "",
            "steps": ["guardrail"],
        }


    def _generate_cypher(self, state: OverallState) -> OverallState:
        NL = "\n"
        examples = TEXT2CYPHER_FEWSHOT_EXAMPLES
        q_tokens = (state.get("question") or "").lower().split()
        def score(ex):
            return len(set(q_tokens) & set(ex["question"].lower().split()))
        top = sorted(examples, key=score, reverse=True)[:5]
        fewshot_examples = (NL * 2).join(
            [f"Question: {el['question']}{NL}Cypher:{el['query']}" for el in top]
        )

        schema_text = self.enhanced_graph.schema or ""
        cypher = self.text2cypher_chain.invoke(
            {
                "question": state.get("question"),
                "fewshot_examples": fewshot_examples,
                "schema": schema_text,
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
        schema_struct = self.enhanced_graph.structured_schema or {}
        try:
            import re, difflib
            # Only count labels outside relationship brackets, e.g., (:Label) not [:REL]
            used_labels = set(re.findall(r"(?<!\[):`?([A-Za-z_][A-Za-z0-9_]*)`?", cypher_to_use or ""))
            used_rels = set(re.findall(r"\[:`?([A-Za-z_][A-Za-z0-9_]*)`?\]", cypher_to_use or ""))
            known_labels = set(schema_struct.get("node_props", {}).keys())
            known_rels = set(rel.get("type") for rel in schema_struct.get("relationships", []) or [])
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
                "cypher": cypher_to_use,
            })
        except Exception:
            llm_out = ValidateCypherOutput(errors=None, filters=None)

        
        if llm_out.errors:
            errors.extend(llm_out.errors)

        if self.strict_value_mapping and llm_out.filters:
            for f in llm_out.filters:
                # Map only string properties
                node_props = schema_struct.get("node_props", {}).get(f.node_label, [])
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
            "results": "; ".join(mapping_errors) if mapping_errors else "",
            "steps": ["validate_cypher"],
        }

    def _correct_cypher(self, state: OverallState) -> OverallState:
        correct_cypher_prompt = ChatPromptTemplate.from_messages(
            [("system", CORRECT_CYPHER_SYSTEM_PROMPT), ("human", CORRECT_CYPHER_HUMAN_PROMPT)]
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
        # TEMP: widen LIMIT to allow filtering for duration/serves while still returning 10.
        if cypher:
            import re
            limit_match = re.search(r"(?i)\bLIMIT\s+(\d+)\b", cypher)
            if limit_match:
                try:
                    limit_value = int(limit_match.group(1))
                except ValueError:
                    limit_value = None
                if limit_value == 10:
                    cypher = re.sub(r"(?i)\bLIMIT\s+10\b", "LIMIT 50", cypher, count=1)
        records = self.enhanced_graph.query(cypher)
        no_results = "I couldn't find any relevant information in the database"
        return {
            "results": records if records else no_results,
            "next_action": "end",
            "steps": ["execute_cypher"],
            "cypher_statement": cypher,
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
        # Edges must use the string keys
        g.add_edge(START, "guardrails")
        g.add_conditional_edges("guardrails", self._guardrails_condition)
        g.add_edge("generate_cypher", "validate_cypher")
        g.add_conditional_edges("validate_cypher", self._validate_cypher_condition)
        g.add_edge("execute_cypher", END)
        g.add_edge("correct_cypher", "validate_cypher")

        return g


    # ---------------------------
    # Routing conditions
    # ---------------------------
    def _guardrails_condition(
        self, state: OverallState
    ) -> Literal["generate_cypher", "__end__"]:
        if state.get("next_action") == "end":
            return END
        elif state.get("next_action") == "recipe":
            return "generate_cypher"

    def _validate_cypher_condition(
        self, state: OverallState
    ) -> Literal["__end__", "correct_cypher", "execute_cypher"]:
        if state.get("next_action") == "end":
            return END
        elif state.get("next_action") == "correct_cypher":
            return "correct_cypher"
        elif state.get("next_action") == "execute_cypher":
            return "execute_cypher"

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
    ], help="Run a specific stage instead of the full graph")
    parser.add_argument("--cypher", type=str, help="Cypher to use for validate/correct/execute stages")
    parser.add_argument("--errors", type=str, help="Comma-separated or JSON list of errors for correct_cypher stage")

    args = parser.parse_args(argv)

    if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
        print("Please set NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD in your environment.", file=sys.stderr)
        return 2

    app = RecipeSearchApp(
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
