from __future__ import annotations

import importlib.util
import csv
from pathlib import Path
import sys


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "one_off"
    / "recover_irishheart_ingredients.py"
)
SPEC = importlib.util.spec_from_file_location(
    "recover_irishheart_ingredients", SCRIPT_PATH
)
assert SPEC and SPEC.loader
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


def test_extracts_every_ingredient_paragraph() -> None:
    html = """
    <div class="ingredients">
      <h3>Ingredients</h3>
      <p>1 tablespoon olive oil</p>
      <p>350g lean minced beef</p>
      <p>350g spaghetti</p>
    </div>
    """

    assert SCRIPT.extract_ingredients(html) == [
        "1 tablespoon olive oil",
        "350g lean minced beef",
        "350g spaghetti",
    ]


def test_splits_line_breaks_inside_one_paragraph() -> None:
    html = """
    <div class="ingredients">
      <p>For the sauce:<br>1 onion<br>2 tomatoes</p>
    </div>
    """

    assert SCRIPT.extract_ingredients(html) == [
        "For the sauce:",
        "1 onion",
        "2 tomatoes",
    ]


def test_validated_report_can_be_reused_without_refetching(tmp_path: Path) -> None:
    recipes = [
        {
            "id": "recipe-1",
            "title": "Soup",
            "url": "https://example.test/soup",
            "ingredients": ["1 onion"],
        }
    ]
    report = tmp_path / "report.csv"
    result = SCRIPT.RecoveryResult(
        recipe_id="recipe-1",
        title="Soup",
        url="https://example.test/soup",
        status="recoverable",
        old_count=1,
        recovered_count=2,
        changed=True,
        old_ingredients="1 onion",
        recovered_ingredients="1 onion | 2 carrots",
    )
    with report.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result.__dict__))
        writer.writeheader()
        writer.writerow(result.__dict__)

    ordered, recovered = SCRIPT._load_validated_report(report, recipes)

    assert ordered == [result]
    assert recovered == {"recipe-1": ["1 onion", "2 carrots"]}
