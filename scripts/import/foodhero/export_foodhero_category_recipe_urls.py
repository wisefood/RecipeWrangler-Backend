#!/usr/bin/env python3
"""Export all current FoodHero recipe URLs from its server-rendered pager."""

# export_foodhero_all_recipes_nojs.py
#
# Export ALL "All Recipes" URLs from FoodHero using the server-rendered nojs pager:
#   https://foodhero.org/healthy-recipes-search/nojs/<page>
#
# Install:
#   pip install requests beautifulsoup4 tqdm
#
# Run:
#   uv run export_foodhero_all_recipes_nojs.py --out foodhero_all_recipes_urls.txt
#   python export_foodhero_all_recipes_nojs.py --out foodhero_all_recipes_urls.txt

from __future__ import annotations

import argparse
import re
from typing import List, Set, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm


USER_AGENT = "Mozilla/5.0 (compatible; FoodHeroNoJSExporter/1.0)"
BASE = "https://foodhero.org"
NOJS_BASE = f"{BASE}/healthy-recipes-search/nojs"


def normalize_recipe_url(url: str) -> str:
    return url.split("#", 1)[0].split("?", 1)[0].rstrip("/")


def is_recipe_url(url: str) -> bool:
    """
    Accept only:
      https://foodhero.org/recipes/<slug>
    Reject:
      /recipes/recipe-categories/...
      /recipes/<slug>/print
      /recipes/<slug>/something
    """
    path = urlparse(url).path.rstrip("/")
    if not path.startswith("/recipes/"):
        return False
    if path.startswith("/recipes/recipe-categories/"):
        return False
    rest = path[len("/recipes/") :].strip("/")
    if not rest:
        return False
    return "/" not in rest  # must be single segment after /recipes/


def extract_recipe_urls_from_html(html: str, page_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[str] = []
    seen: Set[str] = set()

    for a in soup.select('a[href^="/recipes/"]'):
        href = a.get("href") or ""
        if not href:
            continue
        full = normalize_recipe_url(urljoin(BASE, href))
        if is_recipe_url(full) and full not in seen:
            seen.add(full)
            out.append(full)

    return out


def extract_total_pages(html: str) -> Optional[int]:
    """
    Looks for: 'Total Results: 524 | Pages: 15'
    """
    m = re.search(r"Pages:\s*(\d+)", html)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def fetch(sess: requests.Session, url: str) -> str:
    r = sess.get(url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return r.text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="foodhero_all_recipes_urls.txt", help="Output txt file (one URL per line)")
    ap.add_argument("--start-page", type=int, default=1, help="Start page number (default 1)")
    ap.add_argument("--max-pages", type=int, default=500, help="Safety cap if page count can't be detected")
    args = ap.parse_args()

    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})

    # Always start from nojs/1 for All Recipes
    first_url = f"{NOJS_BASE}/{args.start_page}"
    html1 = fetch(sess, first_url)

    total_pages = extract_total_pages(html1)
    if total_pages is None:
        total_pages = args.max_pages  # fallback if site changes
        print(f"Could not detect total pages; falling back to max {args.max_pages}")
    else:
        print(f"Detected total pages: {total_pages}")

    all_urls: List[str] = []
    seen: Set[str] = set()

    with tqdm(total=total_pages, unit="page", dynamic_ncols=True, desc="All Recipes") as pbar:
        # process first page we already fetched
        urls = extract_recipe_urls_from_html(html1, first_url)
        for u in urls:
            if u not in seen:
                seen.add(u)
                all_urls.append(u)
        pbar.update(1)

        # remaining pages
        for page in range(args.start_page + 1, total_pages + 1):
            page_url = f"{NOJS_BASE}/{page}"
            pbar.set_postfix_str(f"page={page}", refresh=True)

            html = fetch(sess, page_url)
            urls = extract_recipe_urls_from_html(html, page_url)

            # Stop early if the page yields no recipes (in case total_pages fallback was used)
            if not urls and total_pages == args.max_pages:
                break

            for u in urls:
                if u not in seen:
                    seen.add(u)
                    all_urls.append(u)

            pbar.update(1)

    with open(args.out, "w", encoding="utf-8") as f:
        for u in all_urls:
            f.write(u + "\n")

    print(f"\nDone. Wrote {len(all_urls)} unique recipe URLs to: {args.out}")
    print(f"Source: {NOJS_BASE}/1..{min(total_pages, args.max_pages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
