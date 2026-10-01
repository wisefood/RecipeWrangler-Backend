"""Create a minimally corrected parser snapshot for deterministic weighting.

The paid parser output remains immutable in ``parsed/``. This script removes
only byte-for-byte identical adjacent rows (a confirmed HealthyFoods scrape
duplication) and applies a short list of source-verified measurement repairs.
"""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

from recipe_wrangler.tools.parse_recipe_tool import (
    _assign_lines_to_entries,
    restore_bouillon_form,
    restore_explicit_source_measurement,
    restore_greasing_measurement,
    restore_leading_quantity_from_source,
    restore_stock_cube_measurement,
    restore_whole_bird_name,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "data/processed/ingredient_parsing_final/2026-09-24"
INPUT_ROOT = SNAPSHOT_ROOT / "parsed"
OUTPUT_ROOT = SNAPSHOT_ROOT / "weight_ready_parsed"

RAW_SOURCE_PATHS = {
    "healthyfoods_final_parsed_fixed.json": [REPO_ROOT / "data/HealthyFoods/HealthyFood_recipes.json"],
    "healthyfoods_remaining_a-n_parsed.json": [REPO_ROOT / "data/HealthyFoods/HealthyFood_recipes.json"],
    "myplate_final_parsed.json": [REPO_ROOT / "data/MyPlate/myplate_recipes.json"],
    "foodhero_final_parsed.json": [REPO_ROOT / "data/FoodHero/foodhero_recipes.json"],
    "best_of_hungary_final_parsed.json": [REPO_ROOT / "data/BestOfHungary/bestofhungary.json"],
    "hungary_soul_final_parsed.json": [REPO_ROOT / "data/TheHungarySoul/thehungarysoul.json"],
    "safefood_v1_parsed.json": sorted((REPO_ROOT / "data/SafeFood_web").glob("*_recipes.json")),
    "slovenian_v1_parsed.json": [REPO_ROOT / "data/SlovenianKitchen/slovenian-kitchen.json"],
    "supervalu_v3_parsed.json": [REPO_ROOT / "data/SuperValu/supervalu.json"],
    "irishheart_v3_parsed.json": [REPO_ROOT / "data/IrishHeart/irish-heart.json"],
}

_DISCRETIONARY_NOTE_RE = re.compile(
    r"\b(?:to taste|to season|optional|garnish|to garnish|for garnishing|"
    r"to serve|for serving|as needed|"
    r"for drizzling|for topping|for dusting|to dust)\b",
    re.IGNORECASE,
)
_NON_INGREDIENT_NAMES = {"instructions", "information", "garnish"}


_VOLUME_STOCK_CUBE_RE = re.compile(r".*\b(?:stock|bouillon)\s+cubes?$", re.IGNORECASE)
_VOLUME_MEASUREMENT_RE = re.compile(r"\b(?:ml|l|litres?|liters?|cups?|pints?)\s*$", re.IGNORECASE)


def _title_key(value: object) -> str:
    text = re.sub(r"\s+", " ", unescape(str(value or "")).strip().casefold())
    # SafeFood parsed titles carry a meal-type suffix the raw source lacks.
    return re.sub(r"\s*\((?:breakfast|lunch|dinner|snack)\)$", "", text)


def _load_raw_source_lines() -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for output_name, paths in RAW_SOURCE_PATHS.items():
        titles: dict[str, list[str]] = {}
        for path in paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            recipes = payload.values() if isinstance(payload, dict) else payload
            for recipe in recipes:
                title = recipe.get("title") or recipe.get("name")
                lines = []
                for raw_line in recipe.get("ingredients") or []:
                    line = str(raw_line)
                    if not lines or line != lines[-1]:
                        lines.append(line)
                if title and lines:
                    titles.setdefault(_title_key(title), lines)
        result[output_name] = titles
    return result

# (file, recipe title, ingredient, old measurement) -> new measurement
VERIFIED_REPAIRS = {
    # Raw source line gives the explicit weight ("1 sweet potato / 130g / 4.5 oz")
    # that the LLM parse discarded, outputting "1.0 g" (a bare count treated as
    # 1 gram) instead of the stated 130g.
    ("safefood_v1_parsed.json", "Sweet Potato Fries (dinner)", "sweet potato", "1.0 g"): "130.0 g",
    # Raw source: "4 skinless chicken breasts, 520g / 1lb 2 1/2 oz." -- same bug,
    # the count (4) was output as the gram measurement instead of the stated 520g.
    ("safefood_v1_parsed.json", "Baked garlic lime chicken breasts (dinner)", "chicken breasts", "4.0 g"): "520.0 g",
    ("safefood_v1_parsed.json", "Stephen McAllister's thai chilli chicken (dinner)", "broccoli", "3.5"): "3.5 floret",
    # Verified against the raw source line ("6 whole wheat buns , split in half to make 12",
    # "1 to 2 hot peppers", "1 punnet (1 ½ cups) fresh strawberries", "450 pouch ... rice").
    ("foodhero_final_parsed.json", "Garden Sloppy Joes", "whole wheat buns", "12.0"): "6.0",
    ("foodhero_final_parsed.json", "Cucumber, Pineapple and Serrano Chile Salad", "hot peppers", "3.0"): "1.5",
    ("healthyfoods_final_parsed_fixed.json", "Super-quick strawberry brûlées", "strawberries", "3.0 cups"): "1.5 cups",
    ("healthyfoods_final_parsed_fixed.json", "Sweet and spicy fish stir-fry", "basmati rice", "1.0 pouch"): "450.0 g",
    # Whole bird weight read from the raw source line, not the count or a range end.
    ("safefood_v1_parsed.json", "Roast chicken with lemon, herbs and pepper (dinner)", "chicken", "1.0 kg"): "1.5 kg",
    ("safefood_v1_parsed.json", "Roast chicken with roasted vegetables and potatoes (dinner)", "chicken", "1.0 lb"): "1.5 kg",
    ("slovenian_v1_parsed.json", "Sunday Lunch", "chicken", "1.0 kg"): "1.5 kg",
    # Parser took the "or <alternative>" clause instead of the primary item.
    ("safefood_v1_parsed.json", "Bang bang chicken salad (lunch)", "chicken breast", "100.0 g"): "1.0 small",
    ("safefood_v1_parsed.json", "Chicken and ginger curry with fragrant rice (dinner)", "onion", "2.0 medium"): "1.0 large",
    ("foodhero_final_parsed.json", "Eggplant Dip", "eggplant", "2.0"): "1.0 large",
    ("foodhero_final_parsed.json", "Spinach and Black Bean Enchiladas", "enchilada sauce", "28.0 oz"): "3.0 cup",
    (
        "safefood_v1_parsed.json",
        "Chicken Caesar salad (lunch)",
        "cos lettuce",
        "1.0 large",
    ): "500.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Salmon, celeriac and leek chowder",
        "celeriac",
        "1.0",
    ): "350.0 g",
    (
        "safefood_v1_parsed.json",
        "Baked cod with lemon and olive oil (dinner)",
        "cod fillet",
        "1.0 large",
    ): "175.0 g",
    (
        "irishheart_v3_parsed.json",
        "Butternut Squash Soup",
        "turnip",
        "1.0 small",
    ): "160.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Vegan walnut and pear cake",
        "pears",
        "3.0 small-medium",
    ): "450.0 g",
    (
        "foodhero_final_parsed.json",
        "Pumpkin Ricotta Stuffed Shells",
        "pasta shells",
        "12.0 jumbo",
    ): "170.097 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Quinoa bowl with grilled chicken, black beans and avocado",
        "chicken tenderloins",
        "8.0 small",
    ): "400.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Pan-seared fish with couscous tabbouleh salad",
        "fish",
        "4.0",
    ): "720.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Quick spring soup with dumplings",
        "vegetarian dumplings",
        "4.0",
    ): "280.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Warm black bean and kale couscous with grilled prawns",
        "kale",
        "0.5 large",
    ): "100.0 g",
    (
        "safefood_v1_parsed.json",
        "Chicken Caesar salad (lunch)",
        "ciabatta loaf",
        "1.0 medium",
    ): "90.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Refreshing noodle salad",
        "cucumber",
        "10.0 piece",
    ): "10.0 cm",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Spring watercress soup",
        "salt",
        "400.0 teaspoon",
    ): "0.25 teaspoon",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Pork banh mi tacos",
        "soft tacos",
        "8.0",
    ): "208.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Prawn and mango salad with chilli lime dressing",
        "baby potatoes",
        "6.0",
    ): "420.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Quick bean and pasta soup",
        "kumara sweet-potato",
        "1.0 small",
    ): "250.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Quinoa, smoked salmon and shredded vegetable sushi rolls",
        "quinoa",
        "1.0 cup",
    ): "190.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Roasted whole cauliflower with tomato, fennel and chickpea galette",
        "cauliflower",
        "1.0",
    ): "1.0 kg",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Salmon fish cakes with dip and pita chips",
        "pita breads",
        "4.0",
    ): "160.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Salmon, tomato and asparagus pasta",
        "penne pasta",
        "4.0 cups",
    ): "400.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Semi-dried tomato, spinach and ricotta fritters",
        "spinach",
        "1.0 large",
    ): "350.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Soy-seared tuna with ginger rice and Asian greens",
        "tuna steak",
        "1.0 large",
    ): "250.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Spanish egg",
        "chopped tomatoes",
        "0.5",
    ): "200.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Spanish egg",
        "tuna",
        "95.0",
    ): "95.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Spiced prawn and pea pilaf",
        "king prawns",
        "350.0",
    ): "350.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Stir-fried barley with chicken and bok choy",
        "pearl barley",
        "1.0 cup",
    ): "200.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Tortilla wrap samosas",
        "peas",
        "0.5 cup",
    ): "75.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Tortilla wrap samosas",
        "spinach",
        "2.0 cups",
    ): "30.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Tofu satay noodle salad",
        "peas",
        "150.0 cup",
    ): "1.0 cup",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Tomato and basil chicken fettuccine",
        "basil",
        "400.0 cup",
    ): "0.5 cup",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Warm beef and lentil salad",
        "spinach",
        "400.0 cups",
    ): "4.0 cups",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Sticky hoisin chicken with sesame noodle coleslaw",
        "chicken tenderloins",
        "4800.0 g",
    ): "400.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Herby chicken, chickpeas and green beans",
        "chicken thighs",
        "2000.0 g",
    ): "500.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Paprika chicken with carrots and ricotta",
        "carrots",
        "1760.0 g",
    ): "440.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Peanut-chicken and noodle salad",
        "chicken breasts",
        "600.0 g",
    ): "300.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Potato-topped chicken pie",
        "chicken breasts",
        "1800.0 g",
    ): "600.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Prawn and scallop skewers",
        "prawns",
        "2400.0 g",
    ): "200.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Prawn and scallop skewers",
        "scallops",
        "2400.0 g",
    ): "200.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Prawn, corn and cauliflower chowder",
        "Agria potatoes",
        "600.0 g",
    ): "300.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Prune and cream cheese-stuffed chicken breast with roasted greens and toasted almonds",
        "kumara sweet-potato",
        "800.0 g",
    ): "400.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Simple chicken casserole",
        "chicken breasts",
        "1800.0 g",
    ): "600.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Soy beef and noodle stir-fry",
        "broccolini",
        "700.0 g",
    ): "350.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Spicy chicken and chickpeas",
        "chicken breasts",
        "1200g",
    ): "400.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Spring chicken with tahini-dressed salad",
        "baby potatoes",
        "400.0 g",
    ): "100.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Steak and roasted vegetable stack",
        "orange kumara sweet-potato",
        "800.0 g",
    ): "400.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Steak with olive and caper mayo",
        "potatoes",
        "500.0 g",
    ): "250.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Sticky lamb kebabs with greens",
        "bok choy",
        "900.0 g",
    ): "300.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Teriyaki salmon with noodle salad",
        "salmon fillets",
        "1600.0 g",
    ): "400.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Tropical fizz",
        "pineapple",
        "300.0 g",
    ): "150.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Tropical pops",
        "kiwi fruit",
        "300.0 g",
    ): "150.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Tuscan meatballs and spaghetti",
        "bacon",
        "150.0 g",
    ): "50.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "American beef burger",
        "cheddar cheese",
        "200.0 g",
    ): "50.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Vegetable carbonara",
        "pancetta",
        "200.0 g",
    ): "50.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Pan-fried fish with cauliflower puree and citrus caper salsa",
        "white fish fillets",
        "2.4 kg",
    ): "600.0 g",
    (
        "healthyfoods_final_parsed_fixed.json",
        "Roasted capsicum, pumpkin and tomato soup",
        "can tomatoes",
        "400.0",
    ): "400.0 g",
    (
        "myplate_final_parsed.json",
        "Jollof Rice",
        "water",
        "64.0 cups",
    ): "8.0 cups",
    (
        "myplate_final_parsed.json",
        "Veggie Loaded Mashed Potato Bowl",
        "chickpeas",
        "0.5 ounces",
    ): "7.5 ounces",
    (
        "best_of_hungary_final_parsed.json",
        "Töltött Paprika - Hungarian Stuffed Peppers",
        "ground pork",
        "453.59",
    ): "453.59 g",
    (
        "safefood_v1_parsed.json",
        "Smoked ham with cranberry chutney (dinner)",
        "smoked ham",
        "1.0",
    ): "1.0 kg",
    (
        "safefood_v1_parsed.json",
        "Smoked ham with cranberry chutney (dinner)",
        "cranberry sauce",
        "450.0",
    ): "450.0 g",
    (
        "safefood_v1_parsed.json",
        "Smoked ham with cranberry chutney (dinner)",
        "crushed pineapple",
        "225.0",
    ): "225.0 g",
    (
        "safefood_v1_parsed.json",
        "Smoked ham with cranberry chutney (dinner)",
        "horseradish",
        "140.0",
    ): "140.0 g",
    (
        "safefood_v1_parsed.json",
        "Stephen McAllister's thai chilli chicken (dinner)",
        "basmati rice",
        "240.0",
    ): "240.0 g",
    (
        "safefood_v1_parsed.json",
        "Vegetable paella (dinner)",
        "paella rice",
        "75.0",
    ): "75.0 g",
    (
        "irishheart_v3_parsed.json",
        "Orange Chicken Stir-Fry",
        "chicken fillets",
        "200.0",
    ): "200.0 g",
    # Quantities recovered from the preserved original source ingredient lines.
    ("healthyfoods_final_parsed_fixed.json", "Peach melba trifle", "raspberries", ""): "0.67 cup",
    ("healthyfoods_final_parsed_fixed.json", "Sake garlic chicken with spinach", "Seasonal salad", ""): "2.0 portion",
    ("healthyfoods_final_parsed_fixed.json", "Smoked chicken tartlets", "oil", ""): "2.0 teaspoon",
    ("healthyfoods_final_parsed_fixed.json", "Spiced lamb with Persian couscous", "reduced-fat Greek Yoghurt", ""): "135.0 ml",
    ("healthyfoods_final_parsed_fixed.json", "Summer vege pasta with tuna", "Basic chargrilled vegetable salad", ""): "1.0 portion",
    ("healthyfoods_final_parsed_fixed.json", "Summer vege pasta with tuna", "Basic lentil mix", ""): "1.0 portion",
    ("healthyfoods_final_parsed_fixed.json", "Sweet potato hash and smoky frijoles", "fresh coriander cilantro", ""): "1.0 bunch",
    ("healthyfoods_final_parsed_fixed.json", "White and green bean tortillas with crispy fish and corn and tomato salsa", "whole kernel corn", ""): "0.5 can",
    ("healthyfoods_final_parsed_fixed.json", "White chocolate raspberry jelly eggs", "raspberry jelly", ""): "1.0 sachet",
    ("healthyfoods_final_parsed_fixed.json", "Zim zam barbecue sauce", "golden syrup", ""): "1.0 tablespoon",
    ("best_of_hungary_final_parsed.json", "Kürtőskalács - Hungarian Chimney Cake", "sugar", ""): "1.0 teaspoon",
    ("slovenian_v1_parsed.json", "Burek", "canola oil", ""): "30.0 g",
    ("slovenian_v1_parsed.json", "Apple Strudel", "canola oil", ""): "15.0 g",
    ("slovenian_v1_parsed.json", "Slovenian Farmer’s Cheese Dumplings (Sirovi štruklji)", "canola oil", ""): "30.0 g",
    ("slovenian_v1_parsed.json", "Pasta with Truffles", "salt", ""): "1.0 teaspoon",
    ("slovenian_v1_parsed.json", "Chocolate Hedgehogs", "dark chocolate", ""): "50.0 g",
    ("slovenian_v1_parsed.json", "Linzer Cookies", "powdered sugar", ""): "100.0 g",
    ("irishheart_v3_parsed.json", "Keralan Curry", "coconut milk", ""): "1.0 tin",
}


