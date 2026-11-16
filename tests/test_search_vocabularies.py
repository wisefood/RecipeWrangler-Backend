"""The values the extractor is told must be values the query builder can use.

The prompt hands the model a closed list per facet and tells it to use only
those. If one of them is a display name, a stale slug, or spelled differently
from what the index holds, the model obeys, the filter matches nothing, and
the search returns an empty page for a question it understood perfectly.
Nothing errors — which is what makes this worth a test rather than a look.

The index itself is not reachable from a unit test, so what is checked here is
every hop this side of it: that the lists are real, that sources resolve
through the slug registry the query builder uses, and that nothing is offered
under a name the builder would not recognise.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.catalog import sources as source_registry
from recipe_wrangler.tools.recipe_search_constraints import SEARCH_FACET_VALUES


class TestTheListsAreReal:
    @pytest.mark.parametrize("facet", sorted(SEARCH_FACET_VALUES))
    def test_no_facet_is_offered_empty(self, facet):
        # An empty list in the prompt reads to the model as "no valid values",
        # and it stops filling that facet at all.
        assert SEARCH_FACET_VALUES[facet], f"{facet} is offered with no values"

    @pytest.mark.parametrize("facet", sorted(SEARCH_FACET_VALUES))
    def test_values_are_plain_lowercase_tokens(self, facet):
        # Display names ("safefood (RCSI)") and mixed case are what a human
        # would write and what the index does not store.
        for value in SEARCH_FACET_VALUES[facet]:
            token = str(value)
            if facet == "nutri_scores":
                assert token in {"A", "B", "C", "D", "E"}
                continue
            assert token == token.strip(), f"{facet}: {token!r} has padding"
            assert token == token.lower(), f"{facet}: {token!r} is not lowercase"
            assert "(" not in token, f"{facet}: {token!r} looks like a display name"

    @pytest.mark.parametrize("facet", sorted(SEARCH_FACET_VALUES))
    def test_no_duplicates(self, facet):
        values = [str(v).lower() for v in SEARCH_FACET_VALUES[facet]]
        assert len(values) == len(set(values)), f"{facet} repeats a value"


class TestSourcesResolve:
    """Sources are offered as slugs and filtered as raw values.

    `build_es_query` turns each slug into the source's `raw` before filtering,
    because that is what the index stores — "irish_safefood" is
    "Curated Irish Recipes" on the document. A slug the registry cannot
    resolve therefore filters on nothing at all.
    """

    def test_every_offered_source_resolves(self):
        unresolved = [
            slug for slug in SEARCH_FACET_VALUES["sources"]
            if source_registry.resolve(slug) is None
        ]
        assert not unresolved, f"offered but unresolvable: {unresolved}"

    def test_every_offered_source_has_a_raw_value_to_filter_on(self):
        missing = [
            slug for slug in SEARCH_FACET_VALUES["sources"]
            if not source_registry.raw_for(slug)
        ]
        assert not missing, f"no raw value to filter on: {missing}"

    def test_retired_sources_are_not_offered(self):
        retired = {s.slug for s in source_registry.SOURCES if s.retired}
        offered = set(SEARCH_FACET_VALUES["sources"])
        assert not (offered & retired), (
            f"the model is told it can ask for retired sources: "
            f"{sorted(offered & retired)}"
        )
