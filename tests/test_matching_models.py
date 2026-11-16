from dataclasses import FrozenInstanceError

import pytest

from recipe_wrangler.matching import (
    EquivalencePrediction,
    FoodCandidate,
    FoodFacets,
    FoodMention,
    MatchDecision,
    MatchProvenance,
    RecipeContext,
    RetrievalEvidence,
)
from recipe_wrangler.matching.evaluation import (
    BenchmarkItem,
    BenchmarkPrediction,
    BenchmarkTarget,
    evaluate_predictions,
    read_benchmark_jsonl,
    summarize_benchmark,
    write_benchmark_jsonl,
)


def test_food_facets_preserve_cooking_and_preservation_separately() -> None:
    facets = FoodFacets(
        identity="chickpea",
        preparation_state="cooked",
        processes=frozenset({"boiled"}),
        preservation_methods=frozenset({"canned"}),
        physical_form="whole",
    )

    assert facets.preparation_state == "cooked"
    assert facets.processes == frozenset({"boiled"})
    assert facets.preservation_methods == frozenset({"canned"})


def test_food_facets_leave_unspecified_properties_unknown() -> None:
    facets = FoodFacets(identity="chickpea")

    assert facets.preparation_state is None
    assert facets.sodium_level is None
    assert facets.processes == frozenset()
    assert facets.preservation_methods == frozenset()


def test_food_facets_are_immutable() -> None:
    facets = FoodFacets(identity="chickpea")

    with pytest.raises(FrozenInstanceError):
        facets.identity = "lentil"  # type: ignore[misc]


def test_food_facets_reject_unknown_controlled_values() -> None:
    with pytest.raises(ValueError, match="preparation_state"):
        FoodFacets(preparation_state="steamed")  # type: ignore[arg-type]


def test_domain_models_capture_a_database_independent_match() -> None:
    mention = FoodMention(
        text="canned chickpeas, drained",
        identity="chickpea",
        facets=FoodFacets(
            identity="chickpea",
            preparation_state="cooked",
            preservation_methods=frozenset({"canned"}),
        ),
        context=RecipeContext(title="Chickpea salad", amount_g=240.0),
        target_region="irish",
    )
    candidate = FoodCandidate(
        record_id="cofid:17-000",
        source_id="irish",
        label="Chickpeas, canned in brine, drained",
        facets=mention.facets,
        retrieval=RetrievalEvidence(rank=1, dense_score=0.91),
    )
    decision = MatchDecision(
        status="exact",
        candidate=candidate,
        confidence=0.96,
        reasons=("identity_exact", "preparation_compatible"),
        provenance=MatchProvenance(matcher_version="wisefood_v1"),
    )

    assert decision.candidate is candidate
    assert mention.context is not None
    assert mention.context.amount_g == 240.0


def test_match_decision_requires_candidate_consistency() -> None:
    provenance = MatchProvenance(matcher_version="wisefood_v1")

    with pytest.raises(ValueError, match="requires a candidate"):
        MatchDecision(
            status="exact",
            candidate=None,
            confidence=None,
            reasons=(),
            provenance=provenance,
        )

    candidate = FoodCandidate(
        record_id="row-1",
        source_id="eu",
        label="Chickpeas, cooked",
        facets=FoodFacets(identity="chickpea", preparation_state="cooked"),
        retrieval=RetrievalEvidence(rank=1),
    )
    with pytest.raises(ValueError, match="abstention"):
        MatchDecision(
            status="abstain",
            candidate=candidate,
            confidence=None,
            reasons=("ambiguous_preparation_state",),
            provenance=provenance,
        )


def test_equivalence_prediction_keeps_incompatibility_distinct_from_abstention() -> None:
    prediction = EquivalencePrediction(
        status="incompatible",
        confidence=0.98,
        reasons=("preparation_state_conflict",),
    )

    assert prediction.status == "incompatible"
    assert prediction.reasons == ("preparation_state_conflict",)


