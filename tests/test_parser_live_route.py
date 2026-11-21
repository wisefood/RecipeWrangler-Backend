"""Live recipe text must reach the evaluated line parser and its repairs."""
import pytest
from langchain_core.runnables import RunnableLambda
from recipe_wrangler.tools import parse_recipe_tool as parser


def recipe(**overrides):
    fields = dict(title='Test', ingredient_lines=['1 cup milk'], directions=['Mix.'], total_time=0, serves=0)
    return parser.ParsedRecipeText(**{**fields, **overrides})


def fake_model(monkeypatch, text_result, entries):
    prompts = []
    class Model:
        def with_structured_output(self, schema, **kwargs):
            def respond(prompt):
                prompts.append((schema, prompt.to_messages()))
                if schema is parser.ParsedRecipeText:
                    return text_result(len(prompts)) if callable(text_result) else text_result
                return parser.ParsedIngredientLines(ingredients=entries)
            return RunnableLambda(respond)
    monkeypatch.setattr(parser, '_parser_llm', lambda _: (Model(), 'function_calling'))
    return prompts


def entry(name, measurement='', display='', note=''):
    return dict(name=name, measurement=measurement, display=display, note=note)


def test_recipe_text_lines_reach_line_parser_and_keep_identity(monkeypatch):
    lines = ['1 3/4 cups gluten free baking flour', '1 (10 ounce) package frozen broccoli in cheese sauce']
    prompts = fake_model(monkeypatch, recipe(ingredient_lines=lines), [
        entry('gluten free baking flour', '1.75 cups', '1 3/4 cups'),
        entry('frozen broccoli in cheese sauce', '10.0 oz', '1 (10 oz) package')])
    result = parser.parse_recipe_tool.invoke({'recipe': 'Cake\nIngredients:\n' + '\n'.join(lines)})
    assert result['ingredient_names'] == ['gluten free baking flour', 'frozen broccoli in cheese sauce']
    assert result['ingredient_match_names'] == result['ingredient_names']
    assert result['measurements'] == ['1.75 cups', '10.0 oz']
    assert result['ingredient_original_texts'] == lines
    assert [schema for schema, _ in prompts] == [parser.ParsedRecipeText, parser.ParsedIngredientLines]
    assert '1. 1 3/4 cups gluten free baking flour' in prompts[1][1][1].content


def test_ingredient_only_text_needs_no_title_or_directions(monkeypatch):
    fake_model(monkeypatch, recipe(title='', directions=[]), [entry('milk', '1.0 cup', '1 cup')])
    result = parser.parse_recipe_tool.invoke({'recipe': '1 cup milk'})
    assert result['title'] == '' and result['directions'] == [] and result['serves'] == 0


def test_recipe_text_schema_failure_retries_once(monkeypatch):
    def flaky(call):
        if call == 1:
            return parser.ParsedRecipeText.model_validate({'title': 'x'})
        return recipe()
    prompts = fake_model(monkeypatch, flaky, [entry('milk', '1.0 cup', '1 cup')])
    assert parser.parse_recipe_tool.invoke({'recipe': '1 cup milk'})['measurements'] == ['1.0 cup']
    assert [schema for schema, _ in prompts].count(parser.ParsedRecipeText) == 2


def test_node_does_not_borrow_amount_from_another_line(monkeypatch):
    lines = ['3 scoops dairy-free vanilla ice cream', '¼ cup ice cube']
    fake_model(monkeypatch, recipe(ingredient_lines=lines), [
        entry('vanilla ice cream', '3.0', '3 scoops', 'dairy-free'), entry('ice', '', '', 'ice cube')])
    state = parser.Recipe_Parser_Node(parser.RecipeState(raw_recipe='\n'.join(lines)))
    assert state.measurements[0].startswith('3.0 ') and 'scoop' in state.measurements[0]


