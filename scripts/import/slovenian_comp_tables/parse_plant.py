"""Parse the Slovenian plant-composition PDF into structured CSV rows."""

import os
import re
import csv
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DIR = REPO_ROOT / "data" / "Slovenian-Comp-Tables"
PDF = f"{DIR}/slovenian-plant-based-composition-tables.pdf"
LAYOUT = f"{DIR}/full_text.txt"
RAW = f"{DIR}/full_text_raw.txt"

if not os.path.exists(LAYOUT):
    subprocess.run(["pdftotext", "-layout", PDF, LAYOUT], check=True)
if not os.path.exists(RAW):
    subprocess.run(["pdftotext", PDF, RAW], check=True)
    text = open(RAW, encoding="utf-8").read().replace("\x0c", "\n")
    open(RAW, "w", encoding="utf-8").write(text)

CATEGORIES = ["ŽITA", "IZDELKI", "KRUH", "ZELENJAVA", "SADJE", "OLJNICE"]
SECTION_HEADERS = {
    "GLAVNE SESTAVINE", "ELEMENTI", "V VODI TOPNI VITAMINI",
    "V MAŠČOBAH TOPNI VITAMINI", "MONO IN DISAHARIDI", "MAŠČOBNE KISLINE",
    "STEROLI", "ORGANSKE KISLINE", "DRUGO",
}

# ---------- names/codes from raw (reading-order) text ----------
raw_text = open(RAW, encoding="utf-8").read()
cat_re = re.compile(r"^(" + "|".join(CATEGORIES) + r")\s*$", re.MULTILINE)
cat_positions = [(m.start(), m.group(1)) for m in cat_re.finditer(raw_text)]
raw_anchor_re = re.compile(r"Energijska vrednost \(povprečna\)")
raw_anchors = [m.start() for m in raw_anchor_re.finditer(raw_text)]
code_re = re.compile(r"\(\s*(\d{2,21})\s*/\s*([A-Z]\d{2,5}[A-Z]?)?\s*\)", re.DOTALL)


def category_for(pos):
    cat = None
    for p, c in cat_positions:
        if p <= pos:
            cat = c
        else:
            break
    return cat


names = []
for i, a in enumerate(raw_anchors):
    cand = [p for p, c in cat_positions if p <= a]
    cs = cand[-1] if cand else 0
    block = raw_text[cs:a]
    block = re.sub(r"^\s*(" + "|".join(CATEGORIES) + r")\s*\n", "", block)
    cut = block.find("Enota/100 g")
    if cut != -1:
        block = block[:cut]
    m = code_re.search(block)
    if m:
        code, code2 = m.group(1), m.group(2) or ""
        slo_name = block[: m.start()].strip()
        remainder = block[m.end():]
        parsed_cleanly = True
    else:
        code, code2 = "", ""
        slo_name = block.strip()
        remainder = ""
        parsed_cleanly = False
    slo_name = re.sub(r"\s*\n\s*", " ", slo_name).strip()
    paras = [re.sub(r"\s*\n\s*", " ", p).strip() for p in remainder.split("\n\n") if p.strip()]
    if len(paras) >= 2:
        latin, eng = " ".join(paras[:-1]), paras[-1]
    elif len(paras) == 1:
        latin, eng = "", paras[0]
    else:
        latin, eng = "", ""
    names.append({
        "category": category_for(a),
        "name_slo": slo_name,
        "code": code,
        "code2": code2,
        "name_latin": latin,
        "name_en": eng,
        "parsed_cleanly": parsed_cleanly,
    })

# manual overrides: these 14 foods interleave English name inside the "(code / code2)"
# parens themselves (source PDF layout quirk), which the generic regex can't split.
# food_id (1-based) -> corrected fields, hand-verified against full_text_raw.txt.
NAME_OVERRIDES = {
    45: dict(name_slo="Francoska štruca", code="46410037000000003004", code2="",
             name_en="Wheat (fancy) white loaf of bread", name_latin=""),
    52: dict(name_slo='Rženi kruh "Svit"', code="46410024000000003004", code2="L0102",
             name_en='Rye mixed bread, low GI "Svit"', name_latin=""),
    56: dict(name_slo="Pirin mešani kruh", code="46410120000000003004", code2="",
             name_en="Spelt mixed flour bread", name_latin=""),
    59: dict(name_slo="Prepečenec, več žit", code="46410200200000003004", code2="",
             name_en="Rusks, mixed flours", name_latin=""),
    81: dict(name_slo="Jabolke, Carjevič", code="48110016000000003000", code2="P0101",
             name_en="Apple, Carjevič", name_latin=""),
    117: dict(name_slo="Koleraba, rumena", code="49140070000000003000", code2="N0105",
              name_en="Turnip, yellow turnip", name_latin="Brassica napus napobrassica"),
    120: dict(name_slo="Špinača, kuhana", code="49110032000000203000", code2="N09039",
              name_en="Spinach, boiled", name_latin=""),
    122: dict(name_slo="Korenje, kuhano", code="49140010000000203000", code2="N09035",
              name_en="Carrot, boiled", name_latin=""),
    137: dict(name_slo="Cvetača, kuhana", code="49120020000000203000", code2="N09030",
              name_en="Cauliflower, boiled", name_latin=""),
    138: dict(name_slo="Paprika, rumena", code="49150031000000003000", code2="N0305",
              name_en="Bell pepper, sweet pepper, yellow", name_latin=""),
    139: dict(name_slo="Krompir, kifeljčar", code="47100000000000003000", code2="N0102",
              name_en="Potato", name_latin="Solanum tuberosum"),
    159: dict(name_slo="Korenje, rumeno", code="49140011000000003000", code2="N0101",
              name_en="Carrot, yellow", name_latin="Daucus carota"),
    179: dict(name_slo="Bučnice, golice", code="44230001000000003000", code2="",
              name_en="Pumpkin seed, Golica", name_latin="Cucurbita pepo L."),
    180: dict(name_slo="Bučnice, belice", code="44230002000000003000", code2="",
              name_en="Pumpkin seed, Belica", name_latin="Cucurbita pepo L."),
}
for fid, override in NAME_OVERRIDES.items():
    n = names[fid - 1]
    n["name_slo"] = override["name_slo"]
    n["code"] = override["code"]
    n["code2"] = override["code2"]
    n["name_en"] = override["name_en"]
    n["name_latin"] = override["name_latin"]
    n["parsed_cleanly"] = True

