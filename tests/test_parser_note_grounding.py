"""Protect the source-grounding instructions actually sent to the ingredient model."""
from langchain_core.runnables import RunnableLambda

from recipe_wrangler.tools import parse_recipe_tool as parser


def test_ingredient_prompt_requires_source_supported_notes(monkeypatch):
    captured = []
    source = ['whipped cream', 'salt', 'salt to taste', '1 tbsp sugar (optional)']
    expected = [
        {'name': 'whipped cream', 'measurement': '', 'display': '', 'note': ''},
        {'name': 'salt', 'measurement': '', 'display': '', 'note': ''},
        {'name': 'salt', 'measurement': '', 'display': '', 'note': 'to taste'},
        {'name': 'sugar', 'measurement': '1.0 tbsp', 'display': '1 tbsp', 'note': 'optional'},
    ]

    def respond(prompt):
        captured.append(prompt.to_messages())
        return parser.ParsedIngredientLines(ingredients=expected)

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return RunnableLambda(respond)

    monkeypatch.setattr(parser, '_parser_llm', lambda _: (Model(), 'function_calling'))
    result = parser.parse_ingredient_lines(source, max_attempts=1, _choices=False)
    assert result == expected
    assert len(captured) == 1
    system, human = captured[0]
    assert 'Include only notes explicitly supported by the source text.' in system.content
    assert "'whipped cream', measurement '', display '', note ''" in system.content
    assert "'salt', measurement '', display '', note ''" in system.content
    assert "'salt to taste' -> name 'salt', measurement '', note 'to taste'" in system.content
    assert "'1 - Salt' -> name 'salt', measurement '', note ''" in system.content
    assert "measurement '' (empty), note 'to taste'" not in system.content
    assert all(line in human.content for line in source)


def test_prompt_distinguishes_primary_units_and_preparation_from_identity():
    prompt = parser._MEASUREMENT_ONLY_SYSTEM_PROMPT
    assert "unit can appear AFTER the ingredient name" in prompt
    assert "measurement '0.5 tsp', display '1/2 level tsp'" in prompt
    assert "'2 eggs (serve with 1 cup milk)' stays measurement '2.0'" in prompt
    assert "name 'nutmeg', measurement '0.5 teaspoon', note 'freshly grated'" in prompt
    assert "food-identity terms such as 'ground beef'" in prompt
