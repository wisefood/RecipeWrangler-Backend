from scripts.postgres.import_foodhero_profile_trace import _duration_minutes
from scripts.neo4j.import_foodhero_recipes_to_neo4j import _parse_duration_minutes


def test_duration_minutes_adds_prep_and_cook_ranges():
    assert _duration_minutes("prep: 5 minutes; cook: 30 to 45 minutes") == 42.5
    assert _parse_duration_minutes("prep: 5 minutes; cook: 30 to 45 minutes") == 42.5


def test_duration_minutes_supports_hours_and_plain_minutes():
    assert _duration_minutes("prep: 10 mins; cook: 1 hr 20 mins") == 90
    assert _duration_minutes(25) == 25
