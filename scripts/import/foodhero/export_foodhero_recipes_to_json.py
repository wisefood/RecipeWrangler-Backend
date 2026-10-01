#!/usr/bin/env python3
"""Scrape FoodHero recipe URLs into a resumable raw JSON collection."""

# export_foodhero_recipes_to_json.py
#
# Single-file scraper + exporter:
# - Reads a txt file of FoodHero recipe URLs (one per line)
# - Scrapes each recipe page (ingredients/directions/notes + nutrition label image)
# - IMPORTANT: ingredients/directions fallback to JSON-LD if HTML sections aren't found
# - Writes one big JSON mapping: { "<title>": { ...details... }, "<title>__2": { ... }, ... }
# - Progress bar + retries + incremental save + optional resume
#
# Install:
#   pip install requests beautifulsoup4 tqdm
#
# Run:
#   uv run data/FoodHero/export_foodhero_recipes_to_json.py --in foodhero_all_recipes_urls.txt --out foodhero_recipes_big.json
#   uv run data/FoodHero/export_foodhero_recipes_to_json.py --resume
#
# Optional:
#   --limit 50
#   --sleep 0.1

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, asdict, replace
from typing import Any, Optional, List, Dict, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm


# =============================================================================
#                               EXPORTER SETTINGS
# =============================================================================

USER_AGENT = "Mozilla/5.0 (compatible; RecipeWranglerFoodHero/1.0)"


# =============================================================================
#                               SCRAPER MODULE
# =============================================================================

