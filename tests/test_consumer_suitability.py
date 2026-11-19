import re

from recipe_wrangler.utils.consumer_suitability import (
    DIETARY_ORIGINS,
    DIETARY_RULE_NOTES,
    GROUP_RULES,
    POSITIVE_EVIDENCE_EXCLUSIONS,
    SUITABILITY_CLASSIFICATION_VERSION,
    VEGAN_NAME_EXCLUSIONS,
    dietary_keyword_evidence,
    keyword_regex,
    positive_keyword_regex,
)


def test_rule_version_and_supported_source_categories() -> None:
    assert SUITABILITY_CLASSIFICATION_VERSION == "vegan-vegetarian-v11"
    origins = {str(item["name"]): item for item in DIETARY_ORIGINS}
    assert origins["dairy"]["vegan_status"] == "not_suitable"
    assert origins["dairy"]["vegetarian_status"] == "suitable"
    assert origins["egg"]["vegan_status"] == "not_suitable"
    assert origins["egg"]["vegetarian_status"] == "suitable"
    assert origins["bee_product"]["vegan_status"] == "not_suitable"
    assert origins["bee_product"]["vegetarian_status"] == "suitable"
    assert origins["animal_meat"]["vegetarian_status"] == "not_suitable"
    assert origins["plant"]["vegan_status"] == "suitable"


def test_animal_derived_standard_terms_are_covered() -> None:
    vegan_terms = set(GROUP_RULES["vegan"]["blocking_keywords"])
    vegetarian_terms = set(GROUP_RULES["vegetarian"]["blocking_keywords"])
    assert {"honey", "beeswax", "propolis", "colostrum", "lanolin"} <= vegan_terms
    assert not {"honey", "beeswax", "propolis", "colostrum", "lanolin"} & (
        vegetarian_terms
    )
    assert {
        "honey",
        "beeswax",
        "propolis",
        "colostrum",
        "lanolin",
    } <= set(GROUP_RULES["vegetarian"]["positive_keywords"])
    assert {"gelatin", "collagen", "animal rennet"} <= vegetarian_terms


def test_keyword_matching_uses_word_boundaries() -> None:
    egg = re.compile(keyword_regex("egg"))
    assert egg.match("whole egg")
    assert not egg.match("eggplant")


def test_vegan_plant_alternatives_are_excluded_from_negative_terms() -> None:
    assert any(re.match(pattern, "oat milk") for pattern in VEGAN_NAME_EXCLUSIONS)
    assert any(
        re.match(pattern, "vegan sausage") for pattern in VEGAN_NAME_EXCLUSIONS
    )
    assert any(
        re.match(pattern, "vegetable suet") for pattern in VEGAN_NAME_EXCLUSIONS
    )
    assert not any(
        re.match(pattern, "vegetarian cheese")
        for pattern in VEGAN_NAME_EXCLUSIONS
    )


def test_alternative_exceptions_do_not_hide_independent_animal_ingredients() -> None:
    from recipe_wrangler.utils.consumer_suitability import VEGETARIAN_NAME_EXCLUSIONS
    for patterns in [VEGAN_NAME_EXCLUSIONS,VEGETARIAN_NAME_EXCLUSIONS]:
        for name in ['vegan sausage with pork','oat milk and cream','butter beans with bacon',
                     'dairy-free cream with egg','butternut with lard']:
            assert not any(re.fullmatch(pattern,name) for pattern in patterns), name
    assert re.fullmatch(positive_keyword_regex('milk'),'milk')
    assert not re.fullmatch(positive_keyword_regex('milk'),'milk and unresolved sauce')
    assert re.fullmatch(positive_keyword_regex('vegan'),'vegan sausage')
    assert not re.fullmatch(positive_keyword_regex('vegan'),'vegan sausage with pork')
    assert not re.fullmatch(positive_keyword_regex('vegan'),'vegetarian cheese')
    assert re.fullmatch(positive_keyword_regex('milk'),'low fat milk')
    assert not re.fullmatch(positive_keyword_regex('milk'),'milk and mystery sauce')


def test_chicken_egg_is_egg_not_chicken_meat() -> None:
    from recipe_wrangler.utils.consumer_suitability import dietary_keyword_evidence
    for name in ["CHICKEN EGG", "chicken egg yolk", "chicken egg hard boiled", "chicken eggs", "hen's eggs"]:
        for group in ("vegetarian", "pescatarian"):
            assert dietary_keyword_evidence(name, group)[1] != "contains", (name, group)
        assert dietary_keyword_evidence(name, "vegan")[1] == "contains", name
    assert dietary_keyword_evidence("chicken egg noodles with chicken breast", "vegetarian")[1] == "contains"
    assert dietary_keyword_evidence("chicken", "vegetarian")[1] == "contains"


def test_seafood_cut_words_keep_pescatarian_positive_evidence() -> None:
    def positive(name):
        return any(re.fullmatch(positive_keyword_regex(k), name) for k in GROUP_RULES["pescatarian"]["positive_keywords"])
    for name in ["salmon fillets", "white fish fillets", "salmon fillet", "tuna steaks", "skinless salmon fillets", "peeled tiger prawns"]:
        assert positive(name), name
    for name in ["fish pie mix with bacon", "oyster mushrooms", "salmon and chicken pie", "anchovy-stuffed olives"]:
        assert not positive(name), name
    assert not any(re.fullmatch(positive_keyword_regex(k), "salmon fillets") for k in GROUP_RULES["vegetarian"]["positive_keywords"])


