from typing import TypedDict, List, Dict, Any
from langchain.tools import tool
import requests
import json


@tool
def parse_recipe_tool_open(raw_recipe: str) -> dict:
    """
    Parses a raw recipe string into structured components: title, ingredients, measurements, and directions.
    """

    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "qwen3:8b",
            "prompt": (
                "Parse this recipe's info in a structured JSON with keys: "
                "title[str], ingredient_names[list], measurements[list], "
                "directions[list], total_time[int], serves[int]. "
                f"Recipe: {raw_recipe}"
            ),
            "stream": False,
            "format": "json",
        },
    )

    # Decode the response from the model server; bubble clear errors.
    response.raise_for_status()
    try:
        response_data = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON from model server: {response.text}") from exc

    if "error" in response_data:
        raise RuntimeError(f"Model server error: {response_data['error']}")
    if "response" not in response_data:
        raise KeyError(f"Missing 'response' field in model reply: {response_data}")

    try:
        result = json.loads(response_data["response"])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model 'response' is not valid JSON: {response_data['response']}") from exc

    return result

def Recipe_Parser_Node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node that converts raw recipe text in state into structured fields 
    (title, ingredients, measurements, directions, total_time, serves).
    """
    debug = state.get("debug", False)   

    result = parse_recipe_tool_open.invoke({
        "raw_recipe": state["raw_recipe"]
    })

    state.update({
        "title": result["title"],
        "ingredient_names": result["ingredient_names"],
        "measurements": result["measurements"],
        "directions": result["directions"],
        "total_time": result["total_time"],
        "serves": result["serves"]
    })

    
    if debug:
        print("[Recipe_Parser_Node] Updated State Keys:", state.keys())
        
    return state
