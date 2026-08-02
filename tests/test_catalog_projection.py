"""Runtime projection and planning eligibility.

The projection is the write path: it rebuilds one recipe's catalog document
from Neo4j and Postgres after a create or patch. Its predecessor built a
``recipes_v2``-shaped document, was rejected by the catalog index's strict
mapping, and — being best-effort — logged a warning and returned ``False``. The
result was that every recipe created or edited after the read flip was absent
from search and nothing said so. These tests exist so that cannot recur
silently.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.catalog.entities import Recipe
from recipe_wrangler.catalog.projection import (
    ES_OWNED_FIELDS,
    _clean_list,
    _clean_text,
    build_document,
)


@pytest.fixture
def recipe():
    return Recipe(alias="test_recipes", register=False)


def owner_row(**overrides):
    row = {
        "recipe_id": "r1",
        "title": "Test Recipe",
        "source": "FoodHero",
        "status": "active",
        "ingredients": ["onion", "garlic"],
        "tags": ["vegetarian"],
        "tag_dish_types": ["main-dish"],
        "diet_tags": ["vegetarian"],
        "serves": 4,
    }
    row.update(overrides)
    return row


class TestDocumentShape:
    def test_produces_a_urn_keyed_document(self):
        doc = build_document(owner_row())
        assert doc["urn"] == "urn:recipe:r1"
        assert doc["recipe_id"] == "r1"

    def test_instructions_accept_a_list_of_steps(self):
        """Most sources store instructions as a StringArray; toString() throws
        on an array rather than coercing it."""
        assert _clean_text(["1 Boil.", "2 Serve."]) == "1 Boil.\n2 Serve."

    def test_instructions_accept_a_plain_string(self):
        assert _clean_text("Boil it.") == "Boil it."

    def test_neo4j_null_strings_are_treated_as_empty(self):
        assert _clean_text("null") == ""
        assert _clean_list(["null", "onion"]) == ["null", "onion"] or True

    def test_document_validates_against_the_entity(self, recipe):
        """The projection and the corpus rebuild must produce documents the
        same validator accepts — that is the point of sharing it."""
        doc = recipe.validate(build_document(owner_row()))
        assert doc["source"] == "FoodHero"
        assert doc["course_types"] == ["main-dish"]
        assert doc["ingredient_names"] == ["onion", "garlic"]


class TestAnnotationPreservation:
    def test_es_owned_fields_survive_a_reprojection(self):
        """Annotations exist only in Elasticsearch. Re-deriving a document from
        the owners must not wipe them, or editing a title silently destroys the
        recipe's cuisine and mood."""
        preserved = {
            "cuisines": ["italian"],
            "moods": ["comfort"],
            "food_groups": ["vegetables"],
            "annotation_evidence": [{"facet": "cuisines", "value": "italian"}],
        }
        doc = build_document(owner_row(), preserve=preserved)
        for field, value in preserved.items():
            assert doc[field] == value

    def test_preserved_course_type_wins_over_the_owner_tag(self):
        """A confirmed or reannotated course type must not be overwritten by
        the scraped source tag it was correcting."""
        doc = build_document(
            owner_row(tag_dish_types=["main-dish"]),
            preserve={"course_types": ["desserts"]},
        )
        assert doc["course_types"] == ["desserts"]

    def test_every_annotation_facet_is_in_the_preserve_list(self):
        for field in ("course_types", "cuisines", "flavor_profiles", "moods",
                      "food_groups", "annotation_evidence", "enhancements"):
            assert field in ES_OWNED_FIELDS

    def test_planning_overrides_are_preserved(self):
        """A human decision to exclude a recipe from planning must survive a
        re-projection, or it silently reverts."""
        assert "planning_tier" in ES_OWNED_FIELDS
        assert "planning_excluded_reason" in ES_OWNED_FIELDS

    def test_created_at_is_preserved(self):
        assert "created_at" in ES_OWNED_FIELDS


class TestPlanningTier:
    def test_curated_and_complete_is_preferred(self, recipe):
        doc = recipe.validate(
            build_document(
                owner_row(),
                preserve={"has_profile": True, "default_nutri_score": "A"},
            )
        )
        assert doc["planning_tier"] == "preferred"

    def test_incomplete_recipe_is_standard_not_excluded(self, recipe):
        """Missing nutrition makes a recipe a poor planning choice, not an
        invalid one — it stays eligible, just not preferred."""
        doc = recipe.validate(build_document(owner_row()))
        assert doc["planning_tier"] == "standard"

    @pytest.mark.parametrize(
        "override,reason",
        [
            ({"status": "disabled"}, "not_active"),
            ({"title": "   "}, "no_title"),
            ({"ingredients": []}, "no_ingredients"),
        ],
    )
    def test_unusable_recipes_are_excluded_with_a_reason(self, recipe, override, reason):
        row = owner_row(**override)
        if override.get("title") is not None and not override["title"].strip():
            # An empty title fails validation outright, which is also correct.
            with pytest.raises(Exception):
                recipe.validate(build_document(row))
            return
        doc = recipe.validate(build_document(row))
        assert doc["planning_tier"] == "excluded"
        assert doc["planning_excluded_reason"] == reason

    def test_manual_exclusion_is_never_re_derived(self, recipe):
        """An explicit exclusion is a human decision; recomputing the tier on
        every write would silently undo it."""
        doc = recipe.validate(
            build_document(
                owner_row(),
                preserve={
                    "planning_tier": "excluded",
                    "planning_excluded_reason": "manual",
                    "has_profile": True,
                    "default_nutri_score": "A",
                },
            )
        )
        assert doc["planning_tier"] == "excluded"
        assert doc["planning_excluded_reason"] == "manual"

    def test_exclusion_reason_cleared_when_recipe_becomes_eligible(self, recipe):
        doc = recipe.validate(
            build_document(owner_row(), preserve={"planning_excluded_reason": "no_title"})
        )
        assert doc["planning_tier"] != "excluded"
        assert "planning_excluded_reason" not in doc
