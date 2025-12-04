import re
from typing import TypedDict, List, Dict, Any
from langchain.tools import tool
import requests
import json
from ollama import chat
import ast
@tool
def diet_type_tagger_open(title: str, ingredient_names: list[str]) -> dict:
    """
    Classifies a recipe based on its title and ingredient list as one of:
    'Vegan', 'Vegetarian', 'Meat-based', or 'Seafood'.
    """

    system_prompt = """
    You are an expert in classifying recipes into one of the following categories:
    - Vegan
    - Vegetarian
    - Meat-based
    - Seafood

    Use the recipe title and ingredients to decide. Only return one category label. Do not explain.

    Examples:
    Title: "Tofu Stir Fry"
    Ingredients: ["tofu", "broccoli", "soy sauce"]
    → Vegan

    Title: "Shrimp Scampi"
    Ingredients: ["shrimp", "garlic", "butter", "lemon"]
    → Seafood
    """

    user_prompt = f"""
    Title: {title}
    Ingredients: {ingredient_names}
    →
    """

    response = chat(
        model='qwen3:8b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        think=False,  # disable reasoning tokens / <thinking> output
    )

    content = response['message']['content']

    return content

@tool
def dish_type_tagger_open(title: str, ingredient_names: list[str]) -> dict:
    """
    Classifies the recipe type (multiple tags possible) based on the recipe title.
    Categories: 'Breakfast', 'Lunch', 'Dinner'
    """

    system_prompt = """
    You are an expert in classifying recipes into one or more of these categories:
    - Breakfast
    - Lunch
    - Dinner

    Only one tag can be applied.
    """

    user_prompt = f"""
    Title: {title}
    Ingredients: {ingredient_names}
    →
    """

    response = chat(
        model='qwen3:8b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        think=False,  # disable reasoning tokens / <thinking> output
    )

    content = response['message']['content']
        
    return content

@tool
def meal_category_tagger_open(title: str, ingredient_names: list[str]) -> dict:
    """
    Classifies the recipe category (multiple tags possible) based on the recipe title.
    Categories: 'Appetizer', 'Main Course', 'Side Dish',
    'Desserts', 'Snacks', 'Brunch', 'Salads', 'Soups & Stews', 'Beverages and Cocktails'.
    """

    system_prompt = """
    You are an expert in classifying recipes into one or more of these categories:
    - Appetizer
    - Main Course
    - Side Dish
    - Desserts
    - Snacks
    - Brunch
    - Salads
    - Soups & Stews
    - Beverages and Cocktails

    More than one tag can be applied. Only return the matching categories, separated by commas.

    Examples:
    Title: "Fruit Salad"
    Ingredients: ["apple", "grapes", "orange", "mint"]
    → Salads, Snacks

    Title: "Beef Stew"
    Ingredients: ["beef", "potatoes", "carrots", "onions"]
    → Main Course, Soups & Stews
    """

    user_prompt = f"""
    Title: {title}
    Ingredients: {ingredient_names}
    →
    """

    response = chat(
        model='qwen3:8b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        think=False,  # disable reasoning tokens / <thinking> output
    )

    content = response['message']['content']
    
    return content

@tool
def main_ingredient_tagger_open(ingredient_names: list[str]) -> dict:
    """
    Identifies the main ingredient and presence of pasta, rice, or potatoes in the recipe ingredients.
    Simplifies the main ingredient.
    """

    system_prompt = """
    You are a cooking expert that can identify the main ingredient(s) in a recipe and detect the presence of:
    - pasta
    - rice
    - potatoes

    Simplify ingredient names. Return a comma-separated list of the main ingredient(s). No explanations.

    Examples:
    Ingredients: ["chicken breast", "rice", "carrots"]
    → Chicken, Rice

    Ingredients: ["penne pasta", "parmesan", "basil"]
    → Pasta

    Ingredients: ["salmon", "asparagus", "olive oil"]
    → Salmon
    """

    user_prompt = f"""
    Ingredients: {ingredient_names}
    →
    """

    response = chat(
        model='qwen3:8b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        think=False,  # disable reasoning tokens / <thinking> output
    )

    content = response['message']['content']
    
    return content

@tool
def allergens_tagger_open(ingredient_names: list[str]) -> dict:
    """
    Identifies possible allergens in the recipe ingredients.
    """

    system_prompt = """
    You are a food expert that can identify possible allergens in a recipe based on its ingredients.

    Common allergens include:
    - 'Milk'
    - 'Egg'
    - 'Peanut'
    - 'Tree Nut'
    - 'Shellfish'
    - 'Soy'
    - 'Wheat'
    - 'Sesame'

    Return a comma-separated list of any allergens you identify. If none are present, return: No allergens
    """

    user_prompt = f"""
    Ingredients: {ingredient_names}
    →
    """

    response = chat(
        model='qwen3:8b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        think=False,  # disable reasoning tokens / <thinking> output
    )

    content = response['message']['content']
    
    return content