def test_discretionary_blank_is_marked_optional_like_batch_path():
    names, measurements, originals = parser.prepare_entries_for_weights(
        [entry('parsley', '', '', 'for garnish')], ['parsley, for garnish'])
    assert (names, measurements, originals) == (['parsley'], ['optional'], ['parsley, for garnish'])


def test_row_without_a_source_line_keeps_its_own_text():
    _, _, originals = parser.prepare_entries_for_weights([entry('pepper', '1.0 tsp', '1 tsp')], ['2 eggs'])
    assert originals == ['1 tsp pepper']


@pytest.mark.parametrize('line,wrong,expected', [
    ('3 (14 1/2 ounce) cans green beans, with liquid', '42.75 oz', '43.5 oz'),
    ('2 (46 fluid ounce) cans pineapple juice', '92.0 oz', '92.0 fl oz'),
    ('2 x 400g tins chopped tomatoes', '400.0 g', '800.0 g'),
    ('1 x 400g tin of black beans', '1.0 g', '400.0 g'),
    ('4 x 175g (6oz) chicken breasts', '4.0', '700.0 g'),
    ('2 tins of tuna in brine, drained, approx. 198g / 7 oz. food', '2.0 g', '396.0 g'),
])
def test_container_totals_come_from_source_arithmetic(line, wrong, expected):
    entries = [entry('food', wrong, '')]
    parser._apply_deterministic_overrides(entries, [line + ' food'])
    assert entries[0]['measurement'] == expected


@pytest.mark.parametrize('line,measurement', [
    ('2 x 400g (drained weight 240g) tins of black beans food', '480.0 g'),
    ('1 (16 ounce) chocolate cake food or (16 ounce) frosting', '16.0 oz'),
])
def test_container_total_leaves_exceptions_to_the_model(line, measurement):
    entries = [entry('food', measurement, '')]
    parser._apply_deterministic_overrides(entries, [line])
    assert entries[0]['measurement'] == measurement


def test_prompt_only_requests_fields_in_the_schema():
    assert 'alternative_measurements' not in parser._MEASUREMENT_ONLY_SYSTEM_PROMPT
    assert "'alternatives'" not in parser._MEASUREMENT_ONLY_SYSTEM_PROMPT


def test_recipe_text_is_plain_before_the_model_copies_it(monkeypatch):
    prompts = fake_model(monkeypatch, recipe(), [entry('milk', '1.0 cup', '1 cup')])
    parser.parse_recipe_tool.invoke({'recipe': 'Crème brûlée\n3⁄4 cup butter\n1½ cups OREO® Cookies\n½ cup milk'})
    sent = prompts[0][1][1].content
    assert '3/4 cup butter' in sent and '1 1/2 cups OREO Cookies' in sent and '1/2 cup milk' in sent
    assert 'Creme brulee' in sent


def test_title_and_steps_keep_source_accents(monkeypatch):
    fake_model(monkeypatch, recipe(title='Dodolle', directions=['Fozd meg a krumplit.', 'Serve warm.']),
               [entry('milk', '1.0 cup', '1 cup')])
    result = parser.parse_recipe_tool.invoke({'recipe': 'Dödölle\n1 cup milk\nFőzd meg a krumplit.\nServe warm.'})
    assert result['title'] == 'Dödölle'
    assert result['directions'] == ['Főzd meg a krumplit.', 'Serve warm.']


def test_prompt_keeps_bare_first_line_as_ingredient():
    assert "'fudge (optional)'" in parser._RECIPE_TEXT_SYSTEM_PROMPT


def test_prompt_keeps_or_options_in_one_line():
    assert "'1 tbsp oil or 1 tbsp butter'" in parser._RECIPE_TEXT_SYSTEM_PROMPT


def test_plain_text_keeps_spacing_around_fractions():
    assert parser._plain_recipe_text('juice of ½ a lemon, 1½ cups') == 'juice of 1/2 a lemon, 1 1/2 cups'
    assert parser._plain_recipe_text('into ¾-1 inch') == 'into 3/4-1 inch'


