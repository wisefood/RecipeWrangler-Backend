"""Read-only: count recipes passing each persona's hard filters (no query text)."""
import json, sys
from pathlib import Path
import requests
from recipe_wrangler.api.config import get_settings
from recipe_wrangler.tools.es_recipe_search import RecipeSearchConstraints, build_es_query

settings = get_settings()
personas = json.loads((Path(__file__).resolve().parents[2] / "evaluation/search/data/personas-2026-11-03/personas.json").read_text())["personas"]
url = f"{settings.elastic_url}/{settings.elastic_index}/_count"
total = requests.post(url, json={"query": {"match_all": {}}}, timeout=10).json()["count"]
print(f"index={settings.elastic_index} total={total}")
for p in personas:
    pr = p["profile"]
    c = RecipeSearchConstraints(
        diet_tags=pr["diet_tags"], exclude_allergens=pr["exclude_allergens"],
        nutrition_claims=pr.get("nutrition_claims", []), convenience=pr.get("convenience", []),
    )
    n = requests.post(url, json={"query": build_es_query(c)["query"]}, timeout=10).json()["count"]
    print(f"{p['id']:26s} eligible={n}")