# second batch: code parsed fine, but the SLO/Latin/English paragraph order was
# reversed (Latin/English first, Slovenian last) so "last paragraph = English"
# grabbed the wrong slot, leaving name_en blank. Hand-split from name_slo blob.
NAME_SPLIT_FIX = {
    40: dict(name_slo="Riž, bel, poliran", name_en="Rice, white, polished", name_latin="Oryza sativa L."),
    42: dict(name_slo="Riževa moka", name_en="Rice flour", name_latin=""),
    43: dict(name_slo="Rižev škrob", name_en="Rice starch", name_latin=""),
    62: dict(name_slo="Pšenični mešani kruh, s semeni", name_en="Wheat mixed flour bread, with seeds", name_latin=""),
    70: dict(name_slo="Ajdov kruh, z orehi", name_en="Buckwheat bread, with walnuts", name_latin=""),
    73: dict(name_slo="Višnje", name_en="Sour cherry", name_latin="Prunus cerasus"),
    76: dict(name_slo="Borovnice", name_en="Blueberry", name_latin="Vaccinium spp."),
    77: dict(name_slo="Robidnice", name_en="Blackberry", name_latin="Rubus spp."),
    92: dict(name_slo="Šipek", name_en="Rose hip, Dog-rose berry", name_latin="Rosa canina"),
    94: dict(name_slo="Kivi", name_en="Kiwifruit, raw", name_latin="Actinidia chinensis Planch."),
    95: dict(name_slo="Pomaranča", name_en="Orange", name_latin="Citrus sinensis"),
    96: dict(name_slo="Mandarina", name_en="Mandarin", name_latin="Citrus deliciosa Ten."),
    97: dict(name_slo="Grenivka", name_en="Grapefruit", name_latin="Citrus x paradisi Macfad."),
    98: dict(name_slo="Limona", name_en="Lemon", name_latin="Citrus limon"),
    99: dict(name_slo="Limeta", name_en="Lime", name_latin="Citrus latifolia"),
    100: dict(name_slo="Ananas", name_en="Pineapple", name_latin="Ananas comosus (L.) Merr."),
    101: dict(name_slo="Rozine", name_en="Raisin", name_latin="Vitis vinifera"),
    103: dict(name_slo="Suhe slive", name_en="Prune, dried plum", name_latin=""),
    104: dict(name_slo="Suhe marelice", name_en="Dried apricot", name_latin=""),
    150: dict(name_slo="Jajčevec", name_en="Aubergine, eggplant", name_latin="Solanum melongena L."),
    182: dict(name_slo="Jušni rezanci, ajdovi, kuhani", name_en="Buckwheat soup noodles, cooked", name_latin=""),
}
for fid, fix in NAME_SPLIT_FIX.items():
    n = names[fid - 1]
    n["name_slo"] = fix["name_slo"]
    n["name_en"] = fix["name_en"]
    n["name_latin"] = fix["name_latin"]
    n["parsed_cleanly"] = True

# ---------- nutrient tables from layout (column-aligned) text ----------
lines = open(LAYOUT, encoding="utf-8").read().split("\n")
layout_anchor_re = re.compile(r"Energijska vrednost \(povprečna\)")
layout_anchors = [i for i, l in enumerate(lines) if layout_anchor_re.search(l)]

assert len(layout_anchors) == len(raw_anchors), (
    f"anchor count mismatch: layout={len(layout_anchors)} raw={len(raw_anchors)}"
)

