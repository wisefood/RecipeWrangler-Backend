"""Content digests, and the properties that make them usable.

A consistency checker has two failure modes, and the second is worse. It can
miss real drift — a bug. Or it can report drift that is not there, on every
run, until everyone learns to ignore it — at which point it is worse than
having no checker, because it looks like one.

Most of these tests are about the second mode: the same recipe, arriving in
different shapes from different stores, must digest identically. The rest check
the first: a real content change must be caught, and named.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.catalog.integrity import (
    DIGESTED_FIELDS,
    content_digest,
    digest_differences,
    digest_payload,
    summarise_difference,
)


def recipe(**overrides):
    base = {
        "recipe_id": "r-1",
        "title": "Caponata",
        "description": "Sicilian aubergine stew",
        "instructions": "Fry. Simmer. Cool.",
        "source": "user",
        "duration": 45,
        "serves": 4,
        "ingredients": ["aubergine", "olive oil", "capers"],
        "allergens": [],
        "tags": ["vegetarian"],
        "diet_tags": ["vegetarian"],
    }
    base.update(overrides)
    return base


class TestStability:
    """Same content, same digest — whatever shape or order it arrives in."""

    def test_identical_documents_agree(self):
        assert content_digest(recipe()) == content_digest(recipe())

    def test_list_order_is_irrelevant(self):
        """Cypher returns collected lists in planner order, which is not stable.

        Hashing that order would report drift on every projection of a recipe
        nobody touched.
        """
        shuffled = recipe(ingredients=["capers", "aubergine", "olive oil"])
        assert content_digest(recipe()) == content_digest(shuffled)

    def test_case_and_whitespace_are_irrelevant_in_sets(self):
        noisy = recipe(ingredients=["  Aubergine ", "OLIVE OIL", "capers"])
        assert content_digest(recipe()) == content_digest(noisy)

    def test_duplicates_within_a_set_are_irrelevant(self):
        dupes = recipe(ingredients=["aubergine", "aubergine", "olive oil", "capers"])
        assert content_digest(recipe()) == content_digest(dupes)

    def test_whole_floats_and_ints_agree(self):
        """Cypher returns `45.0` for a duration Elasticsearch stores as `45`."""
        assert content_digest(recipe(duration=45)) == content_digest(
            recipe(duration=45.0)
        )

    def test_surrounding_whitespace_on_scalars_is_irrelevant(self):
        assert content_digest(recipe(title="Caponata")) == content_digest(
            recipe(title="  Caponata  ")
        )

    def test_nested_and_flat_ingredient_shapes_agree(self):
        """The one that would have broken reconciliation on every recipe.

        The projection builds `["aubergine"]`; `Recipe.validate` reshapes it to
        `[{"name": "aubergine", "position": 0}]` for the nested mapping. A
        digest that saw those as different could never be recomputed from a
        stored document.
        """
        nested = recipe(
            ingredients=[
                {"name": "aubergine", "position": 0},
                {"name": "olive oil", "position": 1},
                {"name": "capers", "position": 2},
            ]
        )
        assert content_digest(recipe()) == content_digest(nested)

    def test_nested_position_does_not_affect_the_digest(self):
        """`position` is an artifact of storing an ordered list, not content."""
        a = recipe(ingredients=[{"name": "aubergine", "position": 0}])
        b = recipe(ingredients=[{"name": "aubergine", "position": 7}])
        assert content_digest(a) == content_digest(b)

    def test_missing_and_empty_lists_agree(self):
        """An absent list and an empty one mean the same thing.

        The cheap reconciliation query omits fields the full projection emits;
        without this they would differ for every recipe that has none of them.
        """
        without = recipe()
        without.pop("allergens")
        assert content_digest(recipe(allergens=[])) == content_digest(without)


class TestSensitivity:
    """A real change must be caught, and named."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("title", "Ratatouille"),
            ("description", "Something else entirely"),
            ("instructions", "Do it differently"),
            ("duration", 90),
            ("serves", 8),
            ("source_id", "changed"),
        ],
    )
    def test_scalar_changes_are_detected(self, field, value):
        changed = recipe(**{field: value})
        assert content_digest(recipe()) != content_digest(changed)
        assert digest_differences(recipe(), changed) == [field]

    def test_an_added_ingredient_is_detected(self):
        changed = recipe(ingredients=["aubergine", "olive oil", "capers", "anchovy"])
        assert digest_differences(recipe(), changed) == ["ingredients"]

    def test_a_removed_allergen_is_detected(self):
        before = recipe(allergens=["fish"])
        assert digest_differences(before, recipe()) == ["allergens"]

    def test_several_changes_are_all_reported(self):
        changed = recipe(title="Ratatouille", serves=8)
        assert set(digest_differences(recipe(), changed)) == {"title", "serves"}

    def test_no_change_reports_nothing(self):
        assert digest_differences(recipe(), recipe()) == []


