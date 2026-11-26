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

    def test_course_types_are_canonicalized_from_the_model(self):
        values, _ = A.validate_facets(
            {"course_types": ["dessert", "dinner"]}, facets=("course_types",)
        )
        assert values["course_types"] == ["desserts", "main-dish"]

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
        for value in (*V.CUISINES, *V.MOODS, *V.FLAVOR_PROFILES):
            assert value in A.SYSTEM_PROMPT

    def test_system_prompt_requests_every_course_type(self):
        assert "course_types" in A.SYSTEM_PROMPT
        for value in A.S.COURSE_TYPES:
            assert value in A.SYSTEM_PROMPT

    def test_system_prompt_permits_abstention(self):
        """An empty answer must be explicitly allowed, or the model guesses."""
        assert "empty list" in A.SYSTEM_PROMPT
        assert "a guess is not" in A.SYSTEM_PROMPT

    def test_system_prompt_warns_against_single_ingredient_inference(self):
        assert "olive oil does not make a dish Italian" in A.SYSTEM_PROMPT

    def test_system_prompt_defines_main_dish_from_owner_review(self):
        """Owner review 2026-11-24: sauces/breads/plain grains are sides; filled pasta, meatballs, fishcakes are mains."""
        prompt = " ".join(A.SYSTEM_PROMPT.split())
        for phrase in ("complete plate", "bare sauce", "plain grain", "Pasta with its sauce", "meatballs", "fishcakes"):
            assert phrase in prompt

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

    def test_model_facets_include_the_four_llm_facets(self):
        assert set(A.MODEL_FACETS) == {
            "course_types",
            "cuisines",
            "flavor_profiles",
            "moods",
        }
        assert "course_types" in A.SYSTEM_PROMPT


class TestFoodGroupResolution:
    @staticmethod
    def path(ingredient, foodon_id, distance, ingredient_id=None):
        return {
            "ingredient": ingredient,
            "ingredient_id": ingredient_id or ingredient,
            "foodon_id": foodon_id,
            "distance": distance,
        }

    def test_egg_group_beats_broader_poultry_ancestor(self):
        groups, evidence = A.derive_food_groups({"ingredient_foodon_paths": [
            self.path("egg white", "FOODON_03304840", 1),
            self.path("egg white", "FOODON_02020686", 3),
        ]})
        assert groups == ["eggs"]
        assert evidence[0]["supporting_ingredients"] == ["egg white"]

    def test_egg_removes_stale_preserved_poultry_group(self):
        groups, _ = A.derive_food_groups({
            "ingredient_foodon_paths": [
                self.path("egg white", "FOODON_03304840", 1),
                self.path("egg white", "FOODON_02020686", 3),
            ],
            "existing_food_groups": ["dairy", "eggs", "poultry"],
        })
        assert groups == ["dairy", "eggs"]

    def test_different_ingredients_can_supply_eggs_and_poultry(self):
        groups, _ = A.derive_food_groups({"ingredient_foodon_paths": [
            self.path("egg", "FOODON_03420225", 0, "egg"),
            self.path("chicken", "FOODON_02020686", 1, "chicken"),
        ]})
        assert groups == ["eggs", "poultry"]

    def test_preserved_poultry_survives_when_chicken_supports_it(self):
        groups, _ = A.derive_food_groups({
            "ingredient_foodon_paths": [
                self.path("egg", "FOODON_03420225", 0, "egg"),
                self.path("egg", "FOODON_02020686", 3, "egg"),
                self.path("chicken", "FOODON_02010107", 3, "chicken"),
                self.path("chicken", "FOODON_02020686", 4, "chicken"),
            ],
            "existing_food_groups": ["eggs", "poultry"],
        })
        assert groups == ["eggs", "poultry"]

    def test_berries_are_fruit_not_broad_vegetables(self):
        groups, _ = A.derive_food_groups({"ingredient_foodon_paths": [
            self.path("mixed berries", "FOODON_00001057", 3),
            self.path("mixed berries", "FOODON_00001261", 4),
        ]})
        assert groups == ["fruit"]

    def test_mixed_berries_repairs_bad_direct_mixed_vegetable_class(self):
        groups, evidence = A.derive_food_groups({"ingredient_foodon_paths": [
            self.path("mixed berries", "FOODON_00002683", 0),
        ]})
        assert groups == ["fruit"]
        assert evidence[0]["foodon_ids"] == ["FOODON_00002683"]

    def test_named_berry_repairs_bad_direct_vegetable_class(self):
        groups, _ = A.derive_food_groups({"ingredient_foodon_paths": [
            self.path("boysenberries", "FOODON_00002683", 0),
        ]})
        assert groups == ["fruit"]

    def test_mint_is_an_herb_not_a_vegetable(self):
        groups, _ = A.derive_food_groups({"ingredient_foodon_paths": [
            self.path("fresh mint", "FOODON_00001261", 3),
        ]})
        assert groups == ["herbs_and_spices"]

    def test_stale_vegetable_group_is_removed_for_fruit_only_recipe(self):
        groups, _ = A.derive_food_groups({
            "ingredient_foodon_paths": [
                self.path("goji berries", "FOODON_03316579", 1),
            ],
            "ingredient_names": ["goji berries", "dark chocolate"],
            "existing_food_groups": ["fruit", "vegetables"],
        })
        assert groups == ["fruit"]

    def test_eggplant_does_not_trigger_the_egg_poultry_rule(self):
        groups, _ = A.derive_food_groups({
            "ingredient_foodon_paths": [
                self.path("eggplant", "FOODON_00001261", 1),
            ],
            "ingredient_names": ["eggplant"],
            "existing_food_groups": ["poultry", "vegetables"],
        })
        assert groups == ["poultry", "vegetables"]

    def test_stale_animal_groups_require_an_identifying_ingredient(self):
        groups, _ = A.derive_food_groups({
            "ingredient_foodon_paths": [
                self.path("squash", "FOODON_00001261", 1),
            ],
            "ingredient_names": ["squash", "eggs", "walnuts"],
            "existing_food_groups": ["eggs", "meat", "seafood", "vegetables"],
        })
        assert groups == ["eggs", "vegetables"]

    def test_culinary_vegetable_override_beats_botanical_fruit(self):
        groups, _ = A.derive_food_groups({"ingredient_foodon_paths": [
            self.path("tomatoes", "FOODON_00001057", 3),
            self.path("tomatoes", "FOODON_00001261", 4),
        ]})
        assert groups == ["vegetables"]

    def test_fruit_and_vegetables_survive_when_supplied_by_different_ingredients(self):
        groups, _ = A.derive_food_groups({"ingredient_foodon_paths": [
            self.path("apple", "FOODON_00001057", 2, "apple"),
            self.path("broccoli", "FOODON_00001261", 2, "broccoli"),
        ]})
        assert groups == ["fruit", "vegetables"]
