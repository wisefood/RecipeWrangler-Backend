"""
The details card carries the recipe's own dietary tags.

FoodChat filters on `diet_tags` and had no way to CHECK the result: the card
returned macros, duration, tags, dish types and allergens, and nothing that
said whether the dish that came back was actually vegetarian. So a plan built
from a vegetarian filter reported "vegetarian: satisfied" because the word had
been sent, and a mislabelled or unfiltered recipe was indistinguishable from a
correct one.

The tags were already in Neo4j, on the same two categories the Elasticsearch
projection reads (`dietary`, `dietary_option`). This exposes them.
"""

from __future__ import annotations

from unittest.mock import patch

from recipe_wrangler.tools import fetch_recipe_info as F


class _FakeRecord:
    """Shaped like neo4j.Record: not a dict, dict()-able."""

    def __init__(self, payload: dict):
        self._payload = payload

    def keys(self):
        return self._payload.keys()

    def __getitem__(self, key):
        return self._payload[key]


def _record(lookup_id="r-1", diet_tags=None):
    return _FakeRecord({
        "lookup_id": lookup_id,
        "recipe": {"recipe_id": lookup_id, "title": "Chickpea stew", "serves": 2},
        "ingredients": [],
        "tags": ["healthy_and_nutritious"],
        "dish_types": ["main"],
        "diet_tags": diet_tags if diet_tags is not None else ["vegetarian", "vegan"],
    })


class TestTheQuery:
    def test_it_reads_the_same_categories_the_projection_reads(self):
        """A recipe's diet tags must mean the same thing whichever store
        answered — otherwise a search and a check disagree about the same dish."""
        query = F._RECIPE_INFO_BULK_QUERY
        assert "'dietary'" in query and "'dietary_option'" in query
        assert "AS diet_tags" in query

    def test_dish_types_and_tags_are_still_collected(self):
        query = F._RECIPE_INFO_BULK_QUERY
        assert "AS dish_types" in query and "AS tags" in query


class TestTheRecord:
    def test_diet_tags_reach_the_recipe_dict(self):
        with patch.object(F, "run_query", return_value=[_record()]):
            result = F.fetch_recipe_info_by_ids(["r-1"])
        assert result["r-1"]["diet_tags"] == ["vegetarian", "vegan"]

    def test_a_recipe_with_no_dietary_tags_gets_an_empty_list(self):
        """Absent must be empty, not missing: a consumer that checks
        `diet_tags` should not have to also check whether the key exists."""
        with patch.object(F, "run_query", return_value=[_record(diet_tags=[])]):
            result = F.fetch_recipe_info_by_ids(["r-1"])
        assert result["r-1"]["diet_tags"] == []

    def test_a_row_without_the_field_at_all_is_not_a_crash(self):
        """Old cached rows and any other producer of this shape."""
        record = _FakeRecord({
            "lookup_id": "r-2",
            "recipe": {"recipe_id": "r-2", "title": "Old", "serves": 1},
            "ingredients": [], "tags": [], "dish_types": [],
        })
        with patch.object(F, "run_query", return_value=[record]):
            result = F.fetch_recipe_info_by_ids(["r-2"])
        assert result["r-2"]["diet_tags"] == []

    def test_blank_tags_are_dropped_like_the_others(self):
        with patch.object(F, "run_query", return_value=[_record(diet_tags=["vegan", "  ", ""])]):
            result = F.fetch_recipe_info_by_ids(["r-1"])
        assert result["r-1"]["diet_tags"] == ["vegan"]


class TestTheCard:
    def test_the_schema_carries_the_field(self):
        from recipe_wrangler.schemas.models import RecipeCardNutrition

        assert "diet_tags" in RecipeCardNutrition.model_fields

    def test_the_builder_passes_it_through(self):
        from recipe_wrangler.api.routers.recipes import _build_card_nutrition

        card = _build_card_nutrition(
            recipe_id="r-1",
            recipe={"title": "Chickpea stew", "diet_tags": ["vegetarian"], "tags": []},
            nutrition=None, allergens=[], nutri_score=None,
        )
        assert card.diet_tags == ["vegetarian"]

    def test_a_recipe_without_them_still_builds(self):
        from recipe_wrangler.api.routers.recipes import _build_card_nutrition

        card = _build_card_nutrition(
            recipe_id="r-1", recipe={"title": "Old"}, nutrition=None,
            allergens=[], nutri_score=None,
        )
        assert card.diet_tags == []


class TestTheCache:
    def test_the_variant_changed_with_the_field(self):
        """An entry cached before the field existed still PARSES — every field
        has a default — so it would come back with `diet_tags` empty, and a
        consumer checking it would read a cached vegetarian recipe as not
        vegetarian. Silent, wrong, and safety-shaped."""
        from recipe_wrangler.api.routers import recipes

        assert recipes._CARD_NUTRITION_CACHE_VERSION == "v2"
        assert "v2" in recipes._card_nutrition_cache_variant(None)

    def test_the_region_still_namespaces_it(self):
        from recipe_wrangler.api.routers import recipes

        assert recipes._card_nutrition_cache_variant("IE") != \
            recipes._card_nutrition_cache_variant("SI")
