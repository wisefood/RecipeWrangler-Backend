from recipe_wrangler.matching.facets import extract_mention_facets
from recipe_wrangler.matching.retrieval import retrieval_queries, retrieve_candidates
from recipe_wrangler.matching.models import FoodMention


def mention(text, identity, note=""):
    return FoodMention(text, identity, extract_mention_facets(identity, text, note).facets)


def test_queries_preserve_qualifiers_and_remove_amount_and_handling():
    assert retrieval_queries(mention("200 g chicken breast, cooked, finely chopped", "chicken breast")) == (
        "chicken breast cooked", "chicken breast",
    )
    assert retrieval_queries(mention("reduced-salt soy sauce", "soy sauce")) == (
        "reduced-salt soy sauce", "soy sauce",
    )
    assert retrieval_queries(mention("chicken breast", "chicken breast", "cooked")) == (
        "chicken breast cooked", "chicken breast",
    )
    assert retrieval_queries(mention("apple", "apple")) == ("apple",)
    assert retrieval_queries(mention("roasted chicken", "chicken")) == ("roasted chicken", "chicken")


def test_query_fusion_deduplicates_without_mutating_hits():
    duplicate = {"id": "same", "rrf_score": 1.0}
    pools = {
        "chicken breast cooked": [duplicate, duplicate, {"id": "qualified"}],
        "chicken breast": [{"id": "identity"}, duplicate],
    }
    result = retrieve_candidates(
        mention("chicken breast cooked", "chicken breast"),
        lambda query: pools[query],
        lambda hit: hit["id"],
    )
    assert result[0]["id"] == "same"
    assert len(result) == 3
    assert result[0]["retrieval_queries"] == ["chicken breast cooked", "chicken breast"]
    assert duplicate == {"id": "same", "rrf_score": 1.0}


def test_zero_limit_does_not_search():
    assert retrieve_candidates(mention("apple", "apple"), lambda query: 1 / 0, lambda hit: hit["id"], 0) == []


def test_percent_fat_is_a_qualifier_not_a_leading_quantity():
    assert retrieval_queries(mention("0% fat yogurt", "yogurt"))[0] == "0% fat yogurt"
    assert retrieval_queries(mention("2.5% fat milk", "milk"))[0] == "2.5% fat milk"
