from recipe_wrangler.utils.neo4j_utils import run_query
from langchain.tools import tool

@tool
def rule_based_search_tool(title: str) -> dict:
    """
    Perform rule-based similarity search using ingredients, similar ingredients, and tags.
    Expects state to contain 'title' of the source recipe.
    Returns a list of similar recipes (id, title, score) under 'similar_recipes'.
    """

    query = """
    WITH $title AS source_recipe_title

    MATCH (r1:Recipe {title: source_recipe_title})
    MATCH (r2:Recipe)
    WHERE r1 <> r2

    CALL (r1, r2){
        WITH r1, r2
        MATCH (r1)-[:HAS_INGREDIENT]->(i:Ingredient)<-[:HAS_INGREDIENT]-(r2)
        RETURN count(i) AS directIngredientScore
    }
    CALL (r1, r2){
        WITH r1, r2
        MATCH (r1)-[:HAS_INGREDIENT]->(i1:Ingredient)
        MATCH (r2)-[:HAS_INGREDIENT]->(i2:Ingredient)
        WHERE i1 <> i2
        MATCH (i1)-[s:SIMILAR_TO]-(i2)
        RETURN sum(s.score) AS similarIngredientScore
    }
    CALL (r1, r2){
        WITH r1, r2
        MATCH (r1)-[:HAS_TAG]->(t:Tag)<-[:HAS_TAG]-(r2)
        RETURN count(t) AS directTagScore
    }

    WITH r1, r2, directIngredientScore, COALESCE(similarIngredientScore, 0) AS similarIngredientScore, directTagScore
    MATCH (r1)-[:HAS_INGREDIENT]->(r1_ing)
    OPTIONAL MATCH (r1)-[:HAS_TAG]->(r1_tag)
    WITH r1, r2, directIngredientScore, similarIngredientScore, directTagScore,
         count(DISTINCT r1_ing) AS r1_ing_count,
         count(DISTINCT r1_tag) AS r1_tag_count
         
    WITH r1, r2,
         (directIngredientScore * 1.0) AS weightedDirectIng,
         (similarIngredientScore * 0.75) AS weightedSimilarIng,
         (directTagScore * 1.0) AS weightedTag,
         (r1_ing_count + r1_tag_count) AS normalizationFactor
    WHERE normalizationFactor > 0

    WITH r1, r2, (weightedDirectIng + weightedSimilarIng + weightedTag) / normalizationFactor AS finalScore
    WHERE finalScore > 0

    RETURN
        r1.title AS source_recipe,
        elementId(r2) AS recommended_recipe_id,
        r2.title AS recommended_recipe_title,
        finalScore
    ORDER BY finalScore DESC
    LIMIT 10
    """

    records = run_query(query, parameters={"title": title})
    similar_recipes = [
        {
            "id": record["recommended_recipe_id"],
            "title": record["recommended_recipe_title"],
            "score": record["finalScore"],
        }
        for record in records
    ]

    return {"similar_recipes": similar_recipes}

def Rule_Based_Similar_Recipes_Node(state: dict) -> dict:

    debug = state.get("debug", False)

    title = state.get('title')
    result = rule_based_search_tool.invoke(title)

    if debug:
        if result:
            print(f"\n[Rule_Based_Similar_Recipes_Node] Similar recipes found for recipe {state['title']}")

    if isinstance(result, dict) and "similar_recipes" in result:
        state["similar_recipes"] = result["similar_recipes"]
    else:
        state["similar_recipes"] = []

    print("\n Similar Recipes Found:\n" + "="*30)
    if state["similar_recipes"]:
        for i, recipe in enumerate(state["similar_recipes"], start=1):
            if isinstance(recipe, dict):
                title = recipe.get("title") or recipe.get("id")
                print(f"{i}. {title}")
            else:
                print(f"{i}. {recipe}")
    else:
        print("No similar recipes found.")

    return state
