from utils.neo4j_utils import run_query
from langchain.tools import tool

@tool
def fetch_recipe_info(recipe_title):
    """
    Retrieves all metadata associated with a recipe node from a Neo4j graph database 
    by matching the recipe's title.
    This tool is used when the recipe's exact title is available from previous interaction.

    Parameters:
        recipe_title (str): The exact or case-insensitive title of the recipe to fetch.

    Returns:
        dict: A dictionary containing all properties of the matching recipe node, which may include:
            - 'title' (str): Recipe title.
            - 'Instructions' (list of str or str): Step-by-step cooking instructions.
            - 'Duration' (float): Total time required (in minutes).
            - 'Serves' (int): Number of servings.
            - 'ServingSize' (float): Serving size in grams.
            - 'Calories' (float): Calories per serving.
            - 'CaloriesFromFat' (float): Calories derived from fat.
            - 'TotalFat' (float): Total fat content (g).
            - 'SaturatedFat' (float): Saturated fat content (g).
            - 'Cholesterol' (float): Cholesterol content (mg).
            - 'Protein' (float): Protein content (g).
            - 'TotalCarbohydrate' (float): Carbohydrate content (g).
            - 'DietaryFiber' (float): Dietary fiber (g).
            - 'Sugars' (float): Sugar content (g).
            - 'Sodium' (float): Sodium content (mg).
            - 'NutriScore' (float): Nutritional quality score (lower is better).
            - 'FSAScore' (float): UK Food Standards Agency health score.
            - 'WhoScore' (float): WHO-based nutrition score.
            - 'Sustainability_per_kg' (float): Environmental impact score per kg.

    """
    
    query = """
    MATCH (r:Recipe)
    WHERE toLower(r.title) = toLower($recipe_title)
    OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
    RETURN r, collect(i.name) AS ingredients
    """
    result = run_query(query, {"recipe_title": recipe_title})
    if not result:
        return {}

    node = result[0]['r']
    properties = dict(node)
    properties['Ingredients'] = result[0]['ingredients']  # add ingredients list

    return properties

