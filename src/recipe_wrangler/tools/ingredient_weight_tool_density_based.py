from typing import Any, Dict, List, Optional, Tuple
from langchain.tools import tool
import re
import chromadb
from pathlib import Path
from recipe_wrangler.utils.get_embeddings import get_embeddings
from recipe_wrangler.utils.query_chromadb import (
    query_density_db,
    query_common_units_db,
)
import ast
from ollama import chat

REPO_ROOT = Path(__file__).resolve().parents[3]
PERSIST_PATH = REPO_ROOT / "chroma_db"
DENSITY_DISTANCE_THRESHOLD = 0.2

SYSTEM_PROMPT = """You are an expert in estimating food ingredient weights in grams. 
You will receive an ingredient name and a measurement. Your goal is to estimate the weight in grams of the ingredient based on the measurement provided.
Always return a single number representing the estimated weight in grams."""

def _as_list(x: Any) -> list:
    """Ensure input is always treated as a list."""
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _best_density_g_ml(ingredient: str, debug: bool = False) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """
    Return the closest density match (g/mL) for an ingredient via foods_density_v1 collection, with hit info if debug.
    """
    
    hits = query_density_db(ingredient)
    hits = sorted(hits, key=lambda h: h.get("distance") or 0.0)
    best_info: Optional[Dict[str, Any]] = None
    for hit in hits:
        meta = hit.get("metadata") or {}
        for key in ("Density in g/ml", "Specific gravity"):
            val = meta.get(key)
            if val is None or val == "":
                continue
            try:
                density = float(val)
            except Exception:
                continue
            if density > 0:
                best_info = {
                    "density": density,
                    "document": hit.get("document"),
                    "distance": hit.get("distance"),
                }
                return density, best_info
    return None, best_info


def _best_volume_ml(measurement: str, debug: bool = False) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """
    Return the closest volume match (mL per unit) for a measurement via common_units collection, with hit info if debug.
    """
    
    try:
        hit = query_common_units_db(measurement)
    except Exception:
        return None, None

    best_info: Optional[Dict[str, Any]] = None
    meta = hit.get("metadata") or {}
    val = meta.get("volume_ml")
    
    if val is not None and val != "":
        try:
            volume = float(val)
        except Exception:
            return None, best_info
        
        if volume > 0:
            unit_name = meta.get("unit")
            best_info = {
                "volume_ml": volume,
                "unit_name": unit_name,
                "document": hit.get("document"),
                "distance": hit.get("distance"),
            }
            return volume, best_info
        
    return None, best_info


def _extract_quantity(measurement: str) -> float:
    """
    Pull the first numeric quantity from the measurement text; default to 1.0 if absent.
    """
    match = re.search(r"\d+(?:\.\d+)?", measurement)
    if not match:
        return 1.0
    try:
        return float(match.group())
    except Exception:
        return 1.0

@tool
def ingredient_weight_tool_open(
    ingredient_names: Any,
    measurements: Any,
    debug: bool = False,
    density_distance_threshold: float = DENSITY_DISTANCE_THRESHOLD,
) -> list:
    """
    Compute ingredient weights by vector-searching density and volume for each pair, returning detail dictionaries.
    LLM fallback is used if no good density match is found.
    """
    
    names: List[str] = [str(v) for v in _as_list(ingredient_names)]
    measures: List[str] = [str(v) for v in _as_list(measurements)]
    results: List[Dict[str, Any]] = []
    
    for idx, name in enumerate(names):
        measurement = measures[idx] if idx < len(measures) else ""
        density, density_info = _best_density_g_ml(name, debug)
        volume, volume_info = _best_volume_ml(measurement, debug)
        qty = _extract_quantity(measurement)
        weight = None
        density_dist = (density_info or {}).get("distance")
        volume_dist = (volume_info or {}).get("distance")
        too_far = density_dist is not None and density_dist > density_distance_threshold
        weight_source = "LLM" if (density is None or too_far) else "Density Dataset"
                
        if density is not None and volume is not None and not too_far:
            try:
                weight = round(float(density * volume * qty), 2)
                
            except Exception:
                
                weight = None
        
        
        if weight_source == "LLM":
            user_input = f"Ingredient: {name}\nMeasurement: {measurement}\n"
                    
            response = chat(
                model='qwen3:8b',
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_input},
                ],
                think=False,  # disable reasoning tokens / <thinking> output
            )
            content = response['message']['content']
            
            weight = ast.literal_eval(content)
        
        if debug:
            log = []
            log.append(f"Ingredient '{name}' with measurement '{measurement}'.")
            
            if density_dist < density_distance_threshold:
                log.append(f"Matched density from '{(density_info or {}).get('document')}' (distance={(density_info or {}).get('distance')}); density={density} g/mL.")
                log.append(f"Matched unit '{(volume_info or {}).get('unit_name')}' (distance={(volume_info or {}).get('distance')}); volume_ml={volume}.")

            else:
                log.append("No density match found, fallback to LLM")

        print(log)

        if weight_source == "Density Dataset":
            results.append({
                "name": name,
                "quantity": str(qty),
                "unit": (volume_info or {}).get("unit_name"),
                "density_match": (density_info or {}).get("document"),
                "density": density,
                "volume_ml": volume,
                "weight": weight,
                "weight_source": weight_source,
                "matched_density_distance": density_dist,
                "matched_unit_distance": volume_dist
            })
        else:            
            results.append({
                "name": name,
                "quantity": str(qty),
                "unit": (volume_info or {}).get("unit_name"),
                "weight": weight,
                "weight_source": weight_source
            })
            
    return results


def Ingredient_Weight_Node(state: dict) -> dict:
    """
    LangGraph node wrapper that writes computed weights back into the state.
    """
    
    names = state.get("ingredient_names") or []
    measurements = state.get("measurements") or []
    debug = state.get("debug", False)
    weights = ingredient_weight_tool_open.invoke({
        "ingredient_names": names,
        "measurements": measurements,
        "debug": debug,
        "density_distance_threshold": state.get("density_distance_threshold", DENSITY_DISTANCE_THRESHOLD),
    })
    state.update({"weights": weights})
    return state
