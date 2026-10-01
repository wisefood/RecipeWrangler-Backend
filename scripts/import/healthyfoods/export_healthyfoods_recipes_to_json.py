"""Scrape discovered HealthyFoods recipe pages into raw recipe JSON."""

import json
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup


INPUT_URLS = "data/HealthyFoods/recipe_urls.json"
OUT_RECIPES = "data/HealthyFoods/HealthyFood_recipes.json"

SITE = "https://www.healthyfood.com"
RECIPE_PREFIX = "https://www.healthyfood.com/healthy-recipes/"


# ---------------------------
# Small utils
# ---------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _first_text(el) -> Optional[str]:
    if not el:
        return None
    t = el.get_text(" ", strip=True)
    return t or None


# ---------------------------
# JSON-LD recipe
# ---------------------------

def _parse_json_ld_recipe(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Healthy Food Guide has emitted literal newlines and other control
            # characters inside JSON strings. They are valid page text but
            # invalid strict JSON; accepting only that relaxed JSON case keeps
            # us on the structured Recipe data instead of silently dropping it.
            try:
                data = json.loads(raw, strict=False)
            except json.JSONDecodeError:
                continue

        candidates: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            if data.get("@type") == "Recipe":
                candidates.append(data)
            if "@graph" in data and isinstance(data["@graph"], list):
                candidates.extend(
                    [x for x in data["@graph"] if isinstance(x, dict) and x.get("@type") == "Recipe"]
                )
        elif isinstance(data, list):
            candidates.extend([x for x in data if isinstance(x, dict) and x.get("@type") == "Recipe"])

        if candidates:
            return candidates[0]
    return None


def _parse_recipe_instructions(value: Any) -> List[str]:
    """Flatten Schema.org Recipe/HowTo instruction shapes in page order."""

    if isinstance(value, str):
        text = _clean(value)
        return [text] if text else []
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            out.extend(_parse_recipe_instructions(item))
        return out
    if not isinstance(value, dict):
        return []

    text = value.get("text")
    if isinstance(text, str) and _clean(text):
        return [_clean(text)]
    for key in ("itemListElement", "steps", "recipeInstructions"):
        nested = value.get(key)
        if nested is not None:
            return _parse_recipe_instructions(nested)
    return []


def _parse_instructions_from_html(soup: BeautifulSoup) -> List[str]:
    """Read displayed instruction blocks when JSON-LD is absent or unusable."""

    container = soup.select_one("#fld_instructions_and_steps")
    if container is None:
        return []

    list_items = container.select("ol li, ul li")
    if list_items:
        return [
            text
            for text in (_clean(item.get_text(" ", strip=True)) for item in list_items)
            if text
        ]

    # A few one-step recipes use a plain direct child <div> with a visible
    # steps_count span. Exclude the mobile UI/header block explicitly.
    blocks = [
        child
        for child in container.find_all(["p", "div"], recursive=False)
        if "mobile-instructions-container" not in (child.get("class") or [])
    ]
    return [
        text
        for text in (_clean(block.get_text(" ", strip=True)) for block in blocks)
        if text
    ]


def _parse_mislabelled_serving_suggestion(soup: BeautifulSoup) -> List[str]:
    """Recover a method accidentally placed in the serving-suggestion field.

    This fallback is deliberately strict: every leaf paragraph must start with
    a consecutive step number beginning at 1. A normal serving suggestion is
    therefore never promoted to recipe instructions.
    """

    container = soup.select_one("#field_serving_suggestion")
    if container is None:
        return []
    paragraphs = [p for p in container.find_all("p") if p.find("p") is None]
    numbered: List[Tuple[int, str]] = []
    for paragraph in paragraphs:
        text = _clean(paragraph.get_text(" ", strip=True))
        match = re.match(r"^(\d+)\s+(.+)$", text)
        if match:
            numbered.append((int(match.group(1)), text))
    if len(numbered) < 2:
        return []
    if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
        return []
    return [text for _, text in numbered]


def _parse_total_time_minutes(iso_duration: Optional[str]) -> Optional[int]:
    if not iso_duration:
        return None
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso_duration.strip())
    if not m:
        return None
    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    total = hours * 60 + mins
    return total or None


def _parse_time_from_html(soup: BeautifulSoup) -> Optional[int]:
    el = soup.select_one(".cooking_time_text")
    if not el:
        return None
    txt = el.get_text(" ", strip=True).lower()
    h = re.search(r"(\d+)\s*(?:h|hr|hrs|hour|hours)", txt)
    m = re.search(r"(\d+)\s*(?:m|min|mins|minute|minutes)", txt)
    minutes = 0
    if h:
        minutes += int(h.group(1)) * 60
    if m:
        minutes += int(m.group(1))
    return minutes or None


# ---------------------------
# Tags (badge tags only)
# ---------------------------

def _parse_badge_tags(soup: BeautifulSoup) -> List[str]:
    tags: Set[str] = set()
    for a in soup.select("a.recipe_circle"):
        title = (a.get("title") or "").strip()
        if title:
            title = re.sub(r"^Click for more\s+", "", title, flags=re.I)
            title = re.sub(r"\s+recipes$", "", title, flags=re.I).strip()
            if title:
                tags.add(title)
            continue
        txt = a.get_text(" ", strip=True)
        if txt:
            tags.add(txt)

    # normalize 2.5 variants to 2½
    normed = []
    for t in tags:
        t = t.replace("2 1/2", "2½").replace("2.5", "2½").strip()
        normed.append(t)
    return sorted(set(normed))


