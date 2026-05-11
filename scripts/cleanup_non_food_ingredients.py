#!/usr/bin/env python3
"""Remove non-food "ingredient" lines (kitchen equipment, sprays, wrappers) from Neo4j.

Two-stage match so real food that merely *mentions* equipment in a note
("almonds, chopped in food processor", "egg, beaten, to brush pastry",
"1 cup quinoa, rinse with a fine-mesh strainer") is NOT swept in:

  1. A broad whole-word regex over Ingredient.name (the only thing Neo4j can
     pre-filter on) collects candidates.
  2. A candidate is *deletable* only if, after removing the matched blocklist
     terms, every remaining content word is a trivial qualifier (sizes, soak
     instructions, "non-stick", brand names, …). Anything with a leftover real
     word ("chicken", "quinoa", "beef", "carrots", …) is kept and listed under
     "EXCLUDED" so it can be reviewed.

Dry-run by default. With --apply it deletes the matched HAS_INGREDIENT edges,
deletes Ingredient nodes left with no recipes, and DETACH DELETEs recipes that
drop below MIN_REAL_INGREDIENTS.

    python3 scripts/cleanup_non_food_ingredients.py            # dry run
    python3 scripts/cleanup_non_food_ingredients.py --apply    # destructive
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.utils.neo4j_utils import driver  # noqa: E402

MIN_REAL_INGREDIENTS = 2

# Hand-checked non-food terms. Whole-word, case-insensitive. Multi-word terms
# are deliberate so a bare collision word (rack -> "rack of lamb", string ->
# "string beans", bag -> "tea bag", pot -> "pot roast") is not matched.
BLOCKLIST_TERMS = [
    # utensils
    "spatula", "whisk", "ladle", "tongs", "rolling pin", "potato masher",
    # brushes / shakers / thermometers
    "pastry brush", "basting brush", "butter brush", "brush",
    "salt shaker", "pepper shaker", "shaker",
    "meat thermometer", "candy thermometer", "probe thermometer", "thermometer",
    # skewers / picks
    "skewer", "skewers", "toothpick", "toothpicks", "cocktail stick", "cocktail sticks", "kebab stick",
    # papers / wraps / foils
    "parchment paper", "parchment", "wax paper", "waxed paper", "baking paper",
    "paper towel", "paper towels", "kitchen towel", "kitchen towels",
    "aluminum foil", "aluminium foil", "tin foil", "foil",
    "plastic wrap", "cling film", "cling wrap", "saran wrap",
    "cheesecloth", "muslin cloth",
    # string / twine
    "kitchen string", "butcher string", "butcher's string", "butchers string",
    "kitchen twine", "butcher twine", "butcher's twine", "butchers twine",
    "cooking twine", "twine",
    # pans / dishes / vessels
    "ramekin", "ramekins", "baking dish", "casserole dish", "flameproof casserole",
    "dutch oven", "sheet pan", "baking sheet", "cookie sheet", "loaf pan",
    "loaf tin", "cake pan", "cake tin", "springform pan", "springform tin",
    "muffin tin", "muffin pan", "pie plate", "pie dish", "pie pan",
    "roasting pan", "roasting tin", "baking pan", "frying pan", "saute pan",
    "saucepan", "stockpot", "griddle", "grill pan", "wire rack", "cooling rack",
    "baking rack", "cooling grid", "baking tray",
    # graters / strainers
    "box grater", "cheese grater", "grater", "microplane", "zester",
    "fine mesh strainer", "fine-mesh strainer", "fine-mesh sieve", "fine mesh sieve",
    "mesh strainer", "sieve", "strainer", "colander",
    # mortar
    "mortar and pestle", "pestle and mortar", "mortar", "pestle",
    # bags / lids (multi-word only to avoid "tea bag" / "bag of …")
    "piping bag", "pastry bag", "ziploc bag", "ziploc bags", "zip-top bag", "zip top bag",
    "freezer bag", "sandwich bag", "paper bag", "brown paper bag",
    "plastic bag", "ziploc", "tight fitting lid", "tight-fitting lid", "tight lid", "lid",
    # appliances
    "food processor", "stand mixer", "hand mixer", "electric mixer", "immersion blender",
]

# The cooking-spray / spray-oil family, as a regex. Only oil/spray *modifier*
# words may sit in front of the core ("olive oil flavored cooking spray" -> all
# eaten), so a real-food line that merely mentions a spray ("x 150g chicken
# breast fillets cooking spray oil", "1 tbsp olive oil (or cooking spray)") is
# left with "chicken"/"olive oil" leftover and therefore kept.
_SPRAY_MOD = (
    r"(?:olive|vegetable|veg|canola|rapeseed|sunflower|coconut|peanut|grapeseed|"
    r"avocado|rice|bran|butter|nonstick|non[\s\-]?stick|baking|cooking|oil|light|"
    r"low|fat|non|flavou?red|flavou?r|extra|virgin)"
)
_SPRAY_CORE = (
    r"(?:cooking\s+spray\s+oil|cooking\s+oil\s+spray|cooking\s+spray|baking\s+spray|"
    r"spray\s+oil|oil\s+spray|nonstick\s+spray(?:\s+coating)?|non[\s\-]?stick\s+spray(?:\s+coating)?)"
)
_SPRAY_PATTERN = rf"(?:{_SPRAY_MOD}[\s\-]+)*{_SPRAY_CORE}(?:[\s\-]+(?:oil|coating))?"

# Words that may be left over once the equipment term is removed and still mean
# "this line is just equipment + qualifiers". Anything NOT here -> keep the line.
# Oil names are intentionally NOT here, so "olive oil, plus extra, to brush"
# keeps "olive oil" and is not deleted.
TRIVIAL_WORDS = {
    # connectives
    "and", "the", "for", "with", "not", "use", "using", "see", "tip", "tips",
    "you", "your", "also", "need", "needs", "needed", "made", "from", "into",
    # non-stick / flavour qualifiers
    "non", "nonstick", "stick", "flavored", "flavoured", "flavor", "flavour",
    "low", "fat", "light", "original", "optional", "plus", "extra", "as",
    "spray", "coating",
    # sizes / shapes / counts
    "bamboo", "wooden", "metal", "stainless", "steel", "barbecue", "bbq",
    "kebab", "kabob", "short", "long", "large", "small", "medium", "mini",
    "piece", "pieces", "square", "squares", "strip", "strips", "length",
    "lengths", "roll", "full", "sheet", "sheets", "hole", "size",
    # soak / prep instructions
    "soaked", "soak", "presoaked", "pre", "rinsed", "water", "cold", "warm",
    "hot", "minutes", "minute", "min", "mins", "hours", "hour", "overnight",
    "prevent", "burning", "scorching", "reduce", "keep", "upright", "when",
    "freezing", "least", "more", "than", "about", "approx", "approximately",
    "wet", "dry", "patted", "drained", "rinse", "well", "first",
    # vessel / line / grease qualifiers
    "board", "glass", "jar", "decorative", "clean", "tight", "fitting",
    "fasten", "tie", "bind", "turning", "turn", "cover", "liner", "liners",
    "cases", "case", "tray", "container", "containers", "baking", "stove",
    "top", "plate", "grid", "mix", "mixture", "grease", "greasing", "frying",
    "fry", "cook", "cooking", "serving", "serve", "bake", "preheat", "line", "lined",
    # brands / generic wrap words (NB: no real-food words here — "cheese grater"
    # is handled as its own blocklist term, not by trivialising "cheese")
    "reynolds", "pam", "glad", "saran", "ziploc", "wrap", "cling", "film",
    "tin", "aluminium", "aluminum", "kitchen", "paper", "towel", "towels",
    "string", "twine", "jute", "probe", "digital", "presoak",
    # stray tokens seen in the data
    "x", "cm", "mm", "ml",
}

_TERM_ALTERNATION = "|".join(re.escape(t) for t in sorted(set(BLOCKLIST_TERMS), key=len, reverse=True))
REGEX_NEO4J = rf"(?i).*(?:\b(?:{_TERM_ALTERNATION})\b|{_SPRAY_PATTERN}).*"
_TERM_RE = re.compile(rf"(?:{_SPRAY_PATTERN})|\b(?:{_TERM_ALTERNATION})\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-zA-Z]{3,}")


def _is_pure_equipment(name: str) -> bool:
    residual = _TERM_RE.sub(" ", str(name or "").lower())
    leftover = [w for w in _WORD_RE.findall(residual) if w not in TRIVIAL_WORDS]
    return not leftover


SCAN_QUERY = """
MATCH (i:Ingredient)
WHERE i.name =~ $regex
OPTIONAL MATCH (r:Recipe)-[hi:HAS_INGREDIENT]->(i)
RETURN i.name AS name, count(hi) AS edges
ORDER BY edges DESC
"""

IMPACT_QUERY = """
MATCH (i:Ingredient) WHERE i.name IN $names
WITH collect(i) AS junk
UNWIND junk AS ji
MATCH (r:Recipe)-[:HAS_INGREDIENT]->(ji)
WITH r, count(DISTINCT ji) AS junk_in_recipe
MATCH (r)-[:HAS_INGREDIENT]->(all_i:Ingredient)
WITH r, junk_in_recipe, count(DISTINCT all_i) AS total_in_recipe
RETURN count(r) AS recipes_touched,
       sum(CASE WHEN total_in_recipe - junk_in_recipe < $min_real THEN 1 ELSE 0 END) AS recipes_below_min
