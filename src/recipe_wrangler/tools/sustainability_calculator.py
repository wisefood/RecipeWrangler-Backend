# Purpose: Compute sustainability/carbon footprint totals via Chroma matches.

import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from langchain.tools import tool

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.utils.query_chromadb import query_sustainability_db
from recipe_wrangler.tools.nutrition_match import (
    clean_query as _nm_clean,
    classes_compatible as _nm_compat,
    food_class as _nm_class,
    _tokens as _nm_tokens,
    _bm25_scores as _nm_bm25,
    _singular as _nm_singular,
    _PROCESSED_MARKERS as _NM_MARKERS,
)

SOURCE_SUSTAINABILITY = "Sustainable FooDB"

# --------------------------------------------------------------------------- #
# Same strategy as the nutrition matcher, scaled down: exact / alias / vector
# with a food-class hard gate, BM25 rerank and cooking-state demotion, and a
# confidence label on every result.  Carbon-footprint values in the
# "Sustainable FooDB" Chroma collection are category-quantised (all beef ≈ the
# same cf_val, etc.), so getting the right *category* is what matters most.
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _norm(s: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(s or "").lower())).strip()


@lru_cache(maxsize=1)
def _cf_index() -> Dict[str, float]:
    """normalized ingredient name -> cf_val (kg CO2e / kg) for every DB entry."""
    try:
        from recipe_wrangler.utils.chroma_client import get_chroma_client

        col = get_chroma_client().get_collection("sustainability_ingredients")
        res = col.get(include=["documents", "metadatas"])
    except Exception:
        return {}
    idx: Dict[str, float] = {}
    for doc, meta in zip(res.get("documents") or [], res.get("metadatas") or []):
        meta = meta or {}
        name = _norm(meta.get("ingredient") or doc)
        try:
            cf = float(meta.get("cf_val"))
        except (TypeError, ValueError):
            continue
        if name and cf > 0:
            idx.setdefault(name, cf)
    return idx


