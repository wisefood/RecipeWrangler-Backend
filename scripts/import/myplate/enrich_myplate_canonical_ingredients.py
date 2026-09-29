"""Parse and weight normalized MyPlate ingredients for graph import."""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    from tqdm import tqdm
except Exception:
    tqdm = None


REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from recipe_wrangler.tools.parse_recipe_tool import parse_recipe_tool
from recipe_wrangler.tools.ingredient_weight_tool import ingredient_weight_tool_usda


DEFAULT_INPUT = Path("myplate_recipes_clean.json")
DEFAULT_OUTPUT = Path("myplate_recipes_with_canonical_ingredients.json")


def _build_raw_recipe(recipe: dict[str, Any], key: str) -> str:
    title = str(recipe.get("title") or key).strip()
    serves = recipe.get("serves")
    duration = recipe.get("duration")
    ingredients = recipe.get("ingredients") or []
    instructions = recipe.get("instructions") or []

    header_lines = [f"Title: {title}"]
    if serves is not None:
        header_lines.append(f"Servings: {serves}")
    if duration is not None:
        header_lines.append(f"Total Time: {duration} minutes")

    ingredients_block = "\n".join(
        f"- {str(i).strip()}"
        for i in ingredients
        if _is_valid_ingredient_line(i)
    )
    directions_block = "\n".join(
        f"{idx + 1}. {str(step).strip()}"
        for idx, step in enumerate(instructions)
        if str(step).strip()
    )

    return (
        "\n".join(header_lines)
        + "\nIngredients\n"
        + ingredients_block
        + "\nDirections\n"
        + directions_block
    )


_LEAD_QTY_RE = re.compile(
    r"^\s*(?P<qty>\d+(?:\.\d+)?(?:\s+\d+/\d+|/\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_PARENS_RE = re.compile(r"\([^)]*\)")


