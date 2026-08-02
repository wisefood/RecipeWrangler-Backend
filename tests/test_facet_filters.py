"""The four annotation facets as filters, end to end through the request models.

These facets existed as *aggregations* long before they worked as *filters*:
search returned `cuisines: {italian: 1175, ...}`, the UI rendered chips from it,
and clicking one changed nothing. Every layer between the click and the query
dropped the value silently — Pydantic ignores unknown fields, and a filter that
never reaches Elasticsearch reads as "no constraint", so the request succeeded
and returned the unfiltered corpus.

That is the failure this module guards: not a wrong result, but a *plausible*
one. The assertions therefore check that the value survives each hop, since any
single hop losing it reproduces the original bug with no error anywhere.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.schemas.models import RecipeSearchFilters, RecipeSearchRequest
from recipe_wrangler.tools.es_recipe_search import RecipeSearchConstraints, build_es_query

FACETS = ("cuisines", "moods", "flavor_profiles", "food_groups")


def _filters_of(query: dict) -> list[dict]:
    return query["query"]["bool"].get("filter", [])


def _terms_for(query: dict, field: str) -> list[str] | None:
    """The values of the top-level `terms` filter on `field`, if there is one."""
    for clause in _filters_of(query):
        terms = clause.get("terms")
        if terms and field in terms:
            return terms[field]
    return None


class TestQueryConstruction:
    """A populated facet must become a `terms` filter on the same-named field."""

    @pytest.mark.parametrize("facet", FACETS)
    def test_facet_becomes_a_terms_filter(self, facet):
        query = build_es_query(RecipeSearchConstraints(**{facet: ["italian"]}))
        assert _terms_for(query, facet) == ["italian"]

    @pytest.mark.parametrize("facet", FACETS)
    def test_empty_facet_adds_no_filter(self, facet):
        """An unselected facet must not narrow anything.

        An empty `terms` filter matches no documents in Elasticsearch, so
        emitting one unconditionally would turn "no cuisine selected" into
        "no results".
        """
        query = build_es_query(RecipeSearchConstraints(**{facet: []}))
        assert _terms_for(query, facet) is None

    def test_facets_intersect_rather_than_replace(self):
        """Selecting across facets narrows; each lands as its own filter.

        Filters in a `bool` are ANDed, so italian + fish means both. This is the
        behaviour the counts imply — 1,175 italian and 499 fish yielding 78.
        """
        query = build_es_query(
            RecipeSearchConstraints(cuisines=["italian"], food_groups=["fish"])
        )
        assert _terms_for(query, "cuisines") == ["italian"]
        assert _terms_for(query, "food_groups") == ["fish"]

    @pytest.mark.parametrize("facet", FACETS)
    def test_values_are_normalized_to_index_spelling(self, facet):
        """The index stores `middle_eastern`; a UI may send `Middle Eastern`.

        `terms` does not analyse its input, so an unnormalized value matches
        nothing — silently.
        """
        query = build_es_query(RecipeSearchConstraints(**{facet: ["Middle Eastern"]}))
        assert _terms_for(query, facet) == ["middle_eastern"]


class TestParamSearchRequestModel:
    """`RecipeSearchFilters` is what /param_search validates against."""

    @pytest.mark.parametrize("facet", FACETS)
    def test_facet_is_accepted_and_kept(self, facet):
        """Pydantic drops unknown fields without complaint.

        Before the field existed, `{"cuisines": ["italian"]}` validated fine and
        arrived as an empty list — which is precisely why the chips did nothing.
        """
        model = RecipeSearchFilters(**{facet: ["italian"]})
        assert getattr(model, facet) == ["italian"]

    @pytest.mark.parametrize("facet", FACETS)
    def test_facet_defaults_to_empty(self, facet):
        assert getattr(RecipeSearchFilters(), facet) == []

    @pytest.mark.parametrize(
        "alias,facet",
        [
            ("cuisine", "cuisines"),
            ("mood", "moods"),
            ("flavor_profile", "flavor_profiles"),
            ("food_group", "food_groups"),
        ],
    )
    def test_singular_alias_and_bare_string(self, alias, facet):
        """`{"cuisine": "italian"}` means one cuisine, not a validation error."""
        assert getattr(RecipeSearchFilters(**{alias: "italian"}), facet) == ["italian"]


class TestNaturalLanguageRequestModel:
    """`RecipeSearchRequest` carries the sidebar alongside the question."""

    @pytest.mark.parametrize("facet", (*FACETS, "dish_types", "sources"))
    def test_selection_is_accepted(self, facet):
        model = RecipeSearchRequest(question="pasta", **{facet: ["x"]})
        assert getattr(model, facet) == ["x"]

    def test_require_diet_tags_is_separate_from_diet_tags(self):
        """The two mean opposite things and must not collapse into one field.

        `diet_tags` are the member's profile groups, applied as soft boosts;
        `require_diet_tags` is a diet the caller ticked, applied as a hard
        filter. Merging them would either filter on a preference the member
        never asked to enforce, or ignore a filter they explicitly chose.
        """
        model = RecipeSearchRequest(
            question="dinner", diet_tags=["vegetarian"], require_diet_tags=["vegan"]
        )
        assert model.diet_tags == ["vegetarian"]
        assert model.require_diet_tags == ["vegan"]

    def test_defaults_are_empty_so_an_unfiltered_question_is_unchanged(self):
        model = RecipeSearchRequest(question="pasta")
        assert all(getattr(model, facet) == [] for facet in FACETS)
        assert model.require_diet_tags == []
