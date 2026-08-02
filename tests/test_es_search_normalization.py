"""Query-vocabulary normalization.

Every bug guarded here shared one shape: the query layer and the index
disagreed about how a value is spelled, a `term` query does not analyse its
input, and Elasticsearch returned **zero results without erroring**. Silent
zeroes are the worst failure mode in search — nothing looks broken, the corpus
just appears empty.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.catalog.sources import canonical_course_type
from recipe_wrangler.tools.es_recipe_search import (
    RecipeSearchConstraints,
    _ingredient_clause,
    _norm,
    _norm_tags,
    build_es_query,
)


class TestTagSlugging:
    """The index stores `gluten_free`; the extractor emits prose."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("gluten free", "gluten_free"),
            ("gluten-free", "gluten_free"),
            ("Gluten Free", "gluten_free"),
            ("  DAIRY-FREE  ", "dairy_free"),
            ("nut free", "nut_free"),
            ("gluten_free", "gluten_free"),
            ("5 ingredients or less", "5_ingredients_or_less"),
        ],
    )
    def test_separators_all_fold_to_underscore(self, raw, expected):
        assert _norm_tags([raw]) == [expected]

    def test_hyphen_and_space_forms_collapse_together(self):
        """The extractor is not deterministic about the separator — the same
        question produced 'gluten free' one run and 'gluten-free' the next."""
        assert _norm_tags(["gluten free", "gluten-free"]) == ["gluten_free"]

    def test_ingredient_names_keep_their_spaces(self):
        """Only tags are slugged. Slugging an ingredient would stop
        'olive oil' matching anything."""
        assert _norm(["olive oil"]) == ["olive oil"]

    def test_empty_and_blank_dropped(self):
        assert _norm_tags(["", "   ", None]) == []


def _should_terms(body: dict) -> list[dict]:
    return [
        sub["term"]
        for clause in body["query"]["bool"]["filter"]
        for sub in clause.get("bool", {}).get("should", [])
        if "term" in sub
    ]


class TestDietFilterUsesSlugs:
    def test_diet_tag_filter_is_slugged(self):
        body = build_es_query(RecipeSearchConstraints(diet_tags=["gluten-free"]))
        assert {"tags": "gluten_free"} in _should_terms(body)

    def test_consumer_group_diets_query_evidence_and_tag(self):
        """vegan/vegetarian are three-state assessments — but the assessment is
        unpopulated, so querying it alone returns zero. Match either."""
        terms = _should_terms(build_es_query(RecipeSearchConstraints(diet_tags=["vegan"])))
        assert {"suitable_for": "vegan"} in terms
        assert {"tags": "vegan"} in terms

    def test_diet_filter_matches_either_signal(self):
        body = build_es_query(RecipeSearchConstraints(diet_tags=["vegetarian"]))
        clause = next(c for c in body["query"]["bool"]["filter"] if "bool" in c)
        assert clause["bool"]["minimum_should_match"] == 1


class TestCourseTypeFilterSurvivesTheReadFlip:
    """v2 stored `dinner` and `lunch` as literal dish types; the catalog index
    canonicalizes them to `main-dish`. A filter must hit both."""

    def _expanded(self, dish_type: str) -> set[str]:
        body = build_es_query(RecipeSearchConstraints(dish_types=[dish_type]))
        values: set[str] = set()
        for clause in body["query"]["bool"]["filter"]:
            for sub in clause.get("bool", {}).get("should", []):
                for field in ("dish_types", "course_types"):
                    if field in sub.get("terms", {}):
                        values.update(sub["terms"][field])
        return values

    @pytest.mark.parametrize("slot", ["dinner", "lunch"])
    def test_meal_slots_reach_main_dish(self, slot):
        assert canonical_course_type(slot) == "main-dish"
        assert "main-dish" in self._expanded(slot)

    def test_legacy_spellings_still_included(self):
        expanded = self._expanded("desserts")
        assert {"desserts", "dessert"} <= expanded

    def test_both_field_names_queried(self):
        """v2 stores dish_types, the catalog index stores course_types."""
        body = build_es_query(RecipeSearchConstraints(dish_types=["desserts"]))
        fields = {
            field
            for clause in body["query"]["bool"]["filter"]
            for sub in clause.get("bool", {}).get("should", [])
            for field in sub.get("terms", {})
        }
        assert fields == {"dish_types", "course_types"}


class TestIngredientClauseShape:
    """Ingredient exclusion drives allergen safety, so under-matching is a
    safety failure rather than a relevance one."""

    def test_flat_index_uses_analysed_text(self):
        clause = _ingredient_clause("chicken", nested=False)
        assert clause == {"match_phrase": {"ingredients": "chicken"}}

    def test_nested_index_uses_nested_query_on_name(self):
        clause = _ingredient_clause("chicken", nested=True)
        assert clause["nested"]["path"] == "ingredients"
        assert "ingredients.name" in clause["nested"]["query"]["match_phrase"]

    def test_never_uses_ingredient_names_term(self):
        """`ingredient_names` holds whole names, so a term query for 'chicken'
        misses 'chicken breast' (304 recipes) and 'chicken thigh' (108) —
        matching only the 73 called exactly 'chicken'."""
        for nested in (True, False):
            assert "term" not in _ingredient_clause("chicken", nested=nested)


class TestRankQueryNeverFilters:
    def test_rank_query_does_not_require_a_match(self):
        """Filters narrow; text only orders. A ranking clause must never
        exclude a document that satisfies the filters."""
        body = build_es_query(
            RecipeSearchConstraints(diet_tags=["vegan"], rank_query="dessert")
        )
        assert body["query"]["bool"]["minimum_should_match"] == 0

    def test_explicit_title_query_does_require_a_match(self):
        body = build_es_query(RecipeSearchConstraints(title_query="chocolate cake"))
        assert body["query"]["bool"]["minimum_should_match"] == 1

    def test_score_sorts_first_when_text_is_present(self):
        body = build_es_query(RecipeSearchConstraints(rank_query="dessert"))
        assert body["sort"][0] == "_score"

    def test_no_text_falls_back_to_deterministic_order(self):
        body = build_es_query(RecipeSearchConstraints())
        assert body["sort"][0] != "_score"
