from scripts.maintenance.sync_parsed_snapshot_to_graph import _choice_props


def test_choice_properties_preserve_first_listed_parser_selection() -> None:
    props = _choice_props(
        {
            "choice": {
                "alternatives": [
                    {"name": "chicken breast"},
                    {"name": "turkey breast"},
                ],
                "selected_index": 0,
                "selection_policy": "first_listed_default",
            }
        }
    )

    assert props == {
        "choice_alternatives": ["chicken breast", "turkey breast"],
        "selected_alternative": "chicken breast",
        "alternative_selection_policy": "first_listed_default",
    }
