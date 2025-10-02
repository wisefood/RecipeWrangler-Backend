from typing import TypedDict, List, Dict, Any
from langchain.tools import tool
import requests
import json


@tool
def parse_recipe_tool_open(raw_recipe: str) -> dict:
    """
    Parses a raw recipe string into structured components: title, ingredients, measurements, and directions.
    """


    response = requests.post('http://localhost:11434/api/generate', json={
    "model": "qwen3:8b",
    "prompt": f"Parse this recipe's info in a structured JSON with keys: title[str], ingredient_names[list], measurements[list], directions[list], total_time[int]. Recipe: {raw_recipe}",
    "stream": False,
    "format": "json"
    })

    # First level decode
    response_data = json.loads(response.content)

    # Second level decode to turn the inner JSON string into a Python dict
    result = json.loads(response_data['response'])

    return result

def Recipe_Parser_Node(state: Dict[str, Any]) -> Dict[str, Any]:

    debug = state.get("debug", False)   

    result = parse_recipe_tool_open.invoke({
        "raw_recipe": state["raw_recipe"]
    })

    state.update({
        "title": result["title"],
        "ingredient_names": result["ingredient_names"],
        "measurements": result["measurements"],
        "directions": result["directions"],
        "total_time": result["total_time"]
    })

    
    if debug:
        print("[Recipe_Parser_Node] Updated State Keys:", state.keys())
        
    return state