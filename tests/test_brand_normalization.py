from recipe_wrangler.utils.brand_normalization import (
    BrandIngredientDecision,
    BrandReviewDecision,
    clean_generic_name,
    generic_name_is_valid,
    validate_brand_decision,
    validate_brand_review_decision,
)


def test_clean_generic_name_preserves_food_identity_words():
    assert clean_generic_name("  Taco Tortilla™  ") == "taco tortilla"
    assert clean_generic_name("gluten-free cola") == "gluten-free cola"


def test_valid_generic_names_cover_brand_removal_and_product_genericization():
    assert generic_name_is_valid(
        "farrah’s taco tortilla",
        "taco tortilla",
        "Farrah’s",
    )
    assert generic_name_is_valid("Coca-Cola", "cola", "Coca-Cola")
    assert generic_name_is_valid(
        "kraft grated parmesan cheese",
        "grated parmesan cheese",
        "Kraft",
    )


def test_unsafe_generic_names_are_rejected():
    assert not generic_name_is_valid("heinz ketchup", "", "Heinz")
    assert not generic_name_is_valid("heinz ketchup", "heinz ketchup", "Heinz")
    assert not generic_name_is_valid("heinz ketchup", "heinz sauce", "Heinz")
    assert not generic_name_is_valid("farrah’s taco tortilla", "food", "Farrah’s")
    assert not generic_name_is_valid("hershey's syrup", "syrup", "Hershey's")
    assert not generic_name_is_valid(
        "tony chachere's seasoning",
        "seasoning",
        "Tony Chachere's",
    )
    assert not generic_name_is_valid("boursin cheese", "cheese", "Boursin")
    assert not generic_name_is_valid("crispix cereal", "cereal", "Crispix")
    assert not generic_name_is_valid("wesson oil", "oil", "Wesson")
    assert not generic_name_is_valid("dorito", "chip", "Dorito")
    assert not generic_name_is_valid("quaker cereals", "cereals", "Quaker")
    assert not generic_name_is_valid("butterfinger bb's", "bb's", "Butterfinger")


def test_non_brand_decision_is_forced_to_keep():
    decision = BrandIngredientDecision(
        ingredient_name="goat's cheese",
        is_branded=False,
        brand_name="goat",
        generic_name="cheese",
        confidence=0.95,
        recommended_action="normalize",
        reason="possessive",
    )
    validated = validate_brand_decision("goat's cheese", decision)
    assert validated.recommended_action == "keep"
    assert validated.brand_name is None
    assert validated.generic_name is None


def test_invalid_llm_normalization_is_downgraded_to_review():
    decision = BrandIngredientDecision(
        ingredient_name="mystery product",
        is_branded=True,
        brand_name="Mystery",
        generic_name="product",
        confidence=0.7,
        recommended_action="normalize",
        reason="commercial name",
    )
    validated = validate_brand_decision("mystery product", decision)
    assert validated.recommended_action == "review"
    assert "unsafe" in validated.reason


def test_source_label_cannot_be_approved_as_a_brand():
    decision = BrandReviewDecision(
        ingredient_name="recipe1m tomato",
        verdict="remove_brand",
        brand_name="recipe1m",
        generic_name="tomato",
        confidence=0.9,
        reason="commercial brand",
    )
    validated = validate_brand_review_decision("recipe1m tomato", decision)
    assert validated.verdict == "keep_original"
    assert "source label" in validated.reason


def test_review_admission_of_lost_flavour_requires_manual_review():
    decision = BrandReviewDecision(
        ingredient_name="orange jell-o",
        verdict="remove_brand",
        brand_name="Jell-O",
        generic_name="gelatin dessert",
        confidence=0.9,
        reason="Jell-O is a brand, but orange flavor is lost.",
    )
    validated = validate_brand_review_decision("orange jell-o", decision)
    assert validated.verdict == "needs_review"
    assert "lost identity" in validated.reason


def test_review_request_or_more_precise_admission_is_not_approved():
    for reason in (
        "Cookie crumb loses the product identity. Needs review.",
        "Light corn syrup would be more precise.",
    ):
        decision = BrandReviewDecision(
            ingredient_name="brand syrup",
            verdict="remove_brand",
            brand_name="Brand",
            generic_name="light syrup",
            confidence=0.9,
            reason=reason,
        )
        validated = validate_brand_review_decision("brand syrup", decision)
        assert validated.verdict == "needs_review"


def test_visible_flavour_is_preserved_even_if_review_reason_misses_it():
    decision = BrandReviewDecision(
        ingredient_name="strawberry jell-o gelatin dessert",
        verdict="remove_brand",
        brand_name="Jell-O",
        generic_name="gelatin dessert",
        confidence=0.9,
        reason="Jell-O is a brand.",
    )
    validated = validate_brand_review_decision(
        "strawberry jell-o gelatin dessert",
        decision,
    )
    assert validated.verdict == "needs_review"
    assert "strawberry" in validated.reason


def test_lexical_gate_allows_equivalent_word_forms():
    decision = BrandReviewDecision(
        ingredient_name="splenda granular",
        verdict="remove_brand",
        brand_name="Splenda",
        generic_name="granulated sweetener",
        confidence=0.9,
        reason="Splenda is a brand.",
    )
    validated = validate_brand_review_decision("splenda granular", decision)
    assert validated.verdict == "remove_brand"


def test_missing_nutrition_qualifier_requires_manual_review():
    decision = BrandReviewDecision(
        ingredient_name="kraft 2% milk shredded cheddar cheese",
        verdict="remove_brand",
        brand_name="Kraft",
        generic_name="shredded cheddar cheese",
        confidence=0.9,
        reason="Kraft is a brand.",
    )
    validated = validate_brand_review_decision(
        "kraft 2% milk shredded cheddar cheese",
        decision,
    )
    assert validated.verdict == "needs_review"
    assert "lost qualifier" in validated.reason