def _fallback_from_original_ingredient(line: Any) -> tuple[str | None, str | None]:
    if line is None:
        return None, None
    text = str(line).strip()
    if not text:
        return None, None

    text = _PARENS_RE.sub("", text).strip()
    text_no_comma_tail = text.split(",", 1)[0].strip()
    m = _LEAD_QTY_RE.match(text_no_comma_tail)
    if not m:
        # No explicit quantity; keep full core phrase as name.
        return text_no_comma_tail.lower(), None

    qty = m.group("qty").strip()
    rest = (m.group("rest") or "").strip()
    if not rest:
        return None, qty

    # Keep first 1-3 tokens from the remainder to avoid long descriptors.
    tokens = [t for t in re.split(r"\s+", rest) if t]
    if not tokens:
        return None, qty
    core_name = " ".join(tokens[:3]).lower()
    return core_name, qty


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def _to_canonical_ingredients(parsed: dict[str, Any], weight_details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    measurements = parsed.get("measurements") or []
    canonical: list[dict[str, Any]] = []

    for idx, detail in enumerate(weight_details):
        measurement = measurements[idx] if idx < len(measurements) else None
        canonical.append(
            {
                "name": detail.get("name"),
                "measurement": measurement,
                "parsed_quantity": detail.get("parsed_quantity"),
                "parsed_unit": detail.get("parsed_unit"),
                "quantity_inferred": detail.get("quantity_inferred"),
                "unit_inferred": detail.get("unit_inferred"),
                "weight_grams": detail.get("weight_grams"),
                "source": _resolve_source(detail),
                "match": _resolve_match(detail),
                "error": detail.get("error"),
                "live_llm_error": detail.get("live_llm_error"),
            }
        )

    return canonical


def _repair_parsed_from_original(
    parsed: dict[str, Any],
    original_ingredients: list[Any],
) -> dict[str, Any]:
    names = list(parsed.get("ingredient_names") or [])
    measures = list(parsed.get("measurements") or [])
    n = min(len(names), len(measures), len(original_ingredients))
    for i in range(n):
        n_i = str(names[i] or "").strip()
        m_i = str(measures[i] or "").strip()
        o_i = original_ingredients[i]
        fb_name, fb_measure = _fallback_from_original_ingredient(o_i)

        # If parser gave empty measurement, use quantity from original when present.
        if not m_i and fb_measure:
            measures[i] = fb_measure

        # If parser name is empty or generic placeholder, replace with original-derived name.
        if (not n_i or n_i.lower() in {"none", "nan", "null"}) and fb_name:
            names[i] = fb_name

        # If parser gave a clearly unrelated name while also missing measurement,
        # trust the original ingredient phrase.
        if not m_i and fb_name and n_i and fb_name not in n_i.lower():
            names[i] = fb_name

    parsed["ingredient_names"] = names
    parsed["measurements"] = measures
    return parsed


def _iter_items(mapping: dict[str, Any]):
    items = list(mapping.items())
    if tqdm is not None:
        yield from tqdm(items, total=len(items), desc="MyPlate canonical", unit="recipe")
        return

    total = len(items)
    for idx, item in enumerate(items, start=1):
        print(f"[{idx}/{total}] processing")
        yield item


def _is_valid_ingredient_line(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    return s.lower() not in {"none", "nan", "null"}


def _is_transient_llm_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    transient_markers = (
        "503",
        "over capacity",
        "rate limit",
        "429",
        "timed out",
        "timeout",
        "connection error",
        "service unavailable",
    )
    return any(marker in msg for marker in transient_markers)


def _parse_with_retries(
    raw_recipe: str,
    recipe_key: str,
    max_retries: int,
    retry_base_seconds: float,
) -> dict[str, Any]:
    attempt = 0
    while True:
        try:
            return parse_recipe_tool.invoke({"recipe": raw_recipe})
        except Exception as exc:
            attempt += 1
            if attempt > max_retries or not _is_transient_llm_error(exc):
                raise
            sleep_s = retry_base_seconds * (2 ** (attempt - 1))
            print(
                f"[retry] {recipe_key} | parse attempt {attempt}/{max_retries} "
                f"after transient error: {exc} | sleeping {sleep_s:.1f}s"
            )
            time.sleep(sleep_s)


def _resolve_source(detail: dict[str, Any]) -> str | None:
    source = detail.get("source")
    if source:
        return str(source)
    if detail.get("fda_fallback"):
        return "FDA"
    if detail.get("live_llm_fallback") or detail.get("llm_fallback"):
        return "LLM fallback"
    match_type = str(detail.get("match_type") or "").lower()
    if "live_llm" in match_type or "llm" in match_type:
        return "LLM fallback"
    if "fda" in match_type:
        return "FDA"
    return "USDA portion tables"


def _resolve_match(detail: dict[str, Any]) -> str | None:
    match = detail.get("match")
    if match:
        return str(match)
    canonical = str(detail.get("usda_match_canonical") or "").strip()
    if canonical:
        return canonical
    usda_id = str(detail.get("usda_id") or "").strip()
    if usda_id:
        return usda_id
    portion = detail.get("portion_match") or {}
    if isinstance(portion, dict):
        source_food = str(portion.get("source_food_name") or "").strip()
        if source_food:
            return source_food
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Parse and enrich MyPlate recipes with canonical ingredients and weight traces. "
            "Writes output JSON after every recipe."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input MyPlate clean JSON")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output JSON path")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Pass debug=True to ingredient_weight_tool_usda (verbose internal details).",
    )
    parser.add_argument(
        "--reprocess-failed",
        action="store_true",
        help=(
            "Re-run only recipes that already exist in output but contain "
            "canonical_ingredients_error or any ingredient with missing weight/error."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run all recipes even if already enriched in output.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=6,
        help="Max transient retry attempts for parse LLM calls (default: 6).",
    )
    parser.add_argument(
        "--retry-base-seconds",
        type=float,
        default=2.0,
        help="Base seconds for exponential retry backoff (default: 2.0).",
    )
    args = parser.parse_args()

    input_path = args.input if args.input.is_absolute() else Path.cwd() / args.input
    output_path = args.output if args.output.is_absolute() else Path.cwd() / args.output

    with input_path.open("r", encoding="utf-8") as f:
        source_data = json.load(f)

    if not isinstance(source_data, dict):
        raise ValueError("Input must be a JSON object keyed by recipe title.")

    enriched: dict[str, Any] = {}
    if output_path.exists():
        try:
            with output_path.open("r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                enriched = existing
        except Exception:
            enriched = {}

    for key, recipe in _iter_items(source_data):
        if not isinstance(recipe, dict):
            enriched[key] = recipe
            _atomic_write_json(output_path, enriched)
            continue

        # Skip/reprocess behavior for existing output entries.
        if key in enriched and isinstance(enriched[key], dict) and not args.force:
            existing_recipe = enriched[key]
            already_enriched = "canonical_ingredients" in existing_recipe
            if already_enriched:
                if args.reprocess_failed:
                    failed_recipe = bool(existing_recipe.get("canonical_ingredients_error"))
                    existing_ci = existing_recipe.get("canonical_ingredients") or []
                    missing_weight = any(
                        isinstance(ci, dict)
                        and (
                            ci.get("weight_grams") is None
                            or ci.get("error") not in (None, "")
                        )
                        for ci in existing_ci
                    )
                    if not failed_recipe and not missing_weight:
                        continue
                else:
                    continue

        out_recipe = dict(recipe)
        try:
            raw_recipe = _build_raw_recipe(recipe, key)
            parsed = _parse_with_retries(
                raw_recipe=raw_recipe,
                recipe_key=key,
                max_retries=max(0, int(args.max_retries)),
                retry_base_seconds=max(0.1, float(args.retry_base_seconds)),
            )
            parsed = _repair_parsed_from_original(
                parsed=parsed,
                original_ingredients=recipe.get("ingredients") or [],
            )
            weight_result = ingredient_weight_tool_usda.invoke(
                {
                    "ingredient_names": parsed.get("ingredient_names") or [],
                    "measurements": parsed.get("measurements") or [],
                    "return_details": True,
                    "debug": bool(args.debug),
                }
            )
            details = weight_result.get("details") if isinstance(weight_result, dict) else []
            details = details if isinstance(details, list) else []

            canonical_ingredients = _to_canonical_ingredients(parsed, details)
            out_recipe["canonical_ingredients"] = canonical_ingredients

            for ci in canonical_ingredients:
                if ci.get("weight_grams") is None:
                    print(
                        f"[missing-weight] {key} | ingredient={ci.get('name')} "
                        f"| qty={ci.get('parsed_quantity')} unit={ci.get('parsed_unit')} "
                        f"| source={ci.get('source')} | error={ci.get('error')} "
                        f"| live_llm_error={ci.get('live_llm_error')}"
                    )
        except Exception as exc:
            out_recipe["canonical_ingredients"] = []
            out_recipe["canonical_ingredients_error"] = str(exc)
            print(f"[recipe-error] {key} -> {exc}")

        enriched[key] = out_recipe
        _atomic_write_json(output_path, enriched)

    print(f"Done. Wrote {len(enriched)} recipes -> {output_path}")


if __name__ == "__main__":
    main()
