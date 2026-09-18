"""Round 2 row 4: searching from Ireland should meet Irish recipes first."""

import unittest

from recipe_wrangler.tools.es_recipe_search import (
    RecipeSearchConstraints,
    build_es_query,
)


def _should(body):
    return body["query"]["bool"].get("should") or []


def _cuisine_boosts(body):
    return [
        clause["term"]["cuisines"]
        for clause in _should(body)
        if "term" in clause and "cuisines" in clause["term"]
    ]


def _source_boosts(body):
    return [
        clause["terms"]
        for clause in _should(body)
        if "terms" in clause and "source" in clause["terms"]
    ]


class RegionalRankingTests(unittest.TestCase):
    def test_a_text_search_from_ireland_boosts_irish_cuisine(self):
        body = build_es_query(
            RecipeSearchConstraints(title_query="vegan meals", region="IE")
        )
        boosts = _cuisine_boosts(body)
        self.assertEqual(len(boosts), 1)
        self.assertEqual(boosts[0]["value"], "irish")
        self.assertGreater(boosts[0]["boost"], 1.0)

    def test_hungary_and_slovenia_get_their_own(self):
        for region, cuisine in (("HU", "hungarian"), ("SI", "slovenian")):
            with self.subTest(region=region):
                body = build_es_query(
                    RecipeSearchConstraints(title_query="soup", region=region)
                )
                self.assertEqual(_cuisine_boosts(body)[0]["value"], cuisine)

    def test_eu_has_no_cuisine_to_prefer(self):
        body = build_es_query(
            RecipeSearchConstraints(title_query="vegan meals", region="EU")
        )
        self.assertEqual(_cuisine_boosts(body), [])

    def test_the_boost_never_filters(self):
        # It rides in `should`, and a title search already requires one clause
        # to match -- the title clauses. A recipe of another cuisine still
        # qualifies.
        body = build_es_query(
            RecipeSearchConstraints(title_query="vegan meals", region="IE")
        )
        bool_q = body["query"]["bool"]
        self.assertNotIn(
            "cuisines", str(bool_q.get("filter") or []),
            "regional preference must never become a filter",
        )
        self.assertEqual(bool_q.get("minimum_should_match"), 1)

    def test_a_plain_browse_keeps_its_source_rank_ordering(self):
        # Adding a `should` clause with no text search would switch the sort
        # branch and drop source_rank, which is what puts curated recipes
        # first when nobody typed anything.
        body = build_es_query(RecipeSearchConstraints(region="IE"))
        self.assertEqual(_cuisine_boosts(body), [])
        self.assertIn({"source_rank": "asc"}, body["sort"])

    def test_ireland_boosts_the_curated_irish_source_by_name(self):
        # The complaint was about curated recipes, and `cuisines` is a model
        # annotation covering 79% of that source -- roughly one curated Irish
        # recipe in five is not annotated `irish` and cuisine alone misses it.
        body = build_es_query(
            RecipeSearchConstraints(title_query="vegan meals", region="IE")
        )
        sources = _source_boosts(body)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source"], ["Curated Irish Recipes"])

    def test_source_outranks_the_cuisine_annotation(self):
        body = build_es_query(
            RecipeSearchConstraints(title_query="vegan meals", region="IE")
        )
        self.assertGreater(
            _source_boosts(body)[0]["boost"], _cuisine_boosts(body)[0]["boost"]
        )

    def test_hungary_and_slovenia_boost_all_their_known_sources(self):
        body = build_es_query(RecipeSearchConstraints(title_query="soup", region="HU"))
        self.assertEqual(
            _source_boosts(body)[0]["source"],
            ["Best of Hungary", "Curated Hungarian Recipes", "The Hungary Soul"],
        )
        body = build_es_query(RecipeSearchConstraints(title_query="soup", region="SI"))
        self.assertEqual(
            _source_boosts(body)[0]["source"],
            ["Curated Slovenian Recipes", "Slovenian Kitchen"],
        )

    def test_a_source_that_only_sounds_regional_is_not_boosted(self):
        # Irish Heart Foundation is 6% `irish`; SOURCE_CUISINE_PRIOR excludes it
        # deliberately and this inherits that judgement.
        body = build_es_query(
            RecipeSearchConstraints(title_query="vegan meals", region="IE")
        )
        self.assertNotIn("Irish Heart Foundation", _source_boosts(body)[0]["source"])

    def test_eu_boosts_no_source_either(self):
        body = build_es_query(
            RecipeSearchConstraints(title_query="vegan meals", region="EU")
        )
        self.assertEqual(_source_boosts(body), [])

    def test_a_plain_browse_boosts_no_source(self):
        body = build_es_query(RecipeSearchConstraints(region="IE"))
        self.assertEqual(_source_boosts(body), [])

    def test_a_rank_query_alone_still_boosts(self):
        body = build_es_query(
            RecipeSearchConstraints(rank_query="something vegan for dinner", region="IE")
        )
        self.assertEqual(_cuisine_boosts(body)[0]["value"], "irish")
        self.assertEqual(_source_boosts(body)[0]["source"], ["Curated Irish Recipes"])


if __name__ == "__main__":
    unittest.main()
