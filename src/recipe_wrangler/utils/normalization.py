# Purpose: Text/ingredient normalization utilities.

# src/recipe_wrangler/utils/normalization.py

import re

def normalize_foodon_label(label: str) -> str:
    """
    Convert a FoodOn label like:
        'cheese (white)@en'
        'feta cheese food product@en'

    Into a usable ingredient name for FlavorGraph:
        'cheese white'
        'feta cheese'
    """
    label = label.lower().replace("@en", "")
    label = label.replace("food product", "")
    label = label.replace("food", "")

    # replace parentheses with spaces
    label = re.sub(r"[\(\)]", " ", label)

    # remove non-alphabetic characters
    label = re.sub(r"[^a-z\s]", " ", label)

    # collapse whitespace
    label = " ".join(label.split())

    return label
