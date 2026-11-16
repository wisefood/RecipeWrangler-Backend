"""What this service reports back to the gateway.

The gateway can see that a search happened and who asked. It cannot see the
things that decide whether the search was any good — that the first pass matched
nothing, that the title requirement had to be demoted, that the extractor cost
1,200 tokens. Those are reported from here, and these tests cover the two parts
that are easy to get quietly wrong: the token shapes different providers use,
and the guarantee that reporting never breaks a request.
"""
from __future__ import annotations

import types

import pytest

from recipe_wrangler.api import activity
from recipe_wrangler.api.identity import Caller


def _generation(message):
    return types.SimpleNamespace(message=message)


def _result(llm_output=None, generations=None):
    return types.SimpleNamespace(
        llm_output=llm_output, generations=generations or []
    )


class TestUsageExtraction:
    """Groq and OpenAI-compatible endpoints name these fields differently, and
    which one is in play depends on `SEARCH_LLM_SOURCE`."""

    def test_openai_style_token_usage(self):
        result = _result(
            llm_output={
                "token_usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                },
                "model_name": "llama-3.1-8b-instant",
            }
        )
        assert activity.usage_from_llm_result(result) == {
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        }

    def test_usage_metadata_on_the_message(self):
        message = types.SimpleNamespace(
            usage_metadata={"input_tokens": 90, "output_tokens": 12, "total_tokens": 102}
        )
        result = _result(generations=[[_generation(message)]])
        assert activity.usage_from_llm_result(result) == {
            "input_tokens": 90,
            "output_tokens": 12,
            "total_tokens": 102,
        }

    def test_response_metadata_fallback(self):
        message = types.SimpleNamespace(
            usage_metadata=None,
            response_metadata={"token_usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )
        result = _result(generations=[[_generation(message)]])
        assert activity.usage_from_llm_result(result) == {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,  # derived, because the provider omitted it
        }

    def test_a_provider_that_reports_nothing_yields_nothing(self):
        assert activity.usage_from_llm_result(_result()) == {}
        assert activity.usage_from_llm_result(_result(llm_output={})) == {}

    def test_a_hostile_result_object_does_not_raise(self):
        class Exploding:
            @property
            def llm_output(self):
                raise RuntimeError("no")

            @property
            def generations(self):
                raise RuntimeError("no")

        assert activity.usage_from_llm_result(Exploding()) == {}

    def test_non_numeric_counts_are_ignored(self):
        result = _result(llm_output={"token_usage": {"prompt_tokens": "lots"}})
        assert activity.usage_from_llm_result(result) == {}


class TestReportingIsInert:
    """Telemetry is off unless configured, and must be silent when it is."""

    def test_reports_are_noops_when_telemetry_is_disabled(self):
        assert activity.wf_telemetry.TELEMETRY.enabled is False
        activity.report_search(
            surface="recipes", raw_query="anything", first_pass=0, final=0
        )
        activity.report_llm_usage(model="m", feature="f")
        activity.report_event("recipe.view", props={"id": "1"})
        assert activity.wf_telemetry.TELEMETRY.stats()["queued"] == 0

    def test_a_broken_telemetry_client_does_not_raise(self, monkeypatch):
        def explode(**kwargs):
            raise RuntimeError("telemetry is broken")

        monkeypatch.setattr(activity.wf_telemetry.TELEMETRY, "search", explode)
        monkeypatch.setattr(activity.wf_telemetry.TELEMETRY, "llm_usage", explode)
        monkeypatch.setattr(activity.wf_telemetry.TELEMETRY, "event", explode)
        activity.report_search(surface="recipes", raw_query="x")
        activity.report_llm_usage(model="m", feature="f")
        activity.report_event("recipe.view")


class TestIdentityMapping:
    def test_the_caller_subject_becomes_the_user(self):
        caller = Caller(sub="sub-1", username="someone", roles=frozenset({"expert"}))
        assert activity._identity(caller) == {"user_id": "sub-1"}

    def test_an_anonymous_caller_reports_no_user(self):
        assert activity._identity(None) == {"user_id": None}
        assert activity._identity(Caller()) == {"user_id": None}


class TestSearchesAreCaptured:
    """Each surface reports under its own name, so agent traffic never lands in
    a report about what people are searching for."""

    @pytest.fixture()
    def captured(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            activity.wf_telemetry.TELEMETRY, "search", lambda **kw: seen.append(kw)
        )
        return seen

    def test_surface_and_counts_are_passed_through(self, captured):
        activity.report_search(
            surface="recipes",
            raw_query="Vegan Desserts",
            # A query-side facet. Profile-derived fields like `diet_tags` are
            # dropped by the allowlist — see TestProfileDataNeverReachesAnalytics.
            filters={"cuisines": ["thai"]},
            first_pass=0,
            final=7,
            relaxed=True,
            lexical_fallback=False,
            latency_ms=310,
            caller=Caller(sub="sub-9"),
        )
        assert captured == [
            {
                "surface": "recipes",
                "raw_query": "Vegan Desserts",
                "filters": {"cuisines": ["thai"]},
                "result_count_first_pass": 0,
                "result_count_final": 7,
                "relaxed": True,
                "lexical_fallback": False,
                "latency_ms": 310,
                "app": "recipewrangler",
                "user_id": "sub-9",
            }
        ]

    def test_a_blank_query_is_reported_as_absent_not_empty(self, captured):
        """param_search has no free text at all; the facets are the query."""
        activity.report_search(surface="param", raw_query="", filters={"cuisines": ["it"]})
        assert captured[0]["raw_query"] is None

    def test_elapsed_time_is_derived_from_a_start_marker(self, captured):
        import time

        activity.report_search(
            surface="recipes", raw_query="x", started=time.perf_counter() - 0.25
        )
        assert 200 <= captured[0]["latency_ms"] <= 400


class TestProfileDataNeverReachesAnalytics:
    """The search payload carries the caller's own profile next to their query.

    `exclude_allergens` is the member's allergies, `boost_tags` their dietary
    groups, `boost_ingredients` the foods they like. Reported alongside a user
    id, that is health data in an analytics table — the thing the platform's
    own tracing policy explicitly forbids.
    """

    def test_medical_and_profile_fields_are_dropped(self):
        kept = activity.reportable_filters({
            "exclude_allergens": ["peanut", "gluten"],
            "boost_tags": ["vegan"],
            "boost_ingredients": ["tofu"],
            "preferred_ingredients": ["tofu"],
            "allergens": ["milk"],
            "diet": ["halal"],
            "rank_query": "something the user typed",
            "cuisines": ["italian"],
            "nutri_scores": ["a"],
        })
        assert kept == {"cuisines": ["italian"], "nutri_scores": ["a"]}

    def test_unknown_keys_are_dropped_by_default(self):
        """A blocklist would report the next field somebody adds; this must not."""
        assert activity.reportable_filters({"newly_added_profile_field": ["x"]}) == {}

    def test_empty_values_are_not_reported(self):
        assert activity.reportable_filters({"cuisines": [], "moods": None, "sources": ""}) == {}

    def test_the_allowlist_reaches_the_wire(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            activity.wf_telemetry.TELEMETRY, "search", lambda **kw: seen.append(kw)
        )
        activity.report_search(
            surface="recipes",
            raw_query="dinner",
            filters={"exclude_allergens": ["peanut"], "cuisines": ["thai"]},
        )
        assert seen[0]["filters"] == {"cuisines": ["thai"]}


class TestUsageExtractionAcrossProviders:
    """The same numbers, in six different places.

    Which one is in play depends on the provider and on whether the call was
    streamed, and the split is the part that matters: output tokens cost
    several times input, so a row carrying only a combined total cannot be
    turned back into money afterwards.
    """

    def test_anthropic_style_usage_block(self):
        result = _result(
            llm_output={"usage": {"input_tokens": 200, "output_tokens": 45}}
        )
        assert activity.usage_from_llm_result(result) == {
            "input_tokens": 200,
            "output_tokens": 45,
            "total_tokens": 245,
        }

    def test_gemini_style_token_counts(self):
        message = types.SimpleNamespace(
            usage_metadata=None,
            response_metadata={
                "usage_metadata": {
                    "prompt_token_count": 64,
                    "candidates_token_count": 16,
                    "total_token_count": 80,
                }
            },
        )
        result = _result(generations=[[_generation(message)]])
        assert activity.usage_from_llm_result(result) == {
            "input_tokens": 64,
            "output_tokens": 16,
            "total_tokens": 80,
        }

    def test_ollama_style_counts_at_the_top_of_response_metadata(self):
        """Ollama nests nothing — the counts sit beside `model` and `done`."""
        message = types.SimpleNamespace(
            usage_metadata=None,
            response_metadata={
                "model": "llama3",
                "done": True,
                "prompt_eval_count": 31,
                "eval_count": 9,
            },
        )
        result = _result(generations=[[_generation(message)]])
        assert activity.usage_from_llm_result(result) == {
            "input_tokens": 31,
            "output_tokens": 9,
            "total_tokens": 40,
        }

    def test_streamed_call_reports_from_generation_info(self):
        """A streamed run has no `llm_output` at all."""
        generation = types.SimpleNamespace(
            message=None,
            generation_info={
                "finish_reason": "stop",
                "token_usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )
        result = _result(llm_output=None, generations=[[generation]])
        assert activity.usage_from_llm_result(result) == {
            "input_tokens": 12,
            "output_tokens": 4,
            "total_tokens": 16,
        }

    def test_a_bare_total_does_not_hide_the_split(self):
        """The aggregate arrives first; the split is further down and must win.

        A provider that reports only `total_tokens` up top used to be the whole
        answer, and the row landed with no input/output at all.
        """
        message = types.SimpleNamespace(
            usage_metadata={"input_tokens": 70, "output_tokens": 30}
        )
        result = _result(
            llm_output={"total_tokens": 100},
            generations=[[_generation(message)]],
        )
        assert activity.usage_from_llm_result(result) == {
            "input_tokens": 70,
            "output_tokens": 30,
            "total_tokens": 100,
        }


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _llm_result(prompt: int = 1, completion: int = 1):
    return _result(
        llm_output={
            "token_usage": {"prompt_tokens": prompt, "completion_tokens": completion},
            "model_name": "a-model",
        }
    )


class TestUsageCallbackLatency:
    """One handler instance serves every concurrent call.

    The handler is built once and attached to a pooled client, so a start
    timestamp stored on the handler is whichever call started last — every
    latency would be somebody else's. The timings are keyed by LangChain's
    `run_id` instead, and these tests are what keeps that true.
    """

    @pytest.fixture()
    def wf(self):
        return activity.wf_telemetry

    @pytest.fixture()
    def rows(self, monkeypatch, wf):
        seen = []
        monkeypatch.setattr(wf.TELEMETRY, "llm_usage", lambda **kw: seen.append(kw))
        return seen

    @pytest.fixture()
    def clock(self, monkeypatch, wf):
        clock = _Clock()
        monkeypatch.setattr(wf, "time", clock)
        return clock

    def test_interleaved_calls_each_get_their_own_latency(self, wf, rows, clock):
        handler = wf.usage_callback("extraction", provider="groq", app="recipewrangler")

        handler.on_llm_start({}, ["a"], run_id="run-a")
        clock.advance(1.0)
        handler.on_llm_start({}, ["b"], run_id="run-b")
        clock.advance(2.0)
        handler.on_llm_end(_llm_result(), run_id="run-b")
        clock.advance(7.0)
        handler.on_llm_end(_llm_result(), run_id="run-a")

        assert [row["latency_ms"] for row in rows] == [2000.0, 10000.0]

    def test_a_chat_model_start_also_starts_the_clock(self, wf, rows, clock):
        """Chat models fire `on_chat_model_start`; only completion models fire
        `on_llm_start`, and every LLM in this platform is a chat model."""
        handler = wf.usage_callback("extraction")
        handler.on_chat_model_start({}, [[]], run_id="run-c")
        clock.advance(0.5)
        handler.on_llm_end(_llm_result(), run_id="run-c")
        assert rows[0]["latency_ms"] == 500.0

    def test_an_unstarted_run_reports_no_latency_rather_than_a_wrong_one(
        self, wf, rows, clock
    ):
        handler = wf.usage_callback("extraction")
        handler.on_llm_end(_llm_result(), run_id="never-started")
        assert rows[0]["latency_ms"] is None

    def test_the_run_map_is_emptied_by_both_endings(self, wf, rows, clock):
        handler = wf.usage_callback("extraction")
        handler.on_llm_start({}, ["a"], run_id="ok")
        handler.on_llm_start({}, ["b"], run_id="boom")
        handler.on_llm_end(_llm_result(), run_id="ok")
        handler.on_llm_error(RuntimeError("provider timeout"), run_id="boom")
        assert handler._runs == {}

    def test_runs_that_never_end_cannot_grow_without_bound(self, wf, rows, clock):
        """A cancelled request leaves no ending callback behind at all."""
        handler = wf.usage_callback("extraction")
        for index in range(wf._MAX_INFLIGHT_LLM_RUNS + 50):
            handler.on_llm_start({}, ["x"], run_id=f"abandoned-{index}")
        assert len(handler._runs) <= wf._MAX_INFLIGHT_LLM_RUNS

    def test_a_failed_call_reports_nothing(self, wf, rows, clock):
        handler = wf.usage_callback("extraction")
        handler.on_llm_start({}, ["a"], run_id="boom")
        handler.on_llm_error(RuntimeError("nope"), run_id="boom")
        assert rows == []

    def test_the_provider_reaches_the_row(self, wf, rows, clock):
        handler = wf.usage_callback("extraction", provider="openrouter")
        handler.on_llm_start({}, ["a"], run_id="r")
        handler.on_llm_end(_llm_result(prompt=11, completion=3), run_id="r")
        assert rows[0]["provider"] == "openrouter"
        assert rows[0]["input_tokens"] == 11
        assert rows[0]["output_tokens"] == 3


class TestUsageCallbackTraceId:
    """A usage row that cannot be pointed at its trace is a number with no story.

    The id is read from the OpenTelemetry context Langfuse makes current for
    the duration of a run — never from the handler's `last_trace_id`, which
    under concurrency is whichever call started most recently and would link a
    row to somebody else's trace.
    """

    @pytest.fixture()
    def wf(self):
        return activity.wf_telemetry

    @pytest.fixture()
    def rows(self, monkeypatch, wf):
        seen = []
        monkeypatch.setattr(wf.TELEMETRY, "llm_usage", lambda **kw: seen.append(kw))
        return seen

    @staticmethod
    def _fake_langfuse(trace_ids):
        """A stand-in SDK whose current trace id is whatever the test says."""
        client = types.SimpleNamespace(
            get_current_trace_id=lambda: trace_ids.pop(0) if trace_ids else None
        )
        return types.SimpleNamespace(get_client=lambda: client)

    def test_the_trace_id_reaches_the_row(self, monkeypatch, wf, rows):
        import sys

        monkeypatch.setitem(
            sys.modules, "langfuse", self._fake_langfuse(["trace-1", "trace-1"])
        )
        handler = wf.usage_callback("extraction")
        handler.on_llm_start({}, ["a"], run_id="r")
        handler.on_llm_end(_llm_result(), run_id="r")
        assert rows[0]["trace_id"] == "trace-1"

    def test_an_id_seen_only_at_the_start_survives_to_the_end(
        self, monkeypatch, wf, rows
    ):
        """Langfuse drops its span in `on_llm_end`, and the two handlers on a
        client run in no guaranteed order — so the id can be gone by the time
        this one is asked."""
        import sys

        monkeypatch.setitem(sys.modules, "langfuse", self._fake_langfuse(["trace-2"]))
        handler = wf.usage_callback("extraction")
        handler.on_llm_start({}, ["a"], run_id="r")
        handler.on_llm_end(_llm_result(), run_id="r")
        assert rows[0]["trace_id"] == "trace-2"

    def test_an_id_seen_only_at_the_end_is_still_reported(self, monkeypatch, wf, rows):
        """The other ordering: this handler ran before Langfuse opened the span."""
        import sys

        monkeypatch.setitem(
            sys.modules, "langfuse", self._fake_langfuse([None, "trace-3"])
        )
        handler = wf.usage_callback("extraction")
        handler.on_llm_start({}, ["a"], run_id="r")
        handler.on_llm_end(_llm_result(), run_id="r")
        assert rows[0]["trace_id"] == "trace-3"

    def test_no_langfuse_means_no_trace_id_and_no_sdk_client(self, monkeypatch, wf, rows):
        """Reaching for the module here would construct a client, and telemetry
        must never be the thing that switches tracing on."""
        import sys

        monkeypatch.delitem(sys.modules, "langfuse", raising=False)
        handler = wf.usage_callback("extraction")
        handler.on_llm_start({}, ["a"], run_id="r")
        handler.on_llm_end(_llm_result(), run_id="r")
        assert rows[0]["trace_id"] is None

    def test_a_broken_sdk_does_not_break_the_call(self, monkeypatch, wf, rows):
        import sys

        def explode():
            raise RuntimeError("langfuse is unhappy")

        monkeypatch.setitem(
            sys.modules,
            "langfuse",
            types.SimpleNamespace(get_client=explode),
        )
        handler = wf.usage_callback("extraction")
        handler.on_llm_start({}, ["a"], run_id="r")
        handler.on_llm_end(_llm_result(), run_id="r")
        assert rows[0]["trace_id"] is None