# recipe ingredient -> a Sustainable-FooDB entry name (all targets verified to
# exist in the collection). Plurals are handled by singularising, so only the
# non-trivial mappings are listed here.
_SUST_ALIAS = {
    "ground beef": "beef", "minced beef": "beef", "beef mince": "beef", "beef chuck": "beef",
    "chuck roast": "beef", "stewing beef": "beef", "stew beef": "beef", "beef stew meat": "beef",
    "beef sirloin": "beef", "beef tenderloin": "beef", "beef brisket": "beef", "steak": "beef",
    "sirloin steak": "beef", "ribeye": "beef", "chuck steak": "beef",
    "ground pork": "pork", "minced pork": "pork", "pork mince": "pork", "pork loin": "pork",
    "pork chop": "pork", "pork chops": "pork", "pork tenderloin": "pork", "pork shoulder": "pork",
    "bacon": "pork", "ham": "pork", "sausage": "pork", "pork sausage": "pork", "prosciutto": "pork",
    "ground lamb": "lamb", "minced lamb": "lamb", "lamb mince": "lamb", "lamb chop": "lamb",
    "lamb chops": "lamb", "lamb shoulder": "lamb", "lamb loin chop": "lamb",
    "ground chicken": "chicken", "minced chicken": "chicken", "chicken mince": "chicken",
    "chicken breast": "chicken", "chicken thigh": "chicken", "chicken breast fillet": "chicken",
    "boneless skinless chicken breast": "chicken", "skinless chicken breast": "chicken",
    "boneless skinless chicken thigh": "chicken", "chicken drumstick": "chicken", "chicken leg": "chicken",
    "salmon fillet": "salmon", "fresh salmon": "salmon",
    "unsalted butter": "butter", "salted butter": "butter",
    "whole milk": "milk", "skim milk": "milk", "skimmed milk": "milk", "low fat milk": "milk",
    "2% milk": "milk", "1% milk": "milk", "nonfat milk": "milk", "buttermilk": "milk", "full fat milk": "milk",
    "greek yogurt": "yogurt", "plain yogurt": "yogurt", "natural yogurt": "yogurt", "low fat yogurt": "yogurt",
    "nonfat yogurt": "yogurt", "yoghurt": "yogurt", "greek yoghurt": "yogurt", "plain yoghurt": "yogurt",
    "low-fat yoghurt": "yogurt",
    "mozzarella cheese": "cheese", "cream cheese": "cheese", "ricotta cheese": "cheese",
    "cottage cheese": "cheese", "grated cheese": "cheese", "shredded cheese": "cheese", "mozzarella": "cheese",
    "ricotta": "cheese", "feta": "feta cheese",
    "all purpose flour": "flour", "all-purpose flour": "flour", "plain flour": "flour", "white flour": "flour",
    "bread flour": "flour", "self raising flour": "flour", "self-raising flour": "flour",
    "whole wheat flour": "wheat flour", "wholemeal flour": "wheat flour", "wholewheat flour": "wheat flour",
    "white rice": "rice", "brown rice": "rice", "basmati rice": "rice", "jasmine rice": "rice",
    "long grain rice": "rice", "arborio rice": "rice", "risotto rice": "rice",
    "penne": "pasta", "macaroni": "pasta", "fusilli": "pasta", "rigatoni": "pasta", "linguine": "pasta",
    "fettuccine": "pasta", "noodles": "pasta", "egg noodles": "pasta", "lasagne": "pasta", "lasagna noodles": "pasta",
    "rolled oats": "oats", "porridge oats": "oats", "oatmeal": "oats", "old fashioned oats": "oats",
    "granulated sugar": "sugar", "white sugar": "sugar", "caster sugar": "sugar", "castor sugar": "sugar",
    "brown sugar": "sugar", "light brown sugar": "sugar", "dark brown sugar": "sugar",
    "powdered sugar": "sugar", "icing sugar": "sugar", "confectioners sugar": "sugar",
    "extra virgin olive oil": "olive oil", "evoo": "olive oil",
    "canola oil": "vegetable oil", "sunflower oil": "vegetable oil", "rapeseed oil": "vegetable oil",
    "sesame oil": "vegetable oil", "coconut oil": "vegetable oil", "cooking oil": "vegetable oil",
    "spring onion": "onion", "scallion": "onion", "green onion": "onion", "red onion": "onion",
    "yellow onion": "onion", "white onion": "onion", "brown onion": "onion", "shallot": "onion",
    "garlic cloves": "garlic", "garlic clove": "garlic", "minced garlic": "garlic", "fresh garlic": "garlic",
    "crushed garlic": "garlic",
    "cherry tomatoes": "tomato", "cherry tomato": "tomato", "grape tomatoes": "tomato",
    "canned tomatoes": "tomato", "tinned tomatoes": "tomato", "chopped tomatoes": "tomato",
    "diced tomatoes": "tomato", "crushed tomatoes": "tomato", "tomato paste": "tomato",
    "fresh tomatoes": "tomato", "vine tomatoes": "tomato", "plum tomatoes": "tomato",
    "broccoli florets": "broccoli", "baby spinach": "spinach", "fresh spinach": "spinach",
    "button mushrooms": "mushroom", "white mushrooms": "mushroom", "cremini mushrooms": "mushroom",
    "portobello mushrooms": "mushroom", "portabella mushrooms": "mushroom", "chestnut mushrooms": "mushroom",
    "baby carrots": "carrot",
    "cheddar": "cheddar cheese", "sharp cheddar": "cheddar cheese", "mature cheddar": "cheddar cheese",
    "grated cheddar": "cheddar cheese", "parmesan": "parmesan cheese", "parmigiano": "parmesan cheese",
    "grated parmesan": "parmesan cheese",
    "fresh ginger": "ginger", "ginger root": "ginger",
    "fresh parsley": "parsley", "fresh basil": "basil", "fresh mint": "mint", "fresh thyme": "thyme",
    "lemon juice": "lemon", "lime juice": "lime", "orange juice": "orange",
    "milk chocolate": "chocolate", "semisweet chocolate": "chocolate", "semisweet chocolate chips": "chocolate",
    "chocolate chips": "chocolate", "white chocolate": "chocolate",
    "smooth peanut butter": "peanut butter", "crunchy peanut butter": "peanut butter",
    "dry white wine": "white wine", "dry red wine": "red wine", "lager": "beer", "ale": "beer",
    "table salt": "salt", "sea salt": "salt", "kosher salt": "salt", "cooking salt": "salt",
    "pepper": "black pepper", "ground black pepper": "black pepper",
    "firm tofu": "tofu", "silken tofu": "tofu", "extra firm tofu": "tofu",
}


# Hand-corrected cf_val for a few ingredients the DB clearly got wrong: it
# tagged stock/broth with the *solid* meat's CF (≈20 kg CO2e/kg for "beef
# stock"!), and salt/spices with vegetable-ish values. Stock is mostly water
# (~1–2 kg/kg liquid); salt's footprint is negligible. These override even an
# exact DB hit.  cf_val is kg CO2e / kg.
_SUST_CF_OVERRIDE = {
    "beef stock": 2.0, "beef broth": 2.0, "beef bouillon": 2.0, "beef stock cube": 4.0,
    "chicken stock": 1.5, "chicken broth": 1.5, "chicken bouillon": 1.5, "chicken stock cube": 3.0,
    "vegetable stock": 0.4, "vegetable broth": 0.4, "vegetable bouillon": 0.4,
    "fish stock": 2.0, "fish broth": 2.0, "bone broth": 2.0, "stock": 1.5, "broth": 1.5, "bouillon": 1.5,
    "salt": 0.02, "table salt": 0.02, "sea salt": 0.02, "kosher salt": 0.02, "cooking salt": 0.02,
    "rock salt": 0.02, "fine salt": 0.02, "flaky salt": 0.02,
}


