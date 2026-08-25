"""Resolve a recipe ingredient to a regional composition-table record.

Wraps the Elasticsearch vector lookup with query cleaning, a BM25 + similarity
rerank that *gates* on lexical overlap, and
a conservative food-class compatibility guard. Returns a Elasticsearch-candidate-shaped
dict plus a confidence label ("curated" | "strong" | "weak" | "none") so callers
can flag weak matches instead of silently trusting or zeroing them.
"""

from __future__ import annotations

import math
import re
from recipe_wrangler.repositories.vector_matchers import (  # noqa: E402
    query_eu_nutrition_candidates,
    query_hungarian_nutrition_candidates,
    query_irish_nutrition_candidates,
    query_slovenian_nutrition_candidates,
)

# --------------------------------------------------------------------------- #
# Query cleaning
# --------------------------------------------------------------------------- #
_PAREN_RE = re.compile(r"\([^)]*\)")
_QUALIFIER_RE = re.compile(
    r"\b(?:ripe|chopped|minced|diced|sliced|grated|shredded|"
    r"peeled|trimmed|drained|rinsed|melted|"
    r"softened|thawed|optional|divided|finely|roughly|coarsely|thinly|"
    r"freshly|large|small|medium|jumbo|organic|prepared|homemade|"
    r"store[- ]?bought|good[- ]?quality|best[- ]?quality|"
    r"to taste|to serve|to garnish|to drizzle|to finish|to brush|to grease|"
    r"for serving|for garnish|for dusting|for sprinkling|for frying|for greasing|"
    r"spray oil|cooking spray|"
    r"no[- ]added[- ]salt|reduced[- ]salt|salt[- ]reduced|low[- ]salt|"
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
    # Preserve the nutrition-relevant prepared state when an upstream parser
    # leaves a container noun at the start of the food name.
    s = re.sub(r"\bcans?\b", "canned", s)
    s = _PAREN_RE.sub(" ", s)
    s = s.replace(",", " ")            # commas usually separate prep notes; keep the words
    s = _QUALIFIER_RE.sub(" ", s)
    s = _NON_NAME_RE.sub(" ", s)
    # Drop a leading quantity/unit when an upstream parser fuses it into the name.
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
    "chickpea": "chickpea", "chickpeas": "chickpea",
    "garbanzo": "chickpea", "garbanzos": "chickpea",
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
                     r"breadcrumb|crumbs|cracker|biscuit|biscuits|rusk|tortilla|cereal|granola|muesli|"
                     r"tapioca|cornstarch|corn\s*starch|arrowroot|grits|pastry|pastries)\b"),
    ("animal_protein", r"\b(beef|steak|chuck|brisket|sirloin|tenderloin|ribeye|"
                       r"rib[- ]?eye|veal|oxtail|pork|ham|bacon|sausage|chorizo|"
                       r"prosciutto|pancetta|salami|pepperoni|kielbasa|bratwurst|"
                       r"lamb|mutton|chicken|turkey|duck|goose|quail|pheasant|"
                       r"fish|salmon|tuna|cod|haddock|tilapia|trout|bass|halibut|"
                       r"snapper|mackerel|sardine|sardines|anchov(?:y|ies)|herring|kipper|"
                       r"flounder|sole|pollock|catfish|mahi|swordfish|shrimp|prawn|"
                       r"crab|lobster|clam|mussel|oyster|scallop|squid|calamari|"
                       r"octopus|crayfish|crawfish|frog|rabbit|venison|bison|"
                       r"liver|kidney|tripe|gizzard|cold\s*cuts?)\b"),
    ("leafy_green", r"\b(lettuce|spinach|arugula|rocket|kale|chard|collard|"
                    r"watercress|endive|escarole|radicchio|mizuna|mesclun|"
                    r"romaine|cabbage|bok\s*choy|pak\s*choi|tatsoi|cress|"
                    r"asian\s*greens?|greens)\b"),
    ("fruit", r"\b(apple|banana|orange|lemon|lime|grape|grapefruit|olive|olives|berry|berries|"
              r"strawberr|blueberr|raspberr|blackberr|cranberr|boysenberr|"
              r"gooseberr|cherry|cherries|peach|peaches|nectarine|plum|prune|"
              r"apricot|mango|mangoe?s|pineapple|melon|watermelon|cantaloupe|"
              r"honeydew|kiwi|papaya|guava|fig|figs|date|dates|raisin|currant|"
              r"sultana|pomegranate|pear|pears|persimmon|lychee|passionfruit|"
              r"tangerine|mandarin|clementine|rhubarb)\b"),
    ("vegetable", r"\b(carrots?|onion|shallot|leek|garlic|potato|potatoes|sweet\s*potato|"
                  r"yam|tomato|tomatoes|cucumber|zucchini|courgette|squash|pumpkin|"
                  r"eggplant|aubergine|capsicum|broccoli|cauliflower|celery|"
                  r"asparagus|artichoke|beet|beets|beetroot|radish|turnip|parsnip|"
                  r"rutabaga|swede|fennel|mushroom|mushrooms|corn|sweetcorn|peas?|"
                  r"green\s*bean|brussels?\s*sprout|okra|scallion|spring\s*onion|"
                  r"chayote|kohlrabi|jicama|daikon|ginger|galangal|horseradish|"
                  r"plantain|cassava|taro|vegetable|vegetables)\b"),
    # A composed salad is not a condiment merely because its source text says
    # "with a little dressing". Keep this before condiment_sauce so the food
    # class guard rejects mayonnaise/dressing composition rows for salad.
    ("salad", r"\bsalad\b(?!\s+dressing)"),
    ("condiment_sauce", r"\b(sauce|ketchup|mayonnaise|mustard|relish|salsa|"
                        r"dressing|vinaigrette|marinade|gravy|chutney|dip|paste|"
                        r"spread|jam|jelly|preserve|marmalade|pickle|vinegar|"
                        r"worcestershire|tabasco|sriracha|hoisin|teriyaki|"
                        r"barbecue|bbq|aioli|pesto|tapenade|hummus|guacamole|"
                        r"tomato\s*paste|stock|broth|bouillon|consomme)\b"),
    # Keep seasonings after concrete food identities. A qualifier such as
    # "no-added-salt tomatoes" must still classify as a vegetable, while
    # standalone salt/cumin/thyme continue to classify as seasonings.
    ("spice_herb", r"\b(salt|peppercorn|cinnamon|cumin|coriander|paprika|turmeric|"
                   r"nutmeg|clove|cardamom|fenugreek|saffron|cayenne|allspice|"
                   r"mace|anise|caraway|sumac|za'?atar|garam\s*masala|"
                   r"curry\s*powder|chili\s*powder|chilli\s*powder|five\s*spice|"
                   r"basil|oregano|thyme|rosemary|sage|cilantro|dill|tarragon|"
                   r"marjoram|bay\s*leaf|chive|chives|spice|spices|seasoning|herb)\b"),
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
        ("salad", "condiment_sauce"),
        ("animal_protein", "legume"), ("animal_protein", "nut_seed"),
        ("animal_protein", "fruit"), ("animal_protein", "vegetable"),
        ("animal_protein", "leafy_green"), ("animal_protein", "spice_herb"),
        ("animal_protein", "sweetener"), ("animal_protein", "oil_fat"),
        ("animal_protein", "alcohol"), ("animal_protein", "condiment_sauce"),
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
        ("oil_fat", "fruit"), ("oil_fat", "leafy_green"),
        ("oil_fat", "vegetable"), ("oil_fat", "sweetener"),
        ("sweetener", "leafy_green"), ("sweetener", "vegetable"),
        ("vegetable", "grain_cereal"),  # red onion ↛ red rice, etc.
        ("legume", "nut_seed"),         # chickpea ↛ peanut
        ("nut_seed", "vegetable"),      # sunflower seed ↛ sweetcorn kernels
        ("leafy_green", "condiment_sauce"),
    ]
)


