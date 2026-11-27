from scripts.postgres.import_foodhero_profile_trace import _duration_minutes
from scripts.neo4j.import_foodhero_recipes_to_neo4j import _parse_duration_minutes


def test_duration_minutes_adds_prep_and_cook_ranges():
    assert _duration_minutes("prep: 5 minutes; cook: 30 to 45 minutes") == 42.5
    assert _parse_duration_minutes("prep: 5 minutes; cook: 30 to 45 minutes") == 42.5


def test_duration_minutes_supports_hours_and_plain_minutes():
    assert _duration_minutes("prep: 10 mins; cook: 1 hr 20 mins") == 90
    assert _duration_minutes(25) == 25


def test_neo4j_importer_main_writes_lines_from_raw_file(tmp_path, monkeypatch):
    import json
    import sys

    from scripts.neo4j import import_foodhero_recipes_to_neo4j as neo

    raw = {"A": {"title": "A", "url": "https://x/a", "prep_time": "5 minutes", "servings": "4",
                 "ingredients": ["1 to 2 Tablespoons lime juice", "", "2 cups cold water", "Ice"],
                 "directions": ["Mix."]}}
    f = tmp_path / "raw.json"
    f.write_text(json.dumps(raw))
    written = []

    class Session:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def run(self, *a, **k): return type("R", (), {"consume": lambda self: None})()
        def execute_write(self, fn, *args):
            if fn is neo._merge_ingredient:
                written.append(args[1:])

    class Driver:
        def session(self): return Session()
        def close(self): pass

    monkeypatch.setattr(neo, "_driver", lambda: Driver())
    monkeypatch.setattr(sys, "argv", ["x", "--input", str(f)])
    neo.main()
    assert [t for _, t in written] == ["1 to 2 Tablespoons lime juice", "2 cups cold water", "Ice"]


def test_both_importers_default_to_the_raw_file():
    from scripts.neo4j import import_foodhero_recipes_to_neo4j as neo
    from scripts.postgres import import_foodhero_profile_trace as pg

    assert neo.DEFAULT_INPUT.name == pg.DEFAULT_INPUT.name == "foodhero_recipes.json"
