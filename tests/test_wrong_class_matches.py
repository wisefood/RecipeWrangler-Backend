"""Wrong-class matches behind Living Lab calorie outliers (26 Nov): the reviewed layer must override them.

Each case is a real HealthyFoods line whose live profile used the named record; source pages give
514 kcal (risotto, ours 1,218) and 373 kcal (mussels, ours 1,088) per serving.
"""
import pytest

from recipe_wrangler.matching.reviewed_aliases import REVIEWED_SELECTIONS, stated_state_name


@pytest.mark.parametrize("name,original,matched,expected", [
    ("butter or cannellini beans", "400g can butter or cannellini beans, drained, rinsed", "Butter", "butter or cannellini beans"),
    ("white or mixed beans", "2 x 400g cans white or mixed beans", "Sugar, white", "white or mixed beans"),
    ("pepper strips", "450g roasted pepper strips, drained", "PEPPER, BLACK PEPPER", "roasted red peppers"),
    ("brown rice", "1 pot 90-second brown rice", "Rice brown raw", "cooked brown rice"),
    ("tagliatelle", "500g fresh tagliatelle", "Pasta, dry, regular, raw", "fresh pasta"),
    ("kumara sweet-potato chips", "600g frozen kumara sweet-potato chips", "Snacks, sweet potato chips, unsalted", "oven sweet potato fries"),
    ("pork roast", "1–1.3kg boneless, skinless pork roast", "Pork belly boneless without skin", "lean pork"),
    ("beef blade roast", "1.4kg beef blade roast, trimmed of fat, cut into 3cm chunks", "Beef, chuck, blade roast, separable lean and fat, raw", "lean beef"),
    ("lamb racks", "2 x 6-cutlet French-trimmed lean lamb racks", "Lamb, rack of, raw, lean and fat", "trimmed lamb cutlets with bone"),
    ("chicken", "1.3kg chicken (see tip)", "Chicken, meat, raw", "whole chicken with bone"),
])
def test_wrong_class_match_gets_the_reviewed_record(name, original, matched, expected):
    assert stated_state_name(name, original, matched) == expected
    assert expected in REVIEWED_SELECTIONS


@pytest.mark.parametrize("name,original,matched", [
    ("butter beans", "400g can butter beans, drained", "Beans, butter, canned, re-heated, drained"),  # already a bean
    ("black pepper", "1 teaspoon ground black pepper", "PEPPER, BLACK PEPPER"),  # the spice is right
    ("brown rice", "1 cup brown rice", "Rice brown raw"),  # dry rice is right
    ("pork belly", "500g pork belly", "Pork belly boneless without skin"),
    ("tagliatelle", "400g dried tagliatelle", "Pasta, dry, regular, raw"),
    ("vanilla bean", "1 vanilla bean, split in half", "Vanilla, pod"),  # not a pulse
    ("chicken", "500g chicken, diced", "Chicken, meat, raw"),  # grams of meat, not a whole bird
    ("lima beans", "2 cups lima beans, cooked (or canned, low-sodium)", "Butter bean or yellow bean, canned, drained"),
    ("broccolini and green beans", "500g mix broccolini and green beans", "Broccoli, green, raw"),
    ("ground pork", "1 pound ground pork", "Pork, belly, raw"),
    ("smoked pork", "350 g smoked pork (for example, pork ribs)", "Pork belly, cured, smoked, raw"),
    ("Pork", "100 g Pork, shoulder joint (collar), raw, lean and fat", "Pork, shoulder joint (collar), raw, lean and fat"),  # source states lean and fat
])
def test_right_matches_are_left_alone(name, original, matched):
    assert stated_state_name(name, original, matched) is None


def test_suggestion_bracket_is_not_part_of_the_food():
    from recipe_wrangler.matching.runtime import without_suggestion
    assert without_suggestion('1 ⅓ cups all-purpose flour ( try half whole-wheat flour)') == '1 ⅓ cups all-purpose flour'
    assert without_suggestion('2 tablespoons soy sauce (try a reduced salt soy sauce)') == '2 tablespoons soy sauce'
    assert without_suggestion('1 cup rice (basmati, cooked)') == '1 cup rice (basmati, cooked)'
