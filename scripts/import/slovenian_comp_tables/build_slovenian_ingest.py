"""Build and load the current Slovenian ingredient-composition table."""

import csv
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DIR = REPO_ROOT / "data" / "Slovenian-Comp-Tables"
PSQL = ["docker", "exec", "-i", "wisefood-postgres", "psql", "-U", "postgres", "-d", "nutrients"]

# --- category translations (English food_group) ---
PLANT_CATEGORY_EN = {
    "ŽITA": "cereals and grain products",
    "IZDELKI": "milled cereal products",
    "KRUH": "bread",
    "ZELENJAVA": "vegetables",
    "SADJE": "fruits",
    "OLJNICE": "oilseeds and olive oil",
}
MEAT_CATEGORY_EN = {
    "SVINJINA": "pork",
    "GOVEDINA": "beef",
    "TELETINA": "veal",
    "OVČETINA": "mutton and lamb",
    "KONJSKO MESO": "horse meat",
    "KOZJE MESO": "goat meat",
    "KUNČJE MESO": "rabbit meat",
    "IZDELKI IZ MESA KLAVNIH ŽIVALI": "meat products",
    "PETELINJE MESO": "rooster meat",
    "PIŠČANČJE MESO": "chicken meat",
    "PURANJE MESO": "turkey meat",
    "GOSJE MESO": "goose meat",
    "RAČJE MESO": "duck meat",
    "IZDELKI IZ PERUTNINE": "poultry products",
    "JELENJAD": "deer (venison)",
    "SRNJAD": "roe deer",
    "DIVJI PRAŠIČ": "wild boar",
    "DIVJI ZAJEC": "hare",
    "PERJAD": "game birds",
    "MORSKE RIBE": "sea fish",
    "SLADKOVODNE RIBE": "freshwater fish",
    "RAKI IN MEHKUŽCI": "crustaceans and molluscs",
    "IZDELKI IZ RIB, RAKOV IN MEHKUŽCEV": "fish, crustacean and mollusc products",
}

# --- crosswalk: slovenian_term -> (english_translation, eu_key_or_None) ---
crosswalk = {}
for row in csv.DictReader(open(f"{DIR}/nutrient_terms_crosswalk.csv", encoding="utf-8")):
    crosswalk[row["slovenian_term"]] = (row["english_translation"], row["eu_ingredient_nutrient"] or None)

# --- EU canonical unit per key (for unit conversion when a match exists), fetched live ---
out = subprocess.run(
    PSQL + ["-t", "-A", "-F", "|", "-c",
            'SELECT key, mode() WITHIN GROUP (ORDER BY value->>\'unit\') '
            'FROM "nutrients-ingredients-eu", jsonb_each(nutrients) AS kv(key, value) '
            'WHERE value->>\'unit\' IS NOT NULL AND value->>\'unit\' <> \'\' '
            'GROUP BY key;'],
    capture_output=True, text=True, check=True,
).stdout
eu_unit = {}
for line in out.splitlines():
    line = line.strip()
    if not line:
        continue
    key, unit = line.rsplit("|", 1)
    eu_unit[key] = unit

GRAM_SCALE = {"g": 1.0, "mg": 1e-3, "µg": 1e-6, "ug": 1e-6, "ng": 1e-9}


def parse_num(s):
    s = s.strip().rstrip("*")
    if not s or s == "-":
        return None
    s = s.lstrip("<").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def convert_unit(value, from_unit, to_unit):
    if from_unit == to_unit or from_unit not in GRAM_SCALE or to_unit not in GRAM_SCALE:
        return value, from_unit
    grams = value * GRAM_SCALE[from_unit]
    return grams / GRAM_SCALE[to_unit], to_unit


