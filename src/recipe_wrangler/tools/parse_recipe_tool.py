# Purpose: LLM-based parser from raw recipe text to structured fields.

from typing import Any, List
import os

from langchain.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field

from recipe_wrangler.schemas import RecipeState

@tool
def parse_recipe_tool(recipe: str) -> dict:
    """Parses a raw recipe text into structured fields."""
    model_name = os.getenv("PARSE_LLM")
    
    if not model_name:
        raise ValueError("Please set PARSE_LLM to a valid model name.")

    class ParsedRecipe(BaseModel):
        title: str = Field(min_length=1)
        ingredient_names: List[str] = Field(min_length=1)
        measurements: List[str] = Field(min_length=1)
        directions: List[str] = Field(min_length=1)
        total_time: int = Field(ge=0)
        serves: int = Field(ge=0)

    llm = ChatGroq(model=model_name, temperature=0.0, max_retries=2)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "Parse this recipe's info into structured fields."),
            ("human", "Recipe: {recipe}"),
        ]
    )
    chain = prompt | llm.with_structured_output(ParsedRecipe, method="json_schema")
    result = chain.invoke({"recipe": recipe})
    return result.model_dump()


def Recipe_Parser_Node(state: RecipeState) -> RecipeState:
    """
    Node that converts raw recipe text in state into structured fields 
    (title, ingredients, measurements, directions, total_time, serves).
    """
    debug = bool(state.debug)

    result = parse_recipe_tool.invoke({"recipe": state.raw_recipe})

    state.title = result["title"]
    state.ingredient_names = result["ingredient_names"]
    state.measurements = result["measurements"]
    state.directions = result["directions"]
    state.total_time = result["total_time"]
    state.serves = result["serves"]

    
    if debug:
        print("[Recipe_Parser_Node] Updated State Keys:", list(state.model_dump().keys()))
        
    return state
