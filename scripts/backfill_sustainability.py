#!/usr/bin/env python3
"""
Backfill sustainability metrics for recipes in Postgres that have nutrition but no sustainability data.
Extracts ingredient weights from the existing trace and calls the SustainabilityCalculator.
"""

import os
import sys
import json
from pathlib import Path
from typing import Optional

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text
from recipe_wrangler.utils.nutrition_postgres import get_connection, _get_config
from recipe_wrangler.tools.sustainability_calculator import sustainability_tool_chroma

def backfill_sustainability():
    cfg = _get_config()
    schema = cfg["schema"]
    table = cfg["profiles_table"]

    # 1. Fetch recipes that need backfilling (streaming)
    query = text(f"""
        SELECT recipe_id, nutrition_source, title, nutrition_profiling_details, total_nutrients, total_nutrients_per_serving, trace
        FROM "{schema}"."{table}"
        WHERE total_sustainability IS NULL
    """)

    try:
        with get_connection() as conn:
            # Using execution_options(yield_per=100) for streaming
            result = conn.execution_options(yield_per=100).execute(query)
            
            count = 0
            for row in result:
                count += 1
                recipe_id = row.recipe_id
                source = row.nutrition_source
                title = row.title
                details = row.nutrition_profiling_details
                total_nutr = row.total_nutrients or {}
                per_serving_nutr = row.total_nutrients_per_serving or {}
                trace = row.trace or {}
                
                if not details:
                    continue

                # 2. Extract names and weights
                names = []
                weights = []
                for item in details:
                    if "ingredient" in item and "weight_g" in item:
                        names.append(item["ingredient"])
                        weights.append(float(item["weight_g"]))

                if not names:
                    continue

                # 3. Infer serves
                serves = 1.0
                try:
                    energy_total = total_nutr.get("energy_kcal") or total_nutr.get("Energy")
                    energy_per = per_serving_nutr.get("energy_kcal") or per_serving_nutr.get("Energy")
                    if energy_total and energy_per and float(energy_per) > 0:
                        serves = float(energy_total) / float(energy_per)
                except Exception:
                    pass

                # 4. Call sustainability tool
                try:
                    res = sustainability_tool_chroma.invoke({
                        "title": title,
                        "ingredient_names": names,
                        "weights": weights,
                        "serves": serves,
                        "min_similarity": 0.5
                    })
                except Exception as e:
                    print(f"[{count}] Error profiling {recipe_id}: {e}")
                    continue

                # 5. Update database
                update_query = text(f"""
                    UPDATE "{schema}"."{table}"
                    SET 
                        total_sustainability = :total_sustainability,
                        total_sustainability_per_serving = :total_sustainability_per_serving,
                        sustainability_per_kg = :sustainability_per_kg,
                        sustainability_profiling_details = CAST(:sustainability_profiling_details AS jsonb),
                        trace = CAST(:trace AS jsonb),
                        updated_at = now()
                    WHERE recipe_id = :recipe_id AND nutrition_source = :nutrition_source
                """)

                if "profiling" in trace:
                    trace["profiling"]["sustainability_profiling_details"] = res["details"]
                
                params = {
                    "total_sustainability": res["total_sustainability"],
                    "total_sustainability_per_serving": res["total_sustainability_per_serving"],
                    "sustainability_per_kg": res["sustainability_per_kg"],
                    "sustainability_profiling_details": json.dumps(res["details"]),
                    "trace": json.dumps(trace),
                    "recipe_id": recipe_id,
                    "nutrition_source": source
                }

                # Use a fresh connection for the update to avoid interleaved cursor issues
                with get_connection() as update_conn:
                    update_conn.execute(update_query, params)
                    update_conn.commit()

                if count % 100 == 0:
                    print(f"[{count}] Processed {recipe_id} ({source}) - {title}")

            print(f"Finished. Total processed: {count}")

    except Exception as e:
        print(f"Critical error during backfill: {e}")

if __name__ == "__main__":
    backfill_sustainability()