value_row_re = re.compile(r"^\s*(.+?)\s{2,}(g|mg|µg|%|kcal|kJ)\s{2,}(\S+)\s{2,}(\S+)\s{2,}(\S+)\s*$")
value_row_nounit_re = re.compile(r"^\s*([A-ZČŠŽa-zčšž][^\s].{1,55}?)\s{4,}(\S+)\s{2,}(\S+)\s{2,}(\S+)\s*$")


def is_page_number(s):
    return bool(re.match(r"^\d{1,4}$", s))


def last_number(s):
    toks = s.split()
    return toks[-1] if toks else ""


foods_rows = []
nutrient_rows = []

for idx, a in enumerate(layout_anchors):
    food_id = idx + 1
    n = names[idx]
    foods_rows.append({
        "food_id": food_id,
        "category": n["category"],
        "name_slo": n["name_slo"],
        "name_en": n["name_en"],
        "name_latin": n["name_latin"],
        "code": n["code"],
        "sub_code": n["code2"],
        "name_parsed_cleanly": n["parsed_cleanly"],
    })

    body_end = layout_anchors[idx + 1] if idx + 1 < len(layout_anchors) else len(lines)
    # trim next food's header block off the tail (same 12-line lookback heuristic)
    if idx + 1 < len(layout_anchors):
        a_next = layout_anchors[idx + 1]
        hs = a_next
        limit = a + 1
        steps = 0
        while hs > limit and steps < 12:
            hs -= 1
            steps += 1
        body_end = hs

    body = lines[a:body_end]

    # total energy: first two lines of body
    kj_line = lines[a]
    kcal_line = lines[a + 1] if a + 1 < len(lines) else ""
    nutrient_rows.append({
        "food_id": food_id, "section": "ENERGIJSKA VREDNOST", "nutrient": "Energijska vrednost, skupaj",
        "unit": "kJ", "avg": last_number(kj_line), "min": "", "max": "",
    })
    if "kcal" in kcal_line:
        nutrient_rows.append({
            "food_id": food_id, "section": "ENERGIJSKA VREDNOST", "nutrient": "Energijska vrednost, skupaj",
            "unit": "kcal", "avg": last_number(kcal_line), "min": "", "max": "",
        })

    section = None
    for l in body:
        s = l.strip()
        if not s or is_page_number(s) or s in CATEGORIES:
            continue
        if s in SECTION_HEADERS:
            section = s
            continue
        if s.startswith("Energijska vrednost") or s.startswith("100 g užitnega dela") \
           or s.startswith("Energijski delež") or s.startswith("Sestava") \
           or (s.startswith("Povprečno") ) or ("Min." in s and "Max." in s):
            continue
        m = value_row_re.match(l)
        if m:
            nutrient, unit, avg, mn, mx = m.groups()
        else:
            m2 = value_row_nounit_re.match(l)
            if m2:
                nutrient, avg, mn, mx = m2.groups()
                unit = ""
            else:
                continue
        nutrient = nutrient.strip()
        sec = "UŽITNI DEL" if nutrient == "Užitni del" else section
        nutrient_rows.append({
            "food_id": food_id, "section": sec, "nutrient": nutrient,
            "unit": unit, "avg": avg, "min": mn, "max": mx,
        })

with open(f"{DIR}/slovenian_foods.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=[
        "food_id", "category", "name_slo", "name_en", "name_latin",
        "code", "sub_code", "name_parsed_cleanly",
    ])
    w.writeheader()
    w.writerows(foods_rows)

# neither book reports a "total sugars" row -- only individual sugars. Compute one,
# summing whichever of the five a food has a value for (mg -> g first); foods with
# no sugar rows at all get no computed row (not fabricated as 0).
def parse_num(s):
    s = s.strip().rstrip("*")
    if not s or s == "-":
        return None
    s = s.lstrip("<").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


SUGARS = {"Fruktoza", "Glukoza", "Maltoza", "Laktoza", "Saharoza"}
sugar_by_food = {}
for r in nutrient_rows:
    if r["nutrient"] in SUGARS:
        v = parse_num(r["avg"])
        if v is None:
            continue
        if r["unit"] == "mg":
            v /= 1000.0
        sugar_by_food[r["food_id"]] = sugar_by_food.get(r["food_id"], 0.0) + v

for food_id, total in sorted(sugar_by_food.items(), key=lambda x: int(x[0])):
    nutrient_rows.append({
        "food_id": food_id, "section": "MONO IN DISAHARIDI",
        "nutrient": "Sugars, total (computed)", "unit": "g",
        "avg": f"{total:.4g}", "min": "", "max": "",
    })

with open(f"{DIR}/slovenian_nutrients_long.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["food_id", "section", "nutrient", "unit", "avg", "min", "max"])
    w.writeheader()
    w.writerows(nutrient_rows)

print(f"foods: {len(foods_rows)}")
print(f"nutrient rows: {len(nutrient_rows)}")
print(f"foods with unclean name parse: {sum(1 for r in foods_rows if not r['name_parsed_cleanly'])}")
print(f"foods with computed sugar total: {len(sugar_by_food)} / {len(foods_rows)}")