def test_benchmark_item_allows_multiple_valid_source_records() -> None:
    item = BenchmarkItem(
        item_id="slovenia-chickpea-001",
        mention=FoodMention(
            text="canned chickpeas, drained",
            identity="chickpea",
            facets=FoodFacets(
                identity="chickpea",
                preparation_state="cooked",
                preservation_methods=frozenset({"canned"}),
            ),
            target_region="slovenian",
        ),
        acceptable_targets=(
            BenchmarkTarget("slovenian", "opkp:123", "exact", preference=1),
            BenchmarkTarget("eu", "cofid:17-000", "acceptable_equivalent", preference=2),
        ),
        abstain_is_valid=False,
        risk_categories=frozenset({"preparation_state", "source_selection"}),
        rationale="Both rows are nutritionally defensible; regional is preferred.",
    )

    assert len(item.acceptable_targets) == 2


def test_benchmark_item_keeps_no_match_cases_explicit() -> None:
    item = BenchmarkItem(
        item_id="eu-equipment-001",
        mention=FoodMention(
            text="paper straws",
            identity="paper straws",
            facets=FoodFacets(),
        ),
        acceptable_targets=(),
        abstain_is_valid=True,
        risk_categories=frozenset({"non_food"}),
        rationale="Equipment is not a food-composition record.",
    )

    assert item.abstain_is_valid is True


def test_benchmark_item_rejects_ambiguous_annotation_shape() -> None:
    mention = FoodMention(text="beans", identity="beans", facets=FoodFacets())

    with pytest.raises(ValueError, match="cannot have acceptable targets"):
        BenchmarkItem(
            item_id="invalid",
            mention=mention,
            acceptable_targets=(BenchmarkTarget("eu", "row-1", "exact"),),
            abstain_is_valid=True,
            risk_categories=frozenset({"underspecified"}),
            rationale="Invalid test fixture.",
        )


def test_benchmark_jsonl_round_trips_context_and_facets(tmp_path) -> None:
    item = BenchmarkItem(
        item_id="irish-chickpea-001",
        mention=FoodMention(
            text="canned chickpeas, drained",
            identity="chickpea",
            facets=FoodFacets(
                identity="chickpea",
                preparation_state="cooked",
                preservation_methods=frozenset({"canned"}),
            ),
            context=RecipeContext(title="Chickpea salad", amount_g=240.0),
            target_region="irish",
        ),
        acceptable_targets=(BenchmarkTarget("irish", "cofid:17-000", "exact"),),
        abstain_is_valid=False,
        risk_categories=frozenset({"preparation_state"}),
        rationale="Reviewed composition row.",
    )
    path = tmp_path / "benchmark.jsonl"

    write_benchmark_jsonl(path, [item])

    assert read_benchmark_jsonl(path) == (item,)


def test_benchmark_summary_reports_annotation_coverage() -> None:
    matched = BenchmarkItem(
        item_id="matched",
        mention=FoodMention(text="chickpeas", identity="chickpea", facets=FoodFacets()),
        acceptable_targets=(
            BenchmarkTarget("irish", "row-1", "exact"),
            BenchmarkTarget("eu", "row-2", "acceptable_equivalent"),
        ),
        abstain_is_valid=False,
        risk_categories=frozenset({"source_selection"}),
        rationale="Two valid sources.",
    )
    abstain = BenchmarkItem(
        item_id="abstain",
        mention=FoodMention(text="paper straws", identity="paper straws", facets=FoodFacets()),
        acceptable_targets=(),
        abstain_is_valid=True,
        risk_categories=frozenset({"non_food"}),
        rationale="Not food.",
    )

    summary = summarize_benchmark([matched, abstain])

    assert summary.item_count == 2
    assert summary.abstain_count == 1
    assert summary.source_target_counts == {"eu": 1, "irish": 1}
    assert summary.risk_category_counts == {"non_food": 1, "source_selection": 1}


