from recipe_wrangler.matching.facets import extract_mention_facets, extract_candidate_facets, recover_source_identity
from recipe_wrangler.matching.equivalence import RuleBasedEquivalenceModel
from recipe_wrangler.matching.retrieval import retrieval_queries
from recipe_wrangler.matching.models import FoodMention, FoodCandidate, RetrievalEvidence


def compare(text, identity, label):
    facets = extract_mention_facets(identity, text).facets
    mention = FoodMention(text, facets.identity, facets)
    candidate = FoodCandidate('test', 'eu', label, extract_candidate_facets(label).facets, RetrievalEvidence())
    return RuleBasedEquivalenceModel().predict(mention, candidate)


def test_breast_is_preserved_separately_from_negated_skin():
    text = 'Chicken meat, breast without skin, boneless'
    facets = extract_mention_facets('Chicken meat', text).facets
    assert facets.food_part == 'breast'
    assert facets.skin_status == 'absent'
    assert facets.bone_status == 'absent'
    assert compare(text, 'Chicken meat', 'Chicken, meat, raw').status != 'acceptable'
    assert compare('raw chicken breast skinless boneless', 'chicken breast', 'Chicken breast raw skinless boneless').status in {'exact', 'acceptable'}
    assert compare('raw chicken breast skinless boneless', 'chicken breast', 'Chicken breast raw with skin bone-in').status == 'incompatible'


def test_scrambled_eggs_cannot_match_raw_or_boiled_eggs():
    facets = extract_mention_facets('eggs', 'eggs, scrambled').facets
    assert facets.preparation_state == 'cooked'
    assert 'scrambled' in facets.processes
    assert compare('eggs scrambled', 'eggs', 'Eggs raw').status == 'incompatible'
    assert compare('eggs scrambled', 'eggs', 'Eggs boiled').status not in {'exact','acceptable'}
    assert compare('eggs scrambled', 'eggs', 'Eggs scrambled').status in {'exact','acceptable'}


def test_separated_yolk_recovers_identity_and_retrieval():
    facets = extract_mention_facets('Eggs', 'Eggs, chicken, yolk, raw').facets
    assert facets.food_part == 'egg_yolk'
    assert facets.identity == 'egg yolk'
    queries = retrieval_queries(FoodMention('raw eggs', 'Eggs', facets))
    assert 'egg yolk' in queries[0]
    assert compare('Eggs, chicken, yolk, raw', 'eggs', 'Eggs raw').status not in {'exact','acceptable'}


def test_jerusalem_artichoke_stays_distinct():
    assert recover_source_identity('Artichoke', 'Artichoke, Jerusalem, boiled') == 'Jerusalem artichoke'
    assert compare('Jerusalem artichoke raw', 'Jerusalem artichoke', 'Artichoke raw').status not in {'exact','acceptable'}


def test_packing_medium_must_be_supported_and_conflicts_block():
    assert compare('anchovies canned in oil drained', 'anchovies', 'Anchovies canned').status == 'insufficient_information'
    assert compare('anchovies canned in oil drained', 'anchovies', 'Anchovies canned in water').status == 'incompatible'
    assert compare('anchovies canned in oil drained', 'anchovies', 'Anchovies canned in oil drained').status in {'exact','acceptable'}
