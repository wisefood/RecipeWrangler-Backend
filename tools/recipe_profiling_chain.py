from langgraph.graph import StateGraph, START, END
from core.state import State
from langchain.tools import tool
from IPython.display import Image, display

# NODES
from tools.parse_recipe_tool import Recipe_Parser_Node
from tools.ingredient_weight_tool import Ingredient_Weight_Node
from tools.recipe_profiling_tool import Recipe_Profiling_Node  # <-- your node from recipe_profiling.py

def build_pipeline():
    builder = StateGraph(State)

    builder.add_node("Recipe_Parser", Recipe_Parser_Node)
    builder.add_node("Weight_Calculator", Ingredient_Weight_Node)
    builder.add_node("Recipe_Profiling_Node", Recipe_Profiling_Node)  # <-- name matches edges

    builder.add_edge(START, "Recipe_Parser")
    builder.add_edge("Recipe_Parser", "Weight_Calculator")
    builder.add_edge("Weight_Calculator", "Recipe_Profiling_Node")
    builder.add_edge("Recipe_Profiling_Node", END)

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

    # Drop raw text from the returned dict
    filtered_state = {k: v for k, v in final_state.items() if k != "raw_recipe"}
    return filtered_state

def visualize_pipeline_graph():
    graph = build_pipeline()
    display(Image(graph.get_graph().draw_mermaid_png()))
