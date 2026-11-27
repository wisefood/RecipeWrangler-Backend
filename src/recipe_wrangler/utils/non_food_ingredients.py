"""Conservative detection of unambiguous recipe equipment entries."""

from __future__ import annotations

import re


_NON_FOOD_PHRASES = (
    "aluminum foil", "aluminium foil", "paper cup", "paper cups",
    "plastic cup", "plastic cups",
    "wooden stick", "wooden sticks", "baking tray", "baking trays",
    "baking sheet", "baking paper", "parchment paper", "muffin tin",
    "marker pen", "wooden spoon",
    "plastic wrap", "sticky tape",
    "popsicle stick", "popsicle sticks", "paper straw", "paper straws",
    "cocktail stick", "cocktail sticks", "skewer stick", "skewer sticks",
    "toothpick", "toothpicks", "wooden skewer", "wooden skewers",
    "bamboo skewer", "bamboo skewers", "measuring spoon", "measuring spoons",
    "measuring cup", "measuring cups", "piping bag", "piping bags",
    "baking dish", "baking dishes", "baking pan", "baking pans",
    "skewer", "skewers", "metal skewer", "metal skewers",
    "ice block holder", "ice block holders", "ice block mould",
    "ice block moulds", "ice block mold", "ice block molds",
    "ice block stick", "ice block sticks",
    "ice cube tray", "ice cube trays", "round cutter", "round cutters",
    "cookie cutter", "cookie cutters", "ramekin", "ramekins",
    "glass jar", "glass jars", "drinking glass", "drinking glasses",
    "egg mould", "egg moulds", "egg mold", "egg molds",
    "muffin tray", "bun tray", "flan tin", "pastry cutter", "pastry cutters",
    "paper case", "paper cases", "patty pan case", "patty pan cases",
    "patty cake case", "patty cake cases", "greaseproof paper", "cooking twine",
    "kitchen twine", "paper cone", "paper cones", "paper bag", "paper bags",
    "plastic bag", "plastic bags", "bamboo stick", "bamboo sticks",
    "muffin cup", "muffin cups", "cupcake container", "cupcake containers",
    "zip lock bag", "zip lock bags", "jam jar", "jam jars", "jelly jars",
    "craft peg", "craft pegs", "pipe cleaner", "pipe cleaners",
    "craft animal eyes", "rolling pin",
)

_NON_FOOD_EXACT = {
    "frying pan", "jar", "jars", "oven bag", "oven bags",
    "twine", "string", "kitchen string", "string or twine", "twine or string",
    "thermometer", "stainless steel turner", "ribbon",
    "culinary pipette", "culinary pipettes",
    "miniature culinary pipette", "miniature culinary pipettes",
}


def is_unambiguous_non_food_ingredient(value: object) -> bool:
    """True only for equipment/supplies, never normal culinary ingredients."""
    name = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    return name in _NON_FOOD_EXACT or any(
        re.search(rf"\b{re.escape(phrase)}\b", name)
        for phrase in _NON_FOOD_PHRASES
    )
