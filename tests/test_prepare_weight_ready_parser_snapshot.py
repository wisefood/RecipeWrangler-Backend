from scripts.prepare_weight_ready_parser_snapshot import (
    _DISCRETIONARY_NOTE_RE,
    _NON_INGREDIENT_NAMES,
)
from recipe_wrangler.tools.parse_recipe_tool import restore_explicit_source_measurement


def restore(item: dict) -> str | None:
    return restore_explicit_source_measurement(item)[0]


def test_restores_bare_oil_spray_from_display() -> None:
    item = {"name": "olive oil", "measurement": "1.0", "display": "spray"}

    assert restore(item) == "1.0 spray"


def test_restores_missing_oil_measurement_from_display() -> None:
    item = {"name": "cooking oil", "measurement": "", "display": "oil spray"}

    assert restore(item) == "1 spray"


def test_restores_source_unit_from_display() -> None:
    item = {"name": "salt", "measurement": "2.0", "display": "2 pinches of salt"}

    assert restore(item) == "2.0 pinch"


def test_restores_standard_unit_dropped_from_display() -> None:
    teaspoon = {
        "name": "paprika",
        "measurement": "0.5",
        "display": "½ teaspoon Italian seasoning",
    }
    cup = {
        "name": "parsley",
        "measurement": "0.25",
        "display": "¼ cup chopped parsley",
    }

    assert restore(teaspoon) == "0.5 teaspoon"
    assert restore(cup) == "0.25 cup"


def test_restores_standard_unit_when_parser_dropped_entire_measurement() -> None:
    item = {
        "name": "raspberries",
        "measurement": "",
        "display": "⅔ cup raspberries",
    }

    assert restore(item) == "0.67 cup"


def test_restores_explicit_units_with_food_or_in_leading_note() -> None:
    display = {
        "name": "mixed food",
        "measurement": "4.0",
        "display": "4 tablespoons vegetables, fruit or cooked meat",
    }
    note = {
        "name": "parsley",
        "measurement": "3.0",
        "display": "3",
        "note": "fresh sprigs",
    }

    assert restore(display) == "4.0 tablespoon"
    assert restore(note) == "3.0 sprig"


def test_restores_source_unit_from_leading_note_when_display_is_numeric() -> None:
    item = {
        "name": "raisins",
        "measurement": "1.0",
        "display": "1",
        "note": "handful",
    }

    assert restore(item) == "1.0 handful"


def test_restores_dimension_and_count_units_from_source_clues() -> None:
    ginger = {"name": "ginger", "measurement": "1.0", "display": "5cm-piece"}
    lettuce = {
        "name": "lettuce",
        "measurement": "8.0",
        "display": "8",
        "note": "washed leaves",
    }
    fennel = {
        "name": "fennel",
        "measurement": "1.0",
        "display": "1",
        "note": "bulb, thinly sliced",
    }

    assert restore(ginger) == "5 cm"
    assert restore(lettuce) == "8.0 leaf"
    assert restore(fennel) == "1.0 bulb"


def test_explicit_count_noun_overrides_size_only_measurement() -> None:
    lettuce = {
        "name": "lettuce",
        "measurement": "8.0 large",
        "display": "8 large",
        "note": "leaves",
    }
    pineapple = {
        "name": "pineapple",
        "measurement": "8.0",
        "display": "8",
        "note": "spears, about 1 ounce each",
    }

    assert restore(lettuce) == "8.0 leaf"
    assert restore(pineapple) == "226.8 g"


def test_does_not_replace_explicit_volume_alternative_with_spray() -> None:
    item = {
        "name": "oil",
        "measurement": "0.25",
        "display": "1/4 teaspoon oil (optional) or cooking spray (optional)",
    }

    assert restore(item) is None


def test_does_not_take_incidental_unit_from_note() -> None:
    item = {
        "name": "spring onions",
        "measurement": "2.0",
        "display": "2",
        "note": "sliced, handful reserved for garnish",
    }

    assert restore(item) is None


def test_only_explicit_discretionary_notes_match_placeholder_policy() -> None:
    assert _DISCRETIONARY_NOTE_RE.search("fresh, to garnish (optional)")
    assert _DISCRETIONARY_NOTE_RE.search("to season")
    assert _DISCRETIONARY_NOTE_RE.search("for garnishing")
    assert _DISCRETIONARY_NOTE_RE.search("for dusting")
    assert not _DISCRETIONARY_NOTE_RE.search("for frying")
    assert not _DISCRETIONARY_NOTE_RE.search("finely chopped")


