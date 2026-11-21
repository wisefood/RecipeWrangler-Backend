"""Bounded temporal pilot; --actions tests source phases and compact Llama editing."""
import hashlib
import json
import os
from pathlib import Path
import sys

import openai

from recipe_wrangler.services.adaptation.models import ComparisonSnapshot
from recipe_wrangler.services.adaptation.comparison_store import load_active_snapshot
from recipe_wrangler.services.adaptation.time_understanding import (
    PROMPT, PROMPT_VERSION, ReviewedOverlap, extract_time_understanding, rewrite_reviewed_overlap, text_hash,
)
from recipe_wrangler.utils.env_loader import load_runtime_env

BASE = Path("analysis/time-adaptation-2026-11-04")
LARGER = "--larger" in sys.argv
FRAGMENTS = "--fragments" in sys.argv
ACTIONS = "--actions" in sys.argv or FRAGMENTS
LLAMA = "--llama" in sys.argv or ACTIONS
if LARGER and LLAMA:
    raise ValueError("choose_one_model")
MODEL = "meta-llama/llama-3.3-70b-instruct" if LLAMA else ("qwen/qwen3-235b-a22b-2507" if LARGER else "qwen/qwen3-32b")
MAX_REQUESTS = 4 if FRAGMENTS else (5 if LARGER or LLAMA else 6)
OUT = BASE / ("openrouter-time-fragments-v4" if FRAGMENTS else ("openrouter-time-actions-v3" if ACTIONS else ("openrouter-time-llama70b-v2" if LLAMA else ("openrouter-time-235b-v1" if LARGER else "openrouter-time-v1"))))


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False))


def freeze():
    OUT.mkdir()
    if LARGER or LLAMA:
        previous = BASE / "openrouter-time-v1"
        for name in ("cases.json", "snapshot.json", "review.json", "editor-input.json"):
            (OUT / name).write_bytes((previous / name).read_bytes())
        save(OUT / "freeze.json", {
            "cases_sha256": hashlib.sha256((OUT / "cases.json").read_bytes()).hexdigest(),
            "fixture_sha256": {name: hashlib.sha256((OUT / name).read_bytes()).hexdigest()
                               for name in ("snapshot.json", "review.json", "editor-input.json")},
            "model": MODEL, "max_requests": MAX_REQUESTS, "budget_usd": .05,
            "prompt_version": PROMPT_VERSION, "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
            "service_sha256": hashlib.sha256(Path("src/recipe_wrangler/services/adaptation/time_understanding.py").read_bytes()).hexdigest(),
            "reservation_per_request_usd": .006,
            "scope": "Same four real extraction cases and independent reviewed editor fixture; no DB access. Prompt version pinned; no reasoning flag for non-thinking endpoints. Development checks, not independent gold accuracy.",
        })
        print("Frozen cached comparison inputs; zero paid requests")
        return
    specs = {
        "f06295e6-52d9-59be-a3db-7832218f7ba2": {"id": "focaccia", "state": "not_applicable", "preparation": "not_applicable"},
        "ac987374-d09a-50d2-9190-2e25a4c430d0": {"id": "separate-rice", "state": "ambiguous", "preparation": "separate", "rice_minutes": None},
        "56e708e5-89a7-5db3-8e2b-50518b9d4d3f": {"id": "ambiguous-state", "state": "ambiguous", "preparation": "unknown", "rice_minutes": None},
        "2825097554": {"id": "already-scheduled", "state": "ambiguous", "preparation": "separate", "rice_minutes": None, "parallel": True},
    }
    cases = []
    for line in (BASE / "coverage-final/recipes.jsonl").open():
        recipe = json.loads(line)
        if recipe["recipe_id"] in specs:
            cases.append({"recipe": recipe, "expected": specs[recipe["recipe_id"]]})
    cases.sort(key=lambda case: list(specs).index(case["recipe"]["recipe_id"]))
    cases.append({"recipe": {"recipe_id": "synthetic-attention-control", "title": "Two continuous tasks",
        "originals": ["sauce", "eggs"], "instructions": ["Stir the sauce continuously for 5 minutes.",
        "Whisk the eggs continuously for 5 minutes. Use one cook; do not leave either task unattended."]},
        "expected": {"id": "attention-control", "state": "not_applicable", "preparation": "not_applicable"}})
    assert len(cases) == 5
    save(OUT / "cases.json", cases)
    save(OUT / "freeze.json", {"cases_sha256": hashlib.sha256((OUT / "cases.json").read_bytes()).hexdigest(),
        "max_requests": 6, "budget_usd": .05, "reservation_per_request_usd": .006,
        "scope": "Five extraction calls including smoke, at most one rewrite. Four real snapshot recipes plus one synthetic control. Expectations are development checks, not gold independent accuracy."})
    recipe = cases[0]["recipe"]
    snapshot = load_active_snapshot(recipe["recipe_id"], "eu", allow_input_estimates=True)
    save(OUT / "snapshot.json", snapshot.model_dump(mode="json"))
    save(OUT / "review.json", ReviewedOverlap(context_hash=text_hash(recipe), anchor_step_id="step:3", work_step_id="step:4",
        passive_anchor_quote="bake for about 30 minutes until cooked through and golden brown.",
        anchor_resources={"oven"}, work_resources={"cook", "hob", "pan"},
        independent_of_anchor_output=True, start_only_no_completion_deadline_claim=True,
        reviewed_by="agent_source_review_before_model_calls; requires human review before use").model_dump(mode="json"))
    print("Frozen five cases, reviewed one start-only overlap, captured one read-only profile; zero paid requests")