def build_nutrients(nutrient_rows_for_food):
    nutrients = {}
    for r in nutrient_rows_for_food:
        term = r["nutrient"]
        unit = r["unit"]
        value = parse_num(r["avg"])
        if value is None:
            continue

        if term == "Energijska vrednost, skupaj":
            key = "Energy" if unit == "kJ" else "Energy, kcal"
            nutrients[key] = {"unit": unit, "value": value}
            continue
        if term == "Užitni del":
            continue  # edible-portion %, not a composition nutrient

        translation, eu_key = crosswalk.get(term, (term, None))
        if eu_key:
            target_unit = eu_unit.get(eu_key, unit)
            conv_value, conv_unit = convert_unit(value, unit, target_unit)
            key = eu_key
            nutrients[key] = {"unit": conv_unit, "value": round(conv_value, 6)}
        else:
            key = translation
            nutrients[key] = {"unit": unit, "value": value}
    return nutrients


def load_food_rows(nutrients_csv):
    by_food = {}
    for r in csv.DictReader(open(f"{DIR}/{nutrients_csv}", encoding="utf-8")):
        by_food.setdefault(r["food_id"], []).append(r)
    return by_food


records = []

plant_foods = list(csv.DictReader(open(f"{DIR}/slovenian_foods.csv", encoding="utf-8")))
plant_nutrients = load_food_rows("slovenian_nutrients_long.csv")
for f in plant_foods:
    fid = f["food_id"]
    records.append({
        "id": f"slovenian_plant:{fid}",
        "food_name": f["name_en"],
        "source": "slovenian_plant",
        "country": "SI",
        "food_group": PLANT_CATEGORY_EN.get(f["category"], f["category"]),
        "nutrients": build_nutrients(plant_nutrients.get(fid, [])),
    })

meat_foods = list(csv.DictReader(open(f"{DIR}/slovenian_meat_foods.csv", encoding="utf-8")))
meat_nutrients = load_food_rows("slovenian_meat_nutrients_long.csv")
for f in meat_foods:
    fid = f["food_id"]
    records.append({
        "id": f"slovenian_meat:{fid}",
        "food_name": f["name_en"],
        "source": "slovenian_meat",
        "country": "SI",
        "food_group": MEAT_CATEGORY_EN.get(f["category"], f["category"]),
        "nutrients": build_nutrients(meat_nutrients.get(fid, [])),
    })

print(f"records: {len(records)}")
empty_name = [r for r in records if not r["food_name"].strip()]
print(f"empty food_name: {len(empty_name)}")
empty_nutrients = [r for r in records if not r["nutrients"]]
print(f"empty nutrients dict: {len(empty_nutrients)}")
dup_ids = len(records) - len(set(r["id"] for r in records))
print(f"duplicate ids: {dup_ids}")


def sql_str(s):
    return "'" + s.replace("'", "''") + "'"


sql_lines = [
    'CREATE TABLE IF NOT EXISTS "nutrients-ingredients-slovenian" ('
    "id text NOT NULL PRIMARY KEY, food_name text NOT NULL, source text NOT NULL, "
    "country text NOT NULL, food_group text, nutrients jsonb NOT NULL);",
    'CREATE INDEX IF NOT EXISTS "nutrients-ingredients-slovenian_food_name_idx" '
    'ON "nutrients-ingredients-slovenian" (food_name);',
    'CREATE INDEX IF NOT EXISTS "nutrients-ingredients-slovenian_source_idx" '
    'ON "nutrients-ingredients-slovenian" (source);',
    'TRUNCATE TABLE "nutrients-ingredients-slovenian";',
]
for r in records:
    vals = ", ".join([
        sql_str(r["id"]), sql_str(r["food_name"]), sql_str(r["source"]), sql_str(r["country"]),
        sql_str(r["food_group"]) if r["food_group"] else "NULL",
        sql_str(json.dumps(r["nutrients"], ensure_ascii=False)) + "::jsonb",
    ])
    sql_lines.append(
        f'INSERT INTO "nutrients-ingredients-slovenian" '
        f"(id, food_name, source, country, food_group, nutrients) VALUES ({vals});"
    )

result = subprocess.run(PSQL + ["-v", "ON_ERROR_STOP=1"], input="\n".join(sql_lines),
                         capture_output=True, text=True)
if result.returncode != 0:
    raise RuntimeError(f"psql load failed:\n{result.stderr}")
print("loaded into nutrients-ingredients-slovenian")
