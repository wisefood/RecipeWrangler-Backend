from recipe_wrangler.matching.facets import extract_mention_facets, extract_candidate_facets
from recipe_wrangler.matching.equivalence import RuleBasedEquivalenceModel
from recipe_wrangler.matching.variants import variant_family
from recipe_wrangler.matching.models import FoodMention, FoodCandidate, RetrievalEvidence


def predict(text, identity, candidate):
    mention = FoodMention(text, identity, extract_mention_facets(identity, text).facets)
    record = FoodCandidate('test', 'eu', candidate, extract_candidate_facets(candidate).facets, RetrievalEvidence())
    return RuleBasedEquivalenceModel().predict(mention, record)


def test_egg_identity_is_separate_from_species_and_preparation():
    text = 'Eggs, chicken, whole, boiled'
    mention = extract_mention_facets('Eggs', text).facets
    candidate = extract_candidate_facets(text).facets
    assert mention.base_identity == candidate.base_identity == 'egg'
    assert mention.biological_source == candidate.biological_source == 'chicken'
    assert predict(text, 'Eggs', text).status in {'exact', 'acceptable'}
    assert predict(text, 'Eggs', 'Eggs, duck, whole, boiled').status == 'incompatible'
    assert predict(text, 'Eggs', 'Eggs, chicken, whole, raw').status == 'incompatible'
    assert predict(text, 'Eggs', 'Eggs, chicken, whole, fried').status not in {'exact', 'acceptable'}


def test_unknown_food_names_are_preserved_without_a_dictionary():
    facets = extract_mention_facets('zorblax', 'zorblax boiled').facets
    assert facets.base_identity == 'zorblax'
    assert predict('zorblax boiled', 'zorblax', 'Zorblax boiled').status in {'exact', 'acceptable'}
    assert predict('zorblax boiled', 'zorblax', 'Zorblax raw').status == 'incompatible'
    assert predict('zorblax boiled', 'zorblax', 'Differentfood boiled').status == 'incompatible'
    assert predict('zorblax boiled', 'zorblax', 'Zorblax mystic-processed boiled').status not in {'exact', 'acceptable'}


def test_product_identity_and_extra_ingredients_are_not_erased():
    for identity, candidate in [('rice', 'rice milk boiled'), ('egg', 'egg substitute boiled'), ('tomato', 'tomato paste boiled')]:
        assert predict(f'{identity} boiled', identity, candidate).status not in {'exact', 'acceptable'}


def test_related_preparations_are_not_equivalent_or_cross_species():
    boiled = extract_candidate_facets('Eggs, chicken, whole, boiled').facets
    fried = extract_candidate_facets('Eggs, chicken, whole, fried').facets
    duck = extract_candidate_facets('Eggs, duck, whole, boiled').facets
    unknown = extract_candidate_facets('Eggs, whole, boiled').facets
    assert variant_family(boiled) == variant_family(fried)
    assert variant_family(boiled) != variant_family(duck)
    assert variant_family(boiled) != variant_family(unknown)
    assert boiled.processes != fried.processes


def test_base_identity_survives_benchmark_serialization():
    from recipe_wrangler.matching.evaluation.io import _facets_to_dict, _facets_from_dict
    facets = extract_candidate_facets('Eggs, chicken, whole, boiled').facets
    assert _facets_from_dict(_facets_to_dict(facets)) == facets