def main():
    if sys.argv[1] == "freeze":
        freeze()
        return
    frozen = json.loads((OUT / "freeze.json").read_text())
    assert hashlib.sha256((OUT / "cases.json").read_bytes()).hexdigest() == frozen["cases_sha256"]
    if "prompt_sha256" not in frozen or frozen["prompt_sha256"] != hashlib.sha256(PROMPT.encode()).hexdigest():
        raise ValueError("frozen_prompt_missing_or_changed; historical experiments cannot be resumed with a different prompt")
    if "service_sha256" in frozen and frozen["service_sha256"] != hashlib.sha256(Path("src/recipe_wrangler/services/adaptation/time_understanding.py").read_bytes()).hexdigest():
        raise ValueError("frozen_service_changed")
    for name, digest in frozen.get("fixture_sha256", {}).items():
        assert hashlib.sha256((OUT / name).read_bytes()).hexdigest() == digest
    load_runtime_env()
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OpenRouter key is missing; no request sent")
    os.environ.update(ADAPT_LLM_SOURCE="openrouter", ADAPT_LLM_MODEL=MODEL,
        ADAPT_LLM_BASE_URL="https://openrouter.ai/api/v1", ADAPT_LLM_TIMEOUT="60")
    # No credential is printed or persisted. The runner always uses the user's OpenRouter key.
    os.environ["ADAPT_LLM_API_KEY"] = key
    ledger_path = OUT / "ledger.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else []
    previous_ledger = json.loads((BASE / "openrouter-time-actions-v3/ledger.json").read_text()) if FRAGMENTS else []
    if FRAGMENTS and len(previous_ledger) != 1:
        raise ValueError("previous_phase_budget_changed")
    request_case = None

    def request(payload, response_model, config, *, max_tokens, system_prompt):
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps(
            {**payload, "response_schema": response_model.model_json_schema()}, ensure_ascii=False)}]
        if len(json.dumps(messages).encode()) > 24000 or max_tokens > 2200:
            raise ValueError("request_exceeds_budgeted_size")
        if len(ledger) >= MAX_REQUESTS or (len(ledger) + len(previous_ledger) + 1) * .006 > .05:
            raise ValueError("paid_call_budget_exhausted")
        if request_case is not None and any(entry.get("case_id") == request_case for entry in ledger):
            raise ValueError("case_already_attempted_no_automatic_retry")
        entry = {"attempt": len(ledger) + 1, "status": "reserved", "max_cost_reservation_usd": .006,
            "case_id": request_case, "model": MODEL, "prompt_sha256": hashlib.sha256(json.dumps(messages).encode()).hexdigest()}
        ledger.append(entry)
        save(OUT / f'messages-{entry["attempt"]}.json', messages)
        save(ledger_path, ledger)  # An interrupted/in-flight call consumes its reservation; never retry it.
        try:
            extra = {"provider": {"only": ["deepinfra"], "allow_fallbacks": False,
                "require_parameters": True,
                "max_price": {"prompt": .12 if LLAMA else (.10 if LARGER else .14), "completion": .35 if LLAMA else (.60 if LARGER else .57)}}}
            if not (LARGER or LLAMA):
                extra["reasoning"] = {"enabled": False}
            with openai.OpenAI(base_url=config["base_url"], api_key=key, timeout=60, max_retries=0) as client:
                response = client.chat.completions.create(model=MODEL, messages=messages,
                    temperature=0., max_tokens=max_tokens, response_format={"type": "json_object"},
                    extra_body=extra)
            entry.update(status="received", generation_id=response.id, model=response.model,
                usage=response.usage.model_dump(mode="json") if response.usage else None,
                finish_reason=response.choices[0].finish_reason)
            raw = response.choices[0].message.content or ""
            save(OUT / f'response-{entry["attempt"]}.json', {"raw": raw, "metadata": entry})
            if response.choices[0].finish_reason != "stop":
                raise ValueError("incomplete_response")
            return raw
        except Exception as error:
            entry.update(status="failed", error_type=type(error).__name__)
            raise
        finally:
            save(ledger_path, ledger)

    cases = json.loads((OUT / "cases.json").read_text())
    results_path = OUT / "results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else []
    chosen = cases[:1] if sys.argv[1] == "smoke" else cases
    # The initial call lacked explicit step IDs. Preserve it; revision two uses
    # four real cases, keeping the attention control local and all attempts ≤6.
    chosen = chosen[:4]
    if FRAGMENTS and sys.argv[1] != "smoke":
        chosen = [cases[i] for i in (0, 1, 3)]
    for case in chosen:
        label = case["expected"]["id"]
        if any(item["id"] == label and item.get("input_version") == "explicit_step_ids_v2" for item in results):
            continue
        row = {"id": label, "recipe_id": case["recipe"]["recipe_id"], "status": "unavailable", "input_version": "explicit_step_ids_v2"}
        request_case = label
        row["reason"] = "request_not_completed; interrupted calls must not be retried"
        results.append(row)
        save(results_path, results)
        try:
            understanding = extract_time_understanding(case["recipe"], request=request)
            row.pop("reason")
            row.update(status="validated_structure", understanding=understanding.model_dump(mode="json"),
                checks={"existing_parallelism": "parallel" not in case["expected"] or understanding.existing_parallelism == case["expected"]["parallel"]})
        except Exception as error:
            row.update(error_type=type(error).__name__, reason=str(error) if isinstance(error, ValueError) else "hosted_call_unavailable")
        save(results_path, results)
        print(label, row["status"], row.get("checks", {}), flush=True)
    if sys.argv[1] != "smoke" and not (OUT / "draft-result.json").exists():
        first = next(item for item in results if item["id"] == "focaccia" and item.get("input_version") == "explicit_step_ids_v2")
        from recipe_wrangler.services.adaptation.time_understanding import TimeUnderstanding
        snapshot = ComparisonSnapshot.model_validate(json.loads((OUT / "snapshot.json").read_text()))
        review = ReviewedOverlap.model_validate(json.loads((OUT / "review.json").read_text()))
        if LARGER or LLAMA:
            fixture = json.loads((OUT / "editor-input.json").read_text())
            editor_input = TimeUnderstanding.model_validate(fixture["understanding"])
            input_basis = fixture["basis"]
        elif first["status"] == "validated_structure":
            editor_input = TimeUnderstanding.model_validate(first["understanding"])
            input_basis = "validated_model_structure; source review still required"
        else:
            # An independent source-reviewed editor test, never an automatic
            # acceptance/repair of rejected model extraction or a new paid retry.
            parents = [[], ["step:0"], ["step:1"], ["step:2"], [], ["step:3", "step:4"]]
            editor_input = TimeUnderstanding(steps=[dict(step_id=f"step:{i}", evidence_quote=text,
                attention="active" if i == 0 else "mixed", depends_on=parents[i], duration_quote=None,
                duration_minutes=None) for i, text in enumerate(cases[0]["recipe"]["instructions"])],
                existing_parallelism=False,
                uncertainties=["Source-reviewed editor fixture after model extraction rejection; no complete time estimate supplied."])
            input_basis = "independent_source_reviewed_editor_fixture; rejected extraction not used"
        if not (LARGER or LLAMA):
            save(OUT / "editor-input.json", {"basis": input_basis, "understanding": editor_input.model_dump(mode="json")})
        request_case = "reviewed-rewrite"
        try:
            draft = rewrite_reviewed_overlap(snapshot, cases[0]["recipe"], editor_input, review)
            save(OUT / "draft-result.json", draft.model_dump(mode="json"))
            print("rewrite", draft.status, draft.reasons, flush=True)
        except Exception as error:
            save(OUT / "draft-result.json", {"status": "unavailable", "error_type": type(error).__name__})
    print(json.dumps({"requests_reserved": len(ledger), "reserved_upper_usd": len(ledger) * .006,
        "reported_cost_usd": sum((entry.get("usage") or {}).get("cost", 0) or 0 for entry in ledger)}, indent=2))


if __name__ == "__main__":
    main()