def _cf_from_name(name: str) -> Tuple[Optional[float], Optional[str]]:
    """Override / exact / alias / singularised lookup. (cf_val, source) or (None, None)."""
    idx = _cf_index()
    key = _norm(name)
    if not key:
        return None, None
    if key in _SUST_CF_OVERRIDE:
        return _SUST_CF_OVERRIDE[key], "override"
    if not idx:
        return None, None
    if key in idx:
        return idx[key], "exact"
    alt = _SUST_ALIAS.get(key)
    if alt:
        alt_n = _norm(alt)
        if alt_n in idx:
            return idx[alt_n], "alias"
    sg = " ".join(_nm_singular(w) for w in key.split())
    if sg != key and sg in idx:
        return idx[sg], "exact"
    return None, None


def best_sustainability_match(ing_name: str) -> Tuple[Optional[float], Optional[str], str]:
    """Return (cf_val, matched_name, confidence). confidence ∈ {exact, alias, strong, weak, none}.
    cf_val is None when nothing compatible was found (the ingredient then contributes 0 CO2e)."""
    cleaned = _nm_clean(ing_name) or str(ing_name or "").strip().lower()
    cf, src = _cf_from_name(cleaned)
    if cf is None and _norm(ing_name) != _norm(cleaned):
        cf, src = _cf_from_name(ing_name)
    if cf is not None:
        return float(cf), cleaned, (src if src in {"exact", "alias", "override"} else "exact")

    # vector path: candidates -> food-class hard gate -> bm25 + overlap rerank -> cooking-state demotion
    q_class = _nm_class(cleaned)
    q_tok = set(_nm_tokens(cleaned))
    q_raw = set(_TOKEN_RE.findall(str(ing_name or "").lower()))
    cands: list[dict] = []
    for query in (cleaned, str(ing_name or "")):
        try:
            hits = query_sustainability_db(query) or []
        except Exception:
            hits = []
        for c in hits:
            if not isinstance(c, dict):
                continue
            meta = c.get("metadata") or {}
            cn = str(meta.get("ingredient") or meta.get("food_name") or c.get("document") or "")
            try:
                cv = float(meta.get("cf_val"))
            except (TypeError, ValueError):
                continue
            if cv <= 0 or not _nm_compat(q_class, _nm_class(cn)):
                continue
            cands.append({"name": cn, "cf": cv, "distance": c.get("distance")})
        if hits:
            break
    if not cands:
        return None, None, "none"

    corpus = [_nm_tokens(c["name"]) for c in cands]
    bm = _nm_bm25(list(q_tok), corpus) if q_tok else [0.0] * len(cands)
    n_q = max(1, len(q_tok))
    best = None
    for c, ct, b in zip(cands, corpus, bm):
        d = c.get("distance")
        sim = (1.0 - float(d)) if d is not None else 0.0
        overlap = len(q_tok & set(ct))
        pen = 0.0
        if overlap == 0 and sim < 0.90:
            pen -= 0.5
        if (_NM_MARKERS & set(_TOKEN_RE.findall(c["name"].lower()))) - q_raw:
            pen -= 0.12
        score = 0.60 * sim + 0.40 * float(b) + 0.15 * min(1.0, overlap / n_q) + pen
        if best is None or score > best[0]:
            best = (score, sim, overlap, c)
    score, sim, overlap, c = best
    if score < 0.30:
        return None, None, "none"
    strong = score >= 0.50 and (overlap > 0 or sim >= 0.90)
    return float(c["cf"]), c["name"], ("strong" if strong else "weak")


