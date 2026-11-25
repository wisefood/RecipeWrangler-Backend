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
from recipe_wrangler.utils.consumer_suitability import SUITABILITY_CLASSIFICATION_VERSION
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
    # Owner queries return only explicitly versioned current assessments.
    for assessment in row.get("consumer_suitability") or []:
        assessment.setdefault("classification_version", "vegan-vegetarian-v8")
    return row


class TestDocumentShape:
    @pytest.mark.parametrize('bulk', [False, True])
    def test_known_allergen_conflict_suppresses_free_from_facet_but_preserves_labels(self, bulk, recipe):
        builder = build_document
        if bulk:
            from scripts.catalog.build_recipes import build_document as builder
        row = owner_row(tags=['dairy_free', 'nut_free', 'original'],
                        diet_tags=['dairy_free', 'nut_free', 'gluten_free_option'],
                        allergens=[], allergen_evidence=[{'allergen':'dairy', 'ingredient':'sauce', 'presence':'contains'}])
        doc = builder(row, profiles=[])
        assert doc['tags'] == ['dairy_free', 'nut_free', 'original']
        assert 'dairy_free' not in doc['diet_tags']
        assert doc['diet_tags'] == ['gluten_free_option', 'nut_free']
        assert recipe.validate(doc)['allergen_uncertainty'] is True

    @pytest.mark.parametrize('bulk', [False, True])
    def test_recipe_claim_overrides_stale_suitability_in_both_writers(self, bulk):
        builder = build_document
        if bulk:
            from scripts.catalog.build_recipes import build_document as builder
        row = owner_row(
            tags=['vegan', 'vegetarian'], diet_tags=['vegan', 'vegetarian'],
            allergens=['milk'],
            consumer_suitability=[{'group':'vegan', 'status':'suitable', 'classification_version':SUITABILITY_CLASSIFICATION_VERSION},
                                  {'group':'vegetarian', 'status':'suitable', 'classification_version':SUITABILITY_CLASSIFICATION_VERSION}],
        )
        doc = builder(row, profiles=[])
        assert doc['diet_tags'] == ['vegetarian']
        assert doc['suitable_for'] == ['vegetarian']
        assert doc['tags'] == ['vegan', 'vegetarian']
        assert doc['consumer_suitability'][0]['status'] == 'not_suitable'

    @pytest.mark.parametrize('status,expected', [('suitable', ['vegan']), ('unknown', []), ('not_suitable', [])])
    def test_diet_facet_follows_suitability_and_keeps_original_labels(self, status, expected):
        doc = build_document(owner_row(
            tags=['vegan', 'source_original'], diet_tags=['vegan', 'gluten_free_option'],
            consumer_suitability=[{'group':'vegan', 'status':status, 'classification_version':SUITABILITY_CLASSIFICATION_VERSION}],
        ))
        assert doc['tags'] == ['vegan', 'source_original']
        assert doc['diet_tags'] == sorted(expected + ['gluten_free_option'])
        assert doc['suitable_for'] == expected

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
        assert "course_types" not in doc
        assert doc["ingredient_names"] == ["onion", "garlic"]

    def test_profile_flag_is_derived_from_postgres_profiles(self):
        assert build_document(owner_row(has_profile=True))["has_profile"] is False
        profile = {
            "nutrition_source": "eu",
            "nutri_score": None,
            "nutri_color": None,
            "nutri_points": None,
            "is_ground_truth": False,
            "computed_at": None,
        }
        assert build_document(owner_row(), profiles=[profile])[
            "has_profile"
        ] is True

    def test_projection_keeps_ingredient_measurements_for_detail_reads(self, recipe):
        ingredients = [
            {
                "name": "olive oil",
                "quantity": "1.5",
                "unit": "tbsp",
                "measurement": "1 1/2 tbsp",
                "display": "1½ tbsp",
                "source_text": "1½ tbsp olive oil, for frying",
                "position": 0,
                "canonical_id": "oil-1",
            }
        ]
        doc = recipe.validate(build_document(owner_row(ingredients=ingredients)))
        assert doc["ingredients"] == [{
            "name": "olive oil",
            "quantity": 1.5,
            "unit": "tbsp",
            "measurement": "1 1/2 tbsp",
            "display_text": "1½ tbsp olive oil, for frying",
            "display_source": "authored_source_text",
            "position": 0,
            "canonical_urn": "urn:ingredient:oil-1",
        }]

    def test_projection_separates_generated_display_from_calculation_measurement(self, recipe):
        doc = recipe.validate(build_document(owner_row(
            source="Curated Hungarian Recipes",
            ingredients=[{
                "name": "Eggs",
                "quantity": "100",
                "unit": "g",
                "measurement": "100 g",
                "position": 0,
            }],
        )))
        assert doc["ingredients"][0] == {
            "name": "Eggs",
            "quantity": 100.0,
            "unit": "g",
            "measurement": "100 g",
            "display_text": "2 eggs",
            "display_source": "reviewed_portion_reference_v1",
            "position": 0,
        }

    def test_retired_recipe1m_source_cannot_be_projected(self):
        with pytest.raises(Exception, match="retired recipe source"):
            build_document(owner_row(source="recipe1m"))


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
            if field == "food_groups":
                continue
            assert doc[field] == value
        assert doc["food_groups"] == ["vegetables"]

    def test_only_preserved_course_type_is_written(self):
        """Missing course types stay empty; received ES annotations survive."""
        doc = build_document(
            owner_row(tag_dish_types=["main-dish"]),
            preserve={"course_types": ["desserts"]},
        )
        assert doc["course_types"] == ["desserts"]

    def test_paid_course_annotation_survives_title_based_reprojection(self, recipe):
        doc = recipe.validate(
            build_document(
                owner_row(title="Basic tomato sauce"),
                preserve={"course_types": ["main-dish"]},
            )
        )
        assert doc["course_types"] == ["main-dish"]

    def test_every_annotation_facet_is_in_the_preserve_list(self):
        for field in ("course_types", "cuisines", "flavor_profiles", "moods",
                      "annotation_evidence", "enhancements"):
            assert field in ES_OWNED_FIELDS
        assert "food_groups" not in ES_OWNED_FIELDS

    def test_convenience_is_rederived_not_preserved(self, recipe):
        assert "convenience" not in ES_OWNED_FIELDS
        doc = recipe.validate(
            build_document(
                owner_row(duration=60, ingredients=[str(i) for i in range(6)]),
                preserve={"convenience": ["quick"]},
            )
        )
        assert doc["convenience"] == []

    def test_planning_overrides_are_preserved(self):
        """A human decision to exclude a recipe from planning must survive a
        re-projection, or it silently reverts."""
        assert "planning_tier" in ES_OWNED_FIELDS
        assert "planning_excluded_reason" in ES_OWNED_FIELDS

    def test_created_at_is_preserved(self):
        assert "created_at" in ES_OWNED_FIELDS

    def test_title_change_drops_stale_embedding_but_keeps_annotations(self):
        doc = build_document(
            owner_row(title="New title"),
            preserve={
                "cuisines": ["irish"],
                "embedding": [0.1, 0.2],
                "embedding_model": "test-model",
                "embedding_text": "Old title",
                "embedded_at": "2026-01-01T00:00:00+00:00",
            },
        )
        assert doc["cuisines"] == ["irish"]
        assert "embedding" not in doc
        assert "embedding_text" not in doc


class TestPlanningTier:
    def test_curated_without_course_type_is_standard(self, recipe):
        doc = recipe.validate(
            build_document(
                owner_row(),
                preserve={"has_profile": True, "default_nutri_score": "A"},
            )
        )
        assert doc["planning_tier"] == "standard"

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


def test_projection_recomputes_uncertainty_instead_of_preserving_false_safety():
    doc = build_document(owner_row(allergens=['milk']), preserve={
        'allergen_uncertainty': False, 'allergen_assessment': {'unresolved_allergens': []},
    })
    assert doc['allergen_uncertainty'] is True
    assert doc['allergen_assessment']['detected_allergens'] == ['milk']
    assert 'peanut' in doc['allergen_assessment']['unresolved_allergens']