def classes_compatible(a: str, b: str) -> bool:
    if a == b:
        return True
    pair = frozenset((a, b))
    if pair in _HARD_INCOMPATIBLE:
        return False
    if "other" in (a, b) or "condiment_sauce" in (a, b):
        return True  # too ambiguous to reject on
    return True


_ANIMAL_KIND_PATTERNS = (
    ("fish", re.compile(r"\b(?:cod|salmon|tuna|trout|haddock|hake|mackerel|sardine|anchovy|eel|fish)\b", re.I)),
    ("shellfish", re.compile(r"\b(?:shrimp|prawn|crab|lobster|oyster|mussel|scallop|clam)\b", re.I)),
    ("poultry", re.compile(r"\b(?:chicken|turkey|duck|goose)\b", re.I)),
    ("pork", re.compile(r"\b(?:pork|pig|ham|bacon|rasher)\b", re.I)),
    ("beef", re.compile(r"\b(?:beef|veal|cow)\b", re.I)),
    ("lamb", re.compile(r"\b(?:lamb|mutton)\b", re.I)),
)


def animal_kind(name: str) -> str | None:
    for kind, pattern in _ANIMAL_KIND_PATTERNS:
        if pattern.search(str(name or "")):
            return kind
    return None


def animal_kinds_compatible(query_name: str, candidate_name: str) -> bool:
    query_kind = animal_kind(query_name)
    candidate_kind = animal_kind(candidate_name)
    return not query_kind or not candidate_kind or query_kind == candidate_kind


