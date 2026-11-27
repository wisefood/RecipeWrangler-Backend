from scripts.prepare_weight_ready_parser_snapshot import _pick_raw_lines


def test_same_title_picks_raw_lines_matching_parsed_names():
    soup_a = ["1 tablespoon chili powder", "1 can black beans"]
    soup_b = ["2 teaspoons oregano", "1 onion", "3 cups water"]
    parsed = [{"name": "oregano"}, {"name": "onion"}]
    assert _pick_raw_lines([soup_a, soup_b], parsed) == soup_b
    assert _pick_raw_lines([soup_b, soup_a], [{"name": "black beans"}]) == soup_a


def test_single_or_no_candidate():
    assert _pick_raw_lines([], [{"name": "x"}]) == []
    assert _pick_raw_lines([["1 egg"]], []) == ["1 egg"]


def test_sync_picks_the_raw_record_of_the_parsed_twin():
    from scripts.maintenance.sync_parsed_snapshot_to_graph import pick_record

    a = {"url": "a", "ingredients": ["1 cup onion", "2 teaspoons chili powder"]}
    b = {"url": "b", "ingredients": ["1/2 cup salsa", "1/4 teaspoon oregano"]}
    assert pick_record([a, b], [{"name": "salsa"}, {"name": "oregano"}])["url"] == "b"
    assert pick_record([a, b], [{"name": "chili powder"}])["url"] == "a"
    assert pick_record([], [{"name": "x"}]) is None
