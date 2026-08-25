"""The commit path: a created recipe must end up stored, profiled and annotated.

A recipe that is stored but not annotated carries no cuisine, mood, flavour or
food group — so it is unreachable by every discovery filter in the UI and
invisible to the meal planner's cuisine preferences. It exists, it is findable
by typing its exact title, and it is in practice lost. "Created" has to mean
more than "written to Neo4j".

The hard part is that the three steps fail differently, and the wrong response
to any of them loses data:

- projection fails  → the recipe is invisible to *everyone*, so this must be
  recorded loudly and be retryable;
- annotation fails  → the recipe is usable but undiscoverable, so this must not
  fail the write, and must not be forgotten either;
- annotation is skipped because the recipe already has facets → not a failure
  at all, and must not be logged as one.

These tests pin those responses. The model call itself is stubbed throughout —
what is under test is the contract around it, not Groq.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.catalog import writer


@pytest.fixture
def stub(monkeypatch):
    """A commit path with every external store replaced.

    Returns the recorder so a test can assert what was written where.
    """

    class Recorder:
        def __init__(self):
            self.pending: list[dict] = []
            self.patched: list[tuple[str, dict]] = []
            self.projected: list[str] = []
            self.document = {
                "recipe_id": "r-1",
                "title": "Caponata",
                "ingredients": [{"name": "aubergine"}, {"name": "capers"}],
                "tags": ["vegetarian"],
                "source": "user",
            }
            self.facets = {"cuisines": ["italian"], "moods": ["comfort"]}
            self.project_error: Exception | None = None
            self.annotate_error: Exception | None = None

    rec = Recorder()

    def fake_mark_pending(recipe_id, *, projection, annotation):
        rec.pending.append(
            {"recipe_id": recipe_id, "projection": projection, "annotation": annotation}
        )

    monkeypatch.setattr(writer, "_mark_pending", fake_mark_pending)

    import recipe_wrangler.catalog.projection as projection

    def fake_project(recipe_id, *, refresh="wait_for"):
        if rec.project_error:
            raise rec.project_error
        rec.projected.append(recipe_id)
        return dict(rec.document)

    monkeypatch.setattr(projection, "project", fake_project)

    import recipe_wrangler.catalog.annotation as annotation

    def fake_suggest(**kwargs):
        if rec.annotate_error:
            raise rec.annotate_error
        requested = set(kwargs.get("facets") or ())
        return {k: v for k, v in rec.facets.items() if k in requested}, 0.9

    monkeypatch.setattr(annotation, "suggest", fake_suggest)
    monkeypatch.setattr(
        annotation, "evidence_for", lambda values, **kw: [{"field": f} for f in values]
    )
    monkeypatch.setattr(annotation, "derive_food_groups", lambda doc: ([], []))

    import recipe_wrangler.catalog.entities as entities

    class FakeEntity:
        def patch(self, recipe_id, changes, **kwargs):
            rec.patched.append((recipe_id, changes))

    monkeypatch.setattr(entities, "recipe_entity", lambda: FakeEntity())
    return rec


class TestHappyPath:
    def test_a_commit_projects_and_annotates(self, stub):
        result = writer.commit("r-1")

        assert result.projected
        assert result.annotated
        assert result.complete
        assert stub.projected == ["r-1"]

    def test_the_annotation_reaches_the_document(self, stub):
        writer.commit("r-1")

        assert len(stub.patched) == 1
        recipe_id, changes = stub.patched[0]
        assert recipe_id == "r-1"
        assert changes["cuisines"] == ["italian"]
        assert changes["moods"] == ["comfort"]

    def test_provenance_is_recorded_alongside_the_values(self, stub):
        """A facet with no evidence cannot be audited or reverted."""
        writer.commit("r-1")

        _, changes = stub.patched[0]
        assert changes["annotation_evidence"]

    def test_a_complete_commit_leaves_nothing_pending(self, stub):
        writer.commit("r-1")

        assert stub.pending[-1]["projection"] is False
        assert stub.pending[-1]["annotation"] is False


class TestProjectionFailure:
    """The recipe is invisible to every reader. Loud, and retryable."""

    def test_projection_failure_does_not_raise(self, stub):
        """Neo4j and Postgres already committed.

        Raising would report failure for a write that partly succeeded, and
        invite a caller to retry the whole creation — producing a duplicate.
        """
        from recipe_wrangler.catalog.projection import ProjectionError

        stub.project_error = ProjectionError("mapping rejected the document")
        result = writer.commit("r-1")

        assert not result.projected
        assert not result.complete
        assert any("projection" in e for e in result.errors)

    def test_projection_failure_is_marked_pending(self, stub):
        from recipe_wrangler.catalog.projection import ProjectionError

        stub.project_error = ProjectionError("boom")
        writer.commit("r-1")

        assert stub.pending[-1]["projection"] is True

    def test_annotation_is_not_attempted_without_a_document(self, stub):
        """Annotation patches a document that must already exist."""
        from recipe_wrangler.catalog.projection import ProjectionError

        stub.project_error = ProjectionError("boom")
        writer.commit("r-1")

        assert stub.patched == []


class TestAnnotationFailure:
    """Usable but undiscoverable. Must not fail the write, must not be lost."""

    def test_a_model_outage_does_not_fail_the_commit(self, stub):
        stub.annotate_error = RuntimeError("groq unavailable")
        result = writer.commit("r-1")

        assert result.projected
        assert not result.annotated
        assert any("annotation" in e for e in result.errors)

    def test_a_model_outage_is_marked_pending(self, stub):
        """Otherwise the gap is invisible and never filled."""
        stub.annotate_error = RuntimeError("groq unavailable")
        writer.commit("r-1")

        assert stub.pending[-1]["annotation"] is True

    def test_an_empty_model_response_is_not_treated_as_annotated(self, stub):
        """Abstention is a gap, not a classification."""
        stub.facets = {}
        result = writer.commit("r-1")

        assert result.projected
        assert not result.annotated
        assert result.annotation_skipped_reason
        assert stub.patched == []

    def test_a_recipe_without_a_title_is_not_classified(self, stub):
        """Noise in a closed vocabulary is worse than a gap.

        A wrong cuisine is indistinguishable from a right one once indexed.
        """
        stub.document = dict(stub.document, title="")
        result = writer.commit("r-1")

        assert not result.annotated
        assert stub.patched == []


class TestReAnnotation:
    def test_an_already_annotated_recipe_is_not_reclassified(self, stub):
        """Every patch would otherwise cost a model call and overwrite a human.

        This is what makes it safe for the edit path to share the commit path.
        """
        stub.document = dict(
            stub.document,
            course_types=["main-dish"],
            cuisines=["greek"],
            moods=["comfort"],
            flavor_profiles=["umami"],
        )
        result = writer.commit("r-1")

        assert result.projected
        assert stub.patched == []
        assert result.annotation_skipped_reason == "already annotated"

    def test_an_already_annotated_recipe_is_not_marked_pending(self, stub):
        """It is not a gap, so it must not appear in a backfill queue."""
        stub.document = dict(
            stub.document,
            course_types=["main-dish"],
            cuisines=["greek"],
            moods=["comfort"],
            flavor_profiles=["umami"],
        )
        writer.commit("r-1")

        assert stub.pending[-1]["annotation"] is False

    def test_only_missing_model_facets_are_filled(self, stub):
        stub.document = dict(stub.document, cuisines=["greek"])
        writer.commit("r-1")

        _, changes = stub.patched[0]
        assert changes["moods"] == ["comfort"]
        assert "cuisines" not in changes

    def test_overwrite_forces_reclassification(self, stub):
        stub.document = dict(stub.document, cuisines=["greek"])
        result = writer.commit("r-1", overwrite_annotation=True)

        assert result.annotated
        assert stub.patched


class TestOptOut:
    def test_annotation_can_be_skipped_deliberately(self, stub):
        """Bulk paths that annotate separately should not pay per-recipe calls."""
        result = writer.commit("r-1", annotate_recipe=False)

        assert result.projected
        assert not result.annotated
        assert stub.patched == []
        assert result.annotation_skipped_reason == "not requested"
