"""Parse the Slovenian meat-composition PDF into structured CSV rows."""

import os
import re
import csv
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DIR = REPO_ROOT / "data" / "Slovenian-Comp-Tables"
PDF = f"{DIR}/slovenian-meat-products-composition-tables.pdf"
LAYOUT = f"{DIR}/full_text_meat.txt"

if not os.path.exists(LAYOUT):
    subprocess.run(["pdftotext", "-layout", PDF, LAYOUT], check=True)

FOOTER = "Slovenske prehranske tabele – meso in mesni izdelki"
SECTION_HEADERS = {
    "GLAVNE SESTAVINE", "ELEMENTI IN ELEMENTI V SLEDOVIH", "MAŠČOBNE KISLINE",
    "AMINOKISLINE", "VITAMINI", "STEROLI", "OSTALE SESTAVINE",
}
SUBSECTIONS = {"Makroelementi", "Mikroelementi", "V maščobah topni", "V vodi topni"}

GROUP_BY_PREFIX = {
    "2.1": "MESO KLAVNIH ŽIVALI",
    "2.2": "PERUTNINA",
    "2.3": "DIVJAČINA",
    "2.4": "RIBE, RAKI IN MEHKUŽCI",
}

text = open(LAYOUT, encoding="utf-8").read()

subcat_re = re.compile(r"^\s*(2\.\d\.\d)\s*\n+\s*(?:[ \t]*\n)*\s*([A-ZŽŠČ][A-ZŽŠČ, ]{2,40})\s*$", re.MULTILINE)
boundaries = [(m.start(), m.group(1), m.group(2).strip()) for m in subcat_re.finditer(text)]

lines = text.split("\n")
anchor_re = re.compile(r"Energijska vrednost \(povprečna\)")
anchors = [i for i, l in enumerate(lines) if anchor_re.search(l)]

# char offset of the start of each line, to map boundaries (char-based) to line index
line_starts = [0]
for l in lines[:-1]:
    line_starts.append(line_starts[-1] + len(l) + 1)


def line_idx_for_offset(off):
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= off:
            lo = mid
        else:
            hi = mid - 1
    return lo


boundary_lines = [(line_idx_for_offset(p), num, name) for p, num, name in boundaries]


def category_for_line(li):
    cat, num = None, None
    for bl, n, name in boundary_lines:
        if bl <= li:
            cat, num = name, n
        else:
            break
    group = GROUP_BY_PREFIX.get(num[:3], "") if num else ""
    return cat, group


col_header_re = re.compile(r"Enota/100 g Beljakovine")


def is_page_number(s):
    return bool(re.match(r"^\d{1,4}$", s))


run_header_re = re.compile(r"^[A-ZŽŠČ][a-zA-Zščžćžà-ž ,.\"']+\([a-zA-Zščžćžà-ž ,.\"']+\)$")
footer_re = re.compile(r"^" + re.escape(FOOTER) + r"\s*\d*$")
SUBCAT_NAMES = {name for _, _, name in boundaries}


def is_noise_line(s):
    if not s:
        return True
    if is_page_number(s):
        return True
    if footer_re.match(s):
        return True
    if re.match(r"^2\.\d(\.\d)?$", s):
        return True
    if s in SUBCAT_NAMES:
        return True
    if run_header_re.match(s):
        return True
    return False


# ---------- names ----------
names = []
for a in anchors:
    ch = None
    for i in range(a, max(a - 5, -1), -1):
        if col_header_re.search(lines[i]):
            ch = i
            break
    l1 = lines[ch - 1].strip() if ch else ""
    l2 = lines[ch - 2].strip() if ch and ch - 2 >= 0 else ""
    if l2 and not is_noise_line(l2):
        cols1 = re.split(r"\s{2,}", l1)
        cols2 = re.split(r"\s{2,}", l2)
        if len(cols1) == len(cols2):
            cols = [f"{c2} {c1}".strip() for c1, c2 in zip(cols1, cols2)]
        else:
            cols = re.split(r"\s{2,}", l1)
    else:
        cols = re.split(r"\s{2,}", l1)
    cols = [c.strip() for c in cols if c.strip()]
    name_slo = cols[0] if len(cols) > 0 else ""
    name_en = cols[1] if len(cols) > 1 else ""
    name_latin = cols[2] if len(cols) > 2 else ""
    cat, group = category_for_line(a)
    names.append({"category": cat, "group": group, "name_slo": name_slo, "name_en": name_en, "name_latin": name_latin})

