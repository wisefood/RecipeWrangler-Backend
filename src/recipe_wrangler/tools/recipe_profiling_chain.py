# Purpose: LangGraph pipeline that parses, weights, and profiles recipes.

from __future__ import annotations

import re
from typing import List, Optional

from IPython.display import Image, display
from langchain.tools import tool
from langgraph.graph import END, START, StateGraph

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.tools.ingredient_weight_tool import Ingredient_Weight_Node
from recipe_wrangler.tools.parse_recipe_tool import Recipe_Parser_Node
from recipe_wrangler.tools.recipe_profiling_tool import Recipe_Profiling_Node

# ---------------------------------------------------------------------------
# Ingredient-line splitting (used by structured pipeline to avoid LLM parse)
# ---------------------------------------------------------------------------

_QTY_RE = re.compile(
    r"^\s*"
    r"(?:[0-9]+\s+)?[0-9]+/[0-9]+"   # e.g. "1 1/2"  or "3/4"
    r"|"
    r"[0-9]+\s+[½⅓⅔¼¾⅛⅜⅝⅞]"        # e.g. "2 ¼"
    r"|"
    r"[0-9]+(?:\.[0-9]+)?"             # plain integer or decimal
    r"|"
    r"[½⅓⅔¼¾⅛⅜⅝⅞]"                  # unicode fractions
)

_UNIT_WORDS = {
    "cup", "cups", "tbsp", "tablespoon", "tablespoons",
    "tsp", "teaspoon", "teaspoons", "oz", "ounce", "ounces",
    "lb", "lbs", "pound", "pounds", "g", "gram", "grams",
    "kg", "ml", "l", "liter", "litre", "liters", "litres",
    "clove", "cloves", "slice", "slices", "piece", "pieces",
    "can", "cans", "bunch", "handfuls", "handful", "pinch",
    "dash", "sprig", "sprigs", "stalk", "stalks", "head", "heads",
    "large", "medium", "small", "package", "packages", "packet", "packets",
    "box", "boxes", "jar", "jars", "container", "containers", "carton", "cartons",
    "fillet", "fillets", "filet", "filets", "chop", "chops", "bun", "buns",
    "sheet", "sheets", "cube", "cubes", "bulb", "bulbs", "breast", "breasts",
    "prawn", "prawns", "muffin", "muffins", "envelope", "envelopes", "block",
    "blocks", "punnet", "punnets",
}
_MULTIWORD_UNITS = (
    "us fluid ounces",
    "fluid ounces",
    "fluid ounce",
    "fl oz",
)