def ingredient_forms_compatible(query_name: str, candidate_name: str) -> bool:
    """Reject a few exact form contradictions embeddings routinely confuse."""
    query_words = set(_TOKEN_RE.findall(str(query_name or "").casefold()))
    candidate_words = set(
        _TOKEN_RE.findall(str(candidate_name or "").casefold())
    )
    # "spray oil" must not become an unrelated product whose brand happens to
    # contain Spray (for example Ocean Spray cranberry drink).
    if "oil" in query_words and "oil" not in candidate_words:
        return False
    if (
        "canned" in query_words
        and not ({"canned", "tinned", "cooked", "boiled"} & candidate_words)
    ):
        return False
    # Reject seed products when the recipe asks for the flesh of a squash or
    # pumpkin. Composition tables frequently rank "pumpkin seed" above the
    # ordinary vegetable because both identity words overlap.
    if (
        {"pumpkin", "squash"} & query_words
        and "seed" not in query_words
        and {"seed", "seeds"} & candidate_words
    ):
        return False
    # A named animal ingredient must retain that animal identity. This catches
    # cases such as tuna in spring water matching bottled spring water.
    query_animal = animal_kind(query_name)
    if query_animal and animal_kind(candidate_name) != query_animal:
        return False
    # Blue-cheese dressing and similar condiments are not interchangeable with
    # the cheese itself at the ingredient's full weight.
    if (
        "cheese" in query_words
        and "dressing" not in query_words
        and "dressing" in candidate_words
    ):
        return False
    # A measured cup of liquid stock cannot use concentrated stock-cube
    # nutrition at the liquid weight.
    if (
        "stock" in query_words
        and {"cube", "cubes"} & candidate_words
        and not ({"cube", "cubes"} & query_words)
    ):
        return False
    return True


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
        return [("hungarian", query_hungarian_nutrition_candidates), ("eu", query_eu_nutrition_candidates)]
    if source == "eu":
        return [("eu", query_eu_nutrition_candidates)]
    if source == "slovenian":
        return [("slovenian", query_slovenian_nutrition_candidates), ("eu", query_eu_nutrition_candidates)]
    if source == "irish":
        return [("irish", query_irish_nutrition_candidates), ("eu", query_eu_nutrition_candidates)]
    raise ValueError(
        f"Unsupported nutrition source '{source}'. Supported sources: irish, hungarian, eu, slovenian"
    )


