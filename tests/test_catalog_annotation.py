"""The shared annotation core.

Everything here runs without a model: prompt construction, vocabulary
enforcement and provenance are the parts that must be deterministic. The model
call itself is the only non-deterministic piece and is deliberately not mocked
into these tests — what matters is that whatever it returns is constrained
before it reaches a document.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.catalog import annotation as A
from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog import vocabularies as V


class TestVocabularyEnforcement:
    def test_out_of_vocabulary_values_are_discarded(self):
        values, _ = A.validate_facets(
            {"cuisines": ["italian", "fusion", "martian"], "confidence": 0.8}
        )
        assert values["cuisines"] == ["italian"]

    def test_a_facet_with_no_valid_values_is_omitted_entirely(self):
        values, _ = A.validate_facets({"cuisines": ["fusion"], "moods": []})
        assert "cuisines" not in values
        assert "moods" not in values

    def test_course_types_are_canonicalized_not_just_filtered(self):
        """A model saying 'dessert' or 'dinner' must land on the stored
        spelling, or the value is unbrowsable."""
        values, _ = A.validate_facets({"course_types": ["dessert", "dinner"]})
        assert set(values["course_types"]) == {"desserts", "main-dish"}

    def test_spacing_and_case_tolerated(self):
        values, _ = A.validate_facets({"moods": ["  Comfort  "], "cuisines": ["ITALIAN"]})
        assert values["moods"] == ["comfort"]
        assert values["cuisines"] == ["italian"]

    def test_multi_word_values_accept_spaces(self):
        values, _ = A.validate_facets({"cuisines": ["middle eastern"]})
        assert values["cuisines"] == ["middle_eastern"]

    @pytest.mark.parametrize("bad", [None, "not-a-number", {}, []])
    def test_unusable_confidence_becomes_none(self, bad):
        _, confidence = A.validate_facets({"confidence": bad})
        assert confidence is None

    def test_confidence_parsed_when_numeric(self):
        _, confidence = A.validate_facets({"confidence": "0.75"})
        assert confidence == 0.75

    def test_only_requested_facets_are_returned(self):
        values, _ = A.validate_facets(
            {"cuisines": ["italian"], "moods": ["comfort"]}, facets=("cuisines",)
        )
        assert set(values) == {"cuisines"}


class TestPromptConstruction:
    def test_system_prompt_lists_every_allowed_value(self):
        for value in (*S.COURSE_TYPES, *V.CUISINES, *V.MOODS, *V.FLAVOR_PROFILES):
            assert value in A.SYSTEM_PROMPT

    def test_system_prompt_permits_abstention(self):
        """An empty answer must be explicitly allowed, or the model guesses."""
        assert "empty list" in A.SYSTEM_PROMPT
        assert "a guess is not" in A.SYSTEM_PROMPT

    def test_system_prompt_warns_against_single_ingredient_inference(self):
        assert "olive oil does not make a dish Italian" in A.SYSTEM_PROMPT

    def test_existing_course_is_stated_when_trusted(self):
        prompt = A.build_user_prompt(
            title="X", existing_course_types=["desserts"], trust_existing_course=True
        )
        assert "do not change" in prompt

    def test_existing_course_is_withheld_when_being_corrected(self):
        """Stating the stored value anchors the model to it — a chocolate
        brownie came back 'main-dish' purely because the prompt said so."""
        prompt = A.build_user_prompt(
            title="Chocolate brownie",
            existing_course_types=["main-dish"],
            trust_existing_course=False,
        )
        assert "main-dish" not in prompt

    def test_source_cuisine_prior_is_offered_as_a_prior_not_a_fact(self):
        prompt = A.build_user_prompt(title="Goulash", source="Curated Hungarian Recipes")
        assert "hungarian" in prompt.lower()
        assert "not a certainty" in prompt

    def test_unknown_source_adds_no_hint(self):
        assert "Source hint" not in A.build_user_prompt(title="X", source="MyPlate")

    def test_internal_tags_are_not_leaked_into_the_prompt(self):
        prompt = A.build_user_prompt(
            title="X", tags=["source:essrg", "type:lunch", "vegetarian"]
        )
        assert "source:essrg" not in prompt
        assert "type:lunch" not in prompt
        assert "vegetarian" in prompt

    def test_ingredients_are_capped(self):
        prompt = A.build_user_prompt(
            title="X", ingredients=[f"ingredient-{i}" for i in range(80)]
        )
        assert "ingredient-39" in prompt
        assert "ingredient-60" not in prompt

    def test_blank_ingredients_dropped(self):
        prompt = A.build_user_prompt(title="X", ingredients=["onion", "", "  ", None])
        assert "onion" in prompt


class TestProvenance:
    def test_user_confirmed_is_distinguishable_from_model(self):
        """A value a person accepted is stronger evidence than one a model
        produced, and a later bulk pass must be able to tell them apart."""
        confirmed = A.evidence_for({"cuisines": ["italian"]}, method="user_confirmed")
        modelled = A.evidence_for({"cuisines": ["italian"]}, method="model")
        assert confirmed[0]["method"] == "user_confirmed"
        assert modelled[0]["method"] == "model"

    def test_one_evidence_entry_per_value(self):
        entries = A.evidence_for(
            {"cuisines": ["italian", "greek"], "moods": ["light"]}, method="model"
        )
        assert len(entries) == 3

    def test_every_entry_carries_the_vocabulary_version(self):
        for entry in A.evidence_for({"moods": ["light"]}, method="model", confidence=0.5):
            assert entry["classification_version"] == V.CLASSIFICATION_VERSION
            assert entry["facet"] == "moods"
            assert entry["confidence"] == 0.5

    def test_empty_input_produces_no_evidence(self):
        assert A.evidence_for({}, method="model") == []


class TestFacetPartitioning:
    def test_food_groups_is_derived_not_modelled(self):
        """It comes from FoodOn ancestry, so it must never be asked of a model
        — including at draft time, where the ingredients are not yet resolved."""
        assert "food_groups" in A.DERIVED_FACETS
        assert "food_groups" not in A.MODEL_FACETS
        assert "food_groups" not in A.SYSTEM_PROMPT

    def test_model_facets_are_exactly_the_subjective_ones(self):
        assert set(A.MODEL_FACETS) == {
            "course_types",
            "cuisines",
            "flavor_profiles",
            "moods",
        }