EXCLUDED_RECIPES_PATH = SNAPSHOT_ROOT / "excluded_recipes.json"


def _load_excluded_recipes() -> set[tuple[str, str]]:
    """Recipes dropped from the weight-ready corpus because an ingredient has no weight.

    Decided 2026-09-25: the LLM could not resolve them and the rest need source data that does not
    exist (sub-recipe portions, unspecified mixes, unit-less counts). The immutable ``parsed/`` files
    still contain them; ``excluded_recipes.json`` lists each with its unresolved rows.
    """
    if not EXCLUDED_RECIPES_PATH.exists():
        return set()
    return {
        (row["source_file"], row["recipe_title"])
        for row in json.loads(EXCLUDED_RECIPES_PATH.read_text(encoding="utf-8"))
    }


def _signature(item: dict) -> tuple[str, str, str, str]:
    return tuple(
        str(item.get(key) or "").strip()
        for key in ("name", "measurement", "display", "note")
    )


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    applied: set[tuple[str, str, str, str]] = set()
    removed_adjacent_duplicates = 0
    files = 0
    recipes = 0
    ingredient_rows = 0
    restored_display_units = 0
    restored_source_units: dict[str, int] = {}
    marked_discretionary_quantities = 0
    removed_non_ingredient_rows = 0
    raw_source_restorations = 0
    raw_source_repairs: list[dict] = []
    raw_source_lines = _load_raw_source_lines()

    excluded_recipes = _load_excluded_recipes()
    excluded_count = 0
    for input_path in sorted(INPUT_ROOT.glob("*.json")):
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        kept = [r for r in payload if (input_path.name, r.get("title")) not in excluded_recipes]
        excluded_count += len(payload) - len(kept)
        payload[:] = kept
        for recipe in payload:
            repaired = []
            previous = None
            ingredients = recipe.get("ingredients") or []
            source_lines = raw_source_lines.get(input_path.name, {}).get(
                _title_key(recipe.get("title")),
                [],
            )
            assigned_source_lines = _assign_lines_to_entries(ingredients, source_lines)
            for item, raw_source_line in zip(ingredients, assigned_source_lines):
                current = dict(item)
                if str(current.get("name") or "").strip().casefold() in _NON_INGREDIENT_NAMES:
                    removed_non_ingredient_rows += 1
                    continue
                key = (
                    input_path.name,
                    str(recipe.get("title") or ""),
                    str(current.get("name") or ""),
                    str(current.get("measurement") or ""),
                )
                if key in VERIFIED_REPAIRS:
                    current["measurement"] = VERIFIED_REPAIRS[key]
                    applied.add(key)
                source_measurement, reason = restore_explicit_source_measurement(current)
                if source_measurement is not None:
                    current["measurement"] = source_measurement
                stock_cube = restore_stock_cube_measurement(
                    str(current.get("name") or ""),
                    raw_source_line if raw_source_line is not None else str(current.get("display") or ""),
                )
                if stock_cube is not None and stock_cube != current.get("measurement"):
                    current["measurement"] = stock_cube
                    reason = "stock_cube"
                    # The cube (dry, salty) must match a stock-cube food, not dilute prepared stock.
                    if "cube" not in str(current.get("name") or "").casefold():
                        current["name"] = f"{current['name']} cube"
                # SuperValu writes made-up stock as "1 litre Chicken Stock Cube": a volume of
                # prepared stock, not a cube (the cube rows are handled by restore_stock_cube_measurement).
                if _VOLUME_STOCK_CUBE_RE.match(str(current.get("name") or "")) and _VOLUME_MEASUREMENT_RE.search(
                    str(current.get("measurement") or "")
                ):
                    current["name"] = re.sub(r"\s+cubes?$", "", str(current["name"]), flags=re.IGNORECASE)
                greasing = restore_greasing_measurement(current)
                if greasing is not None:
                    current["measurement"] = greasing
                bouillon = restore_bouillon_form(current)
                if bouillon is not None:
                    current["name"], current["measurement"] = bouillon
                # "1 cube" of bouillon/stock is the dry concentrate, so name it as one.
                if (
                    re.search(r"\b(?:stock|bouillon|broth)$", str(current.get("name") or ""), re.IGNORECASE)
                    and re.search(r"\bcubes?\s*$", str(current.get("measurement") or ""), re.IGNORECASE)
                ):
                    current["name"] = f"{current['name']} cube"
                bird_name = restore_whole_bird_name(current)
                if bird_name is not None:
                    current["name"] = bird_name
                if raw_source_line is not None:
                    corrected = restore_leading_quantity_from_source(
                        str(current.get("measurement") or ""), raw_source_line, str(current.get("display") or "")
                    )
                    if corrected is not None:
                        raw_source_repairs.append({
                            "source_file": input_path.name,
                            "recipe_title": recipe.get("title"),
                            "ingredient": current.get("name"),
                            "old_measurement": current.get("measurement"),
                            "new_measurement": corrected,
                            "source_line": raw_source_line,
                            "reason": "source_leading_quantity",
                        })
                        current["measurement"] = corrected
                # A to-season/optional row must not borrow a quantity from a
                # same-name line ("1 teaspoon of salt") in the raw source.
                if raw_source_line is not None and not _DISCRETIONARY_NOTE_RE.search(
                    str(current.get("note") or "")
                ):
                    source_entry = dict(current)
                    source_entry["display"] = raw_source_line
                    recovered, recovered_reason = restore_explicit_source_measurement(
                        source_entry, allow_quantity_override=False
                    )
                    if recovered is not None and recovered != current.get("measurement"):
                        old_measurement = str(current.get("measurement") or "")
                        current["measurement"] = recovered
                        reason = recovered_reason
                        raw_source_restorations += 1
                        raw_source_repairs.append({
                            "source_file": input_path.name,
                            "recipe_title": recipe.get("title"),
                            "ingredient": current.get("name"),
                            "old_measurement": old_measurement,
                            "new_measurement": recovered,
                            "source_line": raw_source_line,
                            "reason": recovered_reason,
                        })
                if reason == "display_count":
                    restored_display_units += 1
                elif reason == "source_unit":
                    unit = str(current.get("measurement") or "").split()[-1]
                    restored_source_units[unit] = restored_source_units.get(unit, 0) + 1
                if (
                    not str(current.get("measurement") or "").strip()
                    and _DISCRETIONARY_NOTE_RE.search(str(current.get("note") or ""))
                ):
                    current["measurement"] = "optional"
                    marked_discretionary_quantities += 1
                signature = _signature(current)
                if signature == previous:
                    removed_adjacent_duplicates += 1
                    continue
                repaired.append(current)
                previous = signature
            recipe["ingredients"] = repaired
            recipes += 1
            ingredient_rows += len(repaired)
        output_path = OUTPUT_ROOT / input_path.name
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        files += 1

    # A repair for a recipe that was excluded from the corpus has nothing to apply to.
    missing = sorted(
        key for key in set(VERIFIED_REPAIRS) - applied if (key[0], key[1]) not in excluded_recipes
    )
    if missing:
        raise SystemExit(f"verified repairs not found in input: {missing}")

    report = {
        "files": files,
        "recipes": recipes,
        "ingredient_rows": ingredient_rows,
        "removed_adjacent_exact_duplicates": removed_adjacent_duplicates,
        "removed_non_ingredient_rows": removed_non_ingredient_rows,
        "source_verified_measurement_repairs": len(applied),
        "display_units_restored": restored_display_units,
        "source_units_restored": restored_source_units,
        "source_units_restored_total": sum(restored_source_units.values()),
        "raw_source_measurements_restored": raw_source_restorations,
        "raw_source_repairs": raw_source_repairs,
        "discretionary_blank_measurements_marked_optional": marked_discretionary_quantities,
        "excluded_recipes": excluded_count,
        "repairs": [
            {
                "source_file": key[0],
                "recipe_title": key[1],
                "ingredient": key[2],
                "old_measurement": key[3],
                "new_measurement": VERIFIED_REPAIRS[key],
            }
            for key in sorted(applied)
        ],
    }
    (OUTPUT_ROOT / "CORRECTIONS.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
