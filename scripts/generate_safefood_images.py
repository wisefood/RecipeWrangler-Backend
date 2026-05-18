"""
Generate FLUX.1-dev images locally for Irish SafeFood recipes that have no image_url.
Loads the model once, runs all missing recipes, updates Neo4j image_url.
"""

import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import torch
from diffusers import FluxPipeline
from neo4j import GraphDatabase

IMAGE_DIR = Path("data/Irish_SafeFood/images")
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_URL_PREFIX = "/static/data/Irish_SafeFood/images"

driver = GraphDatabase.driver(os.getenv('NEO4J_URI'), auth=('neo4j', os.getenv('NEO4J_PASSWORD', 'neo4j')))

def get_missing():
    with driver.session() as s:
        return s.run("""
            MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
            WHERE r.source = 'Irish_SafeFood'
              AND r.image_url IS NULL
            WITH r, collect(i.name) as ingredients
            RETURN r.recipe_id, r.title, ingredients, r.instructions
        """).data()

def update_image_url(recipe_id: str, url: str):
    with driver.session() as s:
        s.run("MATCH (r:Recipe {recipe_id:$rid}) SET r.image_url=$url", rid=recipe_id, url=url)

def main():
    missing = get_missing()
    print(f"Recipes without images: {len(missing)}")
    if not missing:
        print("Nothing to do.")
        return

    print("Loading FLUX.1-dev...", flush=True)
    pipe = FluxPipeline.from_pretrained(
        "black-forest-labs/FLUX.1-dev",
        torch_dtype=torch.bfloat16,
        token=os.getenv("HUGGING_FACE_HUB_TOKEN"),
    )
    pipe.enable_sequential_cpu_offload()
    print("Model loaded.\n", flush=True)

    for i, r in enumerate(missing):
        title = r['r.title']
        rid   = r['r.recipe_id']
        ings  = ", ".join(r['ingredients'][:12])

        out_path  = IMAGE_DIR / f"{rid}.png"

        if out_path.exists():
            print(f"[{i+1}/{len(missing)}] Already exists: {out_path.name}")
            update_image_url(rid, f"{IMAGE_URL_PREFIX}/{out_path.name}")
            continue

        print(f"[{i+1}/{len(missing)}] {title}...", end=' ', flush=True)
        prompt = (
            f"Professional food photography for a recipe book. "
            f"{title}. "
            f"Key ingredients: {ings}. "
            f"Realistic, appetising, soft natural lighting, shallow depth of field, "
            f"beautifully plated on a clean white plate, top-down angle, "
            f"high resolution, studio quality."
        )
        try:
            image = pipe(prompt, height=768, width=768, guidance_scale=3.5, num_inference_steps=28).images[0]
            image.save(str(out_path))
            update_image_url(rid, f"{IMAGE_URL_PREFIX}/{out_path.name}")
            print("done")
        except Exception as e:
            print(f"ERROR: {e}")

    driver.close()
    print(f"\nDone. Images saved to {IMAGE_DIR}")

if __name__ == "__main__":
    main()