_UFRACTIONS = "¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞"
_QTY_RE = re.compile(
    rf"""
    (?<![\d.])(?=(
        (?:\d+\s+\d+/\d+)|      # "1 1/2"
        (?:\d+/\d+)|            # "1/2"
        (?:\d+(?:\.\d+)?)|      # "2" or "2.5"
        (?:[{_UFRACTIONS}])     # unicode fraction like "¼"
    )\s)
    """,
    re.VERBOSE,
)


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _first_jsonld_recipe(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = s.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        candidates: List[Dict[str, Any]] = []
        if isinstance(data, dict) and "@graph" in data and isinstance(data["@graph"], list):
            candidates = [x for x in data["@graph"] if isinstance(x, dict)]
        elif isinstance(data, dict):
            candidates = [data]
        elif isinstance(data, list):
            candidates = [x for x in data if isinstance(x, dict)]

        for c in candidates:
            t = c.get("@type")
            if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
                return c
    return None


def _split_ingredient_blob(blob: str) -> List[str]:
    blob = str(blob or "").strip()
    if not blob:
        return []
    if "\n" in blob:
        parts = [p.strip(" \t\r\n•-") for p in blob.splitlines()]
        return [p for p in parts if p]

    blob = re.sub(r"\s+", " ", blob).strip()

    starts = [m.start() for m in _QTY_RE.finditer(blob)]
    if not starts:
        return [blob]

    starts = sorted(set(starts))
    items: List[str] = []
    for i, st in enumerate(starts):
        en = starts[i + 1] if i + 1 < len(starts) else len(blob)
        chunk = blob[st:en].strip(" ,;")
        if chunk:
            items.append(chunk)

    items = [re.sub(r"\s+\)", ")", x) for x in items]
    items = [re.sub(r"\(\s+", "(", x) for x in items]
    return items


def _extract_section_list(soup: BeautifulSoup, header_text: str) -> List[str]:
    h = soup.find(lambda t: t.name in ("h2", "h3") and _text(t) == header_text)
    if not h:
        return []

    if header_text == "Directions":
        ol = h.find_next("ol")
        if not ol:
            return []
        return [li.get_text(" ", strip=True) for li in ol.find_all("li", recursive=False)]

    if header_text == "Ingredients":
        section = h.find_next("div", class_="section")
        ingredient_nodes = section.select('[itemprop="ingredients"]') if section else []
        # Some pages have a stray extra wrapper div with itemprop="ingredients"
        # that itself contains other itemprop="ingredients" divs as children
        # (a markup bug on the source page, not ours) — .select() matches the
        # wrapper too, and .get_text() on it recursively pulls in all its
        # children's text glued together, duplicating every nested ingredient
        # as one extra garbled line. Keep only leaf nodes (no matched
        # descendant) so each real ingredient is extracted exactly once.
        ingredient_nodes = [
            node for node in ingredient_nodes
            if not node.select('[itemprop="ingredients"]')
        ]
        if ingredient_nodes:
            return [node.get_text(" ", strip=True) for node in ingredient_nodes]

    ul = h.find_next("ul")
    p = h.find_next("p")
    next_h = h.find_next(lambda t: t.name in ("h2", "h3") and t is not h)

    def _is_before(a, b) -> bool:
        if not a or not b:
            return True
        if a.sourceline is None or b.sourceline is None:
            return True
        return a.sourceline < b.sourceline

    if header_text == "Notes" and ul and _is_before(ul, next_h):
        return [li.get_text(" ", strip=True) for li in ul.find_all("li", recursive=False)]

    if header_text == "Ingredients" and p and _is_before(p, next_h):
        marker = "\u241e"
        for br in p.find_all("br"):
            br.replace_with(marker)
        return [
            part.strip()
            for part in p.get_text(" ", strip=True).split(marker)
            if part.strip()
        ]

    if ul and _is_before(ul, next_h):
        return [li.get_text(" ", strip=True) for li in ul.find_all("li", recursive=False)]
    if p and _is_before(p, next_h):
        return _split_ingredient_blob(p.get_text(" ", strip=True))

    return []


def _extract_meta_value(soup: BeautifulSoup, label: str) -> Optional[str]:
    strong = soup.find("strong", string=re.compile(rf"^{re.escape(label)}\s*$", re.I))
    if not strong:
        return None
    val = (strong.next_sibling or "").strip()
    return val or None


def _extract_nutrition_label_image_url(nutrition_page_html: str, *, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(nutrition_page_html, "html.parser")

    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        return urljoin(base_url, og["content"])

    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return urljoin(base_url, tw["content"])

    imgs = soup.find_all("img")
    for img in imgs:
        src = img.get("src") or ""
        alt = (img.get("alt") or "").lower()
        if not src:
            continue
        if any(k in src.lower() for k in ("nutrition", "facts", "label")) or any(
            k in alt for k in ("nutrition", "facts", "label")
        ):
            return urljoin(base_url, src)

    if imgs and imgs[0].get("src"):
        return urljoin(base_url, imgs[0]["src"])

    return None


def _normalize_categories(cats: List[str]) -> List[str]:
    cleaned = [" ".join(str(c).split()).strip() for c in cats if str(c).strip()]
    out: List[str] = []
    i = 0
    while i < len(cleaned):
        s = cleaned[i]
        if s.startswith("&") and out:
            out[-1] = f"{out[-1]} {s}".strip()
            i += 1
            continue
        if "(" in s and ")" not in s:
            merged = s
            j = i + 1
            while j < len(cleaned):
                nxt = cleaned[j]
                merged = merged.rstrip()
                sep = "" if merged.endswith("(") else ", "
                merged = f"{merged}{sep}{nxt}"
                if ")" in nxt:
                    break
                j += 1
            out.append(merged)
            i = j + 1
            continue
        out.append(s)
        i += 1
    return out


def _instructions_from_jsonld(j: Dict[str, Any]) -> List[str]:
    ri = j.get("recipeInstructions")
    if not ri:
        return []

    if isinstance(ri, str):
        parts = [p.strip() for p in ri.splitlines() if p.strip()]
        return parts or [ri.strip()]

    if isinstance(ri, list):
        out: List[str] = []
        for step in ri:
            if isinstance(step, str):
                s = step.strip()
                if s:
                    out.append(s)
            elif isinstance(step, dict):
                txt = (step.get("text") or step.get("name") or "").strip()
                if txt:
                    out.append(txt)
        return out

    if isinstance(ri, dict):
        elems = ri.get("itemListElement")
        if isinstance(elems, list):
            out: List[str] = []
            for e in elems:
                if isinstance(e, str):
                    s = e.strip()
                    if s:
                        out.append(s)
                elif isinstance(e, dict):
                    txt = (e.get("text") or e.get("name") or "").strip()
                    if txt:
                        out.append(txt)
            return out

    return []


@dataclass
class FoodHeroRecipe:
    source_url: str
    canonical_url: Optional[str]
    language: Optional[str]

    title: Optional[str]
    description: Optional[str]
    image_url: Optional[str]

    prep_time: Optional[str]
    cook_time: Optional[str]
    makes: Optional[str]
    recipe_yield: Optional[str]

    ingredients: List[str]
    directions: List[str]
    notes: List[str]

    nutrition_label_url: Optional[str]
    nutrition_label_image_url: Optional[str]

    categories: List[str]
    date_published: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_foodhero_recipe_html(html: str, *, base_url: str) -> FoodHeroRecipe:
    soup = BeautifulSoup(html, "html.parser")

    canonical = soup.find("link", rel="canonical")
    canonical_url = canonical["href"] if canonical and canonical.get("href") else None

    lang = soup.html.get("lang") if soup.html else None

    j = _first_jsonld_recipe(soup) or {}

    title = j.get("name") or _text(soup.find("h1")) or None
    description = j.get("description") or None

    image_url = None
    img = j.get("image")
    if isinstance(img, dict) and img.get("url"):
        image_url = img["url"]
    elif isinstance(img, str):
        image_url = img

    recipe_yield = j.get("recipeYield")
    date_published = j.get("datePublished")

    categories: List[str] = []
    rc = j.get("recipeCategory")
    if isinstance(rc, list):
        categories = [str(x) for x in rc if x]
    elif isinstance(rc, str):
        categories = [rc]
    categories = _normalize_categories(categories)

    # HTML extraction first
    ingredients = _extract_section_list(soup, "Ingredients")
    directions = _extract_section_list(soup, "Directions")
    notes = _extract_section_list(soup, "Notes")

    # Fallback to JSON-LD if HTML didn't yield anything
    if not ingredients:
        ri = j.get("recipeIngredient")
        if isinstance(ri, list):
            ingredients = [str(x).strip() for x in ri if str(x).strip()]
        elif isinstance(ri, str) and ri.strip():
            ingredients = [ri.strip()]

    if not directions:
        directions = _instructions_from_jsonld(j)

    prep_time = _extract_meta_value(soup, "Prep time:")
    cook_time = _extract_meta_value(soup, "Cook time:")
    makes = _extract_meta_value(soup, "Makes:")

    nutr = soup.find("a", string=re.compile(r"View label", re.I))
    nutrition_label_url = urljoin(base_url, nutr["href"]) if nutr and nutr.get("href") else None

    return FoodHeroRecipe(
        source_url=base_url,
        canonical_url=canonical_url,
        language=lang,
        title=title,
        description=description,
        image_url=image_url,
        prep_time=prep_time,
        cook_time=cook_time,
        makes=makes,
        recipe_yield=recipe_yield,
        ingredients=ingredients,
        directions=directions,
        notes=notes,
        nutrition_label_url=nutrition_label_url,
        nutrition_label_image_url=None,
        categories=categories,
        date_published=date_published,
    )


def fetch_foodhero_recipe(
    url: str,
    *,
    timeout: int = 25,
    user_agent: str = USER_AGENT,
    session: Optional[requests.Session] = None,
) -> FoodHeroRecipe:
    owns_session = session is None
    sess = session or requests.Session()
    sess.headers.update({"User-Agent": user_agent})

    r = sess.get(url, timeout=timeout)
    r.raise_for_status()
    recipe = parse_foodhero_recipe_html(r.text, base_url=url)

    nutrition_label_image_url = None
    if recipe.nutrition_label_url:
        nr = sess.get(recipe.nutrition_label_url, timeout=timeout)
        nr.raise_for_status()
        nutrition_label_image_url = _extract_nutrition_label_image_url(
            nr.text, base_url=recipe.nutrition_label_url
        )

    if owns_session:
        sess.close()

    return replace(recipe, nutrition_label_image_url=nutrition_label_image_url)


# =============================================================================
#                               EXPORTER LOGIC
# =============================================================================

def read_urls(path: str) -> List[str]:
    urls: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            u = line.strip()
            if not u or u.startswith("#"):
                continue
            urls.append(u)
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def load_existing(out_path: str) -> Dict[str, Any]:
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def unique_title_key(title: str, existing: Dict[str, Any]) -> str:
    base = (title or "Untitled").strip()
    if base not in existing:
        return base
    i = 2
    while True:
        k = f"{base}__{i}"
        if k not in existing:
            return k
        i += 1


def fetch_with_retries(
    sess: requests.Session,
    url: str,
    *,
    retries: int = 2,
    backoff: float = 0.8,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = fetch_foodhero_recipe(url, session=sess)
            d = r.to_dict()
            d["url"] = url
            return d, None
        except (requests.RequestException, requests.Timeout) as e:
            last_err = f"network: {type(e).__name__}: {e}"
        except Exception as e:
            last_err = f"error: {type(e).__name__}: {e}"

        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))

    return None, last_err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", default="foodhero_all_recipes_urls.txt", help="Input txt file of recipe URLs")
    ap.add_argument("--out", dest="out_path", default="foodhero_recipes_big.json", help="Output JSON file")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of URLs (0 = no limit)")
    ap.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between requests")
    ap.add_argument("--resume", action="store_true", help="Resume: load existing JSON and skip URLs already present")
    args = ap.parse_args()

    urls = read_urls(args.in_path)
    if args.limit and args.limit > 0:
        urls = urls[: args.limit]

    existing: Dict[str, Any] = load_existing(args.out_path) if args.resume else {}
    out: Dict[str, Any] = dict(existing)

    scraped_urls = set()
    if args.resume:
        for v in out.values():
            if isinstance(v, dict) and "url" in v:
                scraped_urls.add(v["url"])

    errors: List[Dict[str, str]] = []

    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})

    try:
        with tqdm(total=len(urls), dynamic_ncols=True, desc="Scraping recipes") as pbar:
            for url in urls:
                pbar.set_postfix_str(urlparse(url).path.rsplit("/", 1)[-1][:30], refresh=True)

                if args.resume and url in scraped_urls:
                    pbar.update(1)
                    continue

                data, err = fetch_with_retries(sess, url, retries=2, backoff=0.8)
                if data is None:
                    errors.append({"url": url, "error": err or "unknown"})
                    pbar.update(1)
                    continue

                title = (data.get("title") or "").strip() or "Untitled"
                key = unique_title_key(title, out)
                out[key] = data

                if args.sleep and args.sleep > 0:
                    time.sleep(args.sleep)

                with open(args.out_path, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)

                pbar.update(1)
    finally:
        sess.close()

    if errors:
        err_path = args.out_path.rsplit(".", 1)[0] + "_errors.json"
        with open(err_path, "w", encoding="utf-8") as f:
            json.dump(errors, f, ensure_ascii=False, indent=2)
        print(f"\nCompleted with {len(errors)} errors. Saved: {err_path}")

    print(f"\nDone. Wrote {len(out)} recipes to: {args.out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
