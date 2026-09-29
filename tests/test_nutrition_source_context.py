from recipe_wrangler.tools.recipe_profiling_chain import recover_nutrition_match_name


def test_recovers_canned_chickpea_state_and_packing_medium():
    assert recover_nutrition_match_name(
        "chickpea garbanzo garbanzos",
        "400g can no-added-salt chickpeas in water, drained and rinsed",
    ) == "no-added-salt canned chickpea garbanzo garbanzos in water"


def test_recovers_canned_tuna_and_reduced_sodium_soy_state():
    assert recover_nutrition_match_name(
        "tuna", "1 x 185g tin tuna in spring water, drained"
    ) == "canned tuna in water"
    assert recover_nutrition_match_name(
        "soy sauce", "2 tbsp reduced-sodium soy sauce"
    ) == "reduced-sodium soy sauce"


def test_recovers_smoked_and_tortilla_grain_but_not_prep_prose():
    assert recover_nutrition_match_name(
        "chicken breast", "200g smoked chicken breast, thinly sliced"
    ) == "smoked chicken breast"
    assert recover_nutrition_match_name(
        "tortillas", "8 flour tortillas, warmed"
    ) == "wheat tortillas"
    assert recover_nutrition_match_name(
        "onion", "1 onion, finely chopped"
    ) == "onion"


def test_does_not_borrow_context_from_a_misaligned_original_row():
    assert recover_nutrition_match_name(
        "white wine", "175g dried puy lentils"
    ) == "white wine"
    assert recover_nutrition_match_name(
        "onion", "1 x 400g tin chickpeas"
    ) == "onion"


def test_does_not_combine_alternative_preparation_states():
    assert recover_nutrition_match_name(
        "fresh sage", "1 tbsp fresh sage or 1 tsp dried sage"
    ) == "fresh sage"
    assert recover_nutrition_match_name(
        "corn", "1 can corn or 10 oz frozen corn"
    ) == "canned corn"


def test_can_as_a_verb_is_not_canned_food_context():
    assert recover_nutrition_match_name(
        "water", "1.5 cups water (can take up to 2 cups)"
    ) == "water"
    assert recover_nutrition_match_name(
        "chickpeas", "½ can chickpeas, drained and rinsed"
    ) == "canned chickpeas"
