"""Tests for the declarative index definitions.

These are structural checks that run without a cluster. The mappings are also
validated against a live Elasticsearch during index creation; what these tests
guard is the class of mistake that only shows up at creation time — an invalid
parameter for a field type, a field the code writes but the mapping lacks, or a
generated artefact drifting from its source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recipe_wrangler.catalog.es_schema import (
    DEFAULT_SETTINGS,
    LABEL_ONLY_REGIONS,
    SCORE_REGIONS,
    recipe_index,
    recipe_profile_index,
)
from recipe_wrangler.utils.es_recipe_evidence import (
    RECIPE_EVIDENCE_MAPPING_PROPERTIES,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Parameters Elasticsearch rejects outright for a given field type.
FORBIDDEN_PARAMS = {
    "text": {"doc_values"},
    "nested": {"index", "doc_values", "analyzer"},
    "dense_vector": {"doc_values", "analyzer"},
}


def _walk(properties: dict, prefix: str = ""):
    """Yield (path, spec) for every field, recursing into object/nested."""
    for name, spec in properties.items():
        path = f"{prefix}{name}"
        yield path, spec
        if "properties" in spec:
            yield from _walk(spec["properties"], prefix=f"{path}.")
        for sub, sub_spec in (spec.get("fields") or {}).items():
            yield f"{path}.{sub}", sub_spec


@pytest.fixture(scope="module")
def recipes():
    return recipe_index()


@pytest.fixture(scope="module")
def profiles():
    return recipe_profile_index()


class TestMappingValidity:
    @pytest.mark.parametrize("build", [recipe_index, recipe_profile_index])
    def test_no_forbidden_parameters_for_field_type(self, build):
        """`text` has no doc_values parameter — the original v3 spec JSON
        carried one on `embedding_text` and would have been rejected at index
        creation."""
        offenders = []
        for path, spec in _walk(build()["mappings"]["properties"]):
            forbidden = FORBIDDEN_PARAMS.get(spec.get("type"), set())
            for param in forbidden:
                if param in spec:
                    offenders.append(f"{path}: {spec['type']} cannot take {param!r}")
        assert not offenders, offenders

    @pytest.mark.parametrize("build", [recipe_index, recipe_profile_index])
    def test_every_field_declares_a_type_or_is_an_object(self, build):
        for path, spec in _walk(build()["mappings"]["properties"]):
            assert "type" in spec or "properties" in spec, f"{path} has neither"

    @pytest.mark.parametrize("build", [recipe_index, recipe_profile_index])
    def test_dynamic_mapping_is_strict(self, build):
        assert build()["mappings"]["dynamic"] == "strict"

    @pytest.mark.parametrize("build", [recipe_index, recipe_profile_index])
    def test_settings_attached(self, build):
        assert build()["settings"] is DEFAULT_SETTINGS

    def test_copy_to_targets_exist(self, recipes):
        props = recipes["mappings"]["properties"]
        targets = set()
        for _path, spec in _walk(props):
            target = spec.get("copy_to")
            if target:
                targets.update([target] if isinstance(target, str) else target)
        for target in targets:
            assert target in props, f"copy_to target {target!r} is not a field"

    def test_autocomplete_uses_asymmetric_analysis(self, recipes):
        auto = recipes["mappings"]["properties"]["title"]["fields"]["autocomplete"]
        assert auto["analyzer"] == "autocomplete"
        assert auto["search_analyzer"] == "default_text"

    def test_embedding_dim_is_configurable(self):
        assert recipe_index(768)["mappings"]["properties"]["embedding"]["dims"] == 768


class TestRetrievalSurface:
    """v2 had no `description` field at all, so free-text search saw only
    title/ingredients/tags. `description` must feed retrieval — and
    `instructions` must deliberately not."""

    @pytest.mark.parametrize("field", ["title", "description"])
    def test_retrievable_text_is_indexed(self, recipes, field):
        spec = recipes["mappings"]["properties"][field]
        assert spec["type"] == "text"
        assert spec.get("index") is not False

    @pytest.mark.parametrize("field", ["title", "description"])
    def test_retrievable_text_feeds_all_text(self, recipes, field):
        assert recipes["mappings"]["properties"][field].get("copy_to") == "all_text"

    def test_instructions_never_feed_general_retrieval(self, recipes):
        """Method text is generic cooking nouns; matching it returns every
        recipe that mentions an ingredient rather than those about it."""
        spec = recipes["mappings"]["properties"]["instructions"]
        assert "copy_to" not in spec, "instructions must not feed all_text"

    def test_instructions_remain_queryable_on_explicit_opt_in(self, recipes):
        spec = recipes["mappings"]["properties"]["instructions"]
        assert spec["type"] == "text"
        assert spec.get("index") is not False


class TestEvidenceParity:
    def test_evidence_fields_match_the_code_that_writes_them(self, recipes):
        props = recipes["mappings"]["properties"]
        for name, spec in RECIPE_EVIDENCE_MAPPING_PROPERTIES.items():
            assert name in props, f"{name} missing from the recipes mapping"
            assert props[name] == spec, f"{name} diverges from es_recipe_evidence"


class TestAnnotationModel:
    ANNOTATION_FACETS = [
        "course_types",
        "cuisines",
        "food_groups",
        "flavor_profiles",
        "moods",
    ]

    @pytest.mark.parametrize("facet", ANNOTATION_FACETS)
    def test_facet_is_a_single_field(self, recipes, facet):
        """One field per concept. The parallel `ai_` twin was redundant:
        `annotation_evidence` records method and confidence per value, and
        `enhancements[].before` retains whatever was replaced — so provenance
        survives without a second name for the same thing."""
        props = recipes["mappings"]["properties"]
        assert facet in props
        assert f"ai_{facet}" not in props

    def test_allergens_keep_their_ai_twin(self, recipes):
        """The one exception, and it is a safety one: an inferred allergen
        rendered indistinguishably from a declared one could harm someone."""
        props = recipes["mappings"]["properties"]
        assert "allergens" in props
        assert "ai_allergens" in props

    def test_ai_generated_fields_retained(self, recipes):
        """How a UI badges model-derived content now that per-facet twins are
        gone."""
        assert "ai_generated_fields" in recipes["mappings"]["properties"]

    def test_annotation_evidence_carries_provenance(self, recipes):
        props = recipes["mappings"]["properties"]["annotation_evidence"]
        assert props["type"] == "nested"
        for field in ("facet", "value", "method", "evidence_status",
                      "confidence", "sources", "classification_version"):
            assert field in props["properties"], f"missing provenance: {field}"

    def test_dish_types_removed_in_favour_of_course_types(self, recipes):
        """`dish_types` and `course_types` were the same concept under two
        names. Keeping both is what allowed main-dish/main_dish to drift into
        separate buckets across two writers."""
        props = recipes["mappings"]["properties"]
        assert "course_types" in props
        assert "dish_types" not in props


class TestScoreFields:
    @pytest.mark.parametrize("region", SCORE_REGIONS)
    def test_full_regions_have_the_complete_quartet(self, recipes, region):
        props = recipes["mappings"]["properties"]
        for prefix, expected in (
            ("nutri_score", "keyword"),
            ("nutri_color", "keyword"),
            ("nutri_rank", "byte"),
            ("nutri_points", "float"),
        ):
            field = f"{prefix}_{region}"
            assert field in props, field
            assert props[field]["type"] == expected

    @pytest.mark.parametrize("region", LABEL_ONLY_REGIONS)
    def test_label_only_regions_have_label_and_colour(self, recipes, region):
        props = recipes["mappings"]["properties"]
        assert f"nutri_score_{region}" in props
        assert f"nutri_color_{region}" in props

    def test_default_score_exists_for_list_detail_agreement(self, recipes):
        """The single field a search result and a detail page must both read —
        the fix for a recipe showing C in a list and A on its page."""
        props = recipes["mappings"]["properties"]
        assert props["default_nutri_score"]["type"] == "keyword"
        assert props["default_nutri_rank"]["type"] == "byte"


class TestProfileIndex:
    def test_grain_fields_present(self, profiles):
        props = profiles["mappings"]["properties"]
        for field in ("recipe_id", "recipe_urn", "nutrition_source", "urn"):
            assert field in props

    def test_large_blobs_are_stored_but_not_indexed(self, profiles):
        props = profiles["mappings"]["properties"]
        for field in ("trace", "nutri_score_breakdown", "total_nutrients"):
            assert props[field]["type"] == "object"
            assert props[field]["enabled"] is False

    def test_per_serving_nutrients_are_queryable(self, profiles):
        spec = profiles["mappings"]["properties"]["total_nutrients_per_serving"]
        assert spec["type"] == "nested"
        assert spec["properties"]["value"]["type"] == "float"


class TestGeneratedArtefacts:
    @pytest.mark.parametrize(
        "filename,build",
        [
            ("recipes_v3_mapping.json", recipe_index),
            ("recipe_profiles_v1_mapping.json", recipe_profile_index),
        ],
    )
    def test_json_artefact_matches_its_source(self, filename, build):
        path = REPO_ROOT / "docs/specs" / filename
        assert path.exists(), f"{filename} missing — run scripts/catalog/dump_mappings.py"
        on_disk = json.loads(path.read_text())
        assert on_disk == json.loads(json.dumps(build())), (
            f"{filename} is stale — run: python scripts/catalog/dump_mappings.py"
        )
