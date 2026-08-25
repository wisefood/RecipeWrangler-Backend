"""Curated SafeFood Ireland guidance exposed as an agent knowledge source."""

from __future__ import annotations

import re
from typing import Any, Iterable

SOURCE_NAME = "SafeFood Ireland"
REVIEWED_AT = "2026-08-25"

DOCUMENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "safefood-general-prevention",
        "title": "Four steps to prevent food poisoning",
        "url": "https://www.safefood.net/food-safety/food-poisoning/food-poisoning-causes",
        "topics": ["general", "clean", "cook", "chill", "cross-contamination"],
        "triggers": ["food safety", "food poisoning", "hygiene", "safe"],
        "guidance": [
            "Clean hands and food-contact surfaces.",
            "Cook food thoroughly, keep chilled food cold, and separate raw from ready-to-eat food.",
            "Keep the fridge at 5°C or below.",
        ],
    },
    {
        "id": "safefood-meat-cooking",
        "title": "Cooking meat safely",
        "url": "https://www.safefood.net/food-safety/cooking-food-safety/cooking-meat-fish",
        "topics": ["cooking", "meat", "poultry", "fish", "temperature"],
        "triggers": [
            "chicken", "turkey", "duck", "pork", "burger", "sausage", "kebab",
            "mince", "minced", "meat", "fish", "offal",
        ],
        "guidance": [
            "Cook poultry, pork, minced meat, burgers, sausages and skewered meat all the way through.",
            "A clean thermometer in the thickest part should reach at least 75°C.",
            "Defrost meat covered on the bottom shelf of the fridge, not at room temperature.",
        ],
    },
    {
        "id": "safefood-fridge-leftovers",
        "title": "Keeping food safe in your fridge",
        "url": "https://www.safefood.net/food-safety/storing-food-safely/storing-food-fridge",
        "topics": ["storage", "fridge", "leftovers", "rice", "cooling"],
        "triggers": ["leftover", "leftovers", "fridge", "refrigerate", "rice", "storage", "cool"],
        "guidance": [
            "Keep the fridge at 5°C or below and store raw meat or fish sealed on the bottom shelf.",
            "Refrigerate most leftovers within two hours; cool cooked rice and refrigerate it within one hour.",
            "Use refrigerated leftovers within three days and discard high-risk food left out too long.",
        ],
    },
    {
        "id": "safefood-cross-contamination",
        "title": "How to stop germs spreading",
        "url": "https://www.safefood.net/food-safety/cross-contamination",
        "topics": ["cleaning", "cross-contamination", "raw meat", "allergy"],
        "triggers": ["raw", "chicken", "meat", "cross contamination", "clean", "chopping board", "allergy"],
        "guidance": [
            "Wash hands with warm soapy water after handling raw food and before touching ready-to-eat food.",
            "Clean utensils, chopping boards and surfaces after raw-food contact; do not wash raw chicken.",
            "Keep raw meat and its packaging away from ready-to-eat foods.",
        ],
    },
    {
        "id": "safefood-ready-meals",
        "title": "How to cook ready meals safely",
        "url": "https://www.safefood.net/food-safety/cooking-food-safety/how-to-store-and-prepare-convenience-foods-safely",
        "topics": ["ready meals", "reheating", "use-by", "storage"],
        "triggers": ["ready meal", "convenience food", "reheat", "use by", "microwave meal"],
        "guidance": [
            "Keep ready meals refrigerated, check the use-by date and follow the package instructions.",
            "Heat until steaming hot throughout; SafeFood advises not reheating a ready meal again.",
        ],
    },
)


def _tokens(values: Iterable[object]) -> set[str]:
    text = " ".join(str(value or "").lower() for value in values)
    return set(re.findall(r"[a-z0-9]+", text))


def search_safefood_guidance(
    query: str = "", ingredient_names: Iterable[object] = (), limit: int = 5
) -> list[dict[str, Any]]:
    """Rank curated guidance using explicit topic/ingredient trigger overlap."""
    haystack = " ".join([query, *(str(item) for item in ingredient_names)]).lower()
    query_tokens = _tokens([haystack])
    ranked: list[tuple[int, dict[str, Any]]] = []
    for doc in DOCUMENTS:
        score = sum(
            3 if " " in trigger else 2
            for trigger in doc["triggers"]
            if trigger in haystack
        )
        score += len(query_tokens & _tokens(doc["topics"]))
        if score or doc["id"] == "safefood-general-prevention":
            ranked.append((score, doc))
    ranked.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
    return [
        {**doc, "source": SOURCE_NAME, "reviewed_at": REVIEWED_AT, "advisory": True}
        for _, doc in ranked[: max(1, min(int(limit), 10))]
    ]
