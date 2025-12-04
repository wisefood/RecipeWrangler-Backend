from typing import TypedDict, List, Dict, Any, Optional

class IngredientProfile(TypedDict, total=False):
    # base from parser
    name: str
    measurement: str
    weight_g: float

    # profiling details
    source: str
    matched_nutritional_ingredient: str
    protein_per_100g: float
    carbs_per_100g: float
    fat_per_100g: float
    protein_g: float
    carbs_g: float
    fat_g: float
    distance: float
    sustainability_ingredient: str
    matched_sustainability_ingredient: str
    sustainability_weight_g: float
    cf_val: float
    sustainability_distance: Optional[float]
    contribution: float

class State(TypedDict, total=False):
    raw_recipe: str
    title: str
    # DEPRECATED (kept optional for upstream nodes)
    ingredient_names: List[str]
    measurements: List[str]
    weights: List[float]

    # NEW canonical container
    ingredients: List[IngredientProfile]

    debug: bool
    directions: List[str]
    total_time: int
    tags: List[str]
    allergens: List[str]

    sustainability_per_kg: float
    total_protein_g: float
    total_fat_g: float
    total_carbohydrate_g: float
    total_energy_kcal: float

    profiling_totals: Dict[str, float]
    full_profile: Dict[str, Any]

    # optional inputs if available
    serves: int
    serving_size_g: float
    min_similarity: float

    similar_recipes: List[Dict[str, Any]]
    agent_decision: str
    query: str
    cypher: str
    tag_list: List[str]
