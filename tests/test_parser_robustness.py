"""Development cases for source grounding and bounded schema recovery."""
import pytest
from langchain_core.runnables import RunnableLambda
from recipe_wrangler.tools import parse_recipe_tool as parser


def fake_model(monkeypatch, respond):
    class Model:
        def with_structured_output(self, *args, **kwargs):
            return RunnableLambda(respond)
    monkeypatch.setattr(parser, '_parser_llm', lambda _: (Model(), 'function_calling'))


def test_schema_failure_retries_once_with_repair_instructions(monkeypatch):
    calls = []
    def respond(prompt):
        calls.append(prompt.to_messages())
        if len(calls) == 1:
            return parser.ParsedIngredientLines.model_validate({'ingredients': ['2 pears']})
        return parser.ParsedIngredientLines(ingredients=[
            {'name': 'pears', 'measurement': '2.0', 'display': '2', 'note': ''}])
    fake_model(monkeypatch, respond)
    result = parser.parse_ingredient_lines(['2 pears'], _choices=False)
    assert result[0]['measurement'] == '2.0'
    assert len(calls) == 2
    assert 'previous response failed schema validation' not in calls[0][1].content
    assert 'previous response failed schema validation' in calls[1][1].content
    assert 'Never return ingredient lines as strings' in calls[1][1].content
    assert '1. 2 pears' in calls[1][1].content


@pytest.mark.parametrize('attempts', [1, 2])
def test_persistent_invalid_schema_stops_at_attempt_limit(monkeypatch, attempts):
    calls = []
    def respond(prompt):
        calls.append(prompt)
        return parser.ParsedIngredientLines.model_validate({'ingredients': ['2 pears']})
    fake_model(monkeypatch, respond)
    with pytest.raises(RuntimeError, match=f'after {attempts} attempts'):
        parser.parse_ingredient_lines(['2 pears'], max_attempts=attempts, _choices=False)
    assert len(calls) == attempts


@pytest.mark.parametrize('source', ['parsley', 'parsley, finely chopped', 'parsley (chopped)'])
def test_clears_invented_amount_from_unambiguous_quantity_free_source(source):
    entries = [{'name': 'parsley', 'measurement': '1.0 cup', 'display': '1 cup', 'note': 'chopped'}]
    parser._apply_deterministic_overrides(entries, [source])
    assert entries[0]['measurement'] == entries[0]['display'] == ''
    assert entries[0]['note'] == 'chopped'


@pytest.mark.parametrize('source', [
    '2 parsley sprigs', 'a handful of parsley', 'parsley (half a cup)',
    'parsley (1 cup)', 'parsley (a pinch)', 'parsley (2 tbsp chopped)',
])
def test_quantity_evidence_is_not_classified_as_quantity_free(source):
    assert not parser._clearly_quantity_free({'name': 'parsley'}, source, [source])


def test_duplicate_source_names_are_not_guessed():
    lines = ['parsley, chopped', 'parsley (1 cup)']
    assert not parser._clearly_quantity_free({'name': 'parsley'}, lines[0], lines)


def test_shared_source_line_is_not_classified_as_single_quantity_free_food():
    line = 'parsley and dill'
    assert not parser._clearly_quantity_free({'name': 'parsley'}, line, [line])


@pytest.mark.parametrize('display,amount,expected', [
    ('2-4 teaspoons', '3.0', '3.0 teaspoon'),
    ('¼–¾ cup', '0.5', '0.5 cup'),
    ('10 to 20 grams', '15.0', '15.0 g'),
])
def test_recovers_unit_for_already_averaged_range(display, amount, expected):
    value, reason = parser.restore_explicit_source_measurement(
        {'name': 'vanilla extract', 'measurement': amount, 'display': display})
    assert value == expected
    assert reason == 'source_range_unit'


@pytest.mark.parametrize('display,amount', [
    ('2-4 teaspoons', '2.0'), ('1-20 oz jar', '10.5'),
    ('2-4 teaspoons or 1 cup', '3.0'), ('2-4 teaspoons plus 1 cup', '3.0'),
    ('2-4 inches', '3.0'),
])
def test_range_guard_does_not_guess_wrong_quantities_packages_or_alternatives(display, amount):
    value, reason = parser.restore_explicit_source_measurement(
        {'name': 'vanilla extract', 'measurement': amount, 'display': display})
    assert reason != 'source_range_unit'


def test_pipeline_clears_invented_quantity_without_extra_model_call(monkeypatch):
    calls = []
    def respond(prompt):
        calls.append(prompt)
        return parser.ParsedIngredientLines(ingredients=[
            {'name': 'parsley', 'measurement': '1.0', 'display': '1', 'note': 'chopped'}])
    fake_model(monkeypatch, respond)
    result = parser.parse_ingredient_lines(['parsley, chopped'], _choices=False)
    assert result == [{'name': 'parsley', 'measurement': '', 'display': '', 'note': 'chopped'}]
    assert len(calls) == 1


def test_quantified_duplicate_source_name_is_not_guessed():
    lines = ['2 parsley sprigs', 'parsley, chopped']
    assert not parser._clearly_quantity_free({'name': 'parsley'}, lines[1], lines)


def test_pipeline_recovers_range_unit_from_source_when_display_also_loses_unit(monkeypatch):
    def respond(prompt):
        return parser.ParsedIngredientLines(ingredients=[
            {'name': 'vanilla extract', 'measurement': '3.0', 'display': '2-4', 'note': ''}])
    fake_model(monkeypatch, respond)
    result = parser.parse_ingredient_lines(['2-4 teaspoons vanilla extract'], _choices=False)
    assert result[0]['measurement'] == '3.0 teaspoon'
    assert result[0]['display'] == '2-4'


@pytest.mark.parametrize('source', [
    'parsley (a dozen sprigs)', 'parsley (dozen)', 'parsley (heaped dessertspoon)',
    'parsley (scoop)', 'parsley (pint)',
])
def test_word_amounts_and_known_units_are_not_erased(source):
    assert not parser._clearly_quantity_free({'name': 'parsley'}, source, [source])
