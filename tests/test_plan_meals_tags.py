"""
Claim tags on `plan_meals`.

The corpus carries planning-relevant claims on the `tags` keyword field —
`high_protein` (1676 recipes), `low_fat` (1081), `high_fibre` (457),
`low_calorie` (184), `30_minutes_or_less` (2809) — and `plan_meals` had no way
to ask for any of them. So a caller wanting "high protein meals" could express
it as a diet tag (which no recipe carries, emptying every slot) or not at all.

The decisive design point: a claim is the SOFTEST thing a caller can ask for and
the scarcest annotation in the corpus. `high_fibre` is on 10% of recipes, so two
claims ANDed across 21 slots would starve most of them. `tags` therefore leads
the relaxation ladder — it is surrendered before anything else.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.api.routers.tools import (
    RELAXATION_ORDER,
    MealPlanRequest,
    _filters,
    _validate_options,
    tool_manifest,
)
from recipe_wrangler.catalog import vocabularies as V


def _req(**kw):
    kw.setdefault("slots", [{"slot": "dinner"}])
    return MealPlanRequest(**kw)


class TestTheParameterExists:
    def test_tags_are_accepted(self):
        assert _req(tags=["high_protein"]).tags == ["high_protein"]

    def test_the_default_is_empty(self):
        assert _req().tags == []

    def test_an_unknown_field_is_still_a_422(self):
        """extra="forbid" is what makes the capability gate necessary
        downstream — do not soften it."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _req(claim_tags=["high_protein"])


class TestItBecomesAFilter:
    def _accepted(self, payload):
        return _validate_options(payload)[0]

    def test_a_tag_reaches_the_query(self):
        payload = _req(tags=["high_protein"])
        filters = _filters(payload, self._accepted(payload), (), set())
        assert {"terms": {"tags": ["high_protein"]}} in filters

    def test_two_tags_are_anded_into_one_terms_clause(self):
        payload = _req(tags=["high_protein", "high_fibre"])
        filters = _filters(payload, self._accepted(payload), (), set())
        assert {"terms": {"tags": ["high_protein", "high_fibre"]}} in filters

    def test_values_are_slugified(self):
        payload = _req(tags=["High Protein", "high-fibre"])
        assert self._accepted(payload)["tags"] == ["high_protein", "high_fibre"]

    def test_duplicates_collapse(self):
        payload = _req(tags=["high_protein", "High-Protein"])
        assert self._accepted(payload)["tags"] == ["high_protein"]

    def test_no_tags_means_no_clause(self):
        payload = _req()
        filters = _filters(payload, self._accepted(payload), (), set())
        assert not any("tags" in (f.get("terms") or {}) for f in filters)


class TestItIsTheFirstThingSurrendered:
    def test_tags_lead_the_relaxation_ladder(self):
        """A claim is the softest ask and the scarcest annotation. Dropping it
        first means "high protein and high fibre" narrows when it can and widens
        when it cannot, instead of returning an empty week."""
        assert RELAXATION_ORDER[0] == "tags"

    def test_dropping_tags_removes_the_filter(self):
        payload = _req(tags=["high_protein"])
        accepted = _validate_options(payload)[0]
        filters = _filters(payload, accepted, (), {"tags"})
        assert not any("tags" in (f.get("terms") or {}) for f in filters)

    def test_dropping_tags_leaves_the_hard_filters_alone(self):
        payload = _req(tags=["high_protein"], exclude_allergens=["peanuts"],
                       diet=["vegetarian"])
        accepted = _validate_options(payload)[0]
        relaxed = _filters(payload, accepted, (), set(RELAXATION_ORDER))
        # allergens and diet survive every relaxation level
        rendered = str(relaxed)
        assert "peanut" in rendered
        assert "vegetarian" in rendered


class TestOpenVocabularyHandling:
    def test_an_unlisted_tag_is_reported(self):
        """`tags` is human-authored and open, so an unlisted value may simply be
        newer than the curated list — it is reported, not rejected."""
        payload = _req(tags=["moon_cheese"])
        accepted, rejected = _validate_options(payload)
        assert "tags=moon_cheese" in rejected

    def test_an_unlisted_tag_is_still_applied(self):
        """It relaxes first, so it cannot strand a slot even if it matches
        nothing."""
        payload = _req(tags=["moon_cheese"])
        accepted, _ = _validate_options(payload)
        assert accepted["tags"] == ["moon_cheese"]

    def test_a_listed_tag_is_not_reported(self):
        payload = _req(tags=["high_protein"])
        _accepted, rejected = _validate_options(payload)
        assert not any(r.startswith("tags=") for r in rejected)


class TestTheManifestTellsTheTruth:
    def test_the_planning_tag_vocabulary_is_published(self):
        """Downstream uses the presence of this key as the capability flag, so
        it must exist and be non-empty."""
        vocab = tool_manifest()["vocabularies"]
        assert vocab["tags"], "tags vocabulary must be published"
        assert "high_protein" in vocab["tags"]

    def test_the_published_order_matches_the_source(self):
        assert tool_manifest()["vocabularies"]["tags"] == list(V.PLANNING_TAGS)

    def test_the_relaxation_order_is_published(self):
        assert tool_manifest()["relaxation_order"][0] == "tags"

    def test_never_relaxed_no_longer_under_declares(self):
        """It listed three constraints while several others were equally hard —
        an agent reading it to decide what to send was told the wrong thing."""
        never = set(tool_manifest()["never_relaxed"])
        for hard in ("exclude_allergens", "exclude_ingredients", "diet",
                     "include_ingredients", "min_nutri_score", "sources",
                     "exclude_recipe_ids"):
            assert hard in never, hard
        # and nothing that IS relaxed may be listed as never relaxed
        assert never.isdisjoint(set(RELAXATION_ORDER))


class TestAppliedReportsWhatWeFilteredOn:
    def test_tags_appear_in_applied(self):
        """`applied` is the service's own statement of what it filtered on. A
        tag missing from it would make the plan unexplainable downstream."""
        import inspect

        from recipe_wrangler.api.routers import tools

        src = inspect.getsource(tools)
        applied = src[src.find('"applied": {'):]
        applied = applied[:applied.find("},")]
        assert '"tags"' in applied
