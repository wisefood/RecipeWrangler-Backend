"""Scrape discovered MyPlate pages into resumable raw recipe JSON."""

# data/MyPlate/scrape_myplate_recipes_batch.py
# Reads myplate_recipe_links.txt, scrapes each recipe via r.jina.ai, and writes myplate_recipes.json
# Output schema per recipe:
# { url, source="MyPlate", title, servings(int|None), description, ingredients[], directions[], notes[], image_url }

import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from tqdm import tqdm

URLS_TXT = Path("myplate_recipe_links.txt")
OUT_JSON = Path("myplate_recipes.json")

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

BASE = "https://www.myplate.gov"

DASH_LINE_RE = re.compile(r"^\s*-{3,}\s*$")
TITLE_LINE_RE = re.compile(r"(?im)^\s*Title\s*:\s*(.+?)\s*$")
SERVINGS_RE = re.compile(r"(?im)^\s*(Makes|Servings?|Yield)\s*:\s*([^\n]+)\s*$")

MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)", re.I)
IMG_URL_RE = re.compile(r"https?://\S+\.(?:jpg|jpeg|png|webp)(?:\?\S+)?", re.I)

BAD_IMG_HINTS = (
    "/themes/custom/",
    "/assets/",
    "us_flag",
    "flag",
    "logo",
    "icon",
    "sprite",
    "favicon",
)

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def normalize_title(raw: str) -> str:
    t = clean(raw)
    t = re.sub(r"(?i)^\s*Title\s*:\s*", "", t)
    t = re.sub(r"\s*\|\s*MyPlate\s*$", "", t, flags=re.I)
    return t.strip()

def jina_fetch(url: str, timeout: int = 60, retries: int = 8) -> str:
    proxy_url = "https://r.jina.ai/http://https://" + url.removeprefix("https://")
    last_err = None
    for i in range(retries):
        r = session.get(proxy_url, timeout=timeout)
        if r.status_code == 429:
            sleep_s = min(120, 5 * (2 ** i))  # 5,10,20,40,80,120...
            time.sleep(sleep_s)
            continue
        if r.status_code >= 400:
            last_err = RuntimeError(f"HTTP {r.status_code} for {proxy_url}")
            time.sleep(min(60, 2 ** i))
            continue
        return r.text
    raise last_err or RuntimeError("Failed to fetch after retries")

def find_title(text: str) -> str | None:
    m = TITLE_LINE_RE.search(text)
    if m:
        return normalize_title(m.group(1))
    for l in (x.strip() for x in text.splitlines() if x.strip()):
        if len(l) < 3:
            continue
        return normalize_title(l)
    return None

def extract_section(text: str, header_variants: list[str]) -> str | None:
    header_pat = "|".join(re.escape(h) for h in header_variants)
    m = re.search(rf"(?im)^\s*(?:{header_pat})\s*$", text)
    if not m:
        return None
    start = m.end()
    tail = text[start:]

    next_headers = [
        "Ingredients", "Directions", "Instructions", "Notes",
        "Nutrition", "Nutrition Information", "Food Group",
        "Source", "Related", "More"
    ]
    next_pos = None
    for h in next_headers:
        mh = re.search(rf"(?im)^\s*{re.escape(h)}\s*$", tail)
        if mh:
            pos = mh.start()
            if next_pos is None or pos < next_pos:
                next_pos = pos

    block = tail[:next_pos] if next_pos is not None else tail
    block = block.strip("\n").strip()
    return block or None

def parse_bullets(block: str) -> list[str]:
    if not block:
        return []
    out = []
    for line in block.splitlines():
        s = line.strip()
        if not s or DASH_LINE_RE.match(s):
            continue
        s = re.sub(r"^[\*\-\u2022]\s+", "", s)
        s = clean(s)
        if s:
            out.append(s)
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq

def parse_numbered_steps(block: str) -> list[str]:
    if not block:
        return []
    out = []
    for line in block.splitlines():
        s = line.strip()
        if not s or DASH_LINE_RE.match(s):
            continue
        s = re.sub(r"^\s*\d+[\.\)]\s+", "", s)
        s = clean(s)
        if s and not DASH_LINE_RE.match(s):
            out.append(s)
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq

def parse_notes(block: str) -> list[str]:
    if not block:
        return []
    out = []
    for line in block.splitlines():
        s = line.strip()
        if not s or DASH_LINE_RE.match(s):
            continue
        s = re.sub(r"^[\*\-\u2022]\s+", "", s)
        s = clean(s)
        if s:
            out.append(s)
    return out

def parse_servings_int(text: str) -> int | None:
    m = SERVINGS_RE.search(text)
    if not m:
        return None
    raw = clean(m.group(2))
    # examples: "2 Servings", "Serves 4", "4", "4 servings"
    n = re.search(r"(\d+)", raw)
    return int(n.group(1)) if n else None