# hand fix: names that wrap across 3 physical lines with the SLO/EN column
# split falling in different places than the 2-line join heuristic expects.
NAME_SPLIT_FIX = {
    8: dict(name_slo="Svinjina, podkožna slanina", name_en="Pork, subcutaneous adipose tissue", name_latin=""),
    12: dict(name_slo="Govedina, zunanje stegno (BF)", name_en="Beef, silverside", name_latin="m. biceps femoris"),
    14: dict(name_slo="Govedina, pljučna pečenka", name_en="Beef, fillet, tenderloin", name_latin=""),
    27: dict(name_slo="Jagnjetina, podkožni loj", name_en="Lamb's meat, subcutaneous adipose tissue", name_latin=""),
    31: dict(name_slo="Kunčje meso, hrbet in stegno", name_en="Rabbit meat, back and leg", name_latin=""),
    38: dict(name_slo="Šunka v ovitku (Kuhan pršut, Kuhana šunka)", name_en="Ham in casing", name_latin=""),
    44: dict(name_slo="Prekmurska šunka", name_en='Dry-cured ham "prekmurska šunka"', name_latin=""),
    47: dict(name_slo="Razsoljeni svinjski hrbet, konzervirano", name_en="Pork, loin, cured", name_latin=""),
    48: dict(name_slo="Govedina v ovitku", name_en="Corned beef (german origin)", name_latin=""),
    69: dict(name_slo="Piščančja posebna klobasa", name_en='Chicken sausage "posebna"', name_latin=""),
    88: dict(name_slo="Divji prašič, meso, povprečno", name_en="Wild boar meat, average", name_latin=""),
    104: dict(name_slo="Jezerska zlatovčica, gojena", name_en="American trout, bred", name_latin=""),
    125: dict(name_slo='Zelenjava z ribo "Maestral"', name_en='Canned vegetable with fish "maestral"', name_latin=""),
}
for fid, fix in NAME_SPLIT_FIX.items():
    n = names[fid - 1]
    n["name_slo"] = fix["name_slo"]
    n["name_en"] = fix["name_en"]
    n["name_latin"] = fix["name_latin"]

# ---------- nutrient rows ----------
full_row_re = re.compile(r"^\s*(\S.*?)\s{2,}(g|mg|µg|ng|%|kcal|kJ)\s{2,}(\S+)\s{2,}(\S+)\s{2,}(\S+)\s*$")
val_unit_only_re = re.compile(r"^\s*(g|mg|µg|ng|%|kcal|kJ)\s{2,}(\S+)\s{2,}(\S+)\s{2,}(\S+)\s*$")
val_unit_single_re = re.compile(r"^\s*(g|mg|µg|ng|%|kcal|kJ)\s{2,}(\S+)\s*$")
name_unit_val_re = re.compile(r"^\s*(\S.*?)\s{2,}(g|mg|µg|ng|%|kcal|kJ)\s{2,}(\S+)\s*$")
name_val_re = re.compile(r"^\s*(\S.*?)\s{2,}([\d,.<]+\*?)\s*$")

foods_rows = []
nutrient_rows = []