_SPLIT_RE = re.compile(
    r"^\s*"
    r"(?:(?:[0-9]+\s+)?[0-9]+/[0-9]+|[0-9]+\s+[½⅓⅔¼¾⅛⅜⅝⅞]|[0-9]+(?:\.[0-9]+)?|[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"(?:\s*[-–—to]+\s*(?:(?:[0-9]+\s+)?[0-9]+/[0-9]+|[0-9]+(?:\.[0-9]+)?))?"
    r"\s*"
    r"(?P<rest>.*)"
)
_MULTIPLIER_MASS_RE = re.compile(
    r"^\s*(?P<count>\d+(?:\.\d+)?)\s*x\s*(?P<size>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>g|grams?|kg|oz|ounces?|lb|lbs|pounds?)\s+(?P<name>.+)$",
    re.IGNORECASE,
)
_PACKAGE_AFTER_MASS_RE = re.compile(
    r"^\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<mass_unit>g|grams?|kg|oz|ounces?|lb|lbs|pounds?)\s+"
    r"(?P<package>can|cans|package|packages|packet|packets|box|boxes|jar|jars|container|containers|carton|cartons)\s+"
    r"(?P<name>.+)$",
    re.IGNORECASE,
)
_SLASHLESS_MIXED_FRACTION_RE = re.compile(
    r"^\s*(?P<whole>\d+)\s+(?P<num>\d)(?P<den>[2348])\s+"
    r"(?P<unit>cups?|tbsp|tablespoons?|tsp|teaspoons?)\b",
    re.IGNORECASE,
)
_SLASHLESS_SIMPLE_FRACTION_RE = re.compile(
    r"^\s*(?P<num>\d)(?P<den>[2348])\s+"
    r"(?P<unit>cups?|tbsp|tablespoons?|tsp|teaspoons?)\b",
    re.IGNORECASE,
)
_SLASHLESS_MIXED_FRACTION_MASS_RE = re.compile(
    r"^\s*(?P<whole>\d+)\s+(?P<num>\d)(?P<den>[24])\s+"
    r"(?P<unit>lbs?|pounds?)\b",
    re.IGNORECASE,
)
_SLASHLESS_SIMPLE_FRACTION_MASS_RE = re.compile(
    r"^\s*(?P<num>\d)(?P<den>[24])\s+"
    r"(?P<unit>lbs?|pounds?)\b",
    re.IGNORECASE,
)
_SLASHLESS_RANGE_FRACTION_VOLUME_RE = re.compile(
    r"^\s*(?P<n1>\d)(?P<d1>[2348])\s*-\s*"
    r"(?:(?P<n2>\d)(?P<d2>[2348])(?=\s)|(?P<int2>\d+))\s+"
    r"(?P<unit>cups?|tbsp|tablespoons?|tsp|teaspoons?)\b",
    re.IGNORECASE,
)
_SLASHLESS_RANGE_FRACTION_MASS_RE = re.compile(
    r"^\s*(?P<n1>\d)(?P<d1>[24])\s*-\s*"
    r"(?:(?P<n2>\d)(?P<d2>[24])(?=\s)|(?P<int2>\d+))\s+"
    r"(?P<unit>lbs?|pounds?)\b",
    re.IGNORECASE,
)
_INGREDIENT_BOUNDARY_RE = re.compile(
    r"(?<=[a-z\)])(?=(?:\d+(?:\s+[½⅓⅔¼¾⅛⅜⅝⅞]|\s+\d+/\d+)?|[½⅓⅔¼¾⅛⅜⅝⅞])\s*"
    r"(?:cup|cups|tbsp|tablespoons?|tsp|teaspoons?|oz|ounces?|lb|lbs|pounds?|g|grams?|kg|ml|l|"
    r"can|cans|clove|cloves|slice|slices|stalk|stalks|sprig|sprigs|teaspoon|tablespoon)\b)",
    re.IGNORECASE,
)


def _split_ingredient_line(line: str) -> tuple[str, str]:
    """Split an ingredient line into (measurement, ingredient_name).

    E.g. "1 cup flour" → ("1 cup", "flour")
         "2 tbsp olive oil, chopped" → ("2 tbsp", "olive oil, chopped")
         "salt to taste" → ("", "salt to taste")
    """
    line = _repair_slashless_fraction_prefix(line.strip())
    multiplier = _MULTIPLIER_MASS_RE.match(line)
    if multiplier:
        total = float(multiplier.group("count")) * float(multiplier.group("size"))
        return (f"{total:g} {multiplier.group('unit')}", multiplier.group("name").strip())

    package_after_mass = _PACKAGE_AFTER_MASS_RE.match(line)
    if package_after_mass:
        return (
            f"{package_after_mass.group('qty')} {package_after_mass.group('mass_unit')}",
            package_after_mass.group("name").strip(),
        )

    m = _SPLIT_RE.match(line)
    if not m:
        return ("", line)
    rest = m.group("rest").strip()
    rest_lower = rest.lower()
    for unit in _MULTIWORD_UNITS:
        if rest_lower.startswith(unit):
            name = rest[len(unit):].strip()
            measurement_part = line[: m.start("rest")] + rest[: len(unit)]
            return (measurement_part.strip(), name)

    # rest may start with a unit word
    tokens = rest.split(None, 1)
    if tokens and tokens[0].lower().rstrip(".") in _UNIT_WORDS:
        unit = tokens[0]
        name = tokens[1].strip() if len(tokens) > 1 else ""
        measurement_part = line[: m.start("rest")] + unit
        return (measurement_part.strip(), name)
    # no unit — qty only
    measurement_part = line[: m.start("rest")]
    return (measurement_part.strip(), rest)


