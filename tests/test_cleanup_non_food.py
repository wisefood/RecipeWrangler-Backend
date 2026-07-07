import importlib.util
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_non_food_ingredients.py"
_spec = importlib.util.spec_from_file_location("cleanup_non_food_ingredients", _SCRIPT)
clx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clx)  # safe: GraphDatabase.driver() is lazy, no connection on import


class NonFoodClassifierTests(unittest.TestCase):
    def test_pure_equipment_lines_are_deletable(self):
        for name in (
            "cooking spray",
            "nonstick cooking spray",
            "vegetable oil cooking spray",
            "olive oil flavored cooking spray",
            "butter - flavored cooking spray",
            "spray oil",
            "cooking spray oil",
            "Pam cooking spray",
            "aluminum foil",
            "Reynolds Wrap Foil",
            "plastic wrap",
            "bamboo skewers",
            "wooden skewers, soaked in cold water",
            "baking sheet",
            "pie plate",
            "rolling pin",
            "toothpicks",
            "spatula",
            "frying pan",
            "cheese grater",
            "kitchen string",
            "string or twine",
            "cm cake tin",
            "ziploc bag",
            "Digital probe thermometer",
        ):
            with self.subTest(name=name):
                self.assertTrue(clx._is_pure_equipment(name), name)

    def test_real_food_lines_are_kept(self):
        for name in (
            "olive oil, plus extra, to brush",
            "1 tablespoon olive oil (or cooking spray)",
            "rice bran oil or cooking spray oil",
            "x 150g chicken breast fillets cooking spray oil",
            "egg, beaten, to brush pastry",
            "carrots, julienned or peeled with a grater",
            "1 cup quinoa (if not pre-rinsed, rinse well using a fine-mesh strainer)",
            "almonds, finely chopped in food processor",
            "Chicken thigh and spring onion skewers",
            "maple syrup, plus extra to brush",
            "bocconcini, drained on paper towels, torn",
            "gold leaf foil",
            "edible silver foil",
            # plain food that happens to contain a unit substring
            "string beans",
            "string cheese",
            "rack of lamb",
            "pot roast",
        ):
            with self.subTest(name=name):
                self.assertFalse(clx._is_pure_equipment(name), name)

    def test_neo4j_regex_filter_matches_equipment_but_not_plain_food(self):
        import re
        rx = re.compile(clx.REGEX_NEO4J)
        self.assertTrue(rx.match("cooking spray"))
        self.assertTrue(rx.match("bamboo skewers, soaked"))
        self.assertTrue(rx.match("aluminum foil"))
        self.assertIsNone(rx.match("string beans"))
        self.assertIsNone(rx.match("rack of lamb"))
        self.assertIsNone(rx.match("canned tomatoes"))
        self.assertIsNone(rx.match("all-purpose flour"))


if __name__ == "__main__":
    unittest.main()
