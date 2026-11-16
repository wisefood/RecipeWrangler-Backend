"""Five fresh hosted time-extraction cases; later workflow checks remain offline."""
import hashlib
import json
import os
from pathlib import Path
import sys

import openai

from recipe_wrangler.services.adaptation.time_understanding import PROMPT, PROMPT_VERSION, extract_time_understanding
from recipe_wrangler.utils.env_loader import load_runtime_env

BASE = Path("analysis/time-adaptation-2026-11-04")
OUT = BASE / "time-workflow-v1"
MODEL = "meta-llama/llama-3.3-70b-instruct"


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, ensure_ascii=False))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze():
    import re
    OUT.mkdir(exist_ok=True)
    if (OUT / "ledger.json").exists():
        raise ValueError("cannot_refreeze_after_a_paid_attempt")
    excluded = set()
    def visit(value):
        if isinstance(value, dict):
            if value.get("recipe_id") is not None:
                excluded.add(str(value["recipe_id"]))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for path in list(BASE.glob("*/cases.json")) + list(BASE.glob("*/frozen-cases.json")):
        if path.parent != OUT:
            visit(json.loads(path.read_text()))
    groups = {key: [] for key in ("oven_component", "existing_parallel", "active_only", "waiting", "general")}
    source = BASE / "coverage-final/recipes.jsonl"
    scanned = 0
    with source.open() as stream:
        for scanned, line in enumerate(stream, 1):
            if scanned > 15000:
                raise ValueError("source_scan_exceeds_budget")
            row = json.loads(line)
            steps = row.get("instructions") or []
            if (str(row.get("recipe_id")) in excluded or not 1 <= len(steps) <= 6
                    or not all(isinstance(step, str) and step.strip() for step in steps)
                    or not row.get("originals") or sum(map(len, steps)) > 1600):
                continue
            text = " ".join(steps)
            oven = bool(re.search(r"\b(?:bake|roast|oven)\b", text, re.I))
            parallel = bool(re.search(r"\b(?:while|meanwhile)\b|before the end", text, re.I))
            flags = dict(oven_component=oven and not parallel and bool(re.search(r"\b(?:sauce|dressing|salad)\b", text, re.I)),
                existing_parallel=parallel, active_only=not oven and bool(re.search(r"\b(?:stir|whisk|fry)\b", text, re.I)),
                waiting=bool(re.search(r"\b(?:chill|refrigerate|soak|rise)\b", text, re.I)),
                general=bool(re.search(r"\d+\s*(?:min|hour)", text, re.I)))
            for group, qualifies in flags.items():
                if qualifies:
                    score = hashlib.sha256(f"20261104:{group}:{row['recipe_id']}".encode()).hexdigest()
                    groups[group].append((score, row))
                    groups[group].sort(key=lambda item: item[0])
                    del groups[group][6:]
    chosen, seen = [], set()
    for group, options in groups.items():
        recipe = next(row for _, row in options if str(row["recipe_id"]) not in seen)
        seen.add(str(recipe["recipe_id"]))
        chosen.append(dict(group=group, recipe=recipe))
    assert len(chosen) == len(seen) == 5 and not (seen & excluded)
    save("cases.json", chosen)
    save("freeze.json", dict(source=str(source), source_sha256=sha(source), source_rows=scanned,
        cases_sha256=sha(OUT / "cases.json"), prior_case_ids_excluded=len(excluded),
        selection="Seeded hash minima within five lexical strata, before any response; exposed corpus, new hosted temporal cases, not independent gold accuracy.",
        prompt_version=PROMPT_VERSION, prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
        service_sha256=sha(Path("src/recipe_wrangler/services/adaptation/time_understanding.py")),
        model=MODEL, max_calls=5, max_output_tokens=2200, budget_usd=.05, reservation_usd=.006))
    for case in chosen:
        print(case["group"], case["recipe"]["title"], case["recipe"]["recipe_id"])