"""

DELETE_EDGES_QUERY = """
MATCH (r:Recipe)-[hi:HAS_INGREDIENT]->(i:Ingredient)
WHERE i.name IN $names
DELETE hi
RETURN count(hi) AS deleted_edges
"""

DELETE_ORPHAN_INGREDIENTS_QUERY = """
MATCH (i:Ingredient)
WHERE i.name IN $names AND NOT (:Recipe)-[:HAS_INGREDIENT]->(i)
DETACH DELETE i
RETURN count(i) AS deleted_ingredients
"""

DELETE_EMPTY_RECIPES_QUERY = """
MATCH (r:Recipe)
WITH r, count{ (r)-[:HAS_INGREDIENT]->(:Ingredient) } AS n
WHERE n < $min_real
DETACH DELETE r
RETURN count(r) AS deleted_recipes
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually delete (default is dry-run).")
    parser.add_argument("--min-real", type=int, default=MIN_REAL_INGREDIENTS)
    parser.add_argument("--show", type=int, default=300)
    args = parser.parse_args()

    with driver.session() as session:
        rows = session.run(SCAN_QUERY, regex=REGEX_NEO4J).data()
        deletable, excluded = [], []
        for r in rows:
            (deletable if _is_pure_equipment(r["name"]) else excluded).append((r["name"], r["edges"]))
        names = [n for n, _ in deletable]
        del_edges = sum(e for _, e in deletable)

        print(f"Candidates matched by regex: {len(rows)}")
        print(f"  -> DELETABLE (pure equipment): {len(deletable)} names, {del_edges} HAS_INGREDIENT edges")
        print(f"  -> EXCLUDED  (line names a real food too): {len(excluded)} names")
        print()
        print(f"--- DELETABLE (name | edges), top {min(args.show, len(deletable))} ---")
        for n, e in deletable[: args.show]:
            print(f"  {n!r:<70} {e:>7}")
        if len(deletable) > args.show:
            print(f"  ... and {len(deletable) - args.show} more")
        print()
        print(f"--- EXCLUDED — kept because the name also names real food (top {min(args.show, len(excluded))}) ---")
        for n, e in excluded[: args.show]:
            print(f"  {n!r:<70} {e:>7}")
        if len(excluded) > args.show:
            print(f"  ... and {len(excluded) - args.show} more")
        print()

        if not names:
            print("Nothing deletable. Exiting.")
            return

        impact = session.run(IMPACT_QUERY, names=names, min_real=args.min_real).data()[0]
        print(f"Recipes touched by deletions: {impact['recipes_touched']}")
        print(f"Recipes that would drop below {args.min_real} ingredients (→ DETACH DELETE): {impact['recipes_below_min']}")
        print()

        if not args.apply:
            print("DRY RUN — nothing deleted. Re-run with --apply to execute.")
            return

        print("APPLYING …")
        d1 = session.run(DELETE_EDGES_QUERY, names=names).data()[0]["deleted_edges"]
        print(f"  deleted HAS_INGREDIENT edges: {d1}")
        d2 = session.run(DELETE_ORPHAN_INGREDIENTS_QUERY, names=names).data()[0]["deleted_ingredients"]
        print(f"  deleted orphaned Ingredient nodes: {d2}")
        d3 = session.run(DELETE_EMPTY_RECIPES_QUERY, min_real=args.min_real).data()[0]["deleted_recipes"]
        print(f"  deleted Recipe nodes below {args.min_real} ingredients: {d3}")
        print("Done.")


if __name__ == "__main__":
    main()
