from recipe_wrangler.tools.parse_recipe_tool import (
    IngredientChoiceExtraction,
    IngredientEntry,
    _is_instructional_or,
)


def test_choice_calls_are_off_by_default() -> None:
    import inspect

    from recipe_wrangler.tools.parse_recipe_tool import parse_ingredient_lines

    assert inspect.signature(parse_ingredient_lines).parameters["_choices"].default is False


def test_general_parser_entry_has_no_choice_fields() -> None:
    entry = IngredientEntry(name="carrot", measurement="1", display="1", note="")

    assert "alternatives" not in entry.model_dump()


def test_focused_choice_schema_preserves_complete_text_spans() -> None:
    result = IngredientChoiceExtraction(
        is_choice=True,
        alternatives=["4 chicken breasts", "500 g turkey breast"],
    )

    assert result.alternatives == ["4 chicken breasts", "500 g turkey breast"]


def test_instructional_or_is_not_a_food_choice() -> None:
    assert _is_instructional_or("cook for 10 minutes or until tender")
    assert not _is_instructional_or("vegetable or beef stock")
