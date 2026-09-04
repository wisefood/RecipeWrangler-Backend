import pandas as pd

from scripts.prepare_essrg import extract_components


def test_extract_components_recovers_known_blank_dish_ids() -> None:
    tofu = {"dish_id": 168, "name": "Fried tofu slices", "ingredients": []}
    row = pd.Series(
        {
            "ID.1": float("nan"),
            "Dish #1": "Tofu slices",
            "ID.2": float("nan"),
            "Dish #2": "Unknown component",
        }
    )

    components, missing = extract_components(row, {168.0: tofu})

    assert [component["dish_id"] for component in components] == [168]
    assert missing == [{"dish_id": None, "dish_name": "Unknown component"}]
