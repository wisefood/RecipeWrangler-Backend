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


def test_nutrition_swaps_stay_inside_a_family():
    good = [('Sugar, white', 'Syrup maple'), ('Couscous, plain, cooked', 'Quinoa, black, cooked'),
            ('Spinach, baby, raw', 'Kale, raw'), ('Cheese Edamer', 'Yoghurt low fat')]
    bad = [('Lentils, red, split, dried', 'Lentils, green, puy, uncooked'),  # owner 19 Nov: the gain is a table artifact
           ('Onion, raw', 'Yellow onion, raw'), ('Tomatoes, cherry, raw', 'Crow berries, raw'), ('Cherry tomatoes raw', 'Lime, raw'), ('Tomatoes, raw', 'Radish black raw'),
           ('Garlic, raw', 'Carrot black, raw'), ('Sweet potato, raw', 'Carrot black, raw'), ('Apples, eating, raw', 'Lime, raw'),
           ('Vegetable oil', 'COOKING OIL, AVERAGE, 87% RA'),
           ('2 skinless chicken fillets', 'Goose barnacle raw'), ('Honey', 'Saccharin sweetener'), ('Cucumber, raw', 'Tomatoes, cherry, raw'), ('Courgette', 'Pumpkin, raw'),
           ('Lentils, red, split, dried', 'Lentil sprouts, green, raw'), ('Lentils, red, split, dried', 'Flat bean, raw'), ('Red onion', 'Onion, yellow, raw'), ('Leek', 'Onion, Norwegian, raw'),
           ('Beetroot, raw', 'Carrot black, raw'), ('Liver, chicken, raw', 'Turkey, light meat, raw'), ('Parmesan, grated', 'Yoghurt full fat w cereal/muesli')]
    for a, b in good:
        assert not role_problems(a, b, lenient=True), (a, b, role_problems(a, b, lenient=True))
    for a, b in bad:
        assert role_problems(a, b, lenient=True), (a, b)


def test_poultry_does_not_swap_with_fish_but_may_with_other_meat():
    assert role_problems('Chicken breast', 'Salmon, raw')
    assert not role_problems('Chicken breast', 'Turkey breast skinless boneless raw') and not role_problems('Pork, loin', 'Chicken breast')


def test_poultry_is_not_swapped_for_red_meat():
    assert 'poultry_to_red_meat' in role_problems('Chicken, drumsticks, raw', 'Pork, fillet medallions, raw, lean', lenient=True)
    assert 'poultry_to_red_meat' not in role_problems('Pork, loin, raw', 'Chicken breast meat only raw', lenient=True)


def test_no_game_targets_and_no_processed_to_processed_meat():
    assert 'game_meat_target' in role_problems('Chorizo, uncooked', 'Venison, roebuck, raw', lenient=True)
    assert 'processed_to_processed_meat' in role_problems('Bacon rashers, cooked', 'Toulouse sausage, cooked', lenient=True)
    assert 'processed_to_processed_meat' not in role_problems('Bacon rashers, streaky, raw', 'Chicken, light meat, raw', lenient=True)


def test_runner_beans_are_green_beans_and_fat_is_not_meat():
    from recipe_wrangler.services.adaptation.food_roles import family_of
    assert family_of('Beans, runner, boiled in unsalted water') == 'green_bean' != family_of('BEANS, white, cooked')
    assert 'fat_trimming_is_not_meat' in role_problems('Pork, fat, average, raw', 'Lamb, shoulder, lean, raw', lenient=True)


def test_dry_lentil_variety_swap_is_blocked_but_not_canned_to_cooked():
    from recipe_wrangler.services.adaptation.food_roles import role_problems
    assert 'dry_lentil_variety_swap' in role_problems('Lentils, red, split, dried, raw', 'Lentils, green, puy, uncooked', lenient=True)
    assert 'dry_lentil_variety_swap' not in role_problems('Lentils, canned, drained', 'Lentils, boiled', lenient=True)