# Tuning knobs (kept loose; the audit drives these).
_STRONG_SCORE = 0.50
_WEAK_SCORE = 0.30
_HIGH_SIM_NO_OVERLAP = 0.90  # zero-overlap candidate must clear this to survive
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

    # 1) Gather the complete candidate pool for the selected region. Regional
    #    and EU hits compete in one reranking pass; EU is not a second-stage
    #    fallback. This lets a more precise regional row win without forcing a
    #    weak regional candidate over a stronger EU composition match.
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

    if not cands:
        return {"match": None, "source_key": source, "similarity": None,
                "confidence": "none", "reason": "no_candidates", "matched_name": None,
                "cleaned_query": cleaned}

    # 2) Rerank using Elasticsearch similarity, lexical overlap and hard local
    #    semantic guards. FoodOn used to add a sparse soft nudge here through
    #    Neo4j. It made profiling depend on the graph at request time while
    #    failing open whenever the graph was unavailable, so it was neither a
    #    reliable safety boundary nor worth the latency. The food-class and
    #    animal-species checks below are deterministic and are the hard gates.
    # Hard semantic boundary before ranking. A high embedding similarity must
    # never make cod become pork/beef, or an olive become a "beef olive" dish.
    cands = [
        c
        for c in cands
        if classes_compatible(q_class, food_class(_candidate_name(c)))
        and animal_kinds_compatible(cleaned, _candidate_name(c))
        and ingredient_forms_compatible(cleaned, _candidate_name(c))
    ]
    if not cands:
        return {"match": None, "source_key": source, "similarity": None,
                "confidence": "none", "reason": "no_semantically_compatible_candidates",
                "matched_name": None, "cleaned_query": cleaned}

    names = [_candidate_name(c) for c in cands]
    corpus = [_tokens(n) for n in names]
    bm = _bm25_scores(q_tokens, corpus) if q_tokens else [0.0] * len(cands)
    q_set = set(q_tokens)
    # raw tokenisation of the *original* name (no stopword/singular folding) —
    # used only for the cooking-state / processed-marker exemption.
    q_raw_words = set(_TOKEN_RE.findall(str(name or "").lower()))
    n_q = max(1, len(q_tokens))

    max_rrf = max(
        (float(candidate.get("rrf_score") or 0.0) for candidate in cands),
        default=0.0,
    )

    def _base_score(c, cname, ctoks, bms):
        d = c.get("distance")
        sim = (1.0 - float(d)) if d is not None else 0.0
        ctok_set = set(ctoks)
        overlap = len(q_set & ctok_set)
        rrf = (
            float(c.get("rrf_score") or 0.0) / max_rrf
            if max_rrf > 0.0
            else 0.0
        )
        pen = 0.0
        if overlap == 0 and sim < _HIGH_SIM_NO_OVERLAP:
            pen -= 0.5
        if not classes_compatible(q_class, food_class(cname)):
            pen -= 1.0
        c_raw_words = set(_TOKEN_RE.findall(str(cname or "").lower()))
        if (_PROCESSED_MARKERS & c_raw_words) - q_raw_words:
            pen -= 0.12  # cooking-state / processed / branded marker the query didn't ask for
        elif "raw" in c_raw_words and not (_COOKING_STATES & q_raw_words):
            pen += 0.06  # state-less query -> nudge toward the raw/uncooked record
        return (
            0.60 * sim
            + 0.30 * float(bms)
            + 0.10 * rrf
            + 0.15 * min(1.0, overlap / n_q)
            + pen,
            sim,
            overlap,
        )

    ranked = sorted(
        (( *_base_score(c, cn, ct, bms), c, cn) for c, cn, ct, bms in zip(cands, names, corpus, bm)),
        key=lambda t: -t[0],
    )
    score, sim, overlap, c, cname = ranked[0]
    src_key = c.get("_source_key", source)

    if score < _WEAK_SCORE:
        return {"match": None, "source_key": src_key, "similarity": sim,
                "confidence": "none", "reason": f"below_floor:{score:.2f}",
                "matched_name": cname, "cleaned_query": cleaned}

    strong = (
        score >= _STRONG_SCORE
        and sim >= float(min_similarity)
        and (overlap > 0 or sim >= _HIGH_SIM_NO_OVERLAP)
    )
    if strong:
        confidence = "strong"
        reason = ""
    else:
        confidence = "weak"
        reason = f"weak:{score:.2f}"
    return {
        "match": c, "source_key": src_key, "similarity": sim,
        "confidence": confidence, "reason": reason or "",
        "matched_name": cname, "cleaned_query": cleaned,
    }
