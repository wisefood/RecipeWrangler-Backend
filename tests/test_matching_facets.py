from recipe_wrangler.matching.facets import extract_candidate_facets, extract_mention_facets


def test_extracts_explicit_canned_cooked_and_sodium_facets() -> None:
    result = extract_mention_facets(
        "black beans",
        "reduced-sodium canned black beans, cooked and drained",
    )

    assert result.facets.preparation_state == "cooked"
    assert result.facets.preservation_methods == frozenset({"canned"})
    assert result.facets.processes == frozenset({"drained"})
    assert result.facets.colour == "black"
    assert result.facets.sodium_level == "reduced"


def test_extracts_source_part_form_and_process() -> None:
    facets = extract_mention_facets(
        "chicken breast",
        "grilled chicken breast, sliced",
    ).facets

    assert facets.biological_source == "chicken"
    assert facets.food_part == "breast"
    assert facets.preparation_state == "cooked"
    assert facets.processes == frozenset({"grilled", "sliced"})


def test_unstated_facets_remain_unknown() -> None:
    facets = extract_mention_facets("olive oil", "olive oil").facets

    assert facets.preparation_state is None
    assert facets.biological_source is None
    assert facets.compound is None


def test_extracts_explicit_fresh_as_unpreserved_form() -> None:
    facets = extract_mention_facets("peas", "fresh peas").facets

    assert facets.preservation_methods == frozenset({"fresh"})


def test_conflicting_preparation_words_do_not_force_one_state() -> None:
    facets = extract_mention_facets("chestnuts", "raw or cooked chestnuts").facets

    assert facets.preparation_state == "unknown"
    assert facets.compound is True


def test_measurement_slash_does_not_imply_compound_food() -> None:
    facets = extract_mention_facets(
        "vanilla paste",
        "vanilla paste",
        "1 teaspoon / 5 ml",
    ).facets

    assert facets.physical_form == "paste"
    assert facets.compound is None


def test_extracts_candidate_label_with_same_facet_vocabulary() -> None:
    facets = extract_candidate_facets(
        "Beans, kidney, red, canned, no salt added"
    ).facets

    assert facets.identity == "beans, kidney, red, canned, no salt added"
    assert facets.food_part == "kidney"
    assert facets.preservation_methods == frozenset({"canned"})
    assert facets.colour == "red"
    assert facets.sodium_level == "no_added_salt"


def test_packing_liquid_is_not_the_foods_physical_form() -> None:
    for label in (
        "Pineapple, canned, in juice",
        "Pineapple, in pineapple juice, canned, drained",
    ):
        facets = extract_candidate_facets(label).facets
        assert facets.preservation_methods == frozenset({"canned"})
        assert facets.physical_form is None


def test_leftover_food_is_known_to_be_cooked() -> None:
    facets = extract_mention_facets("turkey", "leftover turkey").facets

    assert facets.preparation_state == "cooked"


def test_defrosted_food_is_known_to_be_frozen() -> None:
    facets = extract_mention_facets("peas", "peas, defrosted").facets

    assert facets.preservation_methods == frozenset({"frozen"})


def test_poultry_slices_candidate_is_a_cooked_product() -> None:
    facets = extract_candidate_facets("Turkey slices").facets

    assert facets.preparation_state == "cooked"


def test_extracts_coordinated_no_added_sugar_and_salt_claim() -> None:
    facets = extract_mention_facets(
        "natural peanut butter",
        "natural peanut butter",
        "no added sugar and salt",
    ).facets

    assert facets.sodium_level == "no_added_salt"
    assert facets.sugar_level == "no_added_sugar"
    assert facets.compound is None


def test_extracts_egg_white_as_food_part() -> None:
    facets = extract_candidate_facets("Egg white, raw").facets

    assert facets.food_part == "egg_white"
    assert facets.colour == "white"


def test_preparation_alternatives_in_note_do_not_make_food_compound() -> None:
    facets = extract_mention_facets(
        "carrot",
        "carrot",
        "peeled and grated or thinly sliced",
    ).facets

    assert facets.compound is None
    assert facets.processes == frozenset({"grated", "peeled", "sliced"})


def test_normalizes_process_inflections() -> None:
    assert extract_mention_facets("chicken", "roast chicken").facets.processes == frozenset({"roasted"})
    assert extract_mention_facets("potato", "bake potato").facets.processes == frozenset({"baked"})


def test_extracts_expanded_cooking_vocabulary_as_cooked() -> None:
    facets = extract_mention_facets(
        "vegetables",
        "blanched, braised, poached, sautéed and toasted vegetables",
    ).facets

    assert facets.preparation_state == "cooked"
    assert facets.processes == frozenset(
        {"blanched", "braised", "poached", "sauteed", "toasted"}
    )


def test_extracts_non_cooking_transformations_without_assuming_cooked() -> None:
    facets = extract_mention_facets(
        "soy product",
        "fermented concentrated refined soy product",
    ).facets

    assert facets.preparation_state is None
    assert facets.processes == frozenset({"concentrated", "fermented", "refined"})


def test_candidate_prepared_state_is_not_silently_ignored() -> None:
    facets = extract_candidate_facets("Chicken fillet prepared").facets

    assert facets.processes == frozenset({"prepared"})


def test_extracts_nutritionally_required_product_qualifiers() -> None:
    milk = extract_mention_facets(
        "milk", "4 dessertspoons of semi-skimmed milk"
    ).facets
    bread = extract_mention_facets(
        "bread", "2 slices wholemeal bread"
    ).facets
    sweetcorn = extract_mention_facets(
        "sweetcorn", "tinned sweetcorn, low sugar and salt"
    ).facets

    assert milk.fat_level == "semi_skimmed"
    assert bread.grain_refinement == "wholegrain"
    assert sweetcorn.sugar_level == "low"
    assert sweetcorn.sodium_level == "low"


def test_whole_oats_is_grain_refinement_not_physical_form() -> None:
    facets = extract_mention_facets("whole oats", "whole oats").facets

    assert facets.grain_refinement == "wholegrain"
    assert facets.physical_form is None


def test_extracts_composition_table_low_fat_and_no_added_claims() -> None:
    half_fat_cheddar = extract_candidate_facets(
        "Cheese, Cheddar type, half fat"
    ).facets
    reduced_fat_cheddar = extract_candidate_facets(
        "Cheese, Cheddar type, '30% less fat'"
    ).facets
    natural_peanut_butter = extract_candidate_facets(
        "Peanut butter 100% peanuts wo salt"
    ).facets

    assert half_fat_cheddar.fat_level == "low"
    assert reduced_fat_cheddar.fat_level == "reduced"
    assert natural_peanut_butter.sodium_level == "no_added_salt"
    assert natural_peanut_butter.sugar_level == "no_added_sugar"


def test_optional_gluten_free_does_not_become_required() -> None:
    required = extract_mention_facets("oats", "gluten-free oats").facets
    optional = extract_mention_facets(
        "oats", "gluten-free oats if necessary"
    ).facets

    assert required.gluten_free is True
    assert optional.gluten_free is None
