from recipe_wrangler.services.adaptation.food_roles import role_of, role_problems


def test_roles():
    assert role_of('Cheese, Feta') == 'cheese' and role_of('Oil, olive') == 'oil' and role_of('Basmati rice, raw') == 'rice_grain'
    assert role_of('Chicken breast skinless') == 'poultry' and role_of('Mushrooms, white, raw') == 'vegetable'


def test_sensible_swaps_pass():
    for a, b in [('Cheese, Feta', 'Mozzarella cheese 18% fat'), ('Cream sour', 'Greek yoghurt, skimmed milk, plain'),
                 ('Rice, white, Italian Arborio risotto, raw', 'Quinoa, black, uncooked'), ('Butter', 'Oil, olive'),
                 ('Pork, loin', 'Turkey breast skinless boneless raw')]:
        assert not [p for p in role_problems(a, b) if p != 'role_unknown'] or role_problems(a, b) == [], (a, b, role_problems(a, b))


def test_the_nonsense_from_the_graph_is_blocked():
    for a, b in [('Extra virgin olive oil', 'TAPAS, MARINATED CHAMPIGNON, OLIVE OIL'), ('Sugar, white', 'Syrup (eg. Mint, strawberry etc.), diluted'),
                 ('Tomato, round, raw', 'Radish, raw'), ('Brown sugar', 'Infusion, brewed, no added sugars'), ('Cream sour', 'Baby milk, follow-on formula, ready'),
                 ('Butter', 'Cocoa milk'), ('Garlic, raw', 'Peas, chick peas, uncooked'), ('Onion, raw', 'Yellow onion, raw'),
                 ('Cherry tomatoes raw', 'Crow berries, raw')]:
        assert role_problems(a, b), (a, b)


def test_sugar_for_syrup_and_syrup_for_tea_are_blocked():
    assert 'sugar_for_other_sugar' in role_problems('Sugar, white', 'Syrup maple')
    assert role_problems('Maple syrup', 'Infusion, brewed, no added sugars')
    assert not role_problems('Sugar, white', 'Sweetener, aspartame, powder')


def test_dishes_and_scale_changes_are_not_substitutions():
    assert role_problems('Eggs, chicken, whole, raw', 'EGG, QUAIL, RAW')
    assert role_problems('Pasta, white, dried, raw', 'Barley, uncooked')
    assert role_problems('Beef, raw', 'Beans, dried, raw')
    assert role_problems('Sweet pepper, green, raw', 'Garden peas and carrots, frozen')
    assert not role_problems('Basmati rice, raw', 'Barley, uncooked')


def test_prepared_dishes_are_not_foods():
    from recipe_wrangler.services.adaptation.food_roles import is_compound
    for label in ['CARROT, BOILED, SALT AND OIL', 'Milk, semi-skimmed, beetroot/a', 'MULTIGRAIN BREAD, CARROT ROLL', 'SWEET POTATO CHICK PEA CURRY', 'Sunflower seed butter, salted']:
        assert is_compound(label), label
    for label in ['Chicken, minced meat, 8 % fat, raw', 'Peas, yellow, boiled', 'Cheese, processed, semi-hard', 'Rice, quinoa and multigrain mix', 'Turkey, flesh only, raw', 'Butter, 80% fat']:
        assert not is_compound(label), label
