"""Discover all current MyPlate recipe URLs with resumable pagination."""

# data/MyPlate/export_myplate_recipe_urls.py
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE = "https://www.myplate.gov"
PAGE0 = "https://www.myplate.gov/myplate-kitchen/recipes"
PAGE_TPL = "https://www.myplate.gov/myplate-kitchen/recipes?sort_bef_combine=title_ASC&page={page}"
LAST_PAGE = 53

OUT = Path("myplate_recipe_links.txt")
STATE = Path("myplate_state.txt")  # stores last completed page number

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

ABS_RE = re.compile(r"https?://(?:www\.)?myplate\.gov/recipes/[a-z0-9-]+", re.I)
REL_RE = re.compile(r"(?<![\w/])(/recipes/[a-z0-9-]+)", re.I)

def load_existing() -> set[str]:
    if not OUT.exists():
        return set()
    return {line.strip() for line in OUT.read_text(encoding="utf-8").splitlines() if line.strip()}

def load_last_page_done() -> int:
    if not STATE.exists():
        return -1
    try:
        return int(STATE.read_text(encoding="utf-8").strip())
    except Exception:
        return -1

def save_state(page_done: int) -> None:
    STATE.write_text(str(page_done), encoding="utf-8")

def jina_fetch(url: str, timeout: int = 60, retries: int = 8) -> str:
    proxy_url = "https://r.jina.ai/http://https://" + url.removeprefix("https://")
    last_err = None
    for i in range(retries):
        r = session.get(proxy_url, timeout=timeout)
        if r.status_code == 429:
            sleep_s = min(120, 5 * (2 ** i))  # 5,10,20,40,80,120...
            print(f"[429] rate limited -> sleep {sleep_s}s")
            time.sleep(sleep_s)
            continue
        if r.status_code >= 400:
            last_err = RuntimeError(f"HTTP {r.status_code} for {proxy_url}")
            sleep_s = min(60, 2 ** i)
            print(f"[http {r.status_code}] -> sleep {sleep_s}s")
            time.sleep(sleep_s)
            continue
        return r.text
    raise last_err or RuntimeError("Failed to fetch after retries")

def extract_recipe_urls(text: str) -> set[str]:
    out = set()
    for u in ABS_RE.findall(text):
        out.add(u.split("?")[0].split("#")[0])
    for path in REL_RE.findall(text):
        out.add(urljoin(BASE, path))
    return out

def append_new(new_urls: set[str]) -> None:
    if not new_urls:
        return
    with OUT.open("a", encoding="utf-8") as f:
        for u in sorted(new_urls):
            f.write(u + "\n")
        f.flush()

def main(batch_size: int = 5, base_sleep: float = 0.8):
    all_urls = load_existing()
    last_done = load_last_page_done()

    # Resume: start from next page after last_done
    start_page = 0 if last_done < 0 else last_done + 1

    print(f"resume from page {start_page} (already have {len(all_urls)} links)")

    # page 0 is special URL
    if start_page == 0:
        txt = jina_fetch(PAGE0)
        found = extract_recipe_urls(txt)
        new = found - all_urls
        all_urls |= found
        append_new(new)
        save_state(0)
        print(f"page 0: +{len(new)} (total={len(all_urls)})")
        time.sleep(base_sleep)

    # pages 1..LAST_PAGE
    for batch_start in range(max(1, start_page), LAST_PAGE + 1, batch_size):
        batch_end = min(LAST_PAGE, batch_start + batch_size - 1)
        print(f"\nbatch {batch_start}-{batch_end}")

        for p in range(batch_start, batch_end + 1):
            txt = jina_fetch(PAGE_TPL.format(page=p))
            found = extract_recipe_urls(txt)
            new = found - all_urls
            all_urls |= found
            append_new(new)
            save_state(p)
            print(f"page {p}/{LAST_PAGE}: +{len(new)} (total={len(all_urls)})")
            time.sleep(base_sleep)

        # extra cool-down between batches to avoid 429
        time.sleep(5)

    print(f"\nDONE. total={len(all_urls)} links in {OUT}")

if __name__ == "__main__":
    main()