def _repair_slashless_fraction_prefix(line: str) -> str:
    """Repair common Recipe1M fraction artifacts like "34 cup" -> "3/4 cup"."""
    range_vol = _SLASHLESS_RANGE_FRACTION_VOLUME_RE.match(line)
    if range_vol:
        rhs = (
            range_vol.group("int2")
            if range_vol.group("int2")
            else f"{range_vol.group('n2')}/{range_vol.group('d2')}"
        )
        repaired = (
            f"{range_vol.group('n1')}/{range_vol.group('d1')}-{rhs} "
            f"{range_vol.group('unit')}"
        )
        return repaired + line[range_vol.end():]

    range_mass = _SLASHLESS_RANGE_FRACTION_MASS_RE.match(line)
    if range_mass:
        rhs = (
            range_mass.group("int2")
            if range_mass.group("int2")
            else f"{range_mass.group('n2')}/{range_mass.group('d2')}"
        )
        repaired = (
            f"{range_mass.group('n1')}/{range_mass.group('d1')}-{rhs} "
            f"{range_mass.group('unit')}"
        )
        return repaired + line[range_mass.end():]

    mixed = _SLASHLESS_MIXED_FRACTION_RE.match(line)
    if mixed:
        repaired = (
            f"{mixed.group('whole')} {mixed.group('num')}/{mixed.group('den')} "
            f"{mixed.group('unit')}"
        )
        return repaired + line[mixed.end():]

    simple = _SLASHLESS_SIMPLE_FRACTION_RE.match(line)
    if simple:
        repaired = f"{simple.group('num')}/{simple.group('den')} {simple.group('unit')}"
        return repaired + line[simple.end():]

    mixed_mass = _SLASHLESS_MIXED_FRACTION_MASS_RE.match(line)
    if mixed_mass:
        repaired = (
            f"{mixed_mass.group('whole')} {mixed_mass.group('num')}/{mixed_mass.group('den')} "
            f"{mixed_mass.group('unit')}"
        )
        return repaired + line[mixed_mass.end():]

    simple_mass = _SLASHLESS_SIMPLE_FRACTION_MASS_RE.match(line)
    if simple_mass:
        repaired = (
            f"{simple_mass.group('num')}/{simple_mass.group('den')} "
            f"{simple_mass.group('unit')}"
        )
        return repaired + line[simple_mass.end():]
    return line


def split_ingredient_lines(lines: List[str]) -> tuple[List[str], List[str]]:
    """Return (ingredient_names, measurements) from a list of ingredient strings."""
    names: List[str] = []
    measurements: List[str] = []
    for line in lines:
        parts = [p.strip() for p in _INGREDIENT_BOUNDARY_RE.split(str(line or "")) if p.strip()]
        if not parts:
            parts = [str(line or "").strip()]
        for part in parts:
            meas, name = _split_ingredient_line(part)
            names.append(name or part.strip())
            measurements.append(meas or part.strip())
    return names, measurements


# ---------------------------------------------------------------------------
# Pipeline builders
# ---------------------------------------------------------------------------

def build_pipeline():
    builder = StateGraph(RecipeState)

    builder.add_node("Recipe_Parser", Recipe_Parser_Node)
    builder.add_node("Weight_Calculator", Ingredient_Weight_Node)
    builder.add_node("Recipe_Profiling_Node", Recipe_Profiling_Node)  # <-- name matches edges

    builder.add_edge(START, "Recipe_Parser")
    builder.add_edge("Recipe_Parser", "Weight_Calculator")
    builder.add_edge("Weight_Calculator", "Recipe_Profiling_Node")
    builder.add_edge("Recipe_Profiling_Node", END)

    graph = builder.compile()
    return graph


def build_pipeline_without_parse():
    """Weight + Profiling only — skips the LLM parse step.

    Requires the caller to pre-populate RecipeState with:
      - ingredient_names (list[str])
      - measurements (list[str])
      - serves (float)
      - title (str)
      - region (str)
    """
    builder = StateGraph(RecipeState)

    builder.add_node("Weight_Calculator", Ingredient_Weight_Node)
    builder.add_node("Recipe_Profiling_Node", Recipe_Profiling_Node)

    builder.add_edge(START, "Weight_Calculator")
    builder.add_edge("Weight_Calculator", "Recipe_Profiling_Node")
    builder.add_edge("Recipe_Profiling_Node", END)

    return builder.compile()