def _positive(name: str, group: str) -> bool:
    from recipe_wrangler.utils.consumer_suitability import PLANT_STAPLE_PATTERNS
    from recipe_wrangler.utils.dietary_ingredient_evidence import canonical_ingredient_name
    name = canonical_ingredient_name(name)
    return (any(re.fullmatch(p, name) for p in PLANT_STAPLE_PATTERNS)
            or any(re.fullmatch(positive_keyword_regex(k), name) for k in GROUP_RULES[group]["positive_keywords"]))


def test_catalogue_plant_staples_and_cofid_names_are_positive_for_vegan_and_vegetarian() -> None:
    for name in ["courgette zucchini", "rocket arugula", "oats oatmeal",
                 "Tomatoes, standard, raw", "Wheat white flour, T500", "Cherries, flesh and skin, raw",
                 "Broccoli, green, steamed", "button mushrooms", "low-sodium soy sauce", "icing frosting sugar"]:
        for group in ("vegan", "vegetarian"):
            assert _positive(name, group), (name, group)


def test_vegetarian_only_staples_are_not_vegan_evidence() -> None:
    for name in ["mayonnaise", "puff pastry", "milk chocolate", "cottage cheese", "lasagne sheets", "white wine", "red wine"]:
        assert _positive(name, "vegetarian"), name
        assert not _positive(name, "vegan"), name


def test_new_staples_do_not_promote_animal_or_mixed_names() -> None:
    for name in ["fish oil", "cod liver oil", "beef stock", "chicken stock", "roast chicken", "oil and vinegar dressing",
                 "stock", "broth", "chicken and mushroom pie", "margarine or butter"]:
        for group in ("vegan", "vegetarian"):
            assert not _positive(name, group), (name, group)


def test_pescatarian_pantry_is_positive_but_stock_and_gelatine_stay_unknown():
    def positive(name):
        return any(re.fullmatch(positive_keyword_regex(k), name) for k in GROUP_RULES["pescatarian"]["positive_keywords"])

    for name in ["oil", "reduced-fat spread", "margarine", "cooking spray", "pesto", "haloumi", "flour tortillas"]:
        assert positive(name), name
    for name in ["stock", "broth", "gelatine", "marshmallows", "fish oil"]:
        assert not positive(name), name


def test_vegan_frozen_sausages_is_not_meat():
    for group in ("vegan", "vegetarian", "pescatarian"):
        assert dietary_keyword_evidence("vegan frozen sausages", group)[0] == [], group


def test_owner_decisions_18_nov_oil_spreads_stock_are_vegan_wine_is_vegetarian_only() -> None:
    for name in ["oil", "cooking spray", "margarine", "reduced-fat spread", "vegetable stock", "low salt vegetable stock"]:
        for group in ("vegan", "vegetarian"):
            assert _positive(name, group), (name, group)
    assert _positive("white wine", "vegetarian") and not _positive("white wine", "vegan")


def test_parmesan_blocks_vegetarian_but_not_pescatarian_or_vegetarian_labelled() -> None:
    for name in ["Parmigiano Reggiano", "parmesan", "grated parmesan cheese"]:
        assert dietary_keyword_evidence(name, "vegetarian")[0], name
        assert not dietary_keyword_evidence(name, "pescatarian")[0], name
    for name in ["eggplant parmigiana", "vegetarian parmesan"]:
        assert not dietary_keyword_evidence(name, "vegetarian")[0], name


def test_haloumi_vegetarian_not_vegan_and_pesto_blocks_both_unless_labelled() -> None:
    assert _positive("haloumi", "vegetarian") and dietary_keyword_evidence("haloumi", "vegan")[0]
    for name in ["pesto", "basil pesto"]:
        assert dietary_keyword_evidence(name, "vegetarian")[0] and dietary_keyword_evidence(name, "vegan")[0], name
        assert not dietary_keyword_evidence(name, "pescatarian")[0], name
    for group in ("vegan", "vegetarian"):
        assert not dietary_keyword_evidence("vegan pesto", group)[0], group


def test_animal_rennet_cheeses_block_vegetarian_and_emmental_is_conditional() -> None:
    for name in ["pecorino romano", "grana padano", "gruyère", "gruyere cheese", "manchego", "grated parmesan"]:
        assert dietary_keyword_evidence(name, "vegetarian")[0], name
    assert not any(re.fullmatch(x, "vegetarian emmental") for x in POSITIVE_EVIDENCE_EXCLUSIONS)
    assert any(re.fullmatch(x, "emmental cheese") for x in POSITIVE_EVIDENCE_EXCLUSIONS)
    assert not dietary_keyword_evidence("emmental", "vegetarian")[0]


def test_plain_products_are_both_vegan_and_vegetarian() -> None:
    for name in ["flour tortillas", "custard powder", "white miso paste", "sweet chilli sauce", "fresh coriander cilantro",
                 "gluten-free baking powder", "shredded coconut", "dark chocolate", "breadcrumbs", "wholegrain bread"]:
        for group in ("vegan", "vegetarian"):
            assert _positive(name, group), (name, group)


def test_rule_notes_cover_each_owner_decision() -> None:
    assert {n["id"] for n in DIETARY_RULE_NOTES} >= {"animal-rennet-cheeses", "pesto", "gelatine", "may-contain-statements"}


def test_dandelion_salad_names_are_positive() -> None:
    assert _positive("Potato with coat cooked", "vegan")
    assert _positive("Chicken egg hard boiled", "vegetarian")


def test_title_quirks_mac_n_cheese_and_nice_cream_are_not_dairy_evidence_for_vegan() -> None:
    from recipe_wrangler.utils.consumer_suitability import dietary_title_evidence
    for title in ["Vegan mac ‘n’ cheese", "Berry ‘nice’ cream with pistachios", "Mango and coconut ‘nice cream’"]:
        assert not dietary_title_evidence(title, "vegan")[0], title
    assert dietary_title_evidence("Mac and cheese", "vegan")[0]
