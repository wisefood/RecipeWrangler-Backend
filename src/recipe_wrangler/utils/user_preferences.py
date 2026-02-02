from __future__ import annotations

from typing import Any, Dict


def get_user_preferences() -> Dict[str, Any]:
    """
    Temporary for local testing.
    Replace with platform-provided preferences later.
    """
    return {
        "preferred_ingredients": ["broccoli"],
        "diet": ["Vegan"],
        "allergens": ["peanut"],
    }

