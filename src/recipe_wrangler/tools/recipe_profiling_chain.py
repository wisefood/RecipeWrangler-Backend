from IPython.display import Image, display
from langchain.tools import tool
from langgraph.graph import END, START, StateGraph

from recipe_wrangler.schemas.state import State
from recipe_wrangler.tools.ingredient_weight_tool import Ingredient_Weight_Node
from recipe_wrangler.tools.parse_recipe_tool import Recipe_Parser_Node
from recipe_wrangler.tools.recipe_profiling_tool import Recipe_Profiling_Node
from recipe_wrangler.tools.tagger_tool_gpt import Tagger_Node_gpt

def build_pipeline():
    builder = StateGraph(State)

    builder.add_node("Recipe_Parser", Recipe_Parser_Node)
    builder.add_node("Weight_Calculator", Ingredient_Weight_Node)
    builder.add_node("Recipe_Profiling_Node", Recipe_Profiling_Node)  # <-- name matches edges
    builder.add_node("Tagger_Node", Tagger_Node_gpt)

    builder.add_edge(START, "Recipe_Parser")
    builder.add_edge("Recipe_Parser", "Weight_Calculator")
    builder.add_edge("Weight_Calculator", "Recipe_Profiling_Node")
    builder.add_edge("Recipe_Profiling_Node", "Tagger_Node")
    builder.add_edge("Tagger_Node", END)

    graph = builder.compile()
    return graph

@tool
def Recipe_Profiling_Chain(recipe_text: str, debug: bool = True):
    """
    Parses unstructured recipe text and extracts structured metadata including 
    ingredients, instructions, nutrition, and sustainability data.
    """
    graph = build_pipeline()
    initial_state = State(raw_recipe=recipe_text, debug=debug)
    final_state = graph.invoke(initial_state)

    # Normalize tags into a list prior to filtering the final state
    tags = final_state.get("tags")
    normalized_tags = []
    if isinstance(tags, str):
        normalized_tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    elif isinstance(tags, list):
        normalized_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    elif tags:
        normalized_tags = [str(tags).strip()]

    final_state["tags"] = normalized_tags

    # Normalize allergens into a list for the returned payload
    allergens = final_state.get("allergens")
    normalized_allergens = []
    if isinstance(allergens, str):
        normalized_allergens = [item.strip() for item in allergens.split(",") if item.strip()]
    elif isinstance(allergens, list):
        normalized_allergens = [str(item).strip() for item in allergens if str(item).strip()]
    elif allergens:
        normalized_allergens = [str(allergens).strip()]
    final_state["allergens"] = normalized_allergens

    # Drop raw text from the returned dict
    filtered_state = {k: v for k, v in final_state.items() if k != "raw_recipe"}
    return filtered_state

def visualize_pipeline_graph():
    graph = build_pipeline()
    display(Image(graph.get_graph().draw_mermaid_png()))
