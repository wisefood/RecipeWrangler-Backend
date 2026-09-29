# -*- coding: utf-8 -*-
"""Build the reviewed Slovenian-to-runtime nutrient crosswalk CSV."""

import csv
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DIR = REPO_ROOT / "data" / "Slovenian-Comp-Tables"

# (slovenian_term, english_translation, eu_ingredient_nutrient_match_or_None, postgres_recipe_profile_field_or_None)
# eu_ingredient_nutrient is matched against the DISTINCT keys actually present in the
# "nutrients-ingredients-eu" Postgres table (CIQUAL/CoFID/NEVO combined, 8148 rows) --
# this is the table this project actually queries. No USDA table is used anywhere live.
ROWS = [
    # --- main components ---
    ("Voda", "Water", "Water", None),
    ("Skupne beljakovine", "Total protein", "Protein", "protein_g"),
    ("Skupni dušik", "Total nitrogen", None, None),
    ("Skupne maščobe", "Total fat", "Total lipid (fat)", "fat_g"),
    ("Skupni ogljikovi hidrati", "Total carbohydrates", "Carbohydrate, by difference", "carbohydrate_g"),
    ("Izkoristljivi ogljikovi hidrati", "Available (utilizable) carbohydrates", None, None),
    ("Skupna prehranska vlaknina", "Total dietary fibre", "Fiber, total dietary", "fibre_g"),
    ("Topna prehranska vlaknina", "Soluble dietary fibre", None, None),
    ("Netopna prehranska vlaknina", "Insoluble dietary fibre", None, None),
    ("Pepel", "Ash", "Ash", None),
    ("Užitni del", "Edible portion", None, None),
    ("Energijska vrednost, skupaj", "Total energy value", "Energy", "energy_kcal"),

    # --- elements ---
    ("Kalij", "Potassium", "Potassium, K", None),
    ("Kalcij", "Calcium", "Calcium, Ca", None),
    ("Fosfor", "Phosphorus", "Phosphorus, P", None),
    ("Magnezij", "Magnesium", "Magnesium, Mg", None),
    ("Mangan", "Manganese", "Manganese, Mn", None),
    ("Železo", "Iron", "Iron, Fe", None),
    ("Cink", "Zinc", "Zinc, Zn", None),
    ("Selen", "Selenium", "Selenium, Se", None),
    ("Baker", "Copper", "Copper, Cu", None),
    ("Natrij", "Sodium", "Sodium, Na", "sodium_mg"),
    ("Natrijev klorid", "Sodium chloride (salt)", "Salt", "salt_g"),
    ("Kuhinjska sol (NaCl)", "Table salt (NaCl)", "Salt", "salt_g"),
    ("Fluor", "Fluorine", None, None),
    ("Jod", "Iodine", "Iodine, I", None),
    ("Brom", "Bromine", None, None),
    ("Rubidij", "Rubidium", None, None),
    ("Stroncij", "Strontium", None, None),
    ("Molibden", "Molybdenum", None, None),
    ("Žveplo", "Sulfur", None, None),
    ("Klor", "Chlorine", "Chloride", None),
    ("Kobalt", "Cobalt", None, None),
    ("Krom", "Chromium", None, None),
    ("Nikelj", "Nickel", None, None),

    # --- vitamins ---
    ("Tiamin", "Thiamin (vitamin B1)", "Thiamin", None),
    ("Tiamin (vitamin B1)", "Thiamin (vitamin B1)", "Thiamin", None),
    ("Riboflavin", "Riboflavin (vitamin B2)", "Riboflavin", None),
    ("Riboflavin (vitamin B2)", "Riboflavin (vitamin B2)", "Riboflavin", None),
    ("Pantotenska kislina", "Pantothenic acid (vitamin B5)", "Pantothenic acid", None),
    ("Pantotenska kislina (vitamin B5)", "Pantothenic acid (vitamin B5)", "Pantothenic acid", None),
    ("Biotin", "Biotin (vitamin B7)", "Biotin", None),
    ("Biotin (vitamin B7)", "Biotin (vitamin B7)", "Biotin", None),
    ("Folna kislina", "Folic acid (vitamin B9)", "Folic acid", None),
    ("Folna kislina (vitamin B9)", "Folic acid (vitamin B9)", "Folic acid", None),
    ("Vitamin B6", "Vitamin B6", "Vitamin B-6", None),
    ("Vitamin B12", "Vitamin B12", "Vitamin B-12", None),
    ("Vitamin C", "Vitamin C", "Vitamin C, total ascorbic acid", None),
    ("Askorbinska kislina", "Ascorbic acid (vitamin C)", "Vitamin C, total ascorbic acid", None),
    ("Vitamin A (retinol ekvivalent)", "Vitamin A (retinol equivalent)", "Vitamin A, RAE", None),
    ("Vitamin D", "Vitamin D", "Vitamin D (D2 + D3)", None),
    ("Vitamin E (aktivni)", "Vitamin E (active)", "Vitamin E (alpha-tocopherol)", None),
    ("Vitamin K", "Vitamin K", "Vitamin K (phylloquinone)", None),

    # --- sugars ---
    ("Fruktoza", "Fructose", "Fructose", None),
    ("Glukoza", "Glucose", "Glucose (dextrose)", None),
    ("Maltoza", "Maltose", "Maltose", None),
    ("Laktoza", "Lactose", "Lactose", None),
    ("Saharoza", "Sucrose", "Sucrose", None),
    ("Sugars, total (computed)", "Total sugars (computed: sum of Fruktoza+Glukoza+Maltoza+Laktoza+Saharoza)", "Sugars, total", "sugar_g"),

    # --- fatty acids (EU key format: spaced "n:n label", no cis/trans split except where shown) ---
    ("C 12:0 lavrinska kislina", "Lauric acid (C12:0)", "12:0", None),
    ("C 14:0 miristinska kislina", "Myristic acid (C14:0)", "14:0", None),
    ("C 14:1 cis-9 miristooleinska kislina", "Myristoleic acid (C14:1 cis-9)", None, None),
    ("C 16:0 palmitinska kislina", "Palmitic acid (C16:0)", "16:0", None),
    ("C 16:1 cis-7 palmitooleinska kislina", "Palmitoleic acid (C16:1 cis-7)", "16:1 undifferentiated", None),
    ("C 16:1 cis-9 palmitooleinska kislina", "Palmitoleic acid (C16:1 cis-9)", "16:1 undifferentiated", None),
    ("C 17:0 margarinska kislina", "Margaric acid (C17:0)", "17:0", None),
    ("C 17:1 heptadekaenojska kislina", "Heptadecenoic acid (C17:1)", None, None),
    ("C 18:0 stearinska kislina", "Stearic acid (C18:0)", "18:0", None),
    ("C 18:1 cis-9 oleinska kislina", "Oleic acid (C18:1 cis-9)", "18:1 undifferentiated", None),
    ("C 18:1 trans-9 elaidinska kislina", "Elaidic acid (C18:1 trans-9)", None, None),
    ("C 18:1 trans-oktadecenjska kislina", "trans-Octadecenoic acid (C18:1 trans)", None, None),
    ("C 18:2 cis-9, cis-12 linolna kislina", "Linoleic acid (C18:2 cis-9,cis-12)", "18:2 n-6 c,c", None),
    ("C 18:2 cis-9, trans-11 konjugirana linolna kislina", "Conjugated linoleic acid, CLA (C18:2 cis-9,trans-11)", None, None),
    ("C 18:2 trans linolna kislina", "trans-Linoleic acid (C18:2 trans)", None, None),
    ("C 18:2 trans-10, cis-12 konjugirana linolna kislina", "Conjugated linoleic acid, CLA (C18:2 trans-10,cis-12)", None, None),
    ("C 18:3 n-3 alfa-linolenska kislina", "alpha-Linolenic acid, ALA (C18:3 n-3)", "18:3 n-3 c,c,c (ALA)", None),
    ("C 18:3 n-3 α-linolenska kislina", "alpha-Linolenic acid, ALA (C18:3 n-3)", "18:3 n-3 c,c,c (ALA)", None),
    ("C 18:3 n-6 γ-linolenska kislina", "gamma-Linolenic acid (C18:3 n-6)", "18:3 n-6 c,c,c", None),
    ("C 18:3 trans linolenska kislina", "trans-Linolenic acid (C18:3 trans)", None, None),
    ("C 18:4 n-3 stearidonska kislina", "Stearidonic acid (C18:4 n-3)", "18:4", None),
    ("C 20:0 arahidinska kislina", "Arachidic acid (C20:0)", "20:0", None),
    ("C 20:1 gadoleinska kislina", "Gadoleic acid (C20:1)", "20:1", None),
    ("C 20:4 n-6 arahidonska kislina", "Arachidonic acid (C20:4 n-6)", "20:4 n-6", None),
    ("C 20:5 n-3 EPA eikozapentaenojska kislina", "EPA, eicosapentaenoic acid (C20:5 n-3)", "20:5 n-3 (EPA)", None),
    ("C 22:0 behenska kislina", "Behenic acid (C22:0)", "22:0", None),
    ("C 22:1 cis-13 eruka kislina", "Erucic acid (C22:1 cis-13)", "22:1 undifferentiated", None),
    ("C 22:5 n-3 DPA dokozapentaenojska kislina", "DPA, docosapentaenoic acid (C22:5 n-3)", "22:5 n-3 (DPA)", None),
    ("C 22:6 n-3 DHA dokozaheksaenojska kislina", "DHA, docosahexaenoic acid (C22:6 n-3)", "22:6 n-3 (DHA)", None),
    ("C 24:0 lignocerska kislina", "Lignoceric acid (C24:0)", "24:0", None),
    ("C 24:1 cis-15 nervonska kislina", "Nervonic acid (C24:1 cis-15)", "24:1 c", None),
    ("Nasičene maščobne kisline (S)", "Total saturated fatty acids", "Fatty acids, total saturated", "saturated_fat_g"),
    ("Enkrat nenasičene maščobne kisline", "Total monounsaturated fatty acids", "Fatty acids, total monounsaturated", None),
    ("Večkrat nenasičene maščobne kisline (P)", "Total polyunsaturated fatty acids", "Fatty acids, total polyunsaturated", None),
    ("n-3 maščobne kisline", "Total n-3 fatty acids", None, None),
    ("n-6 maščobne kisline", "Total n-6 fatty acids", None, None),
    ("Razmerje n-6/n-3", "n-6/n-3 ratio", None, None),
    ("Razmerje P/S", "Polyunsaturated/saturated (P/S) ratio", None, None),
    ("Indeks aterogenosti (IA)", "Atherogenic index (AI)", None, None),

    # --- sterols ---
    ("Holesterol", "Cholesterol", "Cholesterol", None),
    ("Kampesterol", "Campesterol", "Campesterol", None),
    ("Kampestanol", "Campestanol", None, None),
    ("Stigmasterol", "Stigmasterol", "Stigmasterol", None),
    ("Beta-sitosterol", "Beta-sitosterol", "Beta-sitosterol", None),
    ("Sitostanol", "Sitostanol", None, None),
    ("Skupni steroli", "Total sterols", "Phytosterols", None),
    ("D-5-avenasterol", "Delta-5-avenasterol", None, None),
    ("D-5,24-stigmastadienol", "Delta-5,24-stigmastadienol", None, None),
    ("D-7-avenasterol", "Delta-7-avenasterol", None, None),
    ("D-7-stigmasterol", "Delta-7-stigmasterol", None, None),
    ("Navidezni beta-sitosterol", "Apparent beta-sitosterol", None, None),
    ("Klerosterol", "Clerosterol", None, None),
    ("Brasikasterol", "Brassicasterol", None, None),
    ("24-metilenholesterol", "24-Methylenecholesterol", None, None),
    ("Skupni eritrodiol in uvaol", "Total erythrodiol and uvaol", None, None),
    ("Eritordiol", "Erythrodiol", None, None),
    ("Uvaol", "Uvaol", None, None),

    # --- amino acids (EU/CIQUAL-CoFID-NEVO table does not track individual amino acids at all) ---
    ("Alanin", "Alanine", None, None),
    ("Arginin", "Arginine", None, None),
    ("Asparaginska kislina", "Aspartic acid", None, None),
    ("Cistein", "Cysteine", None, None),
    ("Fenilalanin", "Phenylalanine", None, None),
    ("Glicin", "Glycine", None, None),
    ("Glutaminska kislina", "Glutamic acid", None, None),
    ("Histidin", "Histidine", None, None),
    ("Izolevcin", "Isoleucine", None, None),
    ("Levcin", "Leucine", None, None),
    ("Lizin", "Lysine", None, None),
    ("Metionin", "Methionine", None, None),
    ("Prolin", "Proline", None, None),
    ("Serin", "Serine", None, None),
    ("Treonin", "Threonine", None, None),
    ("Tirozin", "Tyrosine", None, None),
    ("Triptofan", "Tryptophan", None, None),
    ("Valin", "Valine", None, None),
    ("Hidroksiprolin", "Hydroxyproline", None, None),

    # --- organic acids (plant book only) ---
    ("Citronska kislina", "Citric acid", None, None),
    ("Jabolčna kislina", "Malic acid", None, None),
    ("Mlečna kislina", "Lactic acid", None, None),
    ("Salicilna kislina", "Salicylic acid", None, None),
    ("Skupna oksalna kislina", "Total oxalic acid", None, None),

    # --- other / polyphenols (plant book only) ---
    ("Skupni biofenoli", "Total biophenols", None, None),
    ("Hidroksitirisol", "Hydroxytyrosol", None, None),
    ("Tirosol", "Tyrosol", None, None),
]

