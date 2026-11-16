from recipe_wrangler.catalog.portion_display import ingredient_display_text


def display(
    name: str, measurement: str, source: str, stored: str = "", source_text: str = ""
):
    return ingredient_display_text(
        ingredient_name=name,
        calculation_measurement=measurement,
        stored_display=stored,
        source_text=source_text,
        source=source,
    )


def test_authored_display_wins_without_duplicating_the_ingredient():
    assert display("eggs", "100 g", "Curated Irish Recipes", "2 medium") == (
        "2 medium eggs", "authored_display"
    )
    assert display("water", "15 ml", "Curated Irish Recipes", "1 tablespoon of water") == (
        "1 tablespoon of water", "authored_display"
    )


def test_exact_source_text_wins_over_display_fragment():
    assert display(
        "eggs", "100 g", "Curated Irish Recipes", "2 medium",
        "2 medium eggs, at room temperature",
    ) == ("2 medium eggs, at room temperature", "authored_source_text")


def test_hungarian_clean_egg_masses_become_counts():
    assert display("Eggs", "50 g", "Curated Hungarian Recipes") == (
        "1 egg", "reviewed_portion_reference_v1"
    )
    assert display("Eggs", "100 g", "Curated Hungarian Recipes") == (
        "2 eggs", "reviewed_portion_reference_v1"
    )


def test_slovenian_source_specific_egg_multiples_become_counts():
    assert display("CHICKEN EGG", "119.68g", "Curated Slovenian Recipes") == (
        "2 eggs", "reviewed_portion_reference_v1"
    )
    assert display("CHICKEN EGG", "418.88 g", "Curated Slovenian Recipes") == (
        "7 eggs", "reviewed_portion_reference_v1"
    )
    assert display("CHICKEN EGG Yolk", "34 g", "Curated Slovenian Recipes") == (
        "2 egg yolks", "reviewed_portion_reference_v1"
    )


def test_ambiguous_fractional_egg_mass_abstains():
    assert display("Eggs", "20 g", "Curated Hungarian Recipes") == (
        "20 g Eggs", "calculation_fallback"
    )


def test_pasta_and_normal_mass_ingredients_stay_in_grams():
    assert display("pasta", "200 g", "Curated Hungarian Recipes") == (
        "200 g pasta", "calculation_fallback"
    )
    assert display("Dried egg pasta", "600 g", "Curated Slovenian Recipes") == (
        "600 g Dried egg pasta", "calculation_fallback"
    )


def test_non_calculation_native_source_is_not_reverse_converted():
    assert display("eggs", "100 g", "Curated Irish Recipes") == (
        "100 g eggs", "calculation_fallback"
    )
