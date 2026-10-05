"""`POST /recipes/profile` carries the model-suggested facets.

The facets are additive and best-effort: a profile must never fail because the
annotation call did, and a client that asked for no annotation must not pay
for one. The profiling chain and allergen detection are stubbed; what is under
test is the wiring around `catalog.annotation.suggest` and the model default
it reaches for.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from pathlib import Path

import pytest

from recipe_wrangler.api.routers import recipes as R
from recipe_wrangler.catalog import annotation
from recipe_wrangler.schemas.models import RecipeProfileRequest
from recipe_wrangler.utils.model_registry import RETIRED, resolve

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def profiled(monkeypatch):
    class FakeChain:
        @staticmethod
        def invoke(payload):
            return {
                "title": "Chicken Tikka Masala",
                "ingredient_names": ["chicken", "garam masala"],
                "profiling_totals": {},
                "ingredients": [],
            }

    monkeypatch.setattr(R, "Recipe_Profiling_Chain", FakeChain)
    monkeypatch.setattr(
        R,
        "_analysis_allergen_fields",
        lambda names: {"allergens": [], "allergen_evidence": []},
    )


def _profile(**fields):
    request = RecipeProfileRequest(raw_recipe="Chicken Tikka Masala\n2 chicken breasts", **fields)
    return asyncio.run(R.recipe_profile(request))


def test_facets_ride_on_the_profile_under_index_field_names(profiled, monkeypatch):
    calls = []

    def suggest(**kwargs):
        calls.append(kwargs)
        return {"course_types": ["main-dish"], "cuisines": ["indian"]}, 0.9

    monkeypatch.setattr(annotation, "suggest", suggest)

    out = _profile()

    assert out["course_types"] == ["main-dish"]
    assert out["cuisines"] == ["indian"]
    # A facet the model abstained on is an explicit empty list, not a missing key.
    assert out["flavor_profiles"] == []
    assert out["moods"] == []
    assert out["annotation_confidence"] == 0.9
    assert "annotations_warning" not in out
    assert calls == [
        {"title": "Chicken Tikka Masala", "ingredients": ["chicken", "garam masala"]}
    ]


def test_annotate_false_skips_the_model_call(profiled, monkeypatch):
    monkeypatch.setattr(
        annotation, "suggest", lambda **_: pytest.fail("annotation must not be called")
    )

    out = _profile(annotate=False)

    for facet in annotation.MODEL_FACETS:
        assert facet not in out
    assert "annotation_confidence" not in out
    assert "annotations_warning" not in out


def test_a_failed_annotation_leaves_the_profile_intact(profiled, monkeypatch):
    def boom(**_):
        raise RuntimeError("model_not_found")

    monkeypatch.setattr(annotation, "suggest", boom)

    out = _profile()

    assert out["message"] == "Success"
    assert out["title"] == "Chicken Tikka Masala"
    assert out["annotations_warning"] == "Annotation failed: model_not_found"
    for facet in annotation.MODEL_FACETS:
        assert facet not in out


class TestAnnotationModelDefault:
    """Groq retired every llama id this stack used to default to; the
    annotation default must be a live model, and every annotation entry point
    must reach for the same one."""

    def test_default_is_not_a_retired_id(self):
        assert annotation.DEFAULT_MODEL not in RETIRED
        assert resolve(annotation.DEFAULT_MODEL) == annotation.DEFAULT_MODEL

    def test_default_without_env_is_the_large_groq_model(self, monkeypatch):
        monkeypatch.delenv("ANNOTATION_MODEL", raising=False)
        try:
            assert importlib.reload(annotation).DEFAULT_MODEL == "openai/gpt-oss-120b"
        finally:
            importlib.reload(annotation)

    @staticmethod
    def _load_script(name: str):
        path = REPO_ROOT / "scripts" / "catalog" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_main_dish_audit_shares_the_default(self):
        audit = self._load_script("audit_main_dish_claims")
        assert audit.DEFAULT_MODEL == annotation.DEFAULT_MODEL

    def test_bulk_annotator_shares_the_default(self):
        script = self._load_script("annotate_recipes")
        assert script.ANNOTATION_DEFAULT_MODEL == annotation.DEFAULT_MODEL
