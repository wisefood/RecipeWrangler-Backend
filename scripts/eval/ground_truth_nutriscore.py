"""Compute Hungarian/Slovenian reference Nutri-Score from ONLY their own ground-truth
weight data (ESSRG ingredient_details / Slovenian xlsx Sestavine) -- no borrowed EU weight,
no borrowed fruit_percentage. Pass --write to persist to Postgres; default is dry-run."""
import sys, os, json, csv
WRITE = "--write" in sys.argv
GRADE_TO_LABEL = {"A": "Nutriscore_A", "B": "Nutriscore_B", "C": "Nutriscore_C", "D": "Nutriscore_D", "E": "Nutriscore_E"}
GRADE_TO_COLOR = {"A": "dark green", "B": "green", "C": "yellow", "D": "orange", "E": "dark orange"}
sys.path.insert(0, "/home/karvanitis/RecipeWrangler-Backend/src")
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values
import psycopg2, psycopg2.extras
import openpyxl
from neo4j import GraphDatabase
from dotenv import load_dotenv
load_dotenv("/home/karvanitis/RecipeWrangler-Backend/.env")

conn = psycopg2.connect(
    host=os.getenv("NUTRITION_HOST", "localhost"), port=os.getenv("NUTRITION_PORT", "5432"),
    dbname=os.getenv("NUTRITION_DB", "nutrients"), user=os.getenv("NUTRITION_USER", "postgres"),
    password=os.getenv("NUTRITION_PASSWORD", "postgres"),
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

driver = GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                               auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "password123")))


def get_serves(db_source):
    with driver.session() as s:
        return {r["rid"]: float(r["serves"] or 1) for r in s.run(
            "MATCH (r:Recipe {source:$s}) RETURN r.recipe_id AS rid, r.serves AS serves", s=db_source)}


def run(label, db_source, ref_source, calc_source, own_weight_fn):
    cur.execute("""SELECT recipe_id, title, total_nutrients_per_serving, nutri_score->>'nutri_score' AS grade
        FROM "nutrients-recipe-profiles" WHERE source=%s AND nutrition_source=%s AND nutri_score IS NOT NULL""",
        (db_source, ref_source))
    ref_rows = {r["recipe_id"]: r for r in cur.fetchall()}
    cur.execute("""SELECT recipe_id, nutri_score FROM "nutrients-recipe-profiles"
        WHERE source=%s AND nutrition_source=%s""", (db_source, calc_source))
    calc_rows = {r["recipe_id"]: r for r in cur.fetchall()}

    own_weights = own_weight_fn()  # {recipe_id: total_weight_g, for the WHOLE recipe}
    serves_map = get_serves(db_source)

    same = flips_better = flips_worse = skipped = 0
    match_stored = match_new = 0
    n = 0
    out_rows = []
    order = "ABCDE"
    for rid, row in ref_rows.items():
        total_w_recipe = own_weights.get(rid)
        serves = serves_map.get(rid, 1.0)
        total_w = (total_w_recipe / serves) if total_w_recipe else None
        if not total_w or total_w <= 0:
            skipped += 1
            continue
        nut = row["total_nutrients_per_serving"] or {}
        kcal = nut.get("energy_kcal") or 0
        values = {
            "energy": kcal * 4.184 / total_w * 100.0,
            "sugar": (nut.get("sugar_g") or 0) / total_w * 100.0,
            "saturated_fats": (nut.get("saturated_fat_g") or 0) / total_w * 100.0,
            "sodium": (nut.get("sodium_mg") or 0) / total_w * 100.0,
            "fibers": (nut.get("fibre_g") or 0) / total_w * 100.0,
            "proteins": (nut.get("protein_g") or 0) / total_w * 100.0,
            "fruit_percentage": 0.0,  # genuinely unknown from ground truth alone
        }
        try:
            result = compute_nutri_score_breakdown_from_values(values, "solid")
        except Exception:
            skipped += 1
            continue
        new_grade = str(result.get("nutri_score") or "").replace("Nutriscore_", "").strip().upper()
        old_grade = (row["grade"] or "").replace("Nutriscore_", "").strip().upper()
        calc_grade = None
        if rid in calc_rows:
            calc_grade = str((calc_rows[rid]["nutri_score"] or {}).get("nutri_score") or "").replace("Nutriscore_", "").strip().upper()
        if new_grade == old_grade:
            same += 1
        elif order.index(new_grade) < order.index(old_grade):
            flips_better += 1
        else:
            flips_worse += 1
        if calc_grade:
            n += 1
            if old_grade == calc_grade:
                match_stored += 1
            if new_grade == calc_grade:
                match_new += 1
        out_rows.append({"recipe_id": rid, "title": row["title"], "own_weight_g": round(total_w, 1),
                          "stored_grade": old_grade, "ground_truth_only_grade": new_grade, "calc_grade": calc_grade})
        if WRITE:
            ns = {"nutri_score": GRADE_TO_LABEL[new_grade], "score": result.get("score"),
                  "color": GRADE_TO_COLOR[new_grade]}
            cur.execute(
                """UPDATE "nutrients-recipe-profiles" SET nutri_score=%s, updated_at=NOW()
                   WHERE recipe_id=%s AND source=%s AND nutrition_source=%s""",
                (json.dumps(ns), rid, db_source, ref_source),
            )

    print(f"{label}: total={len(ref_rows)} skipped={skipped} same={same} flips_better={flips_better} flips_worse={flips_worse}")
    print(f"  agreement stored-vs-calc: {match_stored}/{n} = {match_stored/n*100:.1f}%")
    print(f"  agreement ground-truth-only-vs-calc: {match_new}/{n} = {match_new/n*100:.1f}%")
    out_path = f"/tmp/claude-1001/-home-karvanitis-RecipeWrangler-Backend/dcdd24f6-9eab-44d7-8107-d0c799548319/scratchpad/{label}_ground_truth_nutriscore.csv"
    w = csv.DictWriter(open(out_path, "w", newline=""), fieldnames=list(out_rows[0].keys()))
    w.writeheader()
    w.writerows(out_rows)
    print(f"  wrote {out_path}")


def hungarian_weights():
    data = json.loads(open("/home/karvanitis/RecipeWrangler-Backend/data/ESSRG/ESSRG_recipes_clean.json").read())
    recs = data if isinstance(data, list) else list(data.values())
    out = {}
    for r in recs:
        rid = r.get("recipe_id") or r.get("id")
        details = r.get("ingredient_details") or []
        w = sum(float(d["weight_g"]) for d in details if d.get("weight_g") is not None and float(d["weight_g"]) > 0)
        if w > 0:
            out[rid] = w
    return out


def slovenian_weights():
    wb = openpyxl.load_workbook("/home/karvanitis/RecipeWrangler-Backend/data/Slovenia/Slovenian_Recipes.xlsx", read_only=True)
    ws = wb["Sestavine"]
    out = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        _, _, _, _, recid, amount, unit = row
        if recid is None or amount is None:
            continue
        out[recid] = out.get(recid, 0.0) + float(amount)
    return out


run("hungarian", "Curated Hungarian Recipes", "planeat", "hungarian", hungarian_weights)
run("slovenian", "Curated Slovenian Recipes", "slovenian_original", "slovenian", slovenian_weights)
if WRITE:
    conn.commit()
    print("Committed.")
driver.close()
