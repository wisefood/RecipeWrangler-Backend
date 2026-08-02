"""The default retrieval surface.

Free-text search must be driven by what a recipe *is* (title, ingredients,
classification), not by incidental mentions in its method text.
"""

from __future__ import annotations

from recipe_wrangler.catalog.elastic import DEFAULT_SEARCH_FIELDS


def _field_names() -> set[str]:
    return {f.split("^", 1)[0] for f in DEFAULT_SEARCH_FIELDS}


def _boost(field: str) -> float:
    for entry in DEFAULT_SEARCH_FIELDS:
        name, _, boost = entry.partition("^")
        if name == field:
            return float(boost) if boost else 1.0
    raise AssertionError(f"{field} is not a default search field")


class TestDefaultSearchFields:
    def test_instructions_excluded(self):
        assert "instructions" not in _field_names()

    def test_wildcard_never_used(self):
        """`fields: ["*"]` is what makes a method step score like a title."""
        assert "*" not in _field_names()

    def test_core_identity_fields_present(self):
        assert {"title", "ingredients.name", "tags"} <= _field_names()

    def test_title_outweighs_everything_else(self):
        title = _boost("title")
        for field in _field_names() - {"title"}:
            assert _boost(field) < title, f"{field} must not outrank title"

    def test_ingredients_outrank_free_text_catch_all(self):
        assert _boost("ingredients.name") > _boost("all_text")

    def test_annotation_facets_contribute(self):
        """course_types/cuisines are the facets the category UI needs; they must
        influence retrieval once populated."""
        assert {"course_types", "cuisines"} <= _field_names()

    def test_no_duplicate_fields(self):
        names = [f.split("^", 1)[0] for f in DEFAULT_SEARCH_FIELDS]
        assert len(names) == len(set(names))
