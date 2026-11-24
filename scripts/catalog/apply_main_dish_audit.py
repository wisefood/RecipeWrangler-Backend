"""Apply reviewed demotions from the audit CSV (no model). Dry run unless --apply."""
import argparse, csv, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import audit_main_dish_claims as A

# Owner review 2026-11-24: likely real mains stay main-dish.
KEEP = [
    "Tortellini", "Mushroom ravioli", "Creamy pesto ravioli", "Salsa verde pasta", "Spaghetti with marinara",
    "Pappardelle", "conchiglie", "Spinach and black olive sauce with pasta", "Pasta with tomato and mascarpone",
    "Spaghetti with Quick Meat Sauce", "Italian meatballs", "Beef olives", "sausage rolls", "Chicken pick",
    "Brussels Sprouts with Mushroom Sauce", "Mushrooms with puttanesca", "Potato Pals", "Hiding Rabbits",
    "Zombie feet", "fritters", "Fishcakes", "Falafels", "Jack o", "Calamari", "Basil pesto with risoni",
    "Mini prawn tortillas", "Spoon dumplings", "Scallops, apple",
]

p = argparse.ArgumentParser()
p.add_argument("csv")
p.add_argument("--apply", action="store_true")
a = p.parse_args()
rows = [r for r in csv.DictReader(open(a.csv)) if not any(k.lower() in r["title"].lower() for k in KEEP)]
print(len(rows), "to demote")
entity = A.recipe_entity()
run_id = f"main-dish-audit-{int(time.time())}"
for r in rows:
    doc = entity.get(r["recipe_id"])
    new = r["after"].split("+")
    if not doc or "main-dish" not in (doc.get("course_types") or []):
        print("skip", r["recipe_id"], r["title"]); continue
    if a.apply:
        A.apply_change(entity, doc, new, {"confidence": float(r["confidence"])}, run_id=run_id)
    print("ok" if a.apply else "dry", r["recipe_id"], r["after"], r["title"])