class TestScope:
    """What the digest deliberately does not cover."""

    @pytest.mark.parametrize(
        "field",
        ["cuisines", "moods", "flavor_profiles", "food_groups", "annotation_evidence"],
    )
    def test_annotations_are_excluded(self, field):
        """They exist only in Elasticsearch, so no owner can reproduce them.

        Including them would make every owner-vs-index comparison differ by
        construction — the checker would report the whole corpus as drifted.
        """
        assert field not in DIGESTED_FIELDS
        assert content_digest(recipe()) == content_digest(recipe(**{field: ["x"]}))

    def test_status_is_excluded(self):
        """Disable/enable sync through their own bulk path without re-projecting.

        A digest covering status would go stale on every disable and report
        drift where both stores actually agree.
        """
        assert "status" not in DIGESTED_FIELDS
        assert content_digest(recipe(status="active")) == content_digest(
            recipe(status="disabled")
        )

    def test_nutrition_is_excluded(self):
        """Re-profiling changes nutrition without the recipe changing."""
        assert content_digest(recipe()) == content_digest(
            recipe(profiles=[{"nutri_score": "A"}], nutri_score_eu="A")
        )

    def test_version_is_part_of_the_hash(self):
        """So digests from two schemes can never compare equal by luck."""
        assert digest_payload(recipe())["_v"]


class TestReporting:
    def test_summary_names_both_sides(self):
        line = summarise_difference("title", recipe(), recipe(title="Ratatouille"))
        assert "title" in line and "Caponata" in line and "Ratatouille" in line

    def test_summary_truncates_long_values(self):
        long = recipe(instructions="x" * 500)
        line = summarise_difference("instructions", recipe(), long, width=40)
        assert len(line) < 140


class TestReconcileParity:
    """The reconciler must be able to produce every field the digest covers.

    The digest is computed twice from different sources: once by the projection
    (full Cypher, all fields) and once by `reconcile.py` (a deliberately cheaper
    query, so a whole-corpus check is fast enough to run often). If the cheap
    query is missing a field the digest covers, the two disagree for every
    recipe that has it — and the report says the entire corpus has drifted.

    That is not hypothetical. `diet_tags` and `suitable_for` were in the digest
    and absent from the reconciliation query. It surfaced as clean runs only
    because no digests had been stamped yet, so the comparison never ran.
    """

    def test_reconcile_covers_every_digested_field(self):
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "_reconcile_under_test", "scripts/maintenance/reconcile.py"
        )
        module = importlib.util.module_from_spec(spec)
        # Registered before execution: `@dataclass` resolves the defining
        # module out of `sys.modules`, and a module that is not there yet
        # raises rather than defining the class.
        sys.modules[spec.name] = module
        saved = sys.argv
        sys.argv = ["reconcile.py"]
        try:
            spec.loader.exec_module(module)
        finally:
            sys.argv = saved
            sys.modules.pop(spec.name, None)

        # A row with every field the Cypher RETURNs, so the shaper is exercised
        # exactly as it would be against a real recipe.
        row = {
            "recipe_id": "r-1", "title": "T", "description": "D",
            "instructions": "I", "url": "u", "image_url": "i",
            "source": "s", "source_id": "sid", "duration": 10, "serves": 2,
            "cost_category": "low", "expert_recipe": False,
            "ingredients": ["a"], "allergens": ["milk"], "tags": ["x"],
            "diet_tags": ["vegan"], "suitable_for": ["vegan"],
        }
        produced = module.owner_document(row)

        missing = [f for f in DIGESTED_FIELDS if f not in produced]
        assert not missing, (
            f"reconcile.owner_document does not produce {missing} — the digest "
            "covers them, so every recipe carrying one would report as drifted"
        )

    def test_the_query_selects_every_field_the_shaper_reads(self):
        """A field shaped from a key the Cypher never returns is always empty."""
        import re

        source = open("scripts/maintenance/reconcile.py").read()
        query = re.search(r'OWNER_QUERY = """(.*?)"""', source, re.S).group(1)

        for field in DIGESTED_FIELDS:
            if field == "recipe_id":
                continue  # aliased via coalesce
            assert field in query, f"{field} is digested but never selected"