# ---------------------------
# Image url (robust)
# ---------------------------

def _pick_largest_from_srcset(srcset: str) -> Optional[str]:
    candidates: List[Tuple[int, str]] = []
    for part in (srcset or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(.+?)\s+(\d+)w$", part)
        if not m:
            continue
        url = m.group(1).strip()
        w = int(m.group(2))
        candidates.append((w, url))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _get_best_image_url(soup: BeautifulSoup, ld: Optional[Dict[str, Any]]) -> Optional[str]:
    # 1) JSON-LD image
    img = (ld or {}).get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, str) and img.startswith("http"):
        return img

    # 2) og:image
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        return og["content"].strip()

    # 3) hero image src/srcset
    hero = soup.select_one("#hero_image img") or soup.select_one("img.wp-post-image")
    if hero:
        src = (hero.get("src") or "").strip()
        if src:
            return src
        best = _pick_largest_from_srcset((hero.get("srcset") or "").strip())
        if best:
            return best

    return None


# ---------------------------
# Recipe parser
# ---------------------------

def parse_healthyfood_recipe(url: str, session: requests.Session, timeout: int = 30) -> Dict[str, Any]:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    ld = _parse_json_ld_recipe(soup)

    title = (ld or {}).get("name") or _first_text(soup.select_one("h1")) or url
    description = (ld or {}).get("description") or _first_text(soup.select_one(".recipe_decription"))

    time_minutes = _parse_total_time_minutes((ld or {}).get("totalTime"))
    if time_minutes is None:
        time_minutes = _parse_time_from_html(soup)

    serves = (ld or {}).get("recipeYield")
    if serves is None:
        serves_text = _first_text(soup.select_one(".RECIPE_META_servings"))
        if serves_text:
            m = re.search(r"(\d+)", serves_text)
            serves = m.group(1) if m else serves_text

    image_url = _get_best_image_url(soup, ld)

    ingredients: List[str] = []
    if ld and isinstance(ld.get("recipeIngredient"), list):
        ingredients = [_clean(x) for x in ld["recipeIngredient"] if isinstance(x, str) and _clean(x)]
    else:
        for li in soup.select("#ingredients-list li.ingredient"):
            t = _clean(li.get_text(" ", strip=True))
            if t:
                ingredients.append(t)

    instructions = _parse_recipe_instructions((ld or {}).get("recipeInstructions"))
    if not instructions:
        instructions = _parse_instructions_from_html(soup)
    if not instructions:
        instructions = _parse_mislabelled_serving_suggestion(soup)

    variations: List[str] = []
    var_box = soup.select_one("#field_variations")
    if var_box:
        for p in var_box.select("p"):
            t = _clean(p.get_text(" ", strip=True))
            if t:
                variations.append(t)

    tips: List[str] = []
    tip_box = soup.select_one("#fld_hfg_tip")
    if tip_box:
        for p in tip_box.select("p"):
            t = _clean(p.get_text(" ", strip=True))
            if t:
                tips.append(t)

    nutrition: Dict[str, str] = {}
    for li in soup.select(".nutritional_box li.nutrition p"):
        label = li.select_one("span.big-nut")
        if not label:
            continue
        k = _clean(label.get_text(" ", strip=True)).rstrip(":")
        full = _clean(li.get_text(" ", strip=True))
        v = _clean(full.replace(label.get_text(" ", strip=True), "", 1))
        if k and v:
            nutrition[k] = v

    if not nutrition and ld and isinstance(ld.get("nutrition"), dict):
        for k, v in ld["nutrition"].items():
            if k.startswith("@"):
                continue
            if isinstance(v, str):
                nutrition[k] = _clean(v)

    tags = _parse_badge_tags(soup)

    return {
        "link": url,
        "title": title,
        "description": description,
        "time_minutes": time_minutes,
        "serves": serves,
        "image_url": image_url,
        "ingredients": ingredients,
        "instructions": instructions,
        "variations": variations,
        "tips": tips,
        "nutrition": nutrition,
        "badge_tags": tags,
    }


def _dedupe_title(existing: Set[str], title: str) -> str:
    base = title.strip() or "Untitled"
    key = base
    i = 2
    while key in existing:
        key = f"{base}__{i}"
        i += 1
    existing.add(key)
    return key


def main():
    urls: List[str] = json.load(open(INPUT_URLS, "r", encoding="utf-8"))
    urls = [u for u in urls if isinstance(u, str) and u.startswith(RECIPE_PREFIX)]

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": SITE,
        }
    )

    all_recipes: Dict[str, Dict[str, Any]] = {}
    used_keys: Set[str] = set()

    for idx, url in enumerate(urls, start=1):
        try:
            recipe = parse_healthyfood_recipe(url, session=session)
            key = _dedupe_title(used_keys, recipe.get("title") or url)
            all_recipes[key] = recipe
            print(f"[{idx}/{len(urls)}] OK  {key}")
        except Exception as e:
            print(f"[{idx}/{len(urls)}] FAIL {url} -> {e}")

        time.sleep(0.25)  # be nice to the site

    with open(OUT_RECIPES, "w", encoding="utf-8") as f:
        json.dump(all_recipes, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(all_recipes)} recipes to {OUT_RECIPES}")


if __name__ == "__main__":
    main()