def _to_float_or_none(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

@tool
def sustainability_tool_chroma(
    title: str,
    ingredient_names: List[str],
    weights: List[float],
    serving_size_g: Optional[float] = None,
    serves: Optional[float] = None,
    min_similarity: float = 0.5,
) -> Dict:
    """
    Compute recipe carbon footprint (kg CO2e). 
    Matches ingredients via Chroma.
    """
    details: List[Dict] = []
    total_sustainability = 0.0

    serves_value: Optional[float] = None
    if serves is not None:
        try:
            serves_value = float(serves)
        except (TypeError, ValueError) as exc:
            raise ValueError("sustainability_tool_chroma: 'serves' must be numeric.") from exc
        if serves_value <= 0:
            serves_value = None

    for ing_name, weight_g in zip(ingredient_names, weights):
        cf_val, matched_name, conf = best_sustainability_match(ing_name)

        if cf_val is None:
            details.append({
                "ingredient": ing_name,
                "matched_sustainability_ingredient": None,
                "weight_g": float(weight_g),
                "cf_val": None,
                "distance": None,
                "contribution": 0.0,
                "sustainability_match_confidence": conf,
                "source_sustainability": SOURCE_SUSTAINABILITY,
            })
            continue

        contribution = (float(weight_g) / 1000.0) * float(cf_val)
        total_sustainability += contribution
        details.append({
            "ingredient": ing_name,
            "matched_sustainability_ingredient": matched_name,
            "weight_g": float(weight_g),
            "cf_val": float(cf_val),
            "distance": None,
            "contribution": float(contribution),
            "sustainability_match_confidence": conf,
            "source_sustainability": SOURCE_SUSTAINABILITY,
        })

    # Normalize to kg CO2e per kg of prepared recipe (optional)
    sustainability_per_kg: Optional[float] = None
    if serving_size_g is not None and serves_value is not None:
        try:
            total_weight_g = float(serving_size_g) * serves_value
            if total_weight_g > 0:
                sustainability_per_kg = (total_sustainability * 1000.0) / total_weight_g
        except (TypeError, ValueError):
            sustainability_per_kg = None

    total_sustainability_per_serving: Optional[float] = None
    if serves_value:
        total_sustainability_per_serving = total_sustainability / serves_value

    return {
        "title": title,
        "details": details,
        "total_sustainability": float(total_sustainability),
        "total_sustainability_per_serving": None
        if total_sustainability_per_serving is None
        else float(total_sustainability_per_serving),
        "sustainability_per_kg": None
        if sustainability_per_kg is None
        else float(sustainability_per_kg),
        "serves": serves_value,
        "source_sustainability": SOURCE_SUSTAINABILITY,
    }


def Sustainability_Node(state: RecipeState) -> RecipeState:
    """
    Node to compute carbon footprint via Chroma, scale by ingredient weight/serves, 
    and store per-ingredient details plus per-serving/total CO2e in state.
    """
    debug = bool(state.debug)

    ingredient_names = state.ingredient_names or []
    if not isinstance(ingredient_names, list):
        raise ValueError("Sustainability_Node: 'ingredient_names' must be a list of strings.")

    # Pull gram weights from Weight_Calculator output
    weights_g = None
    if isinstance(state.weights, dict):
        weights_g = state.weights.get("weights")
    elif isinstance(state.weights, list):
        weights_g = state.weights

    if weights_g is None:
        raise ValueError("Sustainability_Node: missing 'weights' (grams) from Weight_Calculator.")

    try:
        weights_g = [float(x) for x in weights_g]
    except (TypeError, ValueError) as e:
        raise ValueError("Sustainability_Node: all weights must be numeric (grams).") from e

    # Ensure equal lengths (tool zips the two lists)
    n = min(len(ingredient_names), len(weights_g))
    ingredient_names = ingredient_names[:n]
    weights_g = weights_g[:n]

    res = sustainability_tool_chroma.invoke({
        "title": state.title,
        "ingredient_names": ingredient_names,
        "weights": weights_g,                       # ✅ correct param name
        "min_similarity": state.min_similarity if state.min_similarity is not None else 0.5,
        "serving_size_g": state.serving_size_g,
        "serves": state.serves,
    })

    state.total_sustainability = res["total_sustainability"]                # kg CO2e
    state.total_sustainability_per_serving = res["total_sustainability_per_serving"]
    state.sustainability_per_kg = res["sustainability_per_kg"]              # kg CO2e/kg
    state.sustainability_details = res["details"]
    state.sustainability_serves = res.get("serves")

    if debug:
        print(f"\n[Sustainability_Node] Computed (ChromaDB) for recipe '{state.title}'.")
        print(f"   total_sustainability = {res['total_sustainability']:.4f} kg CO2e")
        per_serving = res.get("total_sustainability_per_serving")
        if per_serving is not None:
            print(f"   sustainability/serving = {per_serving:.4f} kg CO2e")
        else:
            print("   sustainability/serving = None (serves missing)")
        if res["sustainability_per_kg"] is not None:
            print(f"   sustainability_per_kg = {res['sustainability_per_kg']:.4f} kg CO2e/kg")
        else:
            print("   sustainability_per_kg = None (serving info missing)")
        print(f"\n[Sustainability_Node] Updated State Keys: {list(state.model_dump().keys())}")

    return state
