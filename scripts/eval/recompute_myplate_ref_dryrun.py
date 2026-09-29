import sys, os, json, csv
sys.path.insert(0, "/home/karvanitis/RecipeWrangler-Backend/src")
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values
import psycopg2, psycopg2.extras
from dotenv import load_dotenv
load_dotenv("/home/karvanitis/RecipeWrangler-Backend/.env")

conn = psycopg2.connect(
    host=os.getenv("NUTRITION_HOST", "localhost"), port=os.getenv("NUTRITION_PORT", "5432"),
    dbname=os.getenv("NUTRITION_DB", "nutrients"), user=os.getenv("NUTRITION_USER", "postgres"),
    password=os.getenv("NUTRITION_PASSWORD", "postgres"),
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("""SELECT recipe_id, title, total_nutrients_per_serving, nutri_score->>'nutri_score' AS grade
FROM "nutrients-recipe-profiles" WHERE source='Curated Hungarian Recipes' AND nutrition_source='planeat' AND nutri_score IS NOT NULL""")
myplate_rows = {r["recipe_id"]: r for r in cur.fetchall()}

cur.execute("""SELECT recipe_id, nutrition_profiling_details, nutri_score
FROM "nutrients-recipe-profiles" WHERE source='Curated Hungarian Recipes' AND nutrition_source='hungarian'""")
eu_rows = {r["recipe_id"]: r for r in cur.fetchall()}

cur.execute("MATCH (r:Recipe {source:'Curated Hungarian Recipes'}) RETURN r.recipe_id AS rid, r.serves AS serves" if False else "SELECT 1")

# serves from Neo4j
from neo4j import GraphDatabase
driver = GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                               auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "password123")))
with driver.session() as s:
    serves_map = {r["rid"]: float(r["serves"] or 1) for r in s.run(
        "MATCH (r:Recipe {source:'Curated Hungarian Recipes'}) RETURN r.recipe_id AS rid, r.serves AS serves")}
driver.close()

def fruit_pct(eu_row):
    ns = eu_row.get("nutri_score") or {}
    try:
        return ns["positive_points"]["items"]["fruit_percentage"]["value_per_100g"]
    except Exception:
        return 0.0

rows_out = []
flips_to_better = 0
flips_to_worse = 0
same = 0
skipped = 0
for rid, mrow in myplate_rows.items():
    eu = eu_rows.get(rid)
    if not eu or not eu.get("nutrition_profiling_details"):
        skipped += 1
        continue
    items = eu["nutrition_profiling_details"]
    total_w = sum(float(it["weight_g"]) for it in items if it.get("weight_g") not in (None, 0) and float(it["weight_g"]) > 0)
    serves = serves_map.get(rid, 1.0)
    if total_w <= 0 or serves <= 0:
        skipped += 1
        continue
    per_serving_w = total_w / serves
    nut = mrow["total_nutrients_per_serving"] or {}
    kcal = nut.get("energy_kcal") or 0
    values = {
        "energy": kcal * 4.184 / per_serving_w * 100.0,
        "sugar": (nut.get("sugar_g") or 0) / per_serving_w * 100.0,
        "saturated_fats": (nut.get("saturated_fat_g") or 0) / per_serving_w * 100.0,
        "sodium": (nut.get("sodium_mg") or 0) / per_serving_w * 100.0,
        "fibers": (nut.get("fibre_g") or 0) / per_serving_w * 100.0,
        "proteins": (nut.get("protein_g") or 0) / per_serving_w * 100.0,
        "fruit_percentage": fruit_pct(eu),
    }
    try:
        result = compute_nutri_score_breakdown_from_values(values, "solid")
    except Exception as exc:
        skipped += 1
        continue
    new_grade = str(result.get("nutri_score") or "").replace("Nutriscore_", "").strip().upper()
    old_grade = (mrow["grade"] or "").replace("Nutriscore_", "").strip().upper()
    calc_grade = None
    eu_ns = eu.get("nutri_score") or {}
    calc_grade = str(eu_ns.get("nutri_score") or "").replace("Nutriscore_", "").strip().upper()
    order = "ABCDE"
    if new_grade == old_grade:
        same += 1
    elif order.index(new_grade) < order.index(old_grade):
        flips_to_better += 1
    else:
        flips_to_worse += 1
    rows_out.append({
        "recipe_id": rid, "title": mrow["title"], "stored_reference_grade": old_grade,
        "recomputed_reference_grade": new_grade, "calc_grade": calc_grade,
        "per_serving_w_used": round(per_serving_w, 1),
    })

print(f"total={len(myplate_rows)} skipped={skipped} same={same} flips_to_better={flips_to_better} flips_to_worse={flips_to_worse}")
w = csv.DictWriter(open("/tmp/claude-1001/-home-karvanitis-RecipeWrangler-Backend/dcdd24f6-9eab-44d7-8107-d0c799548319/scratchpad/myplate_ref_recompute.csv", "w", newline=""),
                    fieldnames=["recipe_id", "title", "stored_reference_grade", "recomputed_reference_grade", "calc_grade", "per_serving_w_used"])
w.writeheader()
w.writerows(rows_out)

# new agreement rate: recomputed_reference vs calc
match_recomputed = sum(1 for r in rows_out if r["recomputed_reference_grade"] == r["calc_grade"])
match_stored = sum(1 for r in rows_out if r["stored_reference_grade"] == r["calc_grade"])
print(f"agreement stored-vs-calc: {match_stored}/{len(rows_out)} = {match_stored/len(rows_out)*100:.1f}%")
print(f"agreement recomputed-vs-calc: {match_recomputed}/{len(rows_out)} = {match_recomputed/len(rows_out)*100:.1f}%")
