from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "one_off"
    / "repair_sustainability_canonicalization.py"
)
SPEC = importlib.util.spec_from_file_location(
    "repair_sustainability_canonicalization", SCRIPT_PATH
)
assert SPEC and SPEC.loader
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


def test_projection_uses_parsed_original_not_sustainability_match() -> None:
    originals = [
        {
            "recipe_id": "r1",
            "original_id": "r1:0",
            "position": 0,
            "line": "300g firm tofu, diced into cubes",
        }
    ]
    checkpoints = [
        {
            "recipe_id": "r1",
            "raw_name": "firm tofu, diced into cubes",
            "clean_name": "vegetable cube",
            "measurement": "300g",
            "weight_grams": 300.0,
        }
    ]

    projection = SCRIPT.build_projection(originals, checkpoints)

    assert len(projection) == 1
    assert projection[0].name == "firm tofu"
    assert projection[0].name != "vegetable cube"
    assert projection[0].measurement == "300g"
    assert projection[0].weight_grams == 300.0


def test_projection_restores_original_to_canonical_mapping() -> None:
    projection = SCRIPT.build_projection(
        [
            {
                "recipe_id": "r1",
                "original_id": "r1:0",
                "position": 0,
                "line": "1 red onion, cut into wedges",
            }
        ],
        [],
    )
    payload = SCRIPT._recipe_payloads(projection)[0]

    assert payload["ingredients"][0]["name"] == "red onion"
    assert payload["original_maps"] == [
        {"original_id": "r1:0", "ingredient_name": "red onion"}
    ]


def test_projection_removes_preparation_but_preserves_food_identity() -> None:
    assert SCRIPT.normalize_ingredient_name("garlic, crushed") == "garlic"
    assert SCRIPT.normalize_ingredient_name("cherry tomatoes, halved") == "cherry tomatoes"
    assert (
        SCRIPT.normalize_ingredient_name(
            "frozen mixed Asian stir-fry vegetables, partially thawed"
        )
        == "mixed vegetables"
    )
