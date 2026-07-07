"""Resolve a recipe ingredient to a composition-table record.

Wraps the Chroma vector lookup with: a curated recipe1m->USDA link shortcut,
query cleaning, a BM25 + similarity rerank that *gates* on lexical overlap, and
a conservative food-class compatibility guard. Returns a Chroma-candidate-shaped
dict plus a confidence label ("curated" | "strong" | "weak" | "none") so callers
can flag weak matches instead of silently trusting or zeroing them.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Optional

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

from recipe_wrangler.repositories.chroma_matchers import (  # noqa: E402
    query_hungarian_nutrition_candidates,
    query_irish_nutrition_candidates,
    query_usda_nutrition_candidates,
)

# --------------------------------------------------------------------------- #
# Query cleaning
# --------------------------------------------------------------------------- #
_PAREN_RE = re.compile(r"\([^)]*\)")
_QUALIFIER_RE = re.compile(
    r"\b(?:fresh|ripe|chopped|minced|diced|sliced|grated|shredded|"
    r"crushed|ground|pure?ed|mashed|peeled|trimmed|drained|rinsed|melted|"
    r"softened|thawed|frozen|optional|divided|finely|roughly|coarsely|thinly|"
    r"freshly|large|small|medium|jumbo|baby|organic|low[- ]?fat|nonfat|"
    r"non[- ]?fat|reduced[- ]?fat|fat[- ]?free|skim|skimmed|whole|unsweetened|"
    r"sweetened|salted|unsalted|extra[- ]?virgin|virgin|hot|mild|"
    r"toasted|roasted|smoked|dry|dried|prepared|homemade|store[- ]?bought|"
    r"good[- ]?quality|best[- ]?quality|boneless|skinless|lean|trimmed|"
    r"to taste|to serve|to garnish|to drizzle|to finish|to brush|to grease|"
    r"for serving|for garnish|for dusting|for sprinkling|for frying|for greasing|"
    r"plus more|plus extra|or more|as needed|of your choice|approximately|about|"
    r"halved|quartered|cubed|julienned)\b",
    re.IGNORECASE,
)
_NON_NAME_RE = re.compile(r"[^a-z0-9\s'/-]")
_LEADING_QTY_RE = re.compile(r"^\s*\d+(?:[.\-/]\d+)*\s*(?:%|cups?|tbsps?|tsps?|"
                             r"tablespoons?|teaspoons?|oz|ounces?|lbs?|pounds?|g|"
                             r"grams?|kg|ml|l|cans?|packages?|sticks?|cloves?)?\b",
                             re.IGNORECASE)


def clean_query(name: str) -> str:
    s = str(name or "").lower()
    s = _PAREN_RE.sub(" ", s)
    s = s.replace(",", " ")            # commas usually separate prep notes; keep the words
    s = _QUALIFIER_RE.sub(" ", s)
    s = _NON_NAME_RE.sub(" ", s)
    # drop a leading quantity/unit (recipe1m sometimes fuses it into the name)
    prev = None
    while prev != s:
        prev = s
        s = _LEADING_QTY_RE.sub(" ", s, count=1).lstrip()
    s = re.sub(r"\s+", " ", s).strip(" -'/")
    return s


def _norm(s: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(s or "").lower())).strip()


# --------------------------------------------------------------------------- #
# Tokens / BM25 (self-contained copy)
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "and", "or", "with", "of", "the", "in", "a", "an", "fresh",
    "whole", "large", "small", "medium", "cup", "cups", "tbsp", "tsp",
    "tablespoon", "teaspoon", "style", "type", "kind", "prepared", "made", "from",
    "to", "as", "for", "fl", "oz", "ready",
}


# UK/US spellings + a few high-value cross-locale synonyms; each side maps to a
# set so overlap matches regardless of which the recipe / table uses.
_SYNONYMS = {
    "yoghurt": "yogurt", "yoghourt": "yogurt", "flavour": "flavor", "flavoured": "flavored",
    "colour": "color", "fibre": "fiber", "litre": "liter", "grey": "gray",
    "rocket": "arugula", "courgette": "zucchini", "courgettes": "zucchini",
    "aubergine": "eggplant", "aubergines": "eggplant", "capsicum": "pepper",
    "capsicums": "pepper", "coriander": "cilantro", "prawn": "shrimp", "prawns": "shrimp",
    "mangetout": "snowpea", "swede": "rutabaga", "kumara": "sweetpotato",
    "passata": "tomato", "sultana": "raisin", "sultanas": "raisin",
}


def _singular(t: str) -> str:
    if len(t) <= 3:
        return t
    if t.endswith("ies"):
        return t[:-3] + "y"
    if t.endswith(("ses", "xes", "zes", "ches", "shes", "oes")):
        return t[:-2]
    if t.endswith("ves"):
        return t[:-3] + "f"
    if t.endswith("s") and not t.endswith(("ss", "us", "is", "as", "os")):
        return t[:-1]
    return t


def _tokens(text: object) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN_RE.findall(str(text or "").lower()):
        if len(raw) <= 1:
            continue
        t = _singular(raw)
        if t in _STOP or len(t) <= 1:
            continue
        out.append(t)
        syn = _SYNONYMS.get(raw) or _SYNONYMS.get(t)
        if syn and syn != t:
            out.append(syn)
    return out


def _bm25_scores(query_tokens: list[str], corpus_tokens: list[list[str]]) -> list[float]:
    if not query_tokens or not corpus_tokens:
        return [0.0 for _ in corpus_tokens]
    doc_freq: dict[str, int] = {}
    for tokens in corpus_tokens:
        for token in set(tokens):
            doc_freq[token] = doc_freq.get(token, 0) + 1
    doc_count = len(corpus_tokens)
    avg_len = sum(len(t) for t in corpus_tokens) / max(1, doc_count)
    k1, b = 1.5, 0.75
    query_terms = set(query_tokens)
    scores: list[float] = []
    for tokens in corpus_tokens:
        if not tokens:
            scores.append(0.0)
            continue
        term_counts: dict[str, int] = {}
        for token in tokens:
            term_counts[token] = term_counts.get(token, 0) + 1
        doc_len = len(tokens)
        score = 0.0
        for term in query_terms:
            tf = term_counts.get(term, 0)
            if tf <= 0:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1.0 + (doc_count - df + 0.5) / (df + 0.5))
            denom = tf + k1 * (1.0 - b + b * doc_len / max(avg_len, 1e-9))
            score += idf * (tf * (k1 + 1.0) / denom)
        scores.append(score)
    max_score = max(scores) if scores else 0.0
    if max_score <= 0:
        return scores
    return [s / max_score for s in scores]


# --------------------------------------------------------------------------- #
# Coarse food-class guard (conservative — under-rejects on purpose)
# --------------------------------------------------------------------------- #
_CLASS_PATTERNS = [
    ("alcohol", r"\b(wine|beer|ale|lager|stout|vodka|whisk(?:e)?y|bourbon|gin|"
                r"brandy|rum|liqueur|liquor|sherry|vermouth|sake|tequila|schnapps|"
                r"cognac|champagne|prosecco|kirsch|cointreau|amaretto|kahlua|"
                r"bitters|everclear|grappa|absinthe|aperitif)\b"),
    ("plant_milk", r"\b(soy|soya|almond|oat|coconut|rice|cashew|hemp)\s*(milk|yog(?:h)?urt|cream)\b"
                   r"|\btofu\b|\bsoymilk\b|\btempeh\b|\bseitan\b"),
    ("dairy", r"\b(milk|cream|yog(?:h)?urt|cheese|buttermilk|kefir|custard|ricotta|"
              r"mascarpone|mozzarella|cheddar|parmesan|parmigiano|pecorino|gouda|"
              r"brie|feta|paneer|curd|whey|ghee|half\s*and\s*half|creme\s*fraiche|"
              r"clotted\s*cream|sour\s*cream|condensed\s*milk|evaporated\s*milk)\b"
              r"|\bbutter\b(?!\s*(?:bean|nut|scotch|milk|head\s*lettuce))"),
    ("egg", r"\begg(?:s)?\b(?!\s*plant|nog|roll)"),
    ("oil_fat", r"\b(oil|lard|shortening|tallow|dripping|suet|margarine)\b"),
    ("sweetener", r"\b(sugar|honey|syrup|molasses|agave|stevia|sucralose|"
                  r"aspartame|sweetener|nectar|treacle|jaggery)\b"),
    ("nut_seed", r"\b(almond|walnut|pecan|cashew|peanut|hazelnut|filbert|pistachio|"
                 r"macadamia|brazil\s*nut|pine\s*nut|pinenut|sesame|tahini|"
                 r"sunflower\s*seed|pumpkin\s*seed|flax|flaxseed|chia|hemp\s*seed|"
                 r"poppy\s*seed)\b"),
    ("legume", r"\b(bean|beans|lentil|lentils|chickpea|chickpeas|garbanzo|edamame|"
               r"split\s*pea|black[- ]?eyed\s*pea)\b"),
    ("grain_cereal", r"\b(flour|rice|oat|oats|oatmeal|wheat|barley|rye|cornmeal|"
                     r"polenta|semolina|couscous|bulgur|bulghur|quinoa|millet|"
                     r"farro|spelt|pasta|noodle|noodles|spaghetti|macaroni|penne|"
                     r"linguine|fettuccine|lasagn|vermicelli|orzo|bread|"
                     r"breadcrumb|crumbs|cracker|tortilla|cereal|granola|muesli|"
                     r"tapioca|cornstarch|corn\s*starch|arrowroot|grits)\b"),
    ("animal_protein", r"\b(beef|steak|chuck|brisket|sirloin|tenderloin|ribeye|"
                       r"rib[- ]?eye|veal|oxtail|pork|ham|bacon|sausage|chorizo|"
                       r"prosciutto|pancetta|salami|pepperoni|kielbasa|bratwurst|"
                       r"lamb|mutton|chicken|turkey|duck|goose|quail|pheasant|"
                       r"fish|salmon|tuna|cod|haddock|tilapia|trout|bass|halibut|"
                       r"snapper|mackerel|sardine|sardines|anchov(?:y|ies)|herring|"
                       r"flounder|sole|pollock|catfish|mahi|swordfish|shrimp|prawn|"
                       r"crab|lobster|clam|mussel|oyster|scallop|squid|calamari|"
                       r"octopus|crayfish|crawfish|frog|rabbit|venison|bison|"
                       r"liver|kidney|tripe|gizzard)\b"),
    ("leafy_green", r"\b(lettuce|spinach|arugula|rocket|kale|chard|collard|"
                    r"watercress|endive|escarole|radicchio|mizuna|mesclun|"
                    r"romaine|cabbage|bok\s*choy|pak\s*choi|tatsoi|cress)\b"),
    ("spice_herb", r"\b(salt|peppercorn|cinnamon|cumin|coriander|paprika|turmeric|"
                   r"nutmeg|clove|cardamom|fenugreek|saffron|cayenne|allspice|"
                   r"mace|anise|caraway|sumac|za'?atar|garam\s*masala|"
                   r"curry\s*powder|chili\s*powder|chilli\s*powder|five\s*spice|"
                   r"basil|oregano|thyme|rosemary|sage|cilantro|dill|tarragon|"
                   r"marjoram|bay\s*leaf|chive|chives|spice|spices|seasoning|herb)\b"),
    ("fruit", r"\b(apple|banana|orange|lemon|lime|grape|grapefruit|berry|berries|"
              r"strawberr|blueberr|raspberr|blackberr|cranberr|boysenberr|"
              r"gooseberr|cherry|cherries|peach|peaches|nectarine|plum|prune|"
              r"apricot|mango|mangoe?s|pineapple|melon|watermelon|cantaloupe|"
              r"honeydew|kiwi|papaya|guava|fig|figs|date|dates|raisin|currant|"
              r"sultana|pomegranate|pear|pears|persimmon|lychee|passionfruit|"
              r"tangerine|mandarin|clementine|rhubarb)\b"),
    ("vegetable", r"\b(carrot|onion|shallot|leek|garlic|potato|potatoes|sweet\s*potato|"
                  r"yam|tomato|tomatoes|cucumber|zucchini|courgette|squash|pumpkin|"
                  r"eggplant|aubergine|capsicum|broccoli|cauliflower|celery|"
                  r"asparagus|artichoke|beet|beets|beetroot|radish|turnip|parsnip|"
                  r"rutabaga|swede|fennel|mushroom|mushrooms|corn|sweetcorn|peas?|"
                  r"green\s*bean|brussels?\s*sprout|okra|scallion|spring\s*onion|"
                  r"chayote|kohlrabi|jicama|daikon|ginger|galangal|horseradish|"
                  r"plantain|cassava|taro)\b"),
    ("condiment_sauce", r"\b(sauce|ketchup|mayonnaise|mustard|relish|salsa|"
                        r"dressing|vinaigrette|marinade|gravy|chutney|dip|paste|"
                        r"spread|jam|jelly|preserve|marmalade|pickle|vinegar|"
                        r"worcestershire|tabasco|sriracha|hoisin|teriyaki|"
                        r"barbecue|bbq|aioli|pesto|tapenade|hummus|guacamole|"
                        r"tomato\s*paste|stock|broth|bouillon|consomme)\b"),
]
_CLASS_RES = [(c, re.compile(p, re.IGNORECASE)) for c, p in _CLASS_PATTERNS]


def food_class(name: str) -> str:
    n = str(name or "").lower()
    for cls, rx in _CLASS_RES:
        if rx.search(n):
            return cls
    return "other"


# Only the high-confidence-incompatible pairs. Anything not listed is allowed.
_HARD_INCOMPATIBLE = frozenset(
    frozenset(p)
    for p in [
        ("dairy", "plant_milk"),
        ("animal_protein", "dairy"), ("animal_protein", "plant_milk"),
        ("animal_protein", "egg"), ("animal_protein", "grain_cereal"),
        ("animal_protein", "legume"), ("animal_protein", "nut_seed"),
        ("animal_protein", "fruit"), ("animal_protein", "vegetable"),
        ("animal_protein", "leafy_green"), ("animal_protein", "spice_herb"),
        ("animal_protein", "sweetener"), ("animal_protein", "oil_fat"),
        ("animal_protein", "alcohol"),
        ("alcohol", "grain_cereal"), ("alcohol", "vegetable"),
        ("alcohol", "leafy_green"), ("alcohol", "fruit"), ("alcohol", "nut_seed"),
        ("alcohol", "legume"), ("alcohol", "spice_herb"), ("alcohol", "dairy"),
        ("alcohol", "egg"), ("alcohol", "oil_fat"), ("alcohol", "sweetener"),
        ("spice_herb", "leafy_green"), ("spice_herb", "fruit"),
        ("spice_herb", "nut_seed"), ("spice_herb", "legume"),
        ("spice_herb", "grain_cereal"),  # ground cinnamon ↛ cinnamon bread
        ("egg", "vegetable"), ("egg", "fruit"), ("egg", "grain_cereal"),
        ("egg", "leafy_green"), ("egg", "spice_herb"),
        ("dairy", "leafy_green"), ("dairy", "vegetable"), ("dairy", "fruit"),
        ("dairy", "spice_herb"), ("dairy", "alcohol"),
        ("leafy_green", "fruit"), ("leafy_green", "grain_cereal"),
        ("oil_fat", "fruit"), ("oil_fat", "leafy_green"), ("oil_fat", "sweetener"),
        ("sweetener", "leafy_green"), ("sweetener", "vegetable"),
        ("vegetable", "grain_cereal"),  # red onion ↛ red rice, etc.
    ]
)


def classes_compatible(a: str, b: str) -> bool:
    if a == b:
        return True
    if "other" in (a, b) or "condiment_sauce" in (a, b):
        return True  # too ambiguous to reject on
    return frozenset((a, b)) not in _HARD_INCOMPATIBLE


# --------------------------------------------------------------------------- #
# FoodOn ontology check (reuses the weight tool's Neo4j-backed lookup; layered
# on top of the coarse food-class guard — FoodOn-first, coarse fallback, neutral
# when neither side has a FoodOn class). Cooking-method / form words are dropped
# and UK/US synonyms applied before the lookup so candidate names like
# "Garlic, raw" or "low-fat yoghurt" still resolve to a class.
# --------------------------------------------------------------------------- #
_FORM_WORDS_RE = re.compile(
    r"\b(?:raw|cooked|canned|tinned|boiled|baked|fried|grilled|broiled|steamed|"
    r"stewed|roasted|microwaved|braised|poached|drained|solids?|fluid|liquid|"
    r"with|without|added|prepared|reconstituted|undiluted|diluted|enriched|"
    r"fortified|low|fat|free|reduced|whole|skim|skimmed|part|plain|natural|"
    r"regular|original|unflavou?red)\b",
    re.IGNORECASE,
)


def _foodon_name_variants(name: str) -> tuple[str, ...]:
    raw = str(name or "").strip().lower()
    cleaned = clean_query(raw)
    core = re.sub(r"\s+", " ", _FORM_WORDS_RE.sub(" ", cleaned)).strip()

    def _syn(s: str) -> str:
        return " ".join(_SYNONYMS.get(_singular(w), _singular(w)) for w in s.split())

    out: list[str] = []
    for v in (raw, cleaned, core, _syn(cleaned), _syn(core)):
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return tuple(out)


@lru_cache(maxsize=16384)
def _foodon_class_ids(name: str) -> tuple[str, ...]:
    try:
        from recipe_wrangler.tools.ingredient_weight_tool import (
            _foodon_class_ids_for_ingredient,
        )
    except Exception:
        return ()
    for variant in _foodon_name_variants(name):
        ids = _foodon_class_ids_for_ingredient(variant, "")
        if ids:
            return ids
    return ()


def _foodon_compatible(query_name: str, candidate_name: str) -> Optional[bool]:
    q_ids = _foodon_class_ids(query_name)
    if not q_ids:
        return None
    c_ids = _foodon_class_ids(candidate_name)
    if not c_ids:
        return None
    try:
        from recipe_wrangler.tools.ingredient_weight_tool import (
            _foodon_classes_have_common_ancestor,
        )
    except Exception:
        return None
    return _foodon_classes_have_common_ancestor(q_ids, c_ids)


# --------------------------------------------------------------------------- #
# recipe1m -> USDA links (NB: this table is itself an *embedding* match
# (`embedding_similarity_source: "chroma_embeddings"`), median similarity ~0.77
# — NOT human-verified. So it is treated as one more candidate scored on its own
# similarity, with a small prior bonus, never as an outright override.)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _curated_link_index() -> dict[str, dict]:
    """Normalized canonical name -> {usda_id, sim, label}."""
    try:
        from recipe_wrangler.utils.pipeline_data_pg import load_pipeline_data

        rows = load_pipeline_data("recipe1m-usda-links-canonical")
    except Exception:
        return {}
    idx: dict[str, dict] = {}
    for row in rows or []:
        uid = str(row.get("usda_id") or "").strip()
        cname = str(row.get("canonical") or "").strip()
        if not (uid and cname):
            continue
        try:
            sim = float(row.get("embedding_similarity"))
        except (TypeError, ValueError):
            sim = 0.85
        idx.setdefault(_norm(cname), {
            "usda_id": uid, "sim": sim,
            "label": str(row.get("usda_food_label") or cname).strip(),
        })
    return idx


def _curated_link(cleaned_name: str) -> Optional[dict]:
    key = _norm(cleaned_name)
    if not key:
        return None
    idx = _curated_link_index()
    hit = idx.get(key)
    if hit:
        return hit
    # the table is mostly singular ("mango", "cardamom") — try the singularised
    # + synonym-normalised form before giving up.
    alt = " ".join(_SYNONYMS.get(_singular(w), _singular(w)) for w in key.split())
    if alt != key:
        return idx.get(alt)
    return None


# --------------------------------------------------------------------------- #
# Hand-curated alias table — small, hand-verified overrides for the highest-
# frequency raw proteins / produce / staples (chicken breast -> the raw breast
# record, not a deli roll). Trusted: when an alias hits, it wins outright.
# Built by scripts/build_nutrition_aliases.py; loaded from pipeline_static_data.
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _alias_index() -> dict[str, dict]:
    """Normalized alias -> {usda_id, label}."""
    try:
        from recipe_wrangler.utils.pipeline_data_pg import load_pipeline_data

        rows = load_pipeline_data("ingredient_nutrition_aliases")
    except Exception:
        return {}
    idx: dict[str, dict] = {}
    for row in rows or []:
        uid = str(row.get("usda_id") or "").strip()
        alias = _norm(row.get("alias"))
        if uid and alias:
            idx.setdefault(alias, {"usda_id": uid, "label": str(row.get("usda_food_name") or alias).strip()})
    return idx


def _alias_lookup(cleaned_name: str) -> Optional[dict]:
    key = _norm(cleaned_name)
    if not key:
        return None
    idx = _alias_index()
    hit = idx.get(key)
    if hit:
        return hit
    alt = " ".join(_SYNONYMS.get(_singular(w), _singular(w)) for w in key.split())
    if alt != key:
        return idx.get(alt)
    return None


# --------------------------------------------------------------------------- #
# Candidate naming
# --------------------------------------------------------------------------- #
def _candidate_name(match: dict) -> str:
    meta = match.get("metadata") or {}
    return str(
        meta.get("food_name")
        or meta.get("Food Name")
        or meta.get("title")
        or match.get("document")
        or ""
    ).strip()


def _candidate_pools(source: str) -> list[tuple[str, object]]:
    # Resolved at call time (not import time) so the functions stay patchable.
    if source == "hungarian":
        return [("hungarian", query_hungarian_nutrition_candidates), ("usda", query_usda_nutrition_candidates)]
    if source == "usda":
        return [("usda", query_usda_nutrition_candidates)]
    return [("irish", query_irish_nutrition_candidates), ("usda", query_usda_nutrition_candidates)]


# Tuning knobs (kept loose; the audit drives these).
_STRONG_SCORE = 0.50
_WEAK_SCORE = 0.30
_HIGH_SIM_NO_OVERLAP = 0.90  # zero-overlap candidate must clear this to survive
_CURATED_BONUS = 0.06        # small prior for the recipe1m->USDA link candidate
# When a candidate name carries a cooking-state / processing / brand token that
# the *raw* query didn't ask for, prefer the plainer alternative — recipe
# ingredients are almost always the raw/uncooked form (the cook cooks it; the
# per-serving nutrition is scaled from the raw weight). So "chicken breast"
# should match "Chicken, …, breast, raw", not "Chicken breast, roll, oven-roasted"
# or "Oscar Mayer … honey glazed"; "buttermilk" should not match "…, dried".
_COOKING_STATES = {
    "cooked", "roasted", "roast", "rotisserie", "baked", "grilled", "chargrilled",
    "broiled", "barbecued", "barbecue", "fried", "deepfried", "panfried",
    "stirfried", "braised", "stewed", "casseroled", "simmered", "poached",
    "microwaved", "sauteed", "saute", "boiled", "hardboiled", "steamed", "toasted",
    "blanched", "scrambled", "scalloped", "creamed", "rendered", "gratin",
    "fricassee", "smoked", "cured", "dried", "dehydrated", "canned", "tinned",
    "jellied", "potted", "frozen", "breaded", "coated", "battered", "glazed",
    "roll", "deli", "luncheon", "patties", "patty", "nuggets",
}
_PROCESSED_MARKERS = _COOKING_STATES | {
    "oscar", "mayer", "kraft", "heinz", "campbell", "nestle", "kellogg", "general",
    "mills", "betty", "crocker", "pillsbury", "mccormick", "knorr", "maggi",
    "babyfood", "infant", "powder", "powdered", "concentrate", "concentrated",
    "instant", "reconstituted", "fortified", "snack", "snacks", "takeaway", "fast",
}


def best_nutrition_match(name: str, source: str = "irish", min_similarity: float = 0.7) -> dict:
    """Return {match, source_key, similarity, confidence, reason, matched_name, cleaned_query}."""
    source = (source or "irish").strip().lower()
    cleaned = clean_query(name) or str(name or "").strip().lower()
    q_tokens = _tokens(cleaned)
    q_class = food_class(cleaned)

    # 0) hand-curated alias table — trusted; wins outright when it hits.
    #    Try the raw name first so "unsalted butter" hits its own alias before
    #    clean_query strips "unsalted" and "butter" hits the salted-butter alias.
    alias = _alias_lookup(str(name or ""))
    if alias is None:
        alias = _alias_lookup(cleaned)
    if alias:
        return {
            "match": {"metadata": {"usda_id": alias["usda_id"], "food_name": alias.get("label")},
                      "document": alias.get("label") or cleaned, "distance": 0.0},
            "source_key": "usda", "similarity": 1.0, "confidence": "alias",
            "reason": "alias_table", "matched_name": alias.get("label") or cleaned,
            "cleaned_query": cleaned,
        }

    # 1) gather candidates from the source pool + USDA cross-pool
    cands: list[dict] = []
    for src_key, fn in _candidate_pools(source):
        try:
            hits = fn(cleaned) or []
        except Exception:
            hits = []
        for c in hits:
            if isinstance(c, dict):
                c2 = dict(c)
                c2["_source_key"] = src_key
                cands.append(c2)

    # ...plus the recipe1m->USDA link as one more candidate, scored on its own
    # (machine-derived) similarity — it competes, it doesn't override. If its
    # USDA label is branded / cooked / dried and the query didn't ask for that
    # (the table maps "chicken breast" -> "Oscar Mayer, Chicken Breast",
    # "mango" -> "Mango, dried, sweetened"), it competes as a *vanilla*
    # candidate — no curated prior, no "curated" label.
    link = _curated_link(cleaned)
    if link:
        _q_raw = set(_TOKEN_RE.findall(str(name or "").lower()))
        _label_words = set(_TOKEN_RE.findall(str(link.get("label") or "").lower()))
        _clean_link = not bool((_PROCESSED_MARKERS & _label_words) - _q_raw)
        cands.append({
            "metadata": {"usda_id": link["usda_id"], "food_name": link.get("label") or cleaned},
            "document": link.get("label") or cleaned,
            "distance": max(0.0, 1.0 - float(link.get("sim") or 0.85)),
            "_source_key": "usda", "_curated": _clean_link,
        })
    if not cands:
        return {"match": None, "source_key": source, "similarity": None,
                "confidence": "none", "reason": "no_candidates", "matched_name": None,
                "cleaned_query": cleaned}

    # 2) rerank. Pass 1 (cheap): similarity + BM25 + overlap + coarse-class gate
    #    + curated prior. Pass 2: re-score only the top few with the FoodOn
    #    ontology check (a Neo4j round-trip per candidate — too costly for all).
    names = [_candidate_name(c) for c in cands]
    corpus = [_tokens(n) for n in names]
    bm = _bm25_scores(q_tokens, corpus) if q_tokens else [0.0] * len(cands)
    q_set = set(q_tokens)
    # raw tokenisation of the *original* name (no stopword/singular folding) —
    # used only for the cooking-state / processed-marker exemption.
    q_raw_words = set(_TOKEN_RE.findall(str(name or "").lower()))
    n_q = max(1, len(q_tokens))

    def _base_score(c, cname, ctoks, bms):
        d = c.get("distance")
        sim = (1.0 - float(d)) if d is not None else 0.0
        ctok_set = set(ctoks)
        overlap = len(q_set & ctok_set)
        pen = 0.0
        if overlap == 0 and sim < _HIGH_SIM_NO_OVERLAP:
            pen -= 0.5
        if not classes_compatible(q_class, food_class(cname)):
            pen -= 1.0
        if c.get("_curated"):
            pen += _CURATED_BONUS
        c_raw_words = set(_TOKEN_RE.findall(str(cname or "").lower()))
        if (_PROCESSED_MARKERS & c_raw_words) - q_raw_words:
            pen -= 0.12  # cooking-state / processed / branded marker the query didn't ask for
        elif "raw" in c_raw_words and not (_COOKING_STATES & q_raw_words):
            pen += 0.06  # state-less query -> nudge toward the raw/uncooked record
        return 0.60 * sim + 0.40 * float(bms) + 0.15 * min(1.0, overlap / n_q) + pen, sim, overlap

    pass1 = sorted(
        (( *_base_score(c, cn, ct, bms), c, cn) for c, cn, ct, bms in zip(cands, names, corpus, bm)),
        key=lambda t: -t[0],
    )
    best = None
    for base, sim, overlap, c, cname in pass1[:3]:
        # Soft FoodOn nudge — never alone rejects/rescues; the local graph is sparse.
        fo = _foodon_compatible(name, cname)
        adj = (-0.25 if fo is False else (0.10 if fo is True else 0.0))
        score = base + adj
        if best is None or score > best[0]:
            best = (score, sim, overlap, c, cname, fo)
    # fall back to the pass-1 winner if pass-2 somehow produced nothing
    if best is None:
        base, sim, overlap, c, cname = pass1[0]
        best = (base, sim, overlap, c, cname, None)

    score, sim, overlap, c, cname, fo = best
    src_key = c.get("_source_key", source)
    is_curated = bool(c.get("_curated"))
    fo_tag = ":foodon_incompat" if fo is False else ""

    if score < _WEAK_SCORE:
        return {"match": None, "source_key": src_key, "similarity": sim,
                "confidence": "none", "reason": f"below_floor:{score:.2f}{fo_tag}",
                "matched_name": cname, "cleaned_query": cleaned}

    strong = (
        score >= _STRONG_SCORE
        and sim >= float(min_similarity)
        and (overlap > 0 or sim >= _HIGH_SIM_NO_OVERLAP)
    )
    if strong:
        confidence = "curated" if is_curated else "strong"
        reason = ("curated_link" if is_curated else "") + fo_tag
    else:
        confidence = "weak"
        reason = f"weak:{score:.2f}" + (":curated" if is_curated else "") + fo_tag
    return {
        "match": c, "source_key": src_key, "similarity": sim,
        "confidence": confidence, "reason": reason or "",
        "matched_name": cname, "cleaned_query": cleaned,
    }