@pytest.mark.parametrize('line,measurement', [
    ('1 x 325g / 11½ oz. can of food - 260g / 9 oz. when drained', '260.0 g'),
    ('1 x 400g tin food (240g drained)', '240.0 g'),
])
def test_container_total_keeps_a_stated_drained_mass(line, measurement):
    entries = [entry('food', measurement, '')]
    parser._apply_deterministic_overrides(entries, [line])
    assert entries[0]['measurement'] == measurement


@pytest.mark.parametrize('measurement,expected', [('2.0 g', '396.0 g'), ('396.0 g', '396.0 g')])
def test_size_after_the_name_gives_the_total_even_when_the_model_was_right(measurement, expected):
    entries = [entry('food', measurement, '')]
    parser._apply_deterministic_overrides(entries, ['2 tins of tuna, approx. 198g food'])
    assert entries[0]['measurement'] == expected


def test_a_single_tin_with_a_size_after_the_name_is_left_to_the_model():
    entries = [entry('food', '1.0 g', '')]
    parser._apply_deterministic_overrides(entries, ['1 tin of tuna, approx. 198g food'])
    assert entries[0]['measurement'] != '396.0 g'


def test_container_total_still_applies_when_only_told_to_drain():
    assert parser._container_total('2 x 400g cans chickpeas, rinsed and drained') == '800.0 g'


def test_prompt_keeps_fat_sugar_salt_level_in_name():
    assert "'low-fat cheddar'" in parser._MEASUREMENT_ONLY_SYSTEM_PROMPT


@pytest.mark.parametrize('line,summed,expected', [
    ('5 lbs potatoes, Russet or 5 lbs yukon gold potatoes', '10.0 lbs', '5.0 lbs'),
    ('2 2 empire apples or 2 cortland apples', '4.0', '2.0'),
    ('1 cup cheddar cheese or 1 cup colby cheese, grated', '2.0 cups', '1.0 cups'),
])
def test_or_alternatives_are_never_added_together(line, summed, expected):
    entries = [entry(line.split(' or ')[0].split(',')[0].split(' ', 2)[-1], summed, '')]
    parser._apply_deterministic_overrides(entries, [line])
    assert entries[0]['measurement'] == expected


@pytest.mark.parametrize('line,measurement', [
    ('2 or 3 apples', '2.5'),
    ('1 cup flour, or more as needed', '1.0 cup'),
    ('1 tbsp oil or 2 tbsp butter', '1.0 tbsp'),
])
def test_or_guard_leaves_other_amounts_alone(line, measurement):
    entries = [entry(line.split(',')[0].split(' or ')[-1].split(' ', 1)[-1], measurement, '')]
    parser._apply_deterministic_overrides(entries, [line])
    assert entries[0]['measurement'] == measurement


def test_heading_row_with_empty_name_is_dropped_not_fatal(monkeypatch):
    class Model:
        def with_structured_output(self, *a, **k):
            return RunnableLambda(lambda p: parser.ParsedIngredientLines(ingredients=[
                entry('apples', '3.0 large', '3 large'), entry(''), entry('coconut flakes', '3.0 tablespoons', '3 tablespoons')]))
    monkeypatch.setattr(parser, '_parser_llm', lambda _: (Model(), 'function_calling'))
    result = parser.parse_ingredient_lines(['3 large apples', 'To decorate', '3 tablespoons coconut flakes'])
    assert [e['name'] for e in result] == ['apples', 'coconut flakes']


@pytest.mark.parametrize('line,expected', [
    ('1 8 oz package softened light cream cheese', '8.0 oz'),
    ('2 15-ounce cans of peaches, juice packed', '30.0 oz'),
])
def test_container_size_without_parentheses(line, expected):
    assert parser._container_total(line) == expected


@pytest.mark.parametrize('line', ['2 1/2 cups flour', '1 8 inch pie crust', '3 400g chicken breasts', '1 1/2 lb package beef'])
def test_unparenthesized_size_needs_a_container_word(line):
    assert parser._container_total(line) is None