def best_description(text: str, title: str | None) -> str | None:
    lines = [l.rstrip() for l in text.splitlines()]

    ing_idx = None
    for i, l in enumerate(lines):
        if l.strip().lower() == "ingredients":
            ing_idx = i
            break

    start_idx = 0
    if title:
        for i, l in enumerate(lines[:120]):
            if normalize_title(l) == title or clean(l) == title:
                start_idx = i + 1
                break

    end_idx = ing_idx if ing_idx is not None else min(len(lines), start_idx + 120)
    window = lines[start_idx:end_idx]

    paras = []
    cur = []
    for l in window:
        s = l.strip()
        if not s:
            if cur:
                paras.append(clean(" ".join(cur)))
                cur = []
            continue
        if DASH_LINE_RE.match(s):
            continue
        low = s.lower()
        if low in {"print", "add to cookbook", "myplate kitchen", "search"}:
            continue
        if "u.s. department" in low or "usda" in low:
            continue
        if "http://" in s or "https://" in s:
            continue
        cur.append(s)
    if cur:
        paras.append(clean(" ".join(cur)))

    def score(p: str) -> int:
        low = p.lower()
        sc = min(len(p), 300)
        if "." in p:
            sc += 50
        if "myplate" in low:
            sc -= 80
        if "nutrition" in low or "calories" in low:
            sc -= 50
        return sc

    paras = [p for p in paras if len(p) >= 40]
    if not paras:
        return None
    return max(paras, key=score)

def score_image(u: str) -> int:
    ul = u.lower()
    if any(bad in ul for bad in BAD_IMG_HINTS):
        return -10_000
    sc = 0
    if "myplate-prod.azureedge.us" in ul:
        sc += 500
    if "/styles/recipe/" in ul:
        sc += 400
    if "/recipe/public/" in ul:
        sc += 300
    if "image-of-" in ul:
        sc += 150
    if ul.endswith(".jpg") or ".jpg?" in ul:
        sc += 30
    if ul.endswith(".webp") or ".webp?" in ul:
        sc += 25
    if ul.endswith(".png") or ".png?" in ul:
        sc += 5
    return sc

def find_best_image_url(text: str) -> str | None:
    candidates = []
    for m in MD_IMAGE_RE.finditer(text):
        candidates.append(m.group(1).rstrip(").,"))
    for m in IMG_URL_RE.finditer(text):
        candidates.append(m.group(0).rstrip(").,"))

    uniq, seen = [], set()
    for c in candidates:
        if c not in seen:
            seen.add(c)
            uniq.append(c)

    if not uniq:
        return None
    best = max(uniq, key=score_image)
    return best if score_image(best) > 0 else None

def load_urls() -> list[str]:
    urls = []
    for line in URLS_TXT.read_text(encoding="utf-8").splitlines():
        u = line.strip()
        if not u:
            continue
        if u.startswith("http"):
            urls.append(u)
    return urls

def load_existing_db() -> dict:
    if OUT_JSON.exists():
        try:
            return json.loads(OUT_JSON.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def unique_key(title: str, url: str, db: dict) -> str:
    base = title if title else url.split("/")[-1]
    base = clean(base)
    if base not in db:
        return base
    # disambiguate
    slug = url.rstrip("/").split("/")[-1]
    k = f"{base} ({slug})"
    if k not in db:
        return k
    i = 2
    while True:
        kk = f"{k} #{i}"
        if kk not in db:
            return kk
        i += 1

def scrape_recipe(url: str) -> dict:
    text = jina_fetch(url)
    title = find_title(text)
    return {
        "url": url,
        "source": "MyPlate",
        "title": title,
        "servings": parse_servings_int(text),
        "description": best_description(text, title),
        "ingredients": parse_bullets(extract_section(text, ["Ingredients"]) or ""),
        "directions": parse_numbered_steps(extract_section(text, ["Directions", "Instructions"]) or ""),
        "notes": parse_notes(extract_section(text, ["Notes"]) or ""),
        "image_url": find_best_image_url(text),
    }

def main(batch_size: int = 10, cooldown_s: float = 10.0):
    if not URLS_TXT.exists():
        raise SystemExit(f"Missing {URLS_TXT}")

    urls = load_urls()
    db = load_existing_db()

    # Skip already-scraped by URL
    done_urls = {v.get("url") for v in db.values() if isinstance(v, dict)}
    todo = [u for u in urls if u not in done_urls]

    pbar = tqdm(total=len(todo), desc="MyPlate recipes", unit="recipe")
    processed_in_batch = 0

    for url in todo:
        try:
            data = scrape_recipe(url)
            key = unique_key(data.get("title") or "", url, db)
            db[key] = data

            # checkpoint on every recipe (so you never lose progress)
            OUT_JSON.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

        except Exception as e:
            # keep going; write error entry so you can retry later if you want
            err_key = unique_key(f"ERROR {url.split('/')[-1]}", url, db)
            db[err_key] = {"url": url, "source": "MyPlate", "error": str(e)}
            OUT_JSON.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

        pbar.update(1)
        processed_in_batch += 1

        if processed_in_batch >= batch_size:
            time.sleep(cooldown_s)
            processed_in_batch = 0

    pbar.close()
    print(f"Saved {len(db)} entries to {OUT_JSON}")

if __name__ == "__main__":
    main()
