import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "tag_lines", Path(__file__).resolve().parents[1] / "scripts/neo4j/tag_recipe_allergens_from_lines.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def row(lines, names, graph):
    return {"id": "r1", "source": "s", "title": "t", "lines": lines, "names": names, "graph": graph}


def cache_for(*texts):
    return dict(tool._one(t) for t in texts)


def test_adds_only_what_the_graph_lacks():
    lines = ["150 g Milk, soya, non-dairy alternative to milk", "2 eggs"]
    cache = cache_for(*lines, "milk", "eggs")
    plan = tool.build_plan([row(lines, ["milk", "eggs"], ["egg"])], cache)
    assert [(p["allergen"]) for p in plan] == ["soy"]
    assert plan[0]["lines"] == [lines[0]]


def test_never_plans_for_something_already_present():
    lines = ["2 eggs"]
    assert tool.build_plan([row(lines, ["eggs"], ["egg"])], cache_for("2 eggs", "eggs")) == []


def test_example_lists_are_not_planned():
    lines = ["4 cups chopped vegetables such as onion, carrot, and celery"]
    assert tool.build_plan([row(lines, ["vegetables"], [])], cache_for(*lines, "vegetables")) == []


def test_recipe_without_original_lines_is_skipped():
    assert tool.build_plan([row([], ["milk"], [])], {}) == []


def test_write_and_rollback_use_the_same_marker():
    assert "$version" in tool.WRITE and "$source" in tool.WRITE
    assert "classification_version = $version" in tool.ROLLBACK and "$source IN h.sources" in tool.ROLLBACK
    assert "DELETE" in tool.ROLLBACK and "MERGE" not in tool.ROLLBACK


def test_write_never_deletes_or_overwrites_existing_evidence():
    assert "DELETE" not in tool.WRITE
    assert "ON CREATE SET h." in tool.WRITE and "ON CREATE SET d." in tool.WRITE


def test_section_headings_are_not_ingredient_evidence():
    for heading in ("Pastry", "Noodle salad", "For the dressing"):
        assert tool.build_plan([row([heading], [], [])], cache_for(heading)) == []
    assert tool.build_plan([row(["2 eggs"], [], [])], cache_for("2 eggs"))[0]["allergen"] == "egg"
    assert tool.build_plan([row(["celery"], [], [])], cache_for("celery"))[0]["allergen"] == "celery"