@tool
def techniques_tagger_open(directions: list[str]) -> dict:
    """
    Identifies cooking techniques based on verbs and actions in the recipe directions.
    """

    system_prompt = """
    You are an expert at identifying cooking techniques from recipe directions.

    Your job is to extract common techniques such as:
    - baking
    - roasting
    - grilling
    - frying
    - sautéing
    - boiling
    - steaming
    - poaching
    - simmering
    - broiling
    - blanching
    - slow cooking
    - deep-frying

    Return only the list of techniques you identify, separated by commas.

    If no cooking technique is clearly described, return: No cook

    Examples:
    Directions: ["Preheat the oven to 375°F. Bake for 20 minutes."]
    → Baking

    Directions: ["Toss the salad and serve immediately."]
    → No cook

    Directions: ["Boil the potatoes, then mash them with butter."]
    → Boiling

    Directions: ["Grill the chicken over medium heat for 10 minutes."]
    → Grilling
    """

    user_prompt = f"""
    Directions: {directions}
    →
    """

    response = chat(
        model='qwen3:8b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        think=False,  # disable reasoning tokens / <thinking> output
    )

    content = response['message']['content']

    return content

@tool
def free_tagger_open(ingredient_names: list[str]) -> dict:
    """
    Tags the recipe as 'Dairy Free' or 'Gluten Free' based on the ingredients.
    """

    system_prompt = """
    You are an expert at identifying whether a recipe is Dairy Free and/or Gluten Free based on its ingredients.

    Rules:
    - If no dairy ingredients (e.g., milk, cheese, butter, yogurt, cream) are present, return: Dairy Free
    - If no gluten-containing ingredients (e.g., wheat, flour, bread, pasta, soy sauce) are present, return: Gluten Free
    - If both apply, return both tags, separated by commas.
    - If neither apply, return nothing.

    Examples:
    Ingredients: ["chicken breast", "rice", "carrots"]
    Return: Dairy Free, Gluten Free

    Ingredients: ["bread", "cheddar cheese", "butter"]
    Return: 

    Ingredients: ["almond milk", "quinoa", "berries"]
    Return: Dairy Free, Gluten Free

    Ingredients: ["gluten-free pasta", "tomato sauce", "parmesan"]
    Return: Dairy Free

    Return only the final tag(s), separated by commas.
    Do NOT explain. Do NOT correct. Do NOT include markdown. Do not say your way of thinking. Do NOT say "Final Answer" or anything else.


    """

    user_prompt = f"""
    Ingredients: {ingredient_names}
    →
    """

    response = chat(
        model='qwen3:8b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        think=False,  # disable reasoning tokens / <thinking> output
    )

    content = response['message']['content']

    return content

@tool 
def time_tag_tool(total_time: int) -> dict:
    """
    Tags the recipe based on its total time in minutes.
    """
    tags = []
    total_minutes = total_time

    if total_minutes <= 15:
        tags.append('15-minutes-or-less')
    if total_minutes <= 30:
        tags.append('30-minutes-or-less')
    if total_minutes <= 60:
        tags.append('60-minutes-or-less')
    if total_minutes <= 240:
        tags.append('4-hours-or-less')
    if total_minutes >= 1440:
        tags.append('1-day-or-more')

    return tags

@tool
def ingredient_number_tagger(ingredient_names: list[str]) -> dict:
    """
    Tags the recipe based on the number of ingredients.
    """
    tags = []

    if isinstance(ingredient_names, list) and len(ingredient_names) <= 5:
        tags.append('5-ingredients-or-less')

    return tags

@tool
def steps_tagger(directions: list[str]) -> dict:
    """
    Tags the recipe based on the number of steps in the directions.
    """

    tags = []
    
    if isinstance(directions, str):
        directions = eval(directions)  # Only use eval if you're sure the string represents a valid list
    
    if isinstance(directions, list) and len(directions) <= 5:
        tags.append('Easy')

    return tags

@tool
def region_tagger_open(title: str) -> dict:
    """
    Identifies the region of the recipe based on its title.
    """

    system_prompt = """
    You are an expert in recognizing the regional origin of recipe names.

    Based on the recipe title, identify its region or cuisine. Return one of the following types of regional tags if applicable:
    Examples: Italian, Mexican, Thai, Indian, Greek, Chinese, French, Japanese, Moroccan, etc.

    If the title is generic and does not imply a regional cuisine, return nothing.

    Examples:
    Title: "Spaghetti Carbonara"
    Return: Italian

    Title: "Chicken Tikka Masala"
    Return: Indian

    Title: "Vegetable Stir Fry"
    Return: Chinese

    Title: "Easy Baked Potatoes"
    Return: American
    """

    user_prompt = f"""
    Title: {title}
    →
    """

    response = chat(
        model='qwen3:8b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        think=False,  # disable reasoning tokens / <thinking> output
    )

    content = response['message']['content']
    cleaned = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

    return cleaned