def test_corrects_bad_simple_and_compound_volume_conversions() -> None:
    breadcrumbs = {
        "name": "breadcrumbs",
        "measurement": "0.75 ml",
        "display": "¾ cup",
    }
    spread = {
        "name": "dairy-free spread",
        "measurement": "130 ml",
        "display": "¼ cup + 3 tbsp",
    }

    assert restore(breadcrumbs) == "180.0 ml"
    assert restore(spread) == "105.0 ml"


def test_restores_descriptive_count_units_and_each_masses() -> None:
    bread = {
        "name": "sourdough",
        "measurement": "4.0",
        "display": "4 thick slices",
    }
    steaks = {
        "name": "pork steaks",
        "measurement": "4.0",
        "display": "4",
        "note": "about 120g each, chopped",
    }

    assert restore(bread) == "4.0 slices"
    assert restore(steaks) == "480.0 g"


def test_restores_implied_single_portion_from_leading_note() -> None:
    parsley = {
        "name": "parsley",
        "measurement": "",
        "display": "",
        "note": "small bunch, chopped",
    }

    assert restore(parsley) == "1.0 bunch"


def test_known_section_artifacts_are_not_ingredients() -> None:
    assert {"instructions", "information", "garnish"} <= _NON_INGREDIENT_NAMES


def test_restores_singular_pinch_dash_from_display_or_note() -> None:
    assert restore({"name": "salt", "measurement": "1.0", "display": "1 pinch"}) == "1.0 pinch"
    assert restore({"name": "salt", "measurement": "1", "display": "1", "note": "dash"}) == "1 dash"


def test_display_unit_and_quantity_override_wrong_bare_quantity() -> None:
    item = {"name": "margarine", "measurement": "2.5", "display": "1 ½ Tablespoons"}

    assert restore(item) == "1.5 tablespoon"


def test_a_few_sprigs_and_whole_and_bags_from_display() -> None:
    assert restore({"name": "rosemary", "measurement": "1.0", "display": "a few sprigs"}) == "3 sprig"
    assert restore({"name": "green tea", "measurement": "8.0", "display": "8 single-serving bags"}) == "8.0 bags"


def test_note_mass_recovered_for_bare_count() -> None:
    assert restore({"name": "raspberries", "measurement": "15.0", "display": "15", "note": "60g / 2oz."}) == "60.0 g"


def test_alternative_clause_never_supplies_unit_or_mass() -> None:
    beets = {"name": "beets", "measurement": "6.0", "display": "6", "note": "cooked, or 1 can (15 ounces) drained"}
    paprika = {"name": "paprika", "measurement": "1.0", "display": "1", "note": "or smoked paprika, teaspoon"}

    assert restore(beets) is None
    assert restore(paprika) is None


def test_note_starting_with_or_is_entirely_an_alternative() -> None:
    item = {"name": "crispy lettuce", "measurement": "1.0", "display": "A bag", "note": "or 1 Little Gem lettuce, broken into leaves"}

    assert restore(item) is None


def test_stock_cube_line_uses_cube_not_dissolving_water() -> None:
    from recipe_wrangler.tools.parse_recipe_tool import restore_stock_cube_measurement as cube

    assert cube("beef stock", "1 beef stock cube dissolved in 800 ml water or use homemade stock") == "1.0 cube"
    assert cube("stock", "¼ chicken stock cube, dissolved in 50ml / 1½fl oz. of boiling water") == "0.25 cube"
    assert cube("stock", "2 low-salt stock cubes dissolved in 1,150ml of boiling water") == "2.0 cube"
    assert cube("beef stock", "600 ml beef stock or 2 low salt stock cubes dissolved in 600ml hot water.") is None
    assert cube("vegetable stock", "275ml / ½ pint of vegetable stock, or 1 vegetable stock cube mixed with 275ml") is None
    assert cube("chicken stock cube", "2 pint Chicken Stock Cube") is None
    assert cube("stock", "1 low-salt beef or vegetable stock cube dissolved in 275 ml boiling water") == "1.0 cube"
    assert cube("stock", "1 litre / 1¾ pints of stock or 2 beef cubes dissolved in 1 litre") is None