def run(smoke=False):
    frozen = json.loads((OUT / "freeze.json").read_text())
    assert frozen["cases_sha256"] == sha(OUT / "cases.json")
    assert frozen["prompt_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()
    assert frozen["service_sha256"] == sha(Path("src/recipe_wrangler/services/adaptation/time_understanding.py"))
    load_runtime_env()
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("OpenRouter key missing; no request sent")
    os.environ.update(ADAPT_LLM_SOURCE="openrouter", ADAPT_LLM_MODEL=MODEL,
        ADAPT_LLM_BASE_URL="https://openrouter.ai/api/v1", ADAPT_LLM_TIMEOUT="60", ADAPT_LLM_API_KEY=key)
    ledger = json.loads((OUT / "ledger.json").read_text()) if (OUT / "ledger.json").exists() else []
    rows = json.loads((OUT / "extractions.json").read_text()) if (OUT / "extractions.json").exists() else []
    label = None
    def request(payload, response_model, config, *, max_tokens, system_prompt):
        messages = [dict(role="system", content=system_prompt), dict(role="user", content=json.dumps(
            {**payload, "response_schema": response_model.model_json_schema()}, ensure_ascii=False))]
        if len(json.dumps(messages).encode()) > 24000 or max_tokens > 2200:
            raise ValueError("request_exceeds_frozen_size_budget")
        if len(ledger) >= 5 or (len(ledger) + 1) * .006 > .05 or any(e["case"] == label for e in ledger):
            raise ValueError("request_budget_or_no_retry_guard")
        entry = dict(case=label, attempt=len(ledger) + 1, status="reserved", reservation_usd=.006)
        ledger.append(entry)
        save("ledger.json", ledger)
        save(f"messages-{entry['attempt']}.json", messages)
        try:
            with openai.OpenAI(base_url=config["base_url"], api_key=key, timeout=60, max_retries=0) as client:
                response = client.chat.completions.create(model=MODEL, messages=messages,
                    temperature=0., max_tokens=max_tokens, response_format={"type": "json_object"},
                    extra_body={"provider": {"only": ["deepinfra"], "allow_fallbacks": False,
                        "require_parameters": True, "max_price": {"prompt": .12, "completion": .35}}})
            entry.update(status="received", model=response.model, generation_id=response.id,
                finish_reason=response.choices[0].finish_reason, usage=response.usage.model_dump(mode="json") if response.usage else None)
            raw = response.choices[0].message.content or ""
            save(f"response-{entry['attempt']}.json", dict(raw=raw, metadata=entry))
            if entry["finish_reason"] != "stop":
                raise ValueError("incomplete_response")
            return raw
        except Exception as error:
            entry.update(status="failed", error_type=type(error).__name__)
            raise
        finally:
            save("ledger.json", ledger)
    cases = json.loads((OUT / "cases.json").read_text())
    for case in cases[:1] if smoke else cases:
        label = case["group"]
        if any(row["group"] == label for row in rows):
            continue
        row = dict(group=label, recipe_id=case["recipe"]["recipe_id"], status="unavailable",
            reason="attempt_started_no_completed_result; never retry an interrupted case")
        rows.append(row)
        save("extractions.json", rows)
        try:
            understanding = extract_time_understanding(case["recipe"], request=request)
            row.pop("reason")
            row.update(status="validated_structure", understanding=understanding.model_dump(mode="json"))
        except Exception as error:
            row.update(error_type=type(error).__name__, reason=str(error) if isinstance(error, ValueError) else "hosted_request_unavailable")
        save("extractions.json", rows)
        print(label, row["status"], row.get("reason", ""), flush=True)
    print("requests", len(ledger), "known reported cost", sum((e.get("usage") or {}).get("cost", 0) or 0 for e in ledger))


if __name__ == "__main__":
    if sys.argv[1] == "freeze":
        freeze()
    else:
        run(smoke=sys.argv[1] == "smoke")
