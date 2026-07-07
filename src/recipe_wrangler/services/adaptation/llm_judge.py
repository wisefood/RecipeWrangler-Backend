"""LLM judge for ingredient-substitution sanity-checking.

Sits *on top of* the deterministic candidate set produced by ``service.py``.
The LLM cannot invent substitutes — it can only filter and rerank from the
candidate list it is given, so hallucinated names never reach the simulation
math. If the LLM call fails (network, malformed JSON, timeout, unknown names),
the caller falls back to the deterministic ranking — never 422-ing a request
that the deterministic pipeline could have answered.

Provider-agnostic via the OpenAI-compatible Chat Completions API. Works
unchanged against:
  - vLLM (any model served on an OpenAI-compatible endpoint)
  - Groq (api.groq.com/openai/v1)
  - Any other OpenAI-compatible inference server.

Configuration (read at call time, not module import — env reloads cleanly):

    ADAPT_LLM_SOURCE   "vllm" | "groq"          default: vllm
    ADAPT_LLM_BASE_URL OpenAI-compatible URL    default: http://localhost:8005/v1
                                                (or https://api.groq.com/openai/v1 for groq)
    ADAPT_LLM_MODEL    Model ID                 default: qwen3-32b (vllm) or llama-3.1-8b-instant (groq)
    ADAPT_LLM_API_KEY  Bearer token             default: none (vllm) or GROQ_API_KEY env (groq)
    ADAPT_LLM_TIMEOUT  HTTP timeout seconds     default: 30
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import openai


logger = logging.getLogger(__name__)


_DEFAULTS: dict[str, dict[str, str]] = {
    "vllm": {
        "base_url": "http://localhost:8005/v1",
        "model": "qwen3-32b",
        "api_key": "none",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.1-8b-instant",
        "api_key_env": "GROQ_API_KEY",
    },
}


def _llm_config() -> dict[str, Any]:
    source = os.getenv("ADAPT_LLM_SOURCE", "vllm").strip().lower()
    if source not in _DEFAULTS:
        source = "vllm"
    defaults = _DEFAULTS[source]
    base_url = os.getenv("ADAPT_LLM_BASE_URL", defaults["base_url"])
    model = os.getenv("ADAPT_LLM_MODEL", defaults["model"])
    if "api_key_env" in defaults:
        api_key = os.getenv("ADAPT_LLM_API_KEY") or os.getenv(defaults["api_key_env"]) or "none"
    else:
        api_key = os.getenv("ADAPT_LLM_API_KEY", defaults["api_key"])
    timeout = float(os.getenv("ADAPT_LLM_TIMEOUT", "90"))
    return {"source": source, "base_url": base_url, "model": model, "api_key": api_key, "timeout": timeout}


def _build_prompt(
    recipe_title: str,
    recipe_ingredients: list[dict[str, Any]],
    target_nutrient_label: str | None,
    target_points: int | None,
    offending_ingredient: str,
    offending_pct: float,
    candidates: list[dict[str, Any]],
    mode: str = "nutrition",
) -> str:
    ing_lines = "\n".join(
        f"  - {(d.get('ingredient') or d.get('name') or '').strip()} ({float(d.get('weight_g') or 0):.0f}g)"
        for d in recipe_ingredients
    )

    if mode == "sustainability":
        cand_lines = []
        for c in candidates:
            allergen_str = (
                f" (introduces allergens: {', '.join(c.get('new_allergens') or [])})"
                if c.get("introduces_allergen") else ""
            )
            cand_lines.append(
                f"  {c['rank']}. {c['substitute_name']}  "
                f"[source: {c['source']}, "
                f"CF {c['original_cf_kg_co2e_per_kg']:.2f} → {c['candidate_cf_kg_co2e_per_kg']:.2f} kg CO2e/kg, "
                f"saves {c['co2e_reduction_per_serving_kg'] * 1000:.0f} g CO2e/serving "
                f"({c['co2e_reduction_pct'] * 100:.0f}% lower CF)]"
                f"{allergen_str}"
            )
        target_block = (
            f"OFFENDING INGREDIENT: '{offending_ingredient}' "
            f"({offending_pct:.0f}% of recipe's total CO2e)"
        )
        candidate_block_intro = "CANDIDATE SUBSTITUTES (already filtered to materially reduce CO2e):"
    else:
        cand_lines = []
        for c in candidates:
            deltas = c.get("nutrient_delta_per_serving") or {}
            delta_str = ", ".join(
                f"{k}: {v:+.1f}" for k, v in deltas.items() if abs(v) > 0.05
            )
            allergen_str = (
                f" (introduces allergens: {', '.join(c.get('new_allergens') or [])})"
                if c.get("introduces_allergen") else ""
            )
            cand_lines.append(
                f"  {c['rank']}. {c['substitute_name']}  "
                f"[source: {c['source']}, simulated grade: {c['simulated_nutri_score']}, "
                f"target points saved: {c['nutri_score_points_saved']}]"
                f"{allergen_str}\n     per-serving deltas: {delta_str or '(negligible)'}"
            )
        target_block = (
            f"OFFENDING INGREDIENT: '{offending_ingredient}' "
            f"({offending_pct:.0f}% of recipe's {target_nutrient_label}, "
            f"scoring {target_points} Nutri-Score points)"
        )
        candidate_block_intro = "CANDIDATE SUBSTITUTES (already filtered to improve Nutri-Score):"

    return (
        "You are a chef advising a typical home cook on a single recipe.\n\n"
        "Decision rule for ranking — apply STRICTLY in this order:\n"
        "  1. Would this swap pass the supermarket-aisle test? — would a typical home cook in this "
        "dish's culinary tradition actually buy this substitute and use it here? If the answer is "
        "'no, weird/unusual/specialty' then this candidate is NOT a good fit, regardless of its "
        "nutrition or CO2e numbers.\n"
        "  2. Is it a recognisable everyday equivalent? — pork mince and turkey breast mince are "
        "everyday burger swaps. Offal (hearts, liver, kidneys), exotic meats, and specialty "
        "ingredients are NOT everyday equivalents, even when culturally valid somewhere.\n"
        "  3. Metric (Nutri-Score points saved / CO2e reduction) — used ONLY to break ties between "
        "candidates that already pass (1) and (2). A bigger metric win does NOT outweigh a worse "
        "culinary fit.\n\n"
        "Concrete examples of how to rank:\n"
        "  - American beef burger, candidates {turkey heart, chicken heart, ground pork}: rank "
        "    #1 GROUND PORK (everyday burger meat, the canonical swap), then turkey heart and "
        "    chicken heart far behind — even though hearts save more CO2e. Hearts are organ meat; "
        "    nobody puts them in an American burger.\n"
        "  - Peanut butter fudge frosting, candidates {valencia oranges, peanut powder, peanut "
        "    flour}: rank #1 PEANUT POWDER (preserves the peanut identity). Reject valencia "
        "    oranges (fruit doesn't belong in fudge frosting).\n"
        "  - Butter sauce, candidates {seasoning salt, sour cream, low-fat butter}: rank #1 "
        "    LOW-FAT BUTTER. Reject seasoning salt (salt is not a butter substitute).\n\n"
        "Reject — move to the 'rejected' list — any candidate that:\n"
        "  - is offal/organ meat when the dish calls for muscle meat,\n"
        "  - changes the dish's core identity (e.g. fruit replacing nut paste, sauce replacing fat),\n"
        "  - is a specialty/exotic ingredient when an everyday equivalent exists in the list,\n"
        "  - would require a different cooking technique to work.\n\n"
        f"RECIPE: {recipe_title}\n"
        f"INGREDIENTS:\n{ing_lines}\n\n"
        f"{target_block}\n\n"
        f"{candidate_block_intro}\n{chr(10).join(cand_lines)}\n\n"
        "Return ONLY valid JSON in this exact schema, no prose, no markdown fences:\n"
        "{\n"
        '  "rejected": [{"substitute_name": "<exact name from list>", "reason": "<one short sentence — say WHY a home cook would not pick this>"}],\n'
        '  "ranked":   [{"substitute_name": "<exact name from list>", "rank": 1, '
        '"justification": "<one or two sentences — cite the recipe and say why this is the canonical home-cook swap>"}]\n'
        "}\n\n"
        "Rules:\n"
        "- Only use names that appear in the candidate list above. Do not invent ingredients.\n"
        "- Include EVERY candidate in either rejected or ranked, no duplicates.\n"
        "- #1 rank goes to the swap a typical home cook would actually make, NOT the one with the "
        "biggest metric improvement.\n"
        "- If two candidates are equally good culinarily, use the metric to break the tie."
    )


def _safe_json_extract(text: str) -> dict[str, Any] | None:
    """Pull the first balanced JSON object out of an LLM response."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[-1] if text.count("```") >= 2 else text.lstrip("`")
        text = text.lstrip("json").strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
    return None