for idx, a in enumerate(anchors):
    food_id = idx + 1
    n = names[idx]
    foods_rows.append({"food_id": food_id, "group": n["group"], "category": n["category"],
                        "name_slo": n["name_slo"], "name_en": n["name_en"], "name_latin": n["name_latin"]})

    if idx + 1 < len(anchors):
        body_end = anchors[idx + 1]
    else:
        idx_re = re.compile(r"^STVARNO KAZALO$")
        stop = next((i for i in range(a, len(lines)) if idx_re.match(lines[i].strip())), len(lines))
        body_end = stop
    # trim next food's name lines (found via its own col-header lookup) off the tail
    if idx + 1 < len(anchors):
        a_next = anchors[idx + 1]
        ch_next = None
        for i in range(a_next, max(a_next - 5, -1), -1):
            if col_header_re.search(lines[i]):
                ch_next = i
                break
        if ch_next:
            back = 1
            if ch_next - 2 >= 0 and not is_noise_line(lines[ch_next - 2].strip()) and lines[ch_next - 2].strip():
                back = 2
            body_end = ch_next - back

    body = lines[a:body_end]

    kj_line = lines[a]
    kcal_line = lines[a + 1] if a + 1 < len(lines) else ""

    def last_number(s):
        toks = s.split()
        return toks[-1] if toks else ""

    nutrient_rows.append({"food_id": food_id, "section": "ENERGIJSKA VREDNOST", "subsection": "",
                           "nutrient": "Energijska vrednost, skupaj", "unit": "kJ",
                           "avg": last_number(kj_line), "min": "", "max": ""})
    if "kcal" in kcal_line:
        nutrient_rows.append({"food_id": food_id, "section": "ENERGIJSKA VREDNOST", "subsection": "",
                               "nutrient": "Energijska vrednost, skupaj", "unit": "kcal",
                               "avg": last_number(kcal_line), "min": "", "max": ""})

    section = None
    subsection = ""
    pending_prefix = None
    i = 0
    while i < len(body):
        raw = body[i]
        s = raw.strip()
        i += 1
        if is_noise_line(s):
            continue
        if s in SECTION_HEADERS:
            section, subsection, pending_prefix = s, "", None
            continue
        if s in SUBSECTIONS:
            subsection = s
            continue
        if s.startswith("Sestavina") or s.startswith("Enota/100 g") or s.startswith("Energijska vrednost") \
           or s.startswith("100 g užitnega dela") or s.startswith("Energijski delež") \
           or ("Povprečno" in s and "Min." in s):
            pending_prefix = None
            continue

        m = full_row_re.match(raw)
        if m:
            nutrient, unit, avg, mn, mx = m.groups()
            sec = "UŽITNI DEL" if nutrient.strip() == "Užitni del" else section
            nutrient_rows.append({"food_id": food_id, "section": sec, "subsection": subsection,
                                   "nutrient": nutrient.strip(), "unit": unit, "avg": avg, "min": mn, "max": mx})
            pending_prefix = None
            continue

        m2 = val_unit_only_re.match(raw)
        if m2:
            unit, avg, mn, mx = m2.groups()
            name = pending_prefix or ""
            if i < len(body):
                nxt = body[i].strip()
                if nxt and not is_noise_line(nxt) and nxt not in SECTION_HEADERS and nxt not in SUBSECTIONS \
                   and not full_row_re.match(body[i]) and not val_unit_only_re.match(body[i]):
                    name = (name + " " + nxt).strip()
                    i += 1
            nutrient_rows.append({"food_id": food_id, "section": section, "subsection": subsection,
                                   "nutrient": name, "unit": unit, "avg": avg, "min": mn, "max": mx})
            pending_prefix = None
            continue

        m2b = val_unit_single_re.match(raw)
        if m2b:
            unit, avg = m2b.groups()
            name = pending_prefix or ""
            if i < len(body):
                nxt = body[i].strip()
                if nxt and not is_noise_line(nxt) and nxt not in SECTION_HEADERS and nxt not in SUBSECTIONS \
                   and not full_row_re.match(body[i]) and not val_unit_only_re.match(body[i]) \
                   and not val_unit_single_re.match(body[i]):
                    name = (name + " " + nxt).strip()
                    i += 1
            nutrient_rows.append({"food_id": food_id, "section": section, "subsection": subsection,
                                   "nutrient": name, "unit": unit, "avg": avg, "min": "", "max": ""})
            pending_prefix = None
            continue

        m3 = name_unit_val_re.match(raw)
        if m3:
            nutrient, unit, avg = m3.groups()
            nutrient_rows.append({"food_id": food_id, "section": section, "subsection": subsection,
                                   "nutrient": nutrient.strip(), "unit": unit, "avg": avg, "min": "", "max": ""})
            pending_prefix = None
            continue

        m4 = name_val_re.match(raw)
        if m4:
            nutrient, avg = m4.groups()
            nutrient_rows.append({"food_id": food_id, "section": section, "subsection": subsection,
                                   "nutrient": nutrient.strip(), "unit": "", "avg": avg, "min": "", "max": ""})
            pending_prefix = None
            continue

        # plain text line: buffer as a name-wrap prefix for an upcoming value line
        pending_prefix = s

for r in nutrient_rows:
    r["nutrient"] = re.sub(r"\s+", " ", r["nutrient"]).strip()

with open(f"{DIR}/slovenian_meat_foods.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["food_id", "group", "category", "name_slo", "name_en", "name_latin"])
    w.writeheader()
    w.writerows(foods_rows)

with open(f"{DIR}/slovenian_meat_nutrients_long.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["food_id", "section", "subsection", "nutrient", "unit", "avg", "min", "max"])
    w.writeheader()
    w.writerows(nutrient_rows)

print(f"foods: {len(foods_rows)}")
print(f"nutrient rows: {len(nutrient_rows)}")
empty_names = [r for r in foods_rows if not r["name_slo"]]
print(f"foods with empty slo name: {len(empty_names)}")
for r in empty_names[:10]:
    print(r)