with open(f"{DIR}/nutrient_terms_crosswalk.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["slovenian_term", "english_translation", "eu_ingredient_nutrient", "postgres_recipe_profile_field"])
    for row in ROWS:
        w.writerow([c if c is not None else "" for c in row])

# ---- coverage check against the actual extracted union (regenerated live) ----
plant_n = set(r["nutrient"] for r in csv.DictReader(open(f"{DIR}/slovenian_nutrients_long.csv", encoding="utf-8")))
meat_n = set(r["nutrient"] for r in csv.DictReader(open(f"{DIR}/slovenian_meat_nutrients_long.csv", encoding="utf-8")))
union = plant_n | meat_n
covered = {r[0] for r in ROWS}
missing = sorted(union - covered)
extra = sorted(covered - union)
print(f"crosswalk rows: {len(ROWS)}")
print(f"union terms: {len(union)}")
print(f"missing (in data, not in crosswalk): {len(missing)}")
for m in missing:
    print("  MISSING:", m)
print(f"extra (in crosswalk, not in data): {len(extra)}")
for e in extra:
    print("  EXTRA:", e)

out = subprocess.run(
    ["docker", "exec", "wisefood-postgres", "psql", "-U", "postgres", "-d", "nutrients", "-t", "-A",
     "-c", "SELECT DISTINCT jsonb_object_keys(nutrients) FROM \"nutrients-ingredients-eu\";"],
    capture_output=True, text=True, check=True,
).stdout
eu_keys = set(l.strip() for l in out.splitlines() if l.strip())
bad = [(slo, eu) for slo, en, eu, pg in ROWS if eu and eu not in eu_keys]
print(f"EU-key mismatches: {len(bad)}")
for slo, eu in bad:
    print("  BAD MATCH:", slo, "->", eu)

with_eu = sum(1 for r in ROWS if r[2])
with_pg = sum(1 for r in ROWS if r[3])
print(f"eu matched: {with_eu}/{len(ROWS)}")
print(f"postgres matched: {with_pg}/{len(ROWS)}")