# ---------------------------------------------------------------------------
# Public @tool wrappers
# ---------------------------------------------------------------------------

@tool
def Recipe_Profiling_Chain(
    recipe_text: str,
    debug: bool = True,
    region: str = "IE",
    trusted_serves: Optional[float] = None,
):
    """
    Parses unstructured recipe text and extracts structured metadata including
    ingredients, instructions, nutrition, and sustainability data.
    """
    graph = build_pipeline()
    normalized_region = (region or "IE").strip().upper()
    source = (
        "irish"
        if normalized_region == "IE"
        else ("usda" if normalized_region == "US" else ("hungarian" if normalized_region == "HU" else ("eu" if normalized_region == "EU" else None)))
    )
    initial_state = RecipeState(
        raw_recipe=recipe_text,
        debug=debug,
        region=normalized_region,
        source=source,
        trusted_serves=float(trusted_serves) if trusted_serves else None,
    )
    final_state = graph.invoke(initial_state)
    if not isinstance(final_state, RecipeState):
        final_state = RecipeState.model_validate(final_state)

    # Normalize tags into a list prior to filtering the final state
    tags = final_state.tags
    normalized_tags = []
    if isinstance(tags, str):
        normalized_tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    elif isinstance(tags, list):
        normalized_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    elif tags:
        normalized_tags = [str(tags).strip()]

    final_state.tags = normalized_tags

    # Normalize allergens into a list for the returned payload
    allergens = final_state.allergens
    normalized_allergens = []
    if isinstance(allergens, str):
        normalized_allergens = [item.strip() for item in allergens.split(",") if item.strip()]
    elif isinstance(allergens, list):
        normalized_allergens = [str(item).strip() for item in allergens if str(item).strip()]
    elif allergens:
        normalized_allergens = [str(allergens).strip()]
    final_state.allergens = normalized_allergens

    # Drop raw text from the returned dict
    filtered_state = final_state.model_dump(exclude={"raw_recipe", "trusted_serves"})
    return filtered_state


@tool
def Recipe_Profiling_Chain_Structured(
    title: str,
    ingredient_names: List[str],
    measurements: List[str],
    serves: float,
    total_time: Optional[float] = None,
    directions: Optional[List[str]] = None,
    region: str = "IE",
    debug: bool = False,
):
    """
    Run weight estimation and nutrition/sustainability profiling on pre-structured
    recipe data, skipping the LLM parse step.

    Use this when ingredient names and measurements are already known (e.g. when
    importing recipes from structured JSON datasets). The caller must split
    ingredient lines into separate ``ingredient_names`` and ``measurements`` lists
    before calling this tool (use ``split_ingredient_lines`` for convenience).

    Args:
        title: Recipe title.
        ingredient_names: Clean ingredient names (e.g. ["flour", "olive oil"]).
        measurements: Quantity+unit strings matching each name (e.g. ["2 cups", "1 tbsp"]).
        serves: Number of servings.
        total_time: Total cooking time in minutes (optional).
        directions: List of instruction steps (optional, not used for nutrition).
        region: Nutrition source region — "IE" (Irish), "US" (USDA), "HU" (Hungarian).
        debug: Emit debug output from pipeline nodes.
    """
    graph = build_pipeline_without_parse()
    normalized_region = (region or "IE").strip().upper()
    source = (
        "irish"
        if normalized_region == "IE"
        else ("usda" if normalized_region == "US" else ("hungarian" if normalized_region == "HU" else ("eu" if normalized_region == "EU" else None)))
    )
    initial_state = RecipeState(
        title=title,
        ingredient_names=ingredient_names,
        measurements=measurements,
        serves=float(serves),
        total_time=float(total_time) if total_time is not None else None,
        directions=directions or [],
        debug=debug,
        region=normalized_region,
        source=source,
    )
    final_state = graph.invoke(initial_state)
    if not isinstance(final_state, RecipeState):
        final_state = RecipeState.model_validate(final_state)
    return final_state.model_dump(exclude={"raw_recipe"})


def visualize_pipeline_graph():
    graph = build_pipeline()
    display(Image(graph.get_graph().draw_mermaid_png()))