def safe_to_str(value):

    if isinstance(value, dict):

        if 'main_ingredient_tags' in value:

            tags_list = value['main_ingredient_tags']

            if isinstance(tags_list, list):
                unique_tags = sorted(set(tags_list), key=tags_list.index)  # keep order
                return ", ".join(unique_tags)
            else:
                return str(tags_list)
            
        if 'answer' in value:
            value = value['answer']
        else:
            return str(value)
    
    if isinstance(value, list):
        unique_tags = sorted(set(value), key=value.index)  # preserve order
        return ", ".join(str(v) for v in unique_tags)
    
    if not isinstance(value, str):
        value = str(value)
    
    return value

def Tagger_Node_open(state: dict) -> dict:

    debug = state.get("debug", False)
    title = state.get("title", "")
    ingredient_names = state.get("ingredient_names", [])
    directions = state.get("directions", [])
    total_time = state.get("total_time")

    diet_tags = diet_type_tagger_open.invoke({
        "title": title,
        "ingredient_names": ingredient_names
    })
    
    dish_tags = dish_type_tagger_open.invoke({
        "title": title,
        "ingredient_names": ingredient_names
    })
    
    meal_category_tags = meal_category_tagger_open.invoke({
        "title": title,
        "ingredient_names": ingredient_names
    })

    main_ingr_tags = main_ingredient_tagger_open.invoke({
        "ingredient_names": ingredient_names
    })

    allergens_tags = allergens_tagger_open.invoke({
        "ingredient_names": ingredient_names
    })
    
    free_tags = free_tagger_open.invoke({
        "ingredient_names": ingredient_names
    })

    techniques_tags = techniques_tagger_open.invoke({
        "directions": directions
    })

    time_tags = time_tag_tool.invoke({
        "total_time": total_time
    })

    ingredient_number_tags = ingredient_number_tagger.invoke({
        "ingredient_names": ingredient_names
    })

    steps_tags = steps_tagger.invoke({
        "directions": directions
    })

    region_tags = region_tagger_open.invoke({
        "title": title
    })

    diet_tag_str = safe_to_str(diet_tags)
    dish_tag_str = safe_to_str(dish_tags)
    meal_category_tag_str = safe_to_str(meal_category_tags)
    main_ingr_tags_str = safe_to_str(main_ingr_tags)
    allergens_tags_str = safe_to_str(allergens_tags)
    free_tags_str = safe_to_str(free_tags)
    techniques_tags_str = safe_to_str(techniques_tags)
    time_tags_str = safe_to_str(time_tags)
    ingr_number_tags_str = safe_to_str(ingredient_number_tags)
    steps_tags_str = safe_to_str(steps_tags)
    region_tags_str = safe_to_str(region_tags)

    # Populate a separate 'allergens' field and do NOT mix into tags
    allergens_value = ""
    if isinstance(allergens_tags_str, str) and allergens_tags_str.strip():
        if allergens_tags_str.strip().lower() != "no allergens":
            # Normalize and deduplicate allergens
            parts = [p.strip() for p in allergens_tags_str.split(",") if p.strip()]
            seen_allergens = []
            seen_set = set()
            for p in parts:
                if p not in seen_set:
                    seen_allergens.append(p)
                    seen_set.add(p)
            allergens_value = ", ".join(seen_allergens)
        else:
            allergens_value = ""
    state["allergens"] = allergens_value

    existing_tags = state.get("tags", "")
    
    combined_list = []
    for part in [
        existing_tags, diet_tag_str, dish_tag_str, meal_category_tag_str, main_ingr_tags_str,
        free_tags_str, techniques_tags_str, time_tags_str,
        ingr_number_tags_str, steps_tags_str, region_tags_str
    ]:
        if part:
            combined_list.extend([tag.strip() for tag in part.split(",") if tag.strip()])

    expanded_tags = []
    for tag in combined_list:
        if "," in tag:
            expanded_tags.extend([t.strip() for t in tag.split(",") if t.strip()])
        else:
            expanded_tags.append(tag)

    unique_tags = []
    seen = set()
    for tag in expanded_tags:
        if tag not in seen:
            unique_tags.append(tag)
            seen.add(tag)

    state["tags"] = ", ".join(unique_tags)
    
    if debug:
        print(f"[Tagger_Node] Tags calculated for recipe: {title}")
        print("[Tagger_Node] Updated State Keys:", state.keys())
        if state.get("allergens"):
            print(f"[Tagger_Node] Allergens detected: {state['allergens']}")
        else:
            print("[Tagger_Node] No allergens detected")

    return state