def test_whole_bird_name_needs_whole_evidence_and_no_parts() -> None:
    from recipe_wrangler.tools.parse_recipe_tool import restore_whole_bird_name as bird

    assert bird({"name": "chicken", "measurement": "1.0", "display": "1 whole", "note": "cut up, skin removed"}) == "whole skinless chicken"
    assert bird({"name": "chicken", "measurement": "1.5 kg", "display": "1 medium sized chicken (around 1.5kg)", "note": ""}) == "whole chicken"
    assert bird({"name": "duck", "measurement": "1.0 whole", "display": "1 whole", "note": ""}) == "whole duck"
    assert bird({"name": "chicken", "measurement": "6.0", "display": "6", "note": "drumsticks/thighs/breasts, organic"}) is None
    assert bird({"name": "chicken", "measurement": "1.0 cup", "display": "1 cup", "note": "cooked, diced"}) is None
    assert bird({"name": "chicken breasts", "measurement": "4.0", "display": "4 whole", "note": ""}) is None


def test_volume_stock_cube_name_becomes_prepared_stock() -> None:
    from scripts.prepare_weight_ready_parser_snapshot import _VOLUME_MEASUREMENT_RE, _VOLUME_STOCK_CUBE_RE

    assert _VOLUME_STOCK_CUBE_RE.match("Chicken Stock Cube") and _VOLUME_MEASUREMENT_RE.search("3.0 litre")
    assert _VOLUME_MEASUREMENT_RE.search("2.0 pint") and not _VOLUME_MEASUREMENT_RE.search("1.0 cube")


def test_leading_quantity_corrected_only_when_same_unit_and_no_range() -> None:
    from recipe_wrangler.tools.parse_recipe_tool import restore_leading_quantity_from_source as fix

    assert fix("2.5 cup", "1 ½ cups milk (any type)", "1 ½ cups milk") == "1.5 cup"
    assert fix("5.0 cups", "2 ½ cups all-purpose flour , divided", "2 ½ cups") == "2.5 cup"
    assert fix("0.75 cup", "½ cup to 1 cup nuts", "½ cup") is None
    assert fix("1.5 tbsp", "1½ tablespoons of olive oil", "1½ tablespoons") is None
    assert fix("240 ml", "1 cup milk", "1 cup") is None
    assert fix("3.0 cups", "1 punnet (1 ½ cups) fresh strawberries", "1 ½ cups") is None
    # display of a different (sibling) row must not be corrected from this source line
    assert fix("1.0 tsp", "1/4 teaspoon oregano", "1 teaspoon") is None


def test_bouillon_form_from_unit_and_low_sodium_note() -> None:
    from recipe_wrangler.tools.parse_recipe_tool import restore_bouillon_form as form

    assert form({"name": "chicken bouillon", "measurement": "2.0 tsp", "note": "low sodium"}) == ("low sodium chicken bouillon granules", "2.0 tsp")
    assert form({"name": "chicken bouillon", "measurement": "1.0", "note": "low-sodium, cube"}) == ("low sodium chicken bouillon cube", "1.0 cube")
    assert form({"name": "beef bouillon", "measurement": "2.0 teaspoons", "note": "2 cubes"}) == ("beef bouillon granules", "2.0 teaspoons")
    assert form({"name": "chicken bouillon", "measurement": "500 ml", "note": ""}) is None
    assert form({"name": "chicken stock", "measurement": "1.0 tsp", "note": ""}) is None


def test_blank_oil_for_greasing_becomes_greasing_unit_but_frying_oil_does_not() -> None:
    from recipe_wrangler.tools.parse_recipe_tool import restore_greasing_measurement as grease

    assert grease({"name": "oil", "measurement": "", "display": "", "note": "to grease"}) == "1 greasing"
    assert grease({"name": "oil", "measurement": "", "display": "A little oil for greasing", "note": ""}) == "1 greasing"
    assert grease({"name": "oil", "measurement": "", "display": "oil for frying", "note": ""}) is None
    assert grease({"name": "olive oil", "measurement": "1 tbsp", "display": "", "note": "to grease"}) is None


def test_unicode_mixed_fraction_cups_and_combined_with_clause() -> None:
    assert restore({"name": "flour", "measurement": "1.5 ml", "display": "1½ cups"}) == "360.0 ml"
    item = {"name": "cornflour", "measurement": "1.0 ml", "display": "1 tablespoon cornflour combined with 2 tablespoons cold water"}
    assert restore(item) == "15.0 ml"


def test_container_unit_recovered_from_raw_source_line() -> None:
    item = {"name": "cream cheese", "measurement": "1.0", "display": "1 tub cream cheese (use lite if preferred)"}

    assert restore(item) == "1.0 tub"