def rerank_with_llm(
    recipe_title: str,
    recipe_ingredients: list[dict[str, Any]],
    target_nutrient_label: str | None,
    target_points: int | None,
    offending_ingredient: str,
    offending_pct: float,
    candidates: list[dict[str, Any]],
    mode: str = "nutrition",
) -> dict[str, Any] | None:
    """Filter + rerank ``candidates`` using an OpenAI-compatible LLM.

    Returns a dict ``{"ranked": [...candidate dicts annotated with `llm_justification`],
    "rejected": [{"substitute_name", "reason"}], "model": str, "source": str}``
    on success. Returns ``None`` on any failure — caller should fall back to
    the deterministic ranking. This function never raises.
    """

    if not candidates:
        return None

    cfg = _llm_config()
    try:
        client = openai.OpenAI(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            timeout=cfg["timeout"],
        )
        prompt = _build_prompt(
            recipe_title, recipe_ingredients, target_nutrient_label,
            target_points, offending_ingredient, offending_pct, candidates, mode,
        )
        # Qwen3 models emit <think> tokens by default, which eat the token budget
        # before any JSON is produced. vLLM honours `chat_template_kwargs.enable_thinking=false`
        # when passed via `extra_body`. Harmless for non-Qwen models that ignore the flag.
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": "You are a precise culinary assistant. Respond only with valid JSON matching the requested schema."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=1024,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning("LLM judge call failed (%s); falling back to deterministic ranking.", e)
        return None

    parsed = _safe_json_extract(raw)
    if not parsed or "ranked" not in parsed:
        logger.warning("LLM judge returned unparseable JSON; falling back. Raw head: %s", raw[:200])
        return None

    by_name = {c["substitute_name"].lower(): c for c in candidates}

    ranked_out: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in parsed.get("ranked") or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("substitute_name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen_names or key not in by_name:
            continue
        seen_names.add(key)
        original = dict(by_name[key])
        original["llm_justification"] = (entry.get("justification") or "").strip() or None
        ranked_out.append(original)

    rejected_out: list[dict[str, Any]] = []
    for entry in parsed.get("rejected") or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("substitute_name") or "").strip()
        if not name or name.lower() in seen_names:
            continue
        rejected_out.append({
            "substitute_name": name,
            "reason": (entry.get("reason") or "").strip() or None,
        })

    if not ranked_out:
        logger.info("LLM judge rejected all %d candidates; falling back to deterministic ranking.", len(candidates))
        return None

    for i, entry in enumerate(ranked_out, start=1):
        entry["rank"] = i

    return {
        "ranked": ranked_out,
        "rejected": rejected_out,
        "model": cfg["model"],
        "source": cfg["source"],
    }
