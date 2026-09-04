"""Conservative detection of unambiguous recipe equipment entries."""

from __future__ import annotations

import re


_NON_FOOD_PHRASES = (
    "aluminum foil", "aluminium foil", "paper cup", "paper cups",
    "plastic cup", "plastic cups",
    "wooden stick", "wooden sticks", "baking tray", "baking trays",
    "baking sheet", "muffin tin", "marker pen", "wooden spoon",
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
    "ice cube tray", "ice cube trays", "round cutter", "round cutters",
    "cookie cutter", "cookie cutters", "ramekin", "ramekins",
)


def is_unambiguous_non_food_ingredient(value: object) -> bool:
    """True only for equipment/supplies, never normal culinary ingredients."""
    name = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    return any(re.search(rf"\b{re.escape(phrase)}\b", name) for phrase in _NON_FOOD_PHRASES)