def test_benchmark_jsonl_rejects_non_boolean_abstention_flag(tmp_path) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text(
        '{"item_id":"invalid","mention":{"text":"beans","identity":"beans",'
        '"facets":{"processes":[],"preservation_methods":[]},'
        '"context":null,"target_region":null},'
        '"acceptable_targets":[],"abstain_is_valid":"true",'
        '"risk_categories":["underspecified"],"rationale":"Invalid."}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="abstain_is_valid"):
        read_benchmark_jsonl(path)


def test_evaluation_scores_multiple_targets_and_abstention() -> None:
    items = (
        BenchmarkItem(
            item_id="beans",
            mention=FoodMention(text="beans", identity="beans", facets=FoodFacets()),
            acceptable_targets=(
                BenchmarkTarget("regional", "1", "exact"),
                BenchmarkTarget("eu", "2", "acceptable_equivalent"),
            ),
            abstain_is_valid=False,
            risk_categories=frozenset({"source_selection"}),
            rationale="Either reviewed record is acceptable.",
        ),
        BenchmarkItem(
            item_id="unknown",
            mention=FoodMention(text="unknown", identity="unknown", facets=FoodFacets()),
            acceptable_targets=(),
            abstain_is_valid=True,
            risk_categories=frozenset({"underspecified"}),
            rationale="No composition record is defensible.",
        ),
    )

    report = evaluate_predictions(
        items,
        (
            BenchmarkPrediction("beans", "eu", "2"),
            BenchmarkPrediction("unknown"),
        ),
    )

    assert report.accuracy == 1.0
    assert report.accepted_target_count == 1
    assert report.exact_target_count == 0
    assert report.correct_abstention_count == 1
    assert report.abstention_f1 == 1.0
    assert report.risk_category_accuracy == {
        "source_selection": 1.0,
        "underspecified": 1.0,
    }


def test_evaluation_requires_one_prediction_per_item() -> None:
    item = BenchmarkItem(
        item_id="beans",
        mention=FoodMention(text="beans", identity="beans", facets=FoodFacets()),
        acceptable_targets=(BenchmarkTarget("eu", "1", "exact"),),
        abstain_is_valid=False,
        risk_categories=frozenset(),
        rationale="Reviewed record.",
    )

    with pytest.raises(ValueError, match="missing=.*beans"):
        evaluate_predictions([item], [])


def test_annotation_facets_and_recipe_usage_survive_benchmark_roundtrip(tmp_path):
    facets = FoodFacets(
        identity="pineapple", size="large", size_target="piece", thickness="thick",
        variety="named variety", ripeness="ripe", cooking_extent="soft_boiled",
        texture="smooth", particle_size="coarse", drainage_status="drained",
        reconstitution_state="reconstituted", meat_fat_description="lean and fat",
        fat_content_description="75-90% fat", fortification="fortified",
        seed_status="removed", fat_level="reduced", sugar_level="no_added_sugar",
        grain_refinement="wholegrain", gluten_free=True,
    )
    # A serialization fixture, not a claim these properties describe one real food.
    context = RecipeContext(
        ingredient_role="garnish", optional=False, quantity_qualifier="to taste",
        usage_note="divided",
    )
    item = BenchmarkItem(
        item_id="annotation-roundtrip", mention=FoodMention("annotation fixture", "fixture", facets, context),
        acceptable_targets=(), abstain_is_valid=True, risk_categories=frozenset(),
        rationale="Schema preservation fixture.",
    )
    path = tmp_path / "annotations.jsonl"
    write_benchmark_jsonl(path, [item])
    assert read_benchmark_jsonl(path) == (item,)


def test_size_has_an_explicit_target_and_no_default():
    facets = FoodFacets(identity="egg")
    assert facets.size is None
    assert facets.size_target is None
    assert RecipeContext().optional is None
    with pytest.raises(ValueError, match="size_target"):
        FoodFacets(size="large", size_target="cooking_method")
