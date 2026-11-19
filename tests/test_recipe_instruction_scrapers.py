from importlib import import_module

from bs4 import BeautifulSoup

from scripts.maintenance.backfill_safefood_instructions import recovered_method

foodhero = import_module("scripts.import.foodhero.export_foodhero_recipes_to_json")
bestofhungary = import_module("scripts.import.web.scrape_bestofhungary_recipes")
irishheart = import_module("scripts.import.web.scrape_irishheart_recipes")
supervalu = import_module("scripts.import.web.scrape_supervalu_recipes")
hungary_soul = import_module("scripts.import.web.scrape_thehungarysoul_recipes")


NESTED_INSTRUCTIONS = [
    {
        "@type": "HowToSection",
        "name": "Sauce",
        "itemListElement": [
            {"@type": "HowToStep", "text": "Mix the sauce."},
            {
                "@type": "HowToSection",
                "itemListElement": [
                    {"@type": "HowToStep", "text": "Simmer gently."},
                ],
            },
        ],
    },
    {"@type": "HowToStep", "name": "Plate the dish."},
    "Serve immediately.",
]


def test_schema_instruction_parsers_flatten_nested_sections():
    expected = [
        "Mix the sauce.",
        "Simmer gently.",
        "Plate the dish.",
        "Serve immediately.",
    ]

    assert foodhero._instructions_from_jsonld(
        {"recipeInstructions": NESTED_INSTRUCTIONS}
    ) == expected
    assert bestofhungary._flatten_instructions(NESTED_INSTRUCTIONS) == expected
    assert hungary_soul._flatten_instructions(NESTED_INSTRUCTIONS) == expected


def test_irishheart_method_accepts_paragraphs_and_nested_lists(monkeypatch):
    soup = BeautifulSoup(
        '<div class="method"><p>Prepare the sauce.</p>'
        '<ol><li>Cook the chicken.</li><li>Serve.</li></ol></div>',
        "html.parser",
    )
    monkeypatch.setattr(irishheart, "_get", lambda _url: soup)

    detail = irishheart.fetch_detail("https://example.test/recipe")

    assert detail["method_steps"] == [
        "Prepare the sauce.",
        "Cook the chicken.",
        "Serve.",
    ]


def test_supervalu_method_accepts_non_list_blocks(monkeypatch):
    soup = BeautifulSoup(
        '<main class="item"><h1 itemprop="name">Soup</h1></main>'
        '<div itemprop="recipeInstructions">'
        '<p>Chop the vegetables.</p><p>Simmer until tender.</p></div>',
        "html.parser",
    )
    monkeypatch.setattr(supervalu, "_get", lambda _url: soup)

    detail = supervalu.fetch_detail("https://example.test/recipe")

    assert detail["method_steps"] == [
        "Chop the vegetables.",
        "Simmer until tender.",
    ]


def test_safefood_backfill_recovers_method_but_not_equipment():
    recipe = {
        "method": ["Cook the pasta."],
        "equipment": [
            "Ingredients",
            "Method",
            "100g pasta",
            "Cook the pasta.",
            "Add the sauce and stir well.",
            "Bake until golden.",
            "Saucepan",
            "Wooden spoon",
        ],
    }

    assert recovered_method(recipe) == [
        "Cook the pasta.",
        "Add the sauce and stir well.",
        "Bake until golden.",
    ]


def test_healthyfoods_nested_wrappers_do_not_duplicate_real_occurrences():
    from unittest.mock import Mock
    scraper = import_module("scripts.import.healthyfoods.export_healthyfoods_recipes_to_json")
    html = '<h1>Example</h1><div id="ingredients-list"><ul>' \
        '<li class="ingredient">1 egg</li>' \
        '<li class="ingredient"><ul><li class="ingredient">1 cup milk</li></ul></li>' \
        '<li class="ingredient">1 egg</li></ul></div>' \
        '<div id="fld_instructions_and_steps"><p>1 Mix.</p><p>2 Cook.</p>' \
        '<h4>Nutrition saved</h4><p>90 calories</p></div>' \
        '<script type="application/ld+json">{"@type":"Recipe",' \
        '"recipeInstructions":["1 Mix.","2 Cook.","90 calories"]}</script>'
    session = Mock()
    session.get.return_value = Mock(text=html)
    recipe = scraper.parse_healthyfood_recipe('https://example.test', session)
    assert recipe['ingredients'] == ['1 egg', '1 cup milk', '1 egg']
    assert recipe['instructions'] == ['1 Mix.', '2 Cook.']
