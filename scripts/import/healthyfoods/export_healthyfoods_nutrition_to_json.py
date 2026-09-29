"""Scrape published HealthyFoods nutrition panels into reference JSON."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup


DEFAULT_INPUT_URLS = Path("data/HealthyFoods/recipe_urls.json")
DEFAULT_OUTPUT = Path("data/HealthyFoods/HealthyFood_recipes_nutrition.json")

TARGET_FIELDS = [
    "Calories",
    "Kilojoules",
    "Protein",
    "Total fat",
    "Saturated fat",
    "Carbohydrates",
    "Sugar",
    "Dietary fibre",
    "Sodium",
    "Calcium",
    "Iron",
]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _norm_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def _parse_json_ld_recipe(soup: BeautifulSoup) -> Optional[dict[str, Any]]:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        candidates: list[dict[str, Any]] = []
        if isinstance(data, dict):
            if data.get("@type") == "Recipe":
                candidates.append(data)
            graph = data.get("@graph")
            if isinstance(graph, list):
                candidates.extend(
                    x for x in graph if isinstance(x, dict) and x.get("@type") == "Recipe"
                )
        elif isinstance(data, list):
            candidates.extend(x for x in data if isinstance(x, dict) and x.get("@type") == "Recipe")

        if candidates:
            return candidates[0]
    return None


def _field_aliases() -> dict[str, str]:
    # normalized label -> canonical output label
    return {
        "calories": "Calories",
        "kilojoules": "Kilojoules",
        "protein": "Protein",
        "total fat": "Total fat",
        "saturated fat": "Saturated fat",
        "carbohydrates": "Carbohydrates",
        "sugar": "Sugar",
        "dietary fibre": "Dietary fibre",
        "dietary fiber": "Dietary fibre",
        "sodium": "Sodium",
        "calcium": "Calcium",
        "iron": "Iron",
    }


def _extract_nutrition_from_html(soup: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    aliases = _field_aliases()

    for p in soup.select(".nutritional_box li.nutrition p"):
        label_el = p.select_one("span.big-nut")
        if not label_el:
            continue

        label_text = _clean(label_el.get_text(" ", strip=True)).rstrip(":")
        canonical = aliases.get(_norm_label(label_text))
        if not canonical:
            continue

        full_text = _clean(p.get_text(" ", strip=True))
        value_text = _clean(full_text.replace(label_el.get_text(" ", strip=True), "", 1))
        if value_text and canonical not in out:
            out[canonical] = value_text

    return out


def _extract_nutrition_from_jsonld(recipe_ld: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    nutrition = recipe_ld.get("nutrition")
    if not isinstance(nutrition, dict):
        return out

    key_map = {
        "calories": "Calories",
        "caloriescontent": "Calories",
        "proteincontent": "Protein",
        "fatcontent": "Total fat",
        "saturatedfatcontent": "Saturated fat",
        "carbohydratecontent": "Carbohydrates",
        "sugarcontent": "Sugar",
        "fibercontent": "Dietary fibre",
        "fibrecontent": "Dietary fibre",
        "sodiumcontent": "Sodium",
        "calciumcontent": "Calcium",
        "ironcontent": "Iron",
    }

    for k, v in nutrition.items():
        if not isinstance(v, str):
            continue
        mapped = key_map.get(_norm_label(k).replace(" ", ""))
        if mapped and mapped not in out:
            out[mapped] = _clean(v)

    return out


def _extract_title(soup: BeautifulSoup, recipe_ld: Optional[dict[str, Any]], fallback: str) -> str:
    if recipe_ld and isinstance(recipe_ld.get("name"), str) and recipe_ld["name"].strip():
        return _clean(recipe_ld["name"])
    h1 = soup.select_one("h1")
    if h1:
        text = _clean(h1.get_text(" ", strip=True))
        if text:
            return text
    if soup.title:
        text = _clean(soup.title.get_text(" ", strip=True))
        if text:
            return text
    return fallback


def _empty_nutrition_payload() -> dict[str, Optional[str]]:
    return {field: None for field in TARGET_FIELDS}


def scrape_recipe_nutrition(url: str, session: requests.Session, timeout: int = 30) -> dict[str, Any]:
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    recipe_ld = _parse_json_ld_recipe(soup)

    title = _extract_title(soup, recipe_ld, fallback=url)
    nutrition = _extract_nutrition_from_html(soup)
    if not nutrition and recipe_ld:
        nutrition = _extract_nutrition_from_jsonld(recipe_ld)

    fields = _empty_nutrition_payload()
    for k, v in nutrition.items():
        if k in fields:
            fields[k] = v

    return {
        "url": url,
        "title": title,
        "nutrition_per_serve": fields,
    }


def load_urls(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array in {path}")
    out: list[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape HealthyFood recipe title + nutrition fields into JSON."
    )
    parser.add_argument("--input-urls", type=Path, default=DEFAULT_INPUT_URLS, help="Path to recipe_urls.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output JSON path")
    parser.add_argument("--limit", type=int, default=0, help="Optional max recipes to process (0 = all)")
    parser.add_argument("--sleep", type=float, default=0.15, help="Delay between requests in seconds")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP request timeout (seconds)")
    args = parser.parse_args()

    urls = load_urls(args.input_urls)
    if args.limit and args.limit > 0:
        urls = urls[: args.limit]

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    recipes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    total = len(urls)
    for idx, url in enumerate(urls, start=1):
        try:
            recipes.append(scrape_recipe_nutrition(url, session=session, timeout=args.timeout))
        except Exception as exc:
            failures.append({"url": url, "error": str(exc)})
        if args.sleep > 0:
            time.sleep(args.sleep)
        if idx % 50 == 0 or idx == total:
            print(f"[{idx}/{total}] scraped={len(recipes)} failed={len(failures)}")

    output_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "https://www.healthyfood.com/healthy-recipes/",
        "target_fields": TARGET_FIELDS,
        "count": len(recipes),
        "failure_count": len(failures),
        "recipes": recipes,
        "failures": failures,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(recipes)} recipes to {args.output}")
    if failures:
        print(f"Failures: {len(failures)} (included in output JSON)")


if __name__ == "__main__":
    main()
