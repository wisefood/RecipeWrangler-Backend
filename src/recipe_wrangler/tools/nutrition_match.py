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
import unicodedata
from functools import lru_cache

from recipe_wrangler.repositories.vector_matchers import (  # noqa: E402
    get_nutrition_candidate_by_source_id,
    query_eu_nutrition_candidates,
    query_hungarian_nutrition_candidates,
    query_irish_nutrition_candidates,
    query_slovenian_nutrition_candidates,
)
from recipe_wrangler.utils.pipeline_data_pg import load_pipeline_data
from recipe_wrangler.utils.non_food_ingredients import (
    is_unambiguous_non_food_ingredient,
)

# --------------------------------------------------------------------------- #
# Query cleaning
# --------------------------------------------------------------------------- #
_PAREN_RE = re.compile(r"\([^)]*\)")
_QUALIFIER_RE = re.compile(
    r"\b(?:ripe|chopped|minced|diced|sliced|grated|shredded|"
    r"peeled|trimmed|drained|rinsed|melted|"
    r"softened|thawed|optional|divided|finely|roughly|coarsely|thinly|"
    r"freshly|large|small|medium|jumbo|organic|prepared|homemade|chilled|"
    r"store[- ]?bought|good[- ]?quality|best[- ]?quality|"
    r"to taste|to serve|to garnish|to drizzle|to finish|to brush|to grease|to top up|"
    r"for serving|for garnish|for dusting|for sprinkling|for frying|for greasing|"
    r"spray oil|cooking spray|"
    r"no[- ]added[- ]salt|reduced[- ]salt|salt[- ]reduced|low[- ]salt|"
    r"no[- ]added[- ]sodium|reduced[- ]sodium|sodium[- ]reduced|low[- ]sodium|"
    r"plus more|plus extra|plus|or more|as needed|of your choice|approximately|about|"
    r"halved|quartered|cubed|julienned)\b",
    re.IGNORECASE,
)
_NON_NAME_RE = re.compile(r"[^a-z0-9\s'/-]")
# ponytail: catches the "x"/"s" residue an upstream synonym-collapse step leaves
# behind (e.g. "courgette zucchini x s zucchini", "...flour X") — not a general
# parser, just strips these two stray single-char tokens wherever they land.
_STRAY_TOKEN_RE = re.compile(r"\b[xs]\b", re.IGNORECASE)
# Unicode vulgar fractions -> ascii, so the leading-quantity stripper below can
# see them (_NON_NAME_RE would otherwise delete them as unrecognised chars).
_UNICODE_FRACTION_MAP = {
    "¼": "1/4", "½": "1/2", "¾": "3/4",
    "⅓": "1/3", "⅔": "2/3", "⅛": "1/8", "⅜": "3/8",
    "⅝": "5/8", "⅞": "7/8",
}
_LEADING_QTY_RE = re.compile(r"^\s*\d+(?:[.\-/]\d+)*\s*(?:%|cups?|tbsps?|tsps?|"
                             r"tablespoons?|teaspoons?|oz|ounces?|lbs?|pounds?|g|"
                             r"grams?|kg|ml|l|cans?|packages?|sticks?|cloves?)?\b",
                             re.IGNORECASE)


def _normalize_food_phrases(value: str) -> str:
    """Normalize high-value culinary synonyms used by retrieval and identity."""
    value = re.sub(r"\bapple\s+puree\b|\bapplesauce\b", "apple sauce", value)
    value = re.sub(r"\binstant\s+yeast\b", "dried yeast", value)
    value = re.sub(r"\bbread\s+soda\b", "bicarbonate of soda", value)
    value = re.sub(r"\balmond\s+meal\b", "almond flour", value)
    value = re.sub(r"\bchicken\s+tenderloins?\b", "chicken breast", value)
    value = re.sub(r"\bheavy\s+cream\b", "whipping cream", value)
    value = re.sub(r"\bprosciutto\b", "dry cured ham", value)
    value = re.sub(r"\bre[- ]fried\b", "refried", value)
    value = re.sub(r"\bchick\s+peas?\b", "chickpea", value)
    value = re.sub(r"\bsun[- ]dried\s+tomato\s+pesto\b", "red pesto", value)
    value = re.sub(r"\bharissa\s+paste\b", "harissa", value)
    value = re.sub(r"\bdry\s+mustard\b", "mustard powder", value)
    value = re.sub(r"\bgarlic[- ]infused\s+olive\s+oil\b", "olive oil", value)
    value = re.sub(r"\bportobello\s+mushrooms?\b", "mushroom all types raw", value)
    value = re.sub(r"\borange\s+kumara\s+sweet[- ]potato\b", "sweet potato", value)
    value = re.sub(r"\bjasmine\s+rice\b", "white long grain rice raw", value)
    value = re.sub(r"\bsoy\s+milk\b", "soy drink", value)
    value = re.sub(r"\bkecap\s+manis\b", "sweet soy sauce ketjap", value)
    return re.sub(
        r"^\s*tamari(?:\s+soy\s+sauce|\s+sauce)?\s*$",
        "soy sauce",
        value,
    )


def clean_query(name: str) -> str:
    # Accent-fold first -- otherwise letters like e/i in "crème fraîche"
    # vanish (downstream regexes are ASCII-only, treating accented chars as
    # separators), mangling the query into garbage before retrieval runs.
    # Found 2026-09-21 via "crème fraîche" -> "cr me fra che".
    s = _ascii_fold(str(name or "")).lower()
    if re.fullmatch(
        r"\s*(?:non[- ]?stick\s+)?(?:vegetable\s+)?"
        r"(?:cooking spray|spray(?: oil)?|oil spray)\s*",
        s,
    ):
        return "spray oil"
    s = re.sub(
        r"^\s*([a-z]+)\s+(?:cooking spray|spray oil|oil spray)\s*$",
        r"\1 oil",
        s,
    )
    s = re.sub(r"\boil spray\b", "oil", s)
    s = re.sub(r"\beaster eggs?\b", "chocolate", s)
    s = re.sub(r"\ball[- ]spice\b", "allspice", s)
    s = re.sub(r"\b(chil(?:i|li))\s+power\b", r"\1 powder", s)
    s = _normalize_food_phrases(s)
    s = re.sub(r"\bsilver\s*beet\b", "chard", s)
    for frac, ascii_frac in _UNICODE_FRACTION_MAP.items():
        s = s.replace(frac, ascii_frac + " ")
    # Preserve the nutrition-relevant prepared state when an upstream parser
    # leaves a container noun at the start of the food name.
    s = re.sub(r"\bcans?\b", "canned", s)
    s = _PAREN_RE.sub(" ", s)
    s = s.replace(",", " ")            # commas usually separate prep notes; keep the words
    s = _QUALIFIER_RE.sub(" ", s)
    s = _NON_NAME_RE.sub(" ", s)
    s = _STRAY_TOKEN_RE.sub(" ", s)
    # Drop a leading quantity/unit when an upstream parser fuses it into the name.
    prev = None
    while prev != s:
        prev = s
        s = _LEADING_QTY_RE.sub(" ", s, count=1).lstrip()
    s = re.sub(r"\s+", " ", s).strip(" -'/")
    # Collapse immediately-repeated words left behind once the stray tokens
    # between them are gone (e.g. "zucchini x s zucchini" -> "zucchini zucchini").
    words = s.split(" ")
    deduped = [w for i, w in enumerate(words) if i == 0 or w != words[i - 1]]
    return " ".join(deduped)


def _norm(s: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(s or "").lower())).strip()


# --------------------------------------------------------------------------- #
# Tokens / BM25 (self-contained copy)
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_UNMATCHABLE_PLACEHOLDERS = {
    "extra", "ingredient", "ingredients", "filling ingredient",
    "filling ingredients", "hot liquid",
}


def _ascii_fold(s: str) -> str:
    """Strip accents so "pâté" tokenizes as "pate" instead of vanishing
    entirely under `_TOKEN_RE = [a-z0-9]+` (accented chars act as
    separators, and the resulting 1-char fragments get dropped)."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
_STOP = {
    "and", "or", "with", "of", "the", "in", "a", "an", "fresh",
    "whole", "large", "small", "medium", "cup", "cups", "tbsp", "tsp",
    "tablespoon", "teaspoon", "style", "type", "kind", "prepared", "made", "from",
    "to", "as", "for", "fl", "oz", "ready", "mixed", "baby",
    # Colour words: real information when paired with the right food noun
    # (red pepper, brown rice), but as the ONLY shared token they give a
    # totally unrelated food (a fish, a sauce) false overlap credit against
    # a completely different food that merely shares the same colour word
    # (see "brown onion" -> "Brown meagre, raw"). "orange" excluded — it is
    # itself a food name, not just a colour. "black"/"white" excluded too —
    # unlike red/green/brown pepper (bell-pepper colour variants), "black
    # pepper" and "white pepper" are themselves specific, different spices,
    # not a colour modifying a shared base food. Stopping them let "ground
    # black pepper" lose to "Pepper, cayenne, ground" on shared "ground"
    # once the one word distinguishing black pepper from cayenne was gone —
    # verified live regression, not hypothetical.
    "brown", "red", "green", "yellow", "purple", "pink",
    # State/prep/form words: same trap as colour words above, just a
    # different word class. Found 2026-09-21 via a full-corpus sweep: each
    # of these was the ONLY shared token behind a confidently wrong "strong"
    # match, e.g. "ground meat"->"Coffee, ground", "wild garlic"->"Wild
    # boar, raw", "stewed tomatoes"->"Heart, pig, stewed", "dry lentils"->
    # "Cider, dry", "smoked garlic"->"Sprat fillet smoked", "savoy
    # cabbage"->"Savoy-style sponge cake", "tub margarine"->"Tub gurnard,
    # raw", "celery flakes"->"Chocolate flakes av", "corn meal"->"Meal
    # replacement...", "crushed tomatoes"->"Aniseed comfits crushed...",
    # "almond sticks"->"Seafood sticks", "almond extract"->"Yeast extract",
    # "pea shoots"->"Bamboo shoots, raw", "vegetable stock pot"->"cooking
    # pot", "light muscovado sugar"->"Juice drink light", "mild chiles"->
    # "Juice multifruit mild...", "english cucumber"->"Muffins, English
    # style, white". Same rationale as colours: real information when
    # paired with the right food noun, false overlap credit as the ONLY
    # shared word. "white"/"black" intentionally NOT added here -- see the
    # colour-word comment above, they're spice identities not states.
    "ground", "stewed", "dry", "smoked", "crushed", "head", "tub", "savoy",
    "flakes", "meal", "stick", "sticks", "extract", "shoot", "shoots",
    "pot", "light", "mild", "wild", "bamboo", "english",
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
    # Found 2026-09-21: neither has a shared substring with its usual EU
    # composition-table name, so no prefix/compound rescue can bridge them
    # -- genuine vocabulary gaps, confirmed scoring below the match floor
    # despite the right food existing in the corpus.
    "scallion": "onion", "scallions": "onion",
    "jalapeno": "chilli", "jalapenos": "chilli",
    # _singular() strips "-ies" -> "-y" (correct for curries/berries), which
    # mis-stems "chillies" to "chilly" (the weather word) instead of
    # "chilli" — narrow irregular-plural exception rather than a general
    # rule change.
    "chilly": "chilli",
    "chili": "chilli",
    "chile": "chilli", "chiles": "chilli",
    "caster": "castor",
    "wholegrain": "wholemeal",
    "reduced": "low",
    "half": "low",
    "frosting": "icing",
    "sweetcorn": "corn",
    "oatmeal": "oat",
    "macaroni": "pasta",
    "natural": "plain",
    "whitefish": "fish",
    "beet": "beetroot", "beets": "beetroot",
    "broccolini": "broccoli",
    "silken": "silky",
    "peppercorn": "pepper", "peppercorns": "pepper",
    "cornflour": "cornstarch",
    "bocconcini": "mozzarella",
    "cannelini": "cannellini",
    "five": "5",
    "canola": "rapeseed",
    "nonfat": "skimmed",
}
# _SYNONYMS is asymmetric (e.g. "coriander" -> "cilantro" but not the
# reverse), so a query using only the value-side word ("cilantro" alone)
# never got the key-side word added and could fail to overlap with a
# candidate that only says "coriander". Look up both directions.
_SYNONYMS_REV = {v: k for k, v in _SYNONYMS.items()}
# Several spellings map to the same canonical chilli token. Keep the common
# American spelling as the reverse retrieval expansion; otherwise adding the
# rarer "chiles" variant silently changes every existing chilli query.
_SYNONYMS_REV["chilli"] = "chili"
_RETRIEVAL_EXPANSIONS = {
    # Passata is sold/indexed as tomato puree or coulis in several European
    # composition tables; "tomato" alone retrieves raw tomatoes too strongly.
    "passata": {"tomato", "puree", "coulis"},
}


def _singular(t: str) -> str:
    if t == "peas":
        return "pea"
    if t == "greens":
        return "greens"
    if t == "cloves":
        return "clove"
    if t == "pistachios":
        return "pistachio"
    if t == "bases":
        return "base"
    if t == "avocados":
        return "avocado"
    if t == "tortillas":
        return "tortilla"
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
    # _ascii_fold exists precisely so an accented word tokenizes as its
    # plain-letter form ("pâté" -> "pate") instead of the accent character
    # acting as a token separator and silently dropping the fragment (â
    # splits "pâté" into "p"/"t"/"" under _TOKEN_RE = [a-z0-9]+, each too
    # short to survive the len<=1 filter). It was only ever wired into the
    # processed-marker raw-word sets, not here — every accented ingredient
    # name (routine in the Hungarian/Slovenian datasets) was silently losing
    # tokens on both the query and candidate side.
    normalized = _ascii_fold(str(text or "").lower())
    normalized = re.sub(r"\bcornstarch\b|\bcornflour\b", "corn starch", normalized)
    for raw in _TOKEN_RE.findall(normalized):
        if len(raw) <= 1:
            continue
        t = _singular(raw)
        if t in _STOP or len(t) <= 1:
            continue
        out.append(t)
        syn = (
            _SYNONYMS.get(raw) or _SYNONYMS.get(t)
            or _SYNONYMS_REV.get(raw) or _SYNONYMS_REV.get(t)
        )
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
              r"brie|feta|paneer|curd|whey|ghee|quark|half\s*and\s*half|creme\s*fraiche|"
              r"clotted\s*cream|sour\s*cream|condensed\s*milk|evaporated\s*milk)\b"
              r"|\bbutter\b(?!\s*(?:bean|nut|scotch|milk|head\s*lettuce))"),
    ("egg", r"\begg(?:s)?\b(?!\s*plant|nog|roll)"),
    ("oil_fat", r"\b(oil|lard|shortening|tallow|dripping|suet|margarine|fat)\b"),
    ("sweetener", r"\b(sugar|honey|syrup|molasses|agave|stevia|sucralose|"
                  r"aspartame|sweetener|nectar|treacle|jaggery|toffees?|"
                  r"fudge|caramels?)\b"),
    ("nut_seed", r"\b(almond|walnut|pecan|cashew|peanut|hazelnut|filbert|pistachio|"
                 r"macadamia|brazil\s*nut|pine\s*nut|pinenut|sesame|tahini|"
                 r"sunflower\s*seed|pumpkin\s*seed|flax|flaxseed|chia|hemp\s*seed|"
                 r"poppy\s*seed|coconut|chestnut|nuts?)\b"),
    ("legume", r"\b(bean|beans|lentil|lentils|chickpea|chickpeas|garbanzo|edamame|"
               r"split\s*pea|black[- ]?eyed\s*pea|"
               # "sauce" excluded -- "soy sauce" is a condiment, not a legume;
               # legume checked before condiment_sauce in this list, same
               # order-collision as the vegetable/pepper case above.
               r"soy(?!\s*sauce)|soya(?!\s*sauce)|soybean)\b"),
    ("grain_cereal", r"\b(flour|rice|oat|oats|oatmeal|wheat|barley|rye|cornmeal|"
                     r"polenta|semolina|couscous|bulgur|bulghur|quinoa|millet|"
                     r"farro|spelt|pasta|noodle|noodles|spaghetti|macaroni|penne|"
                     r"linguine|fettuccine|lasagn|vermicelli|orzo|bread|crispbread|"
                     r"breadcrumb|crumbs|cracker|biscuit|biscuits|rusk|tortilla|cereal|granola|muesli|"
                     r"tapioca|cornstarch|corn\s*starch|arrowroot|grits|pastry|pastries)\b"),
    ("animal_protein", r"\b(beef|steak|chuck|brisket|sirloin|tenderloin|ribeye|"
                       r"rib[- ]?eye|veal|oxtail|pork|ham|bacon|sausage|chorizo|"
                       r"prosciutto|pancetta|salami|pepperoni|kielbasa|bratwurst|"
                       r"lamb|mutton|sheep|boar|chicken|turkey|duck|goose|quail|pheasant|"
                       r"fish|salmon|tuna|cod|haddock|tilapia|trout|bass|halibut|"
                       r"snapper|mackerel|sardine|sardines|anchov(?:y|ies)|herring|kipper|"
                       r"flounder|sole|pollock|catfish|mahi|swordfish|shrimp|prawn|"
                       r"crab|lobster|clam|mussel|oyster|scallop|squid|calamari|"
                       r"octopus|crayfish|crawfish|frog|rabbit|venison|bison|"
                       r"liver|kidney|tripe|gizzard|cold\s*cuts?|shellfish|meat|"
                       r"eel|ray|pomfret|ling|coley|dentex|bloater|winkle|hen|poussin|"
                       r"hake|pike|plaice|mullet|whiting)\b"),
    ("leafy_green", r"\b(lettuce|spinach|arugula|rocket|kale|chard|collard|"
                    r"watercress|endive|escarole|radicchio|mizuna|mesclun|"
                    r"romaine|cabbage|bok\s*choy|pak\s*choi|tatsoi|cress|chicory|"
                    r"asian\s*greens?|greens)\b"),
    ("fruit", r"\b(apple|banana|orange|lemon|lime|grape|grapefruit|olive|olives|berry|berries|"
              r"strawberr|blueberr|raspberr|blackberr|cranberr|boysenberr|"
              r"gooseberr|cherry|cherries|peach|peaches|nectarine|plum|prune|"
              r"apricot|mango|mangoe?s|pineapple|melon|watermelon|cantaloupe|"
              r"honeydew|kiwi|papaya|guava|fig|figs|date|dates|raisin|currant|"
              r"sultana|pomegranate|pear|pears|persimmon|lychee|passionfruit|"
              r"tangerine|mandarin|clementine|rhubarb|fruits?)\b"),
    ("vegetable", r"\b(carrots?|onion|shallot|leek|garlic|potato|potatoes|sweet\s*potato|"
                  r"yam|tomato|tomatoes|cucumber|zucchini|courgette|squash|pumpkin|"
                  r"eggplant|aubergine|capsicum|"
                  # "chili/chilli pepper" as an exact compound, not bare
                  # "pepper" -- "Chili pepper, raw" was falling to "other"
                  # (only "capsicum" was listed), which let a prefix-overlap
                  # rescue wrongly credit "pepperoni" against it. A bare
                  # "pepper" alternative was tried and reverted: composition
                  # names like "Pepper, cayenne, ground" put the spice
                  # modifier AFTER "pepper", which a before-only negative
                  # lookbehind guard can't catch, and vegetable is checked
                  # before spice_herb in this list -- it misclassified that
                  # exact record as a vegetable instead of a spice. The
                  # narrower compound-only match has no such ambiguity.
                  r"chil[il]?\s*pepper|"
                  r"broccoli|cauliflower|celery|"
                  r"asparagus|artichoke|beet|beets|beetroot|radish|turnip|parsnip|"
                  r"rutabaga|swede|fennel|mushroom|mushrooms|corn|sweetcorn|peas?|"
                  r"green\s*bean|brussels?\s*sprout|okra|scallion|spring\s*onion|"
                  r"chayote|kohlrabi|jicama|daikon|ginger|galangal|horseradish|"
                  r"plantain|cassava|taro|seaweed|salsify|gourd|celeriac|"
                  r"vegetable|vegetables)\b"),
    # A composed salad is not a condiment merely because its source text says
    # "with a little dressing". Keep this before condiment_sauce so the food
    # class guard rejects mayonnaise/dressing composition rows for salad.
    ("salad", r"\bsalad\b(?!\s+dressing)"),
    ("condiment_sauce", r"\b(sauce|ketchup|mayonnaise|mustard|relish|salsa|"
                        r"dressing|vinaigrette|marinade|gravy|chutney|dip|paste|"
                        r"passata|spread|jam|jelly|preserve|marmalade|pickle|vinegar|"
                        r"worcestershire|tabasco|sriracha|hoisin|teriyaki|"
                        r"barbecue|bbq|aioli|pesto|tapenade|hummus|guacamole|harissa|"
                        r"tomato\s*paste|stock|broth|bouillon|consomme)\b"),
    # Keep seasonings after concrete food identities. A qualifier such as
    # "no-added-salt tomatoes" must still classify as a vegetable, while
    # standalone salt/cumin/thyme continue to classify as seasonings.
    ("spice_herb", r"\b(salt|peppercorn|cinnamon|cumin|coriander|paprika|turmeric|"
                   r"nutmeg|clove|cardamom|fenugreek|saffron|cayenne|allspice|"
                   r"mace|anise|caraway|sumac|za'?atar|garam\s*masala|"
                   r"curry\s*powder|chili\s*powder|chilli\s*powder|five\s*spice|"
                   r"basil|oregano|thyme|rosemary|sage|cilantro|dill|tarragon|"
                   r"marjoram|bay\s*leaf|chive|chives|spice|spices|seasoning|herb|"
                   r"parsley|mint|lovage|chervil|sorrel)\b"),
]
_CLASS_RES = [(c, re.compile(p, re.IGNORECASE)) for c, p in _CLASS_PATTERNS]


def food_class(name: str) -> str:
    n = str(name or "").lower()
    # Some class patterns only list the singular form (e.g. "apple", not
    # "apples") -- found via the 2026-09-16 curation pass: "baking apples"
    # and "baking potatoes" fell through to "other" and then matched "Baking
    # powder" on the shared word "baking", with no class guard to reject it.
    # Retry against singularized tokens rather than patching every pattern.
    singularized = " ".join(_singular(tok) for tok in _TOKEN_RE.findall(n))
    for cls, rx in _CLASS_RES:
        if rx.search(n) or (singularized != n and rx.search(singularized)):
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
        ("spice_herb", "vegetable"),
        ("spice_herb", "grain_cereal"),  # ground cinnamon ↛ cinnamon bread
        ("egg", "vegetable"), ("egg", "fruit"), ("egg", "grain_cereal"),
        ("egg", "leafy_green"), ("egg", "spice_herb"),
        ("egg", "dairy"),
        ("dairy", "leafy_green"), ("dairy", "vegetable"), ("dairy", "fruit"),
        ("dairy", "spice_herb"), ("dairy", "alcohol"),
        ("dairy", "legume"),      # yellow wax beans ↛ cheese in wax casing
        # ("dairy", "grain_cereal") deliberately NOT added: tried it
        # 2026-09-21, verified via the same before/after sweep used
        # elsewhere in this pass — it fixed "old fashion oats" (was
        # matching "Cheese Old Amsterdam") but broke "macaroni" and
        # "oatmeal", which went from a reasonable prepared-dish match
        # (macaroni cheese; porridge with milk) to no match at all. A
        # rejected match silently zero-fills downstream in
        # nutritional_calculator.py, which is a worse outcome than an
        # imperfect one for a plain, common ingredient — net negative here,
        # left out. Revisit with a narrower, evidence-specific gate instead
        # of a blanket class pair if more bad dairy/grain_cereal matches
        # turn up.
        ("leafy_green", "fruit"), ("leafy_green", "grain_cereal"),
        ("oil_fat", "fruit"), ("oil_fat", "leafy_green"),
        ("oil_fat", "vegetable"), ("oil_fat", "sweetener"),
        ("sweetener", "leafy_green"), ("sweetener", "vegetable"),
        ("sweetener", "fruit"),
        ("grain_cereal", "fruit"),      # wheat berries ↛ elderberries
        ("vegetable", "grain_cereal"),  # red onion ↛ red rice, etc.
        ("legume", "nut_seed"),         # chickpea ↛ peanut
        ("legume", "grain_cereal"),     # shell pasta ↛ broad bean, to shell
        ("nut_seed", "grain_cereal"),   # coconut flakes ↛ oat flakes
        ("nut_seed", "fruit"),          # passion fruit pulp ↛ coconut pulp
        ("nut_seed", "dairy"),          # tahini dressing ↛ yogurt dressing
        ("nut_seed", "vegetable"),      # sunflower seed ↛ sweetcorn kernels
        ("vegetable", "fruit"),         # dried mixed mushrooms ↛ dried mixed fruit
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
    # Fish and poultry used to be lumped into one "fish"/"poultry" bucket
    # each, so a query for "salmon fillets" (13g fat/100g, oily fish) could
    # legitimately match "Tuna fillet" (1g fat/100g, lean fish) because the
    # guard only checked "both are fish" — the exact same failure class as
    # the original "duck instead of chicken" bug this project started from,
    # just still open at the species level. Split into per-species kinds;
    # each specific-species pattern must come before the generic
    # fish/poultry fallback below so a named species doesn't fall through
    # to the loose bucket.
    ("salmon", re.compile(r"\bsalmon\b", re.I)),
    ("tuna", re.compile(r"\btuna\b", re.I)),
    ("cod", re.compile(r"\bcod\b", re.I)),
    ("trout", re.compile(r"\btrout\b", re.I)),
    ("bass", re.compile(r"\bbass\b", re.I)),
    ("catfish", re.compile(r"\bcatfish\b", re.I)),
    ("flounder", re.compile(r"\bflounder\b", re.I)),
    ("halibut", re.compile(r"\bhalibut\b", re.I)),
    ("herring", re.compile(r"\b(?:herring|kippers?)\b", re.I)),
    ("mahi", re.compile(r"\bmahi\b", re.I)),
    ("pollock", re.compile(r"\bpollock\b", re.I)),
    ("snapper", re.compile(r"\bsnapper\b", re.I)),
    ("sole", re.compile(r"\bsole\b", re.I)),
    ("swordfish", re.compile(r"\bswordfish\b", re.I)),
    ("tilapia", re.compile(r"\btilapia\b", re.I)),
    ("frog", re.compile(r"\bfrog\b", re.I)),
    ("haddock", re.compile(r"\bhaddock\b", re.I)),
    ("hake", re.compile(r"\bhake\b", re.I)),
    ("mackerel", re.compile(r"\bmackerel\b", re.I)),
    ("sardine", re.compile(r"\bsardines?\b", re.I)),
    ("anchovy", re.compile(r"\banchov(?:y|ies)\b", re.I)),
    ("eel", re.compile(r"\beel\b", re.I)),
    ("ray", re.compile(r"\bray\b", re.I)),
    ("pike", re.compile(r"\bpike\b", re.I)),
    ("pomfret", re.compile(r"\bpomfret\b", re.I)),
    ("ling", re.compile(r"\bling\b", re.I)),
    ("coley", re.compile(r"\bcoley\b", re.I)),
    ("dentex", re.compile(r"\bdentex\b", re.I)),
    ("bloater", re.compile(r"\bbloater\b", re.I)),
    ("winkle", re.compile(r"\bwinkle\b", re.I)),
    ("plaice", re.compile(r"\bplaice\b", re.I)),
    ("mullet", re.compile(r"\bmullet\b", re.I)),
    ("whiting", re.compile(r"\bwhiting\b", re.I)),
    ("jack_fish", re.compile(r"\bjack\b", re.I)),
    ("fish", re.compile(r"\b(?:fish|whitefish)\b", re.I)),  # generic/unnamed species only
    ("shellfish", re.compile(
        r"\b(?:shrimp|prawn|crab|lobster|oyster|mussel|scallop|clam|"
        r"squid|calamari|octopus|crayfish|crawfish|shellfish)\b", re.I
    )),
    ("chicken", re.compile(r"\b(?:chicken|hen|poussin)\b", re.I)),
    ("turkey", re.compile(r"\bturkey\b", re.I)),
    ("duck", re.compile(r"\bduck\b", re.I)),
    ("goose", re.compile(r"\bgoose\b", re.I)),
    ("quail", re.compile(r"\bquail\b", re.I)),
    ("pheasant", re.compile(r"\bpheasant\b", re.I)),
    ("rabbit", re.compile(r"\brabbit\b", re.I)),
    ("venison", re.compile(r"\bvenison\b", re.I)),
    ("bison", re.compile(r"\bbison\b", re.I)),
    ("pork", re.compile(r"\b(?:pork|pig|ham|bacon|rasher|boar|prosciutto|pancetta)\b", re.I)),
    # Lamb must precede the generic beef-cut words below. Otherwise the word
    # "steak" makes "lamb steak" resolve to beef before "lamb" is seen.
    ("lamb", re.compile(r"\b(?:lamb|mutton|sheep)\b", re.I)),
    ("beef", re.compile(
        r"\b(?:beef|veal|cow|steaks?|chuck|brisket|sirloin|tenderloin|ribeye|"
        r"rib[- ]?eye|oxtail)\b", re.I
    )),
    # Species-ambiguous by design — "sausage"/"liver"/"kidney"/"tripe"/
    # "gizzard"/"chorizo"/"salami"/"pepperoni" can each legitimately be any
    # species (chicken liver vs beef liver are nutritionally very
    # different), so no single kind is correct here. Left unmapped rather
    # than guessing one: `animal_kinds_compatible` only rejects when BOTH
    # sides resolve to a kind, so these still get zero species protection —
    # a known, documented residual gap, not an oversight.
)

_ORGAN_MEAT_WORDS = {
    "heart", "hearts", "liver", "livers", "kidney", "kidneys", "gizzard",
    "gizzards", "tripe", "tongue", "tongues", "brain", "brains", "offal",
    "sweetbread", "sweetbreads",
}


def animal_kind(name: str) -> str | None:
    for kind, pattern in _ANIMAL_KIND_PATTERNS:
        if pattern.search(str(name or "")):
            return kind
    return None


def _is_generic_stock_powder_parent(query_name: str, candidate_name: str) -> bool:
    query_words = set(_TOKEN_RE.findall(str(query_name or "").casefold()))
    candidate_words = set(_TOKEN_RE.findall(str(candidate_name or "").casefold()))
    return bool(
        {"stock", "broth", "bouillon"} & query_words
        and {"stock", "broth", "bouillon"} & candidate_words
        and "powder" in query_words
        and "powder" in candidate_words
        and animal_kind(candidate_name) is None
    )


def animal_kinds_compatible(query_name: str, candidate_name: str) -> bool:
    query_kind = animal_kind(query_name)
    candidate_kind = animal_kind(candidate_name)
    if query_kind and _is_generic_stock_powder_parent(query_name, candidate_name):
        return True
    return not query_kind or not candidate_kind or query_kind == candidate_kind


# Words that mark a candidate as a manufactured concentrate/dry-mix form
# rather than the ready-to-use food a recipe line usually means. Deliberately
# narrower than _PROCESSED_MARKERS (which is a soft ranking nudge): this list
# gates a hard rejection, so it only holds words that are unambiguous signals
# of "not the diluted/prepared form" — not general processing words like
# "canned"/"frozen" that are often exactly what the recipe asked for.
_CONCENTRATE_FORM_MARKERS = {
    "cube", "cubes", "gel", "dehydrated", "granules", "granule",
    "bouillon", "concentrate", "concentrated", "powder",
}


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
        and not ({"seed", "seeds"} & query_words)
        and {"seed", "seeds"} & candidate_words
    ):
        return False
    # A named animal ingredient must retain that animal identity. This catches
    # cases such as tuna in spring water matching bottled spring water.
    query_animal = animal_kind(query_name)
    candidate_animal = animal_kind(candidate_name)
    if query_animal and candidate_animal != query_animal:
        generic_stock_powder = _is_generic_stock_powder_parent(
            query_name, candidate_name
        )
        if not generic_stock_powder:
            return False
    # An ordinary cut of meat (bare "chicken", "cooked turkey", "lamb steak")
    # must not silently become an organ meat. Embeddings routinely rank
    # "Heart, chicken, cooked" above any chicken-breast row for the bare word
    # "chicken" because both share species + "cooked"; nothing else in the
    # query signals organ meat, so treat it as a hard mismatch.
    if (
        query_animal
        and not (_ORGAN_MEAT_WORDS & query_words)
        and (_ORGAN_MEAT_WORDS & candidate_words)
    ):
        return False
    # Same failure mode, different product: a plain roast/whole cut of chicken
    # or turkey must not become sliced deli ham of that species (e.g. "cooked
    # whole chicken" -> "Cooked ham, from chicken, in slices"). Ham is a
    # distinct cured/reformed product, not a stand-in for roasted meat.
    if (
        query_animal in {"chicken", "turkey", "pork"}
        and "ham" not in query_words
        and "ham" in candidate_words
    ):
        return False
    # A prepared animal food can contain the requested oil/sauce as a minor
    # ingredient. Do not treat that mention as the product identity (for
    # example chilli oil -> anchovy fillets marinated in chilli oil).
    candidate_primary = re.split(r"[,;(]", candidate_name, maxsplit=1)[0]
    candidate_primary_words = set(
        _TOKEN_RE.findall(candidate_primary.casefold())
    )
    # A single named vegetable/legume is not a generic multi-vegetable mix.
    # This caught green peas being accepted as a five-vegetable frozen blend.
    if (
        {"vegetable", "vegetables"} & candidate_primary_words
        and not ({"vegetable", "vegetables", "mix", "mixed", "blend"} & query_words)
    ):
        return False
    # Preparation words such as "mashed" do not turn an ordinary banana into
    # the nutritionally distinct plantain species.
    if "banana" in query_words and "plantain" not in query_words and "plantain" in candidate_words:
        return False
    # Fresh/raw animal portions must not silently become canned products.
    if (
        query_animal
        and not ({"can", "canned", "tin", "tinned"} & query_words)
        and not re.search(
            r"\b(?:in\s+(?:spring\s+)?(?:water|brine|oil)|(?:water|oil)[ -]packed)\b",
            query_name,
            re.I,
        )
        and {"canned", "tinned"} & candidate_words
    ):
        return False
    # Product words must describe the candidate's primary product, not merely
    # a packing medium or secondary ingredient.
    if (
        "juice" in query_words
        and not re.search(r"\b(?:in|with)\s+juice\b", query_name, re.I)
        and "juice" not in candidate_primary_words
    ):
        return False
    if (
        {"pasta", "noodle", "noodles", "fettuccine", "penne", "fusilli", "spaghetti"}
        & query_words
        and "soup" not in query_words
        and "soup" in candidate_primary_words
    ):
        return False
    if "scone" in query_words and "mix" in query_words and "mix" not in candidate_words:
        return False
    if (
        query_animal == "fish"
        and not ({"breaded", "ball", "balls"} & query_words)
        and {"breaded", "ball", "balls"} & candidate_words
    ):
        return False
    # Grain identity is essential for tortillas.
    if (
        {"tortilla", "tortillas"} & query_words
        and {"corn", "maize", "sweetcorn"} & query_words
        and "wheat" in candidate_words
    ):
        return False
    # Generic wholegrain bread must not invent a rye, sourdough, or nut subtype.
    if (
        "bread" in query_words
        and {"wholegrain", "wholemeal", "wholewheat"} & query_words
        and not ({"rye", "sourdough", "nut"} & query_words)
        and {"rye", "sourdough", "nut"} & candidate_words
    ):
        return False
    if (
        not query_animal
        and animal_kind(candidate_primary)
        and food_class(query_name) != "animal_protein"
    ):
        return False
    # Blue-cheese dressing and similar condiments are not interchangeable with
    # the cheese itself at the ingredient's full weight.
    if (
        "cheese" in query_words
        and "dressing" not in query_words
        and "dressing" in candidate_words
    ):
        return False
    # A sauce name appearing inside a pasta/meal description does not make
    # that whole prepared dish a sauce. The product word must occur in the
    # candidate's primary name segment.
    if (
        "sauce" in query_words
        and not (
            {"sauce", "paste", "ketchup", "relish", "salsa", "gravy"}
            & set(_TOKEN_RE.findall(candidate_primary.casefold()))
        )
    ):
        return False
    # A measured cup/litre of liquid stock or broth cannot use a concentrate's
    # nutrition at the liquid weight — "vegetable stock" matching "Stock gel"
    # or "Broth or stock, beef, dehydrated" is the same bug as the cube case
    # below, just with a different concentrate word. Found via the 2026-09-16
    # corpus-wide plausibility audit: 42% of flagged lines were this shape.
    if (
        {"stock", "broth"} & query_words
        and _CONCENTRATE_FORM_MARKERS & candidate_words
        and not (_CONCENTRATE_FORM_MARKERS & query_words)
    ):
        return False
    # "soda water"/"club soda"/"seltzer water"/"sparkling water" is a drink;
    # "bicarbonate of soda" (the EU corpus's only record for "baking soda")
    # is a raising agent -- 68,484mg sodium/serving in one recipe, the worst
    # outlier remaining after the 2026-09-17 recompute. food_class() puts
    # both at "other" (no distinguishing class to lean on), so this is a
    # narrow, evidence-based lexical gate like the ones above it.
    if (
        "water" in query_words
        and "bicarbonate" not in query_words
        and "bicarbonate" in candidate_words
    ):
        return False
    # A vegetable/food "cut into cubes" is a shape, not a stock/bouillon
    # concentrate -- "roasted pumpkin cubes" matching "Stock cubes,
    # vegetable" on the shared word "cubes" is the same bug shape as the
    # stock/broth gate above, just without "stock"/"broth" in the query to
    # trigger it. Found via the 2026-09-17 outlier sweep. Gate on the
    # candidate actually being a stock/broth/bouillon product, not just any
    # word-cube collision.
    if (
        {"stock", "broth", "bouillon"} & candidate_words
        and _CONCENTRATE_FORM_MARKERS & candidate_words
        and not ({"stock", "broth", "bouillon"} & query_words)
    ):
        return False
    # "hot water"/"hot milk" is a temperature, not the chilli-pepper sense of
    # "hot" -- found matching "Pepper red hot paste" at the recipe's full
    # liquid weight (1,440g). Scoped to liquids explicitly, not a blanket
    # strip of "hot" (which must stay meaningful for an actual hot-pepper
    # ingredient).
    if (
        "hot" in query_words
        and {"water", "milk"} & query_words
        and {"pepper", "chilli", "chili", "paste", "sauce"} & candidate_words
    ):
        return False
    # "cold water"/"cold milk" is a temperature, not "cold cuts" (deli meat)
    # -- found matching a Hungarian composition record "spring cold cuts" on
    # the shared word "cold", with water's zero-calorie identity replaced by
    # a meat product's protein/fat/sodium. Same shape as the hot-water gate
    # above, scoped the same way.
    if (
        "cold" in query_words
        and {"water", "milk"} & query_words
        and {"cut", "cuts"} & candidate_words
    ):
        return False
    # "ground pepper" conventionally means the spice. Do not let the shared
    # word pepper turn it into a raw bell/capsicum vegetable.
    if (
        "ground" in query_words
        and "pepper" in query_words
        and not ({"capsicum", "bell", "sweet", "chilli", "chili", "cayenne"} & query_words)
        and ({"capsicum", "bell", "sweet", "chilli", "chili", "cayenne"} & candidate_words)
    ):
        return False
    # "flavoured" as a plain descriptor ("flavoured tomatoes", "full-
    # flavoured liquid vegetable stock") is not a request for the specific
    # commercial product "Seasoning flavoured liquid" -- only let it through
    # when the query itself names a seasoning/stock/bouillon/gravy product.
    if (
        "flavoured" in query_words
        and "seasoning" in candidate_words
        and not ({"seasoning", "stock", "broth", "bouillon", "gravy"} & query_words)
    ):
        return False
    # "zero alcohol"/"non-alcoholic"/"alcohol-free" is a negation -- matching
    # "Pure alcohol" is the opposite of what the recipe means. clean_query
    # doesn't strip these (they're food-defining, unlike "chilled"), so gate
    # them here instead.
    if (
        "alcohol" in query_words
        and ({"zero", "non", "free"} & query_words)
        and "pure" in candidate_words
        and "alcohol" in candidate_words
    ):
        return False
    if {"baking", "mix"} <= query_words and "mix" not in candidate_words:
        return False
    if (
        "sauce" in query_words
        and not ({"bean", "beans"} & query_words)
        and {"bean", "beans"} & candidate_words
    ):
        return False
    # A candidate that explicitly negates the queried food cannot satisfy it.
    # Scope this to recipe-level seasoning language; a broad "no X" rule
    # would incorrectly reject useful products such as no-added-salt tomatoes.
    if (
        "seasoning" in query_words
        and re.search(r"\b(?:without|no)\s+seasoning\b", candidate_name, re.I)
    ):
        return False
    # food_class() sees "spaghetti" first in both strings, so it cannot by
    # itself distinguish pasta from the vegetable spaghetti squash.
    if (
        "spaghetti" in query_words
        and "squash" not in query_words
        and "squash" in candidate_words
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
    "scratchings", "frankfurter", "frankfurters", "pate", "pâté",
    # Composed/prepared-dish markers: a plain single-food query should not
    # lose to a multi-ingredient dish just because the dish's name happens
    # to contain the query word (e.g. "mushrooms" -> "Pate with mushrooms",
    # "potatoes" -> "Baby soup, with vegetables and potatoes").
    # "salad" deliberately excluded: real plain-ingredient composition-table
    # names routinely say "for salad" as usage context (e.g. "Spinach, young
    # leaves for salad, raw"), so this marker penalised legitimate plain
    # ingredients, not just composed salad dishes — and a proper "salad"
    # food_class already exists (line ~281) for the cases that need it.
    "with", "soup", "stew", "casserole", "hash", "bake", "pie",
    "sauce", "and",
}

# These words may describe an ingredient, but they are not its food identity.
# Keep them available to retrieval/state matching; exclude them only from the
# final identity sanity check so one shared adjective cannot validate a link.
_IDENTITY_MODIFIERS = _STOP | _COOKING_STATES | {
    "black", "white", "orange", "european", "italian", "spanish",
    "french", "greek", "firm", "soft", "chunk", "chunks", "cube",
    "cubes", "slice", "slices", "piece", "pieces", "fillet", "fillets",
    "leaf", "leaves", "floret", "florets", "bulb", "bulbs", "wedge",
    "wedges", "rind", "sprig", "sprigs", "pod", "pods", "kernel",
    "kernels", "aril", "arils", "sheet", "sheets", "shell", "shells",
    "mix", "blend", "weed",
    "vacuum",
    "spear", "spears", "pocket", "pockets", "meat", "spray",
    "boneless", "skinless",
    "cooking", "liquid", "spring", "thai", "mexican", "moroccan",
    "ripe", "chopped", "minced", "diced", "sliced", "grated", "shredded",
    "peeled", "trimmed", "drained", "rinsed", "melted", "softened",
    "thawed", "optional", "divided", "halved", "quartered", "cubed",
}
# These describe a preceding food's shape, cut, or part; they are not a safe
# identity anchor. A lone "clove" or "steak" remains meaningful, but in
# "garlic cloves" or "lamb steak" the preceding word carries the identity.
_GENERIC_TRAILING_PARTS = {
    "ball", "base", "bit", "block", "bulb", "chip", "chunk", "clove", "cube",
    "fillet", "flake", "half", "head", "heart", "kernel", "leaf", "piece",
    "loaf", "pack", "pod", "pocket", "pulp", "rind", "ring", "root", "round", "shell", "skin", "slice",
    "spear", "sprig", "stalk", "steak", "stem", "stick", "strip", "thread", "tip",
    "tube", "wedge",
}
_SOURCE_REQUIRED_HEADS = {
    "stock", "broth", "bouillon", "juice", "oil", "flour", "meal",
    "butter", "milk", "cheese", "yogurt", "yoghurt", "cream", "sauce",
    "paste", "puree", "powder", "extract", "syrup", "wine", "beer",
    "vinegar", "rice", "pasta", "noodle", "bread", "soup", "salad",
    "cereal", "sugar", "tofu", "water", "margarine", "spread", "shoot",
    "bean", "seed", "salt", "starch", "essence", "jam", "jelly", "marmalade",
    "preserve", "nectar", "bitter", "formula", "dressing", "marinade",
    "spice", "seasoning", "mayonnaise", "chutney",
}
_COLOR_SENSITIVE_HEADS = {
    "pepper", "fish", "bean", "rice", "wine", "sugar", "chocolate",
    "bread", "tea", "lentil", "pea",
}
_IDENTITY_COLORS = {"black", "white", "brown", "red", "green", "yellow"}
_PRODUCT_SUBTYPE_WORDS = {
    "angel", "hair", "fettuccine", "fusilli", "linguine", "macaroni",
    "penne", "rigatoni", "spaghetti", "tagliatelle", "vermicelli",
    "hot", "cold", "warm", "boiling", "wholemeal",
}
_IDENTITY_QUALIFIERS = {
    "potato": {"sweet"},
    "beet": {"silver"},
    "beetroot": {"silver"},
    "pepper": {"cayenne", "chilli", "chili", "sweet"},
    "capsicum": {"cayenne", "chilli", "chili", "sweet"},
    "mushroom": {
        "swiss", "button", "chestnut", "chanterelle", "shiitake", "oyster",
        "portobello", "porcini", "cremini",
    },
    "berry": {"goji"},
    "corn": {"baby"},
    "lime": {"kaffir", "makrut"},
    "zest": {"lemon", "lime", "orange"},
    "seasoning": {"cajun", "italian", "mexican", "moroccan", "tuscan"},
    "spice": {"cajun", "italian", "mexican", "moroccan", "tuscan"},
}

# A candidate that omits the named source is not the same failure as one that
# contradicts it.  Keep this deliberately small: the product family must be
# nutritionally interchangeable enough for a generic parent to be useful.
# For example rice vinegar -> vinegar is an acceptable fallback, while rice
# vinegar -> wine vinegar and vanilla paste -> curry paste are conflicts.
_SAFE_GENERIC_PARENT_HEADS = {"vinegar"}
_SAFE_GENERIC_PARENT_SOURCES = {
    "salt": {"sea", "kosher", "himalayan", "coarse", "table"},
}
_GENERIC_PARENT_WORDS = {
    "average", "food", "plain", "raw", "prepared", "retail", "commercial",
}
_PRODUCT_HEAD_EQUIVALENTS = (
    {"stock", "broth", "bouillon"},
    {"yogurt", "yoghurt", "yoghourt"},
    {"extract", "essence"},
    {
        "pasta", "macaroni", "spaghetti", "penne", "linguine", "fettuccine",
        "tagliatelle", "fusilli", "rigatoni", "vermicelli", "orzo", "noodle",
    },
)

_FORM_WORDS = {
    "raw": {"raw", "uncooked"},
    "cooked": {
        "cooked", "boiled", "steamed", "roasted", "fried", "refried", "baked",
        "stewed", "grilled", "braised", "poached", "toasted", "mashed",
    },
    "frozen": {"frozen"},
    "canned": {"canned", "tinned"},
    "pickled": {"pickled"},
    # Culinary flakes are normally the dried form (chilli, parsley, coconut).
    # Treating "flakes" only as a disposable shape let dried chilli resolve
    # to raw fresh pepper, which is not nutritionally interchangeable.
    "dried": {"dry", "dried", "dehydrated", "flake", "flakes"},
    "powder": {"powder", "powdered"},
    "concentrate": {"concentrate", "concentrated"},
    "instant": {"instant"},
    "smoked": {"smoked"},
    "cured": {"cured"},
    "salted": {"salted", "brined"},
    "skin": {"skin"},
    "spread": {"spread"},
    "juice": {"juice"},
    "oil": {"oil"},
    "syrup": {"syrup"},
    "starch": {"starch"},
    "bread": {"bread", "breads", "loaf", "loaves"},
    "extract": {"extract", "essence"},
    "cream_product": {"cream", "creamed"},
    "cheese_product": {"cheese"},
    "milk_product": {"milk"},
    "nectar_product": {"nectar"},
    "jam_product": {"jam", "jelly", "marmalade", "preserve", "preserves"},
    "bran_product": {"bran"},
    "sausage_product": {"sausage", "sausages"},
    "alcohol_product": {"spirit", "spirits", "alcohol", "liqueur", "liquor"},
    "nut_product": {"nut", "nuts"},
    "clarified_fat": {"ghee", "clarified"},
    "seasoned_product": {
        "spice", "spices", "herb", "herbs", "seasoning", "seasonings",
    },
    "yogurt_product": {"yogurt", "yoghurt", "yoghourt"},
    "dip_product": {"hummus"},
    "quorn_product": {"quorn"},
    "wholegrain_product": {"wholegrain", "wholemeal", "wholewheat"},
    "gluten_free": {"glutenfree", "gluten"},
    "sauce": {
        "sauce", "ketchup", "mayonnaise", "dressing", "aioli", "relish",
        "salsa", "marinade", "chutney", "dip", "vinaigrette", "gravy",
    },
    "paste": {"paste", "puree", "pureed"},
    "pesto": {"pesto"},
    "passata": {"passata", "puree", "pureed", "coulis"},
    "drink": {"drink", "beverage"},
    "flavoured": {"flavored", "flavoured"},
    "creamed": {"creamed"},
    "seed": {"seed", "seeds", "kernel", "kernels"},
    "shoot": {"shoot", "shoots"},
    "flour": {"flour"},
    "composite": {
        "cereal", "curry",
        "pie", "cake", "pudding", "biscuit", "pastry", "sandwich",
        "pizza", "casserole", "burger", "fishcake", "fishcakes", "layer",
        "cheesecake", "turnover", "tart", "mousse", "strudel", "compote",
        "bolognese", "crouton", "fritter", "fritters", "crumble", "cobbler",
        "pies", "cakes", "puddings", "biscuits", "sandwiches", "pizzas",
        "casseroles", "burgers", "layers", "muffin", "muffins",
    },
    "confectionery": {
        "candy", "choc", "chocolate", "confectionery", "caramel", "sugared",
    },
    "snack": {
        "crisp", "crisps", "chip", "chips", "popcorn", "pop-corn",
        "snack", "snacks",
    },
}
_DANGEROUS_EXTRA_FORMS = {
    "cooked", "dried", "powder", "concentrate", "instant", "smoked", "cured", "salted", "pickled",
    "skin", "spread", "juice", "oil", "syrup", "starch", "bread", "extract", "sauce", "paste",
    "cream_product", "cheese_product", "nectar_product", "jam_product",
    "bran_product", "sausage_product", "alcohol_product", "nut_product",
    "clarified_fat",
    "seasoned_product",
    "yogurt_product", "dip_product",
    "quorn_product",
    "wholegrain_product",
    "gluten_free",
    "pesto", "passata", "drink", "flavoured", "creamed", "seed", "shoot", "flour",
    "composite", "confectionery", "snack",
}
_REQUIRED_QUERY_FORMS = set(_FORM_WORDS) - {
    "skin", "seed", "salted", "wholegrain_product",
}
_COOKING_METHODS = {
    "baked": {"baked", "roasted", "roast"},
    "fried": {"fried", "refried", "deepfried", "panfried", "stirfried"},
    "boiled": {"boiled", "poached", "simmered"},
    "steamed": {"steamed"},
    "grilled": {"grilled", "chargrilled", "broiled", "barbecued"},
}


def _token_variants(raw: str) -> set[str]:
    token = _singular(raw)
    variants = {token}
    synonym = (
        _SYNONYMS.get(raw) or _SYNONYMS.get(token)
        or _SYNONYMS_REV.get(raw) or _SYNONYMS_REV.get(token)
    )
    if synonym:
        variants.add(_singular(synonym))
    for equivalent_group in _PRODUCT_HEAD_EQUIVALENTS:
        if variants & equivalent_group:
            variants.update(equivalent_group)
    return variants


def _identity_token_groups(value: object) -> list[set[str]]:
    """Identity tokens in word order, with synonyms kept in the same slot."""
    result: list[set[str]] = []
    normalized = _ascii_fold(str(value or "")).casefold()
    normalized = re.sub(r"\bcornstarch\b", "corn starch", normalized)
    normalized = re.sub(r"\bcornflour\b", "corn starch", normalized)
    normalized = re.sub(r"\bkiwifruit\b", "kiwi fruit", normalized)
    normalized = re.sub(r"\bpassionfruit\b", "passion fruit", normalized)
    normalized = re.sub(r"\bcavolo\s+nero\b", "kale", normalized)
    normalized = re.sub(r"\bsilver\s*beet\b", "chard", normalized)
    normalized = re.sub(r"\b(chil(?:i|li))\s+power\b", r"\1 powder", normalized)
    normalized = re.sub(r"\b(cumin|caraway)\s+powder\b", r"\1", normalized)
    normalized = _normalize_food_phrases(normalized)
    for raw in _TOKEN_RE.findall(normalized):
        variants = _token_variants(raw)
        token = _singular(raw)
        if len(token) <= 1 or (
            token in _IDENTITY_MODIFIERS
            and token not in _SOURCE_REQUIRED_HEADS
        ):
            continue
        if not result or variants != result[-1]:
            result.append(variants)
    if len(result) > 1 and result[-1] & _GENERIC_TRAILING_PARTS:
        result.pop()
    return result


def _identity_tokens(value: object) -> list[str]:
    """Return food-identity tokens, excluding state/form descriptors."""
    result: list[str] = []
    for variants in _identity_token_groups(value):
        for token in variants:
            if token not in result:
                result.append(token)
    return result


def _token_matches(token: str, candidates: set[str]) -> bool:
    return token in candidates


def _compound_identities_match(identity_name: str, candidate_name: str) -> bool:
    """A multi-food query must not be satisfied by only one component."""
    alternative_parts = [
        part for part in re.split(r"\bor\b|/", identity_name, flags=re.I)
        if _identity_token_groups(part)
    ]
    if len(alternative_parts) > 1:
        head_groups = [
            _identity_token_groups(part)[-1] for part in alternative_parts
        ]
        # Synonym spellings such as zucchini/courgette are one identity.
        if not set.intersection(*head_groups):
            return False
    parts = re.split(r"\b(?:and|or|with)\b|[,/&;]+", identity_name, flags=re.I)
    food_parts = [part for part in parts if _identity_token_groups(part)]
    if len(food_parts) < 2:
        return True
    return all(
        _identity_match_level(part, candidate_name) != "incompatible"
        for part in food_parts
    )


def _identity_match_level(identity_name: str, candidate_name: str) -> str:
    """Return exact, safe_parent, or incompatible for food identity."""
    query_groups = _identity_token_groups(identity_name)
    if not query_groups:
        return "exact"
    candidate_identity = _ascii_fold(candidate_name).casefold()
    candidate_identity = re.sub(r"\bre[- ]fried\b", "refried", candidate_identity)
    candidate_identity = re.sub(r"\bchick\s+peas?\b", "chickpea", candidate_identity)
    candidate_tokens = {
        _singular(token)
        for token in _TOKEN_RE.findall(candidate_identity)
    }
    for token in tuple(candidate_tokens):
        candidate_tokens.update(_token_variants(token))
    if re.search(
        r"\b(?:0|1|2)\s*%?\s*fat\b|\b(?:semi[- ]?skimmed|skimmed)\b",
        candidate_name,
        re.I,
    ):
        candidate_tokens.update({"half", "low", "reduced"})
    head_group = query_groups[-1]
    raw_identity_tokens = [
        _singular(token)
        for token in _TOKEN_RE.findall(_ascii_fold(identity_name).casefold())
    ]
    raw_head = raw_identity_tokens[-1] if raw_identity_tokens else ""
    # ``head_group`` is a set of equivalent product names. Selecting its
    # first element made matching depend on Python's randomized set order:
    # the same "angel hair pasta" process could choose `pasta` and pass, or
    # choose `noodle` and treat "angel hair" as mandatory food sources.
    head = (
        raw_head
        if raw_head in head_group and raw_head in _SOURCE_REQUIRED_HEADS
        else next(
            (token for token in sorted(head_group) if token in _SOURCE_REQUIRED_HEADS),
            sorted(head_group)[0],
        )
    )
    if (
        head == "powder"
        and any(group & {"onion", "scallion"} for group in query_groups[:-1])
        and "onion" in candidate_tokens
        and "dried" in candidate_tokens
    ):
        candidate_tokens.add("powder")
    if head in _SOURCE_REQUIRED_HEADS:
        if not any(
            _token_matches(token, candidate_tokens) for token in head_group
        ):
            return "incompatible"
        sources = [
            group for group in query_groups[:-1]
            if not (
                group & {"hot", "cold", "warm", "boiling"}
                or (
                    head in {
                        "pasta", "macaroni", "spaghetti", "penne", "linguine",
                        "fettuccine", "tagliatelle", "fusilli", "rigatoni",
                        "vermicelli", "orzo", "noodle",
                    }
                    and group & _PRODUCT_SUBTYPE_WORDS
                )
                or (head == "bread" and group & {"wholemeal"})
            )
        ]
        if head == "vinegar":
            query_colors = _IDENTITY_COLORS & set(
                _TOKEN_RE.findall(_ascii_fold(identity_name).casefold())
            )
            sources.extend({color} for color in sorted(query_colors))
        matched_sources = [
            group for group in sources
            if any(_token_matches(source, candidate_tokens) for source in group)
        ]
        if not sources or len(matched_sources) == len(sources):
            return "exact"
        missing_sources = [group for group in sources if group not in matched_sources]
        if head == "sauce" and all(group <= {"sweet"} for group in missing_sources):
            return "safe_parent"
        if (
            head == "powder"
            and any(group & {"stock", "broth", "bouillon"} for group in sources)
            and candidate_tokens & {"stock", "broth", "bouillon"}
            and not (
                candidate_tokens
                - head_group
                - {"stock", "broth", "bouillon"}
                - _GENERIC_PARENT_WORDS
            )
        ):
            # A named chicken/vegetable stock powder can use an otherwise
            # generic stock-powder row. Form stays exact (powder), and a row
            # naming a conflicting species still fails this branch.
            return "safe_parent"
        candidate_sources = (
            candidate_tokens - head_group - _GENERIC_PARENT_WORDS
        )
        safe_sources = _SAFE_GENERIC_PARENT_SOURCES.get(head, set())
        if not candidate_sources and (
            head in _SAFE_GENERIC_PARENT_HEADS
            or (
                safe_sources
                and all(group <= safe_sources for group in sources)
            )
        ):
            return "safe_parent"
        return "incompatible"
    if head_group & _COLOR_SENSITIVE_HEADS:
        return "exact" if any(
            _token_matches(token, candidate_tokens) for token in head_group
        ) else "incompatible"

    # For ordinary food names, the final non-descriptor token is the identity
    # anchor.  Matching any word was what allowed "flour tortillas" ->
    # "corn flour" and "pumpkin pie spice" -> "shepherd's pie".
    return "exact" if any(
        _token_matches(token, candidate_tokens) for token in head_group
    ) else "incompatible"


def _identity_matches(identity_name: str, candidate_name: str) -> bool:
    return _identity_match_level(identity_name, candidate_name) != "incompatible"


def _colors_match(identity_name: str, query_name: str, candidate_name: str) -> bool:
    identity_groups = _identity_token_groups(identity_name)
    if not identity_groups or not (identity_groups[-1] & _COLOR_SENSITIVE_HEADS):
        return True
    query_colors = _IDENTITY_COLORS & set(
        _TOKEN_RE.findall(_ascii_fold(query_name).casefold())
    )
    candidate_words = set(
        _TOKEN_RE.findall(_ascii_fold(candidate_name).casefold())
    )
    candidate_colors = _IDENTITY_COLORS & candidate_words
    candidate_colors.update(
        color
        for color in _IDENTITY_COLORS
        if any(word.startswith(color) for word in candidate_words)
    )
    query_words = set(_TOKEN_RE.findall(_ascii_fold(query_name).casefold()))
    if (
        identity_groups[-1] & {"sugar"}
        and query_words & {"caster", "castor"}
        and not query_colors
        and "brown" in candidate_colors
    ):
        return False
    if not query_colors:
        return True
    return query_colors <= candidate_colors


def _qualifiers_match(identity_name: str, query_name: str, candidate_name: str) -> bool:
    if not _sodium_qualifiers_match(query_name, candidate_name):
        return False
    identity_groups = _identity_token_groups(identity_name)
    if not identity_groups:
        return True
    qualifiers = next(
        (
            _IDENTITY_QUALIFIERS[token]
            for token in identity_groups[-1]
            if token in _IDENTITY_QUALIFIERS
        ),
        None,
    )
    if not qualifiers:
        return True
    query_words = {
        variant
        for raw in _TOKEN_RE.findall(_ascii_fold(query_name).casefold())
        for variant in _token_variants(raw)
    }
    candidate_words = {
        variant
        for raw in _TOKEN_RE.findall(_ascii_fold(candidate_name).casefold())
        for variant in _token_variants(raw)
    }
    if (
        {"kaffir", "lime"} <= query_words
        and query_words & {"leaf", "leaves"}
        and not (
            {"kaffir", "lime"} <= candidate_words
            and candidate_words & {"leaf", "leaves"}
        )
    ):
        return False
    query_qualifiers = query_words & qualifiers
    candidate_qualifiers = candidate_words & qualifiers
    # Synonym expansion deliberately keeps both the raw and canonical token
    # ("chili" + "chilli"). Equality therefore rejects a genuine synonym:
    # jalapeno expands only to "chilli", while the table's "chili pepper"
    # carries both spellings. Require shared meaning while still rejecting a
    # different named subtype such as jalapeno -> cayenne.
    if query_qualifiers and candidate_qualifiers:
        return bool(query_qualifiers & candidate_qualifiers)
    return query_qualifiers == candidate_qualifiers


def _sodium_qualifiers_match(query_name: str, candidate_name: str) -> bool:
    """Do not silently replace an explicit low-sodium food with a regular row."""
    return not _LOW_SODIUM_RE.search(query_name) or bool(
        _LOW_SODIUM_RE.search(candidate_name)
    )


def _forms(value: str) -> set[str]:
    words = set(_TOKEN_RE.findall(_ascii_fold(value).casefold()))
    forms = {
        form
        for form, variants in _FORM_WORDS.items()
        if words & variants
    }
    # "without skin" and "skinless" describe the absence of skin; treating
    # them as a requested skin product inverted the meaning of the candidate.
    if re.search(r"\b(?:without|wo)\s+skin\b|\bskinless\b", value, re.I):
        forms.discard("skin")
    if re.search(r"\b(?:without|wo)\s+seeds?\b|\bseedless\b", value, re.I):
        forms.discard("seed")
    if re.search(r"\bcream[- ]style\b", value, re.I):
        forms.add("creamed")
    if words & {"cornstarch", "cornflour"}:
        forms.add("starch")
    if words & {
        "cheddar", "feta", "gouda", "mozzarella", "bocconcini", "parmesan",
        "parmigiano", "pecorino", "ricotta", "mascarpone", "brie", "paneer",
        "quark",
    }:
        forms.add("cheese_product")
    if {"creme", "fraiche"} <= words:
        forms.add("cream_product")
    if words & {
        "almond", "almonds", "walnut", "walnuts", "pecan", "pecans",
        "cashew", "cashews", "peanut", "peanuts", "hazelnut", "hazelnuts",
        "pistachio", "pistachios", "macadamia", "chestnut", "chestnuts",
    }:
        forms.add("nut_product")
    if words & {
        "chorizo", "salami", "pepperoni", "kielbasa", "bratwurst",
        "frankfurter", "frankfurters",
    }:
        forms.add("sausage_product")
    if {"bran", "flakes"} <= words or "cornflakes" in words:
        forms.add("composite")
    # In "tart cherries", tart means sour fruit rather than a pastry.
    if "tart" in words and {"cherry", "cherries"} & words:
        forms.discard("composite")
    return forms


def _cooking_method_matches(query_name: str, candidate_name: str) -> bool:
    query_words = set(_TOKEN_RE.findall(_ascii_fold(query_name).casefold()))
    candidate_words = set(
        _TOKEN_RE.findall(_ascii_fold(candidate_name).casefold())
    )
    requested = {
        method for method, words in _COOKING_METHODS.items()
        if query_words & words
    }
    candidate_methods = {
        method for method, words in _COOKING_METHODS.items()
        if candidate_words & words
    }
    return not requested or bool(requested & candidate_methods)


def _form_conflicts(
    identity_name: str,
    query_name: str,
    candidate_name: str,
) -> set[str]:
    unexpected = (
        _forms(candidate_name) - _forms(query_name)
    ) & _DANGEROUS_EXTRA_FORMS
    query_class = food_class(identity_name)
    query_words = set(_TOKEN_RE.findall(_ascii_fold(query_name).casefold()))
    candidate_words = set(
        _TOKEN_RE.findall(_ascii_fold(candidate_name).casefold())
    )
    if (
        query_class == "dairy"
        and "fruit" in candidate_words
        and "fruit" not in query_words
    ):
        unexpected.add("flavoured")
    if (
        query_class == "grain_cereal"
        and "milk" in candidate_words
        and "milk" not in query_words
    ):
        unexpected.add("milk_product")
    if (
        query_class == "legume"
        and "rice" in candidate_words
        and "rice" not in query_words
    ):
        unexpected.add("composite")
    if query_class == "nut_seed":
        unexpected.discard("seed")
    if query_class == "oil_fat":
        unexpected.discard("oil")
    if {"stir", "fry"} <= query_words:
        unexpected.discard("oil")
        unexpected.discard("cooked")
    if query_class == "condiment_sauce":
        unexpected.discard("sauce")
        unexpected.discard("paste")
    if "edamame" in query_words:
        unexpected.discard("cooked")
    if "baked" in query_words and {"bean", "beans"} & query_words:
        # Baked beans are conventionally sold in sauce even when the recipe
        # uses the short product name rather than spelling the sauce out.
        unexpected.discard("sauce")
    if "passata" in query_words:
        unexpected.discard("paste")
    identity_words = set(
        _TOKEN_RE.findall(_ascii_fold(identity_name).casefold())
    )
    # These words name foods whose ordinary composition-table label commonly
    # spells out an inherent form omitted by recipes.
    if "seed" in unexpected and identity_words & {"anise", "cumin", "caraway"}:
        unexpected.discard("seed")
    if "dried" in unexpected and "chia" in identity_words:
        unexpected.discard("dried")
    if "dried" in unexpected and "goji" in identity_words:
        unexpected.discard("dried")
    if "dried" in unexpected and identity_words & {
        "fettuccine", "fusilli", "linguine", "macaroni", "noodle", "noodles",
        "orzo", "pasta", "penne", "rigatoni", "spaghetti", "tagliatelle",
        "vermicelli",
    }:
        unexpected.discard("dried")
    if "paste" in unexpected and "tahini" in identity_words:
        unexpected.discard("paste")
    if "concentrate" in unexpected and {"tomato", "paste"} <= identity_words:
        unexpected.discard("concentrate")
    if "bread" in unexpected and "sourdough" in identity_words:
        unexpected.discard("bread")
    if "flavoured" in unexpected:
        identity_groups = _identity_token_groups(identity_name)
        named_flavours = set().union(*identity_groups[:-1]) if len(identity_groups) > 1 else set()
        named_flavours -= {"fat", "half", "low", "plain", "reduced"}
        if named_flavours & candidate_words:
            unexpected.discard("flavoured")
    missing = (
        _forms(query_name) - _forms(candidate_name)
    ) & _REQUIRED_QUERY_FORMS
    # Composition tables normally call this product a wheat tortilla rather
    # than a flour tortilla. Wheat supplies the missing flour identity; this
    # is not permission for a bare flour query to match a tortilla.
    if (
        "flour" in missing
        and identity_words & {"tortilla", "tortillas"}
        and "tortilla" in candidate_words
        and "wheat" in candidate_words
    ):
        missing.discard("flour")
    if (
        "onion" in identity_words
        and "powder" in _forms(query_name)
        and "dried" in _forms(candidate_name)
    ):
        missing.discard("powder")
        unexpected.discard("dried")
    if "powder" in missing and identity_words & {"cumin", "caraway"}:
        missing.discard("powder")
    return unexpected | missing


def _selected_match_level(
    identity_name: str,
    query_name: str,
    candidate_name: str,
) -> str:
    """Return exact, safe_parent, or incompatible for the selected winner."""
    identity_level = _identity_match_level(identity_name, candidate_name)
    identity_words = set(_TOKEN_RE.findall(_ascii_fold(identity_name).casefold()))
    candidate_primary_words = set(
        _TOKEN_RE.findall(re.split(r"[,;(]", candidate_name, maxsplit=1)[0].casefold())
    )
    if (
        identity_level == "incompatible"
        or ("juice" not in identity_words and "juice" in candidate_primary_words)
        or not _compound_identities_match(identity_name, candidate_name)
        or not _colors_match(identity_name, query_name, candidate_name)
        or not _qualifiers_match(identity_name, query_name, candidate_name)
        or not _cooking_method_matches(query_name, candidate_name)
    ):
        return "incompatible"
    conflicts = _form_conflicts(identity_name, query_name, candidate_name)
    if conflicts:
        # Omitting "smoked" is a generic-parent fallback; inventing a
        # different preparation remains incompatible.
        query_forms = _forms(query_name)
        candidate_forms = _forms(candidate_name)
        if (
            conflicts <= {"smoked", "creamed"}
            and conflicts & query_forms
            and not (conflicts & candidate_forms)
        ):
            return "safe_parent"
        identity_words = set(
            _TOKEN_RE.findall(_ascii_fold(identity_name).casefold())
        )
        if (
            conflicts <= {"powder", "dried"}
            and "onion" in identity_words
            and "powder" in _forms(query_name)
            and "dried" in _forms(candidate_name)
        ):
            return "safe_parent"
        return "incompatible"
    return identity_level


def _selected_match_is_compatible(
    identity_name: str,
    query_name: str,
    candidate_name: str,
) -> bool:
    return _selected_match_level(identity_name, query_name, candidate_name) != "incompatible"


# --------------------------------------------------------------------------- #
# Curated alias short-circuit — checked before the ES pool, wins outright.
# Keyed on the raw ingredient name (checked first) then the clean_query'd name,
# because clean_query strips qualifiers like "skim"/"low-fat" that change which
# food a name means. Degrades to a no-op (matcher falls through to the ES pool)
# if the alias table isn't loaded — same failure-open behaviour the old
# curated layer had.
#
# Each row curates an EU record (required) and, where a genuinely equivalent
# record exists in a *regional* table, that too (optional, per region). A
# regional run uses its own curated record when one is set; otherwise it uses
# the EU record. This matters: pinning every alias to the EU record for every
# region would make "region" a no-op for aliased ingredients and silently
# collapse the very regional comparison this curation pass exists to fix —
# see [[project_nutrition_matcher]]. Many rows have no regional id because the
# regional table genuinely has no equivalent (e.g. no plain "water"/"salt" row
# in Irish/Hungarian/Slovenian) — that is a real corpus gap, documented in the
# row's `note`, not something to paper over with a non-equivalent record.
# --------------------------------------------------------------------------- #
_REGION_ALIAS_COLUMNS = {
    "irish": "irish_food_id",
    "hungarian": "hungarian_food_id",
    "slovenian": "slovenian_food_id",
}
_REGION_COLLECTIONS = {
    "irish": "nutritional_ingredients_irish",
    "hungarian": "nutritional_ingredients_hungarian",
    "slovenian": "nutritional_ingredients_slovenian",
    "eu": "nutritional_ingredients_eu",
}

_INTENTIONALLY_NEGLIGIBLE_SEASONING_RE = re.compile(
    r"^(?:"
    r"(?:dried\s+)?(?:red\s+)?(?:chilli|chili|pepper)\s+flakes?"
    r"|(?:italian|tuscan|moroccan|taco)\s+seasonings?"
    r"|pumpkin\s+pie\s+(?:spice|seasoning)"
    r"|seasonings?"
    r"|sumac"
    r")$",
    re.IGNORECASE,
)
_AMBIGUOUS_CHICKPEA_WORDS = {
    "chickpea", "chickpeas", "chick", "pea", "peas",
    "garbanzo", "garbanzos", "bean", "beans",
}

_AMBIGUOUS_GENERIC_IDENTITIES = {
    "bean": "Specify the bean variety; the composition table has no defensible generic bean row.",
    "beans": "Specify the bean variety; the composition table has no defensible generic bean row.",
    "broth": "Specify the broth source; the composition table has no defensible generic prepared broth row.",
    "stock": "Specify the stock source; the composition table has no defensible generic prepared stock row.",
    "meatballs": "Specify the meat and preparation; the composition table has no defensible generic meatball row.",
    "noodles": "Specify the noodle type and preparation state.",
    "green vegetables": "Specify the vegetables; a mashed mixed-vegetable row is not a defensible generic match.",
    "leftover vegetables": "Specify the vegetables; a mashed mixed-vegetable row is not a defensible generic match.",
    "roasting vegetables": "Specify the vegetables; a mashed mixed-vegetable row is not a defensible generic match.",
    "vegetable sticks": "Specify the vegetables; a mashed mixed-vegetable row is not a defensible generic match.",
    "grain": "Specify the grain; one arbitrary cereal grain was not substituted.",
    "grains": "Specify the grain; one arbitrary cereal grain was not substituted.",
    "topping": "Specify the topping; one arbitrary dessert topping was not substituted.",
    "toppings": "Specify the topping; one arbitrary dessert topping was not substituted.",
}

_AMBIGUOUS_PREPARATION_IDENTITIES = {
    "black beans", "brown lentils", "cannelini beans", "cannellini beans", "green lentils",
    "green split peas", "haricot beans", "kidney beans", "lentils",
    "pinto beans", "puy lentils", "red beans", "red kidney beans", "split peas",
    "white beans", "yellow split peas",
}

_UNSUPPORTED_STOCK_QUALIFIER_RE = re.compile(
    r"\b(?:no[- ]added|reduced|low)[- ]+(?:salt|sodium)\b"
    r"|\b(?:salt|sodium)[- ]+reduced\b",
    re.IGNORECASE,
)

_LOW_SODIUM_RE = re.compile(
    r"\b(?:no[- ]added[- ](?:salt|sodium)|no[- ](?:salt|sodium)[- ]added|"
    r"reduced[- ](?:salt|sodium)|low[- ](?:salt|sodium)|"
    r"(?:salt|sodium)[- ]reduced|unsalted)\b",
    re.IGNORECASE,
)


def _intentionally_negligible_result(
    identity: str,
    source: str,
    cleaned: str,
) -> dict | None:
    if not _INTENTIONALLY_NEGLIGIBLE_SEASONING_RE.fullmatch(identity):
        return None
    return {
        "match": None,
        "source_key": source,
        "similarity": None,
        "confidence": "none",
        "reason": "intentional_negligible_seasoning",
        "matched_name": None,
        "cleaned_query": cleaned,
        "intentionally_ignored": True,
        "nutrition_match_note": (
            "Nutrition contribution intentionally treated as zero under the "
            "configured negligible-seasoning policy."
        ),
    }


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, dict[str, str]]:
    try:
        rows = load_pipeline_data("ingredient_composition_aliases")
    except Exception:
        return {}
    index: dict[str, dict[str, str]] = {}
    for row in rows or []:
        alias = _norm(row.get("alias"))
        eu_food_id = str(row.get("eu_food_id") or "").strip()
        if not alias or not eu_food_id:
            continue
        entry = {"eu_food_id": eu_food_id}
        for region, column in _REGION_ALIAS_COLUMNS.items():
            food_id = str(row.get(column) or "").strip()
            if food_id:
                entry[region] = food_id
        index[alias] = entry
    return index


def _curated_alias_lookup(raw_name: str, cleaned: str) -> dict | None:
    index = _alias_index()
    if not index:
        return None
    for key in (_norm(raw_name), _norm(cleaned)):
        hit = index.get(key)
        if hit:
            return hit
    return None


def _strip_low_sodium_qualifier(text: str) -> str | None:
    """Remove a matched low-sodium/reduced-salt/no-added-salt qualifier phrase from
    text, returning the cleaned string, or None if no qualifier was present."""
    if not _LOW_SODIUM_RE.search(text):
        return None
    stripped = _LOW_SODIUM_RE.sub(" ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped or None


def best_nutrition_match(
    name: str,
    source: str = "irish",
    min_similarity: float = 0.7,
    identity_name: str | None = None,
    _qualifier_retry: bool = False,
) -> dict:
    """Return {match, source_key, similarity, confidence, reason, matched_name, cleaned_query}."""
    source = (source or "irish").strip().lower()
    if source not in {"irish", "hungarian", "eu", "slovenian"}:
        raise ValueError(
            f"Unsupported nutrition source '{source}'. Supported sources: irish, hungarian, eu, slovenian"
        )
    # A "reduced-sodium"/"no-added-salt"/etc query: first give the QUALIFIED name
    # itself a real chance at matching normally (a genuine "unsalted butter" or
    # "no-salt-added kidney beans" composition row, whether via alias or the
    # ordinary ranking path, must win outright -- never downgraded). Only if that
    # fails does this fall back to the BASE food's composition (accurate for
    # everything except sodium, an approximation from the regular product) instead
    # of the old behaviour of silently contributing zero for the whole ingredient.
    # Generalizes what used to be two special-cased guards (stock/broth/bouillon,
    # soy sauce) plus per-phrasing alias rows -- any food, any qualifier synonym,
    # no alias needed for the fallback tier.
    if not _qualifier_retry and _LOW_SODIUM_RE.search(str(name or "")):
        qualified_result = best_nutrition_match(
            name, source, min_similarity, identity_name=identity_name, _qualifier_retry=True,
        )
        if qualified_result.get("match") is not None:
            return qualified_result
        stripped_name = _strip_low_sodium_qualifier(str(name or ""))
        if stripped_name:
            stripped_identity = (
                _strip_low_sodium_qualifier(str(identity_name)) or identity_name
                if identity_name else None
            )
            base_result = best_nutrition_match(
                stripped_name, source, min_similarity,
                identity_name=stripped_identity, _qualifier_retry=True,
            )
            if base_result.get("match") is not None:
                previous_reason = str(base_result.get("reason") or "")
                base_result["reason"] = ";".join(
                    part for part in ("qualifier_proxy_sodium_unverified", previous_reason) if part
                )
                base_result["nutrition_match_note"] = (
                    "Matched to the regular (non-reduced) product; the source line stated a "
                    "reduced-sodium/no-added-salt qualifier we don't have verified composition "
                    "data for. All values except sodium should be accurate; sodium is likely "
                    "an overestimate for this specific product."
                )
                return base_result
            # neither the qualified name nor the base food matched -- fall
            # through to normal handling of the original name below (whatever
            # that normally produces, e.g. an abstention reason).
    # Explicit identity is parser output, not a retrieval query. Query cleanup
    # can erase the food itself (for example standalone cooking spray), so
    # preserve it. Calls without a parser identity retain the historic cleaned
    # fallback, which removes trailing prep additions such as "... spray oil".
    raw_identity = _ascii_fold(str(identity_name or "")).casefold()
    raw_identity = re.sub(r"[^a-z0-9\s,/&;'-]", " ", raw_identity)
    raw_identity = re.sub(r"[-']", " ", raw_identity)
    raw_identity = re.sub(r"\s+", " ", raw_identity).strip(" ,/&;")
    if re.fullmatch(
        r"(?:non stick )?(?:vegetable )?"
        r"(?:cooking spray(?: oil)?|spray(?: oil)?|oil spray)", raw_identity
    ):
        raw_identity = "spray oil"
    identity = (
        raw_identity
        if identity_name is not None
        else clean_query(name) or _norm(_ascii_fold(str(name or "")))
    )
    if is_unambiguous_non_food_ingredient(identity):
        return {"match": None, "source_key": source, "similarity": None,
                "confidence": "none", "reason": "non_food", "matched_name": None,
                "cleaned_query": identity}
    if identity in _UNMATCHABLE_PLACEHOLDERS:
        return {"match": None, "source_key": source, "similarity": None,
                "confidence": "none", "reason": "invalid_identity", "matched_name": None,
                "cleaned_query": identity}
    if (
        re.search(r"\b(?:stock|broth|bouillon)\b", identity)
        and _UNSUPPORTED_STOCK_QUALIFIER_RE.search(str(name or ""))
        # A curated alias is a verified row for exactly this variant (e.g. reduced-salt cube).
        and _curated_alias_lookup(str(name or ""), str(name or "")) is None
    ):
        return {
            "match": None,
            "source_key": source,
            "similarity": None,
            "confidence": "none",
            "reason": "unsupported_nutrition_variant",
            "matched_name": None,
            "cleaned_query": identity,
            "nutrition_match_note": (
                "No verified reduced-salt stock composition row is available; "
                "regular prepared stock was not substituted."
            ),
        }
    if (
        re.search(r"\bsoy(?:a)?\s+sauce\b", identity)
        and _UNSUPPORTED_STOCK_QUALIFIER_RE.search(str(name or ""))
    ):
        return {
            "match": None,
            "source_key": source,
            "similarity": None,
            "confidence": "none",
            "reason": "unsupported_nutrition_variant",
            "matched_name": None,
            "cleaned_query": identity,
            "nutrition_match_note": (
                "No verified reduced-sodium soy-sauce composition row is "
                "available; regular soy sauce was not substituted."
            ),
        }
    cleaned_identity_probe = clean_query(name) or str(name or "").strip().lower()
    if (
        identity in _AMBIGUOUS_GENERIC_IDENTITIES
        and cleaned_identity_probe == identity
        # A curated alias is a deliberate, reviewed exception to "no defensible
        # generic row exists" for this exact identity (e.g. an averaged
        # external-source default) -- let it through instead of blocking.
        and _curated_alias_lookup(str(name or ""), str(name or "")) is None
    ):
        return {
            "match": None,
            "source_key": source,
            "similarity": None,
            "confidence": "none",
            "reason": "ambiguous_food_identity",
            "matched_name": None,
            "cleaned_query": identity,
            "nutrition_match_note": _AMBIGUOUS_GENERIC_IDENTITIES[identity],
        }
    identity_words = set(_TOKEN_RE.findall(identity))
    if (
        identity_words
        and identity_words <= _AMBIGUOUS_CHICKPEA_WORDS
        and identity_words & {"chickpea", "chickpeas", "chick", "garbanzo", "garbanzos"}
        and not re.search(
            r"\b(?:can|canned|tinned|cooked|boiled|dried|dry|rinsed|drained)\b",
            str(name or ""),
            re.IGNORECASE,
        )
    ):
        return {
            "match": None,
            "source_key": source,
            "similarity": None,
            "confidence": "none",
            "reason": "ambiguous_preparation_state",
            "matched_name": None,
            "cleaned_query": identity,
            "nutrition_match_note": (
                "Specify dried, cooked, or canned chickpeas; their composition "
                "differs substantially because of water content."
            ),
        }
    if (
        identity in _AMBIGUOUS_PREPARATION_IDENTITIES
        and not re.search(
            r"\b(?:can|canned|tinned|cooked|boiled|dried|dry|uncooked|raw|"
            r"rinsed|drained)\b",
            str(name or ""),
            re.IGNORECASE,
        )
    ):
        return {
            "match": None,
            "source_key": source,
            "similarity": None,
            "confidence": "none",
            "reason": "ambiguous_preparation_state",
            "matched_name": None,
            "cleaned_query": identity,
            "nutrition_match_note": (
                "Specify dried, cooked, or canned preparation; water content "
                "makes these composition rows non-interchangeable."
            ),
        }
    if identity == "mixed beans":
        return {
            "match": None,
            "source_key": source,
            "similarity": None,
            "confidence": "none",
            "reason": "ambiguous_food_identity",
            "matched_name": None,
            "cleaned_query": identity,
            "nutrition_match_note": (
                "The bean varieties in the mixture are unknown; one arbitrary "
                "bean row was not substituted."
            ),
        }
    cleaned = clean_query(name) or str(name or "").strip().lower()

    # An explicit "or" describes alternatives, not a compound food. Match
    # each choice independently and use the best supported one. Keeping the
    # selected choice in both structured metadata and a human-readable note
    # makes the approximation visible downstream instead of silently treating
    # "margarine or butter" as one food.
    alternative_text = identity if re.search(r"\bor\b", identity, re.I) else cleaned
    alternatives = [
        re.sub(r"^either\s+", "", part, flags=re.I).strip(" ,;/")
        for part in re.split(r"\s+or\s+", alternative_text, flags=re.I)
    ]
    alternatives = [part for part in alternatives if part]
    if len(alternatives) > 1:
        # In recipe prose the final alternative normally carries the shared
        # noun: "orange or red bell pepper", "vegetable or beef stock".
        # Prefer choices from right to left so a shortened earlier branch is
        # not interpreted as another food (orange fruit, plain vegetable).
        # When the final choice is only a cut/part word, carry the food prefix
        # across: "white fish fillet or steak" -> "white fish steak".
        last_words = alternatives[-1].split()
        first_words = alternatives[0].split()
        shared_qualifiers = [
            word for word in first_words
            if word in {"wholegrain", "wholemeal", "wholewheat"}
        ]
        if shared_qualifiers and not set(shared_qualifiers).intersection(last_words):
            alternatives[-1] = " ".join([*shared_qualifiers, *last_words])
            last_words = alternatives[-1].split()
        if (
            len(last_words) == 1
            and _singular(last_words[0]) in _GENERIC_TRAILING_PARTS
            and len(first_words) > 1
            and _singular(first_words[-1]) in _GENERIC_TRAILING_PARTS
        ):
            alternatives[-1] = " ".join(first_words[:-1] + last_words)
        elif len(last_words) == 1 and len(first_words) > 1:
            shared_forms = {
                "dried", "fresh", "frozen", "ground", "raw", "smoked",
                "cooked", "powder", "paste", "spread", "essence", "extract",
            }
            if _singular(last_words[0]) in shared_forms:
                alternatives[-1] = " ".join(first_words[:-1] + last_words)
            elif first_words[0] in shared_forms:
                alternatives[-1] = f"{first_words[0]} {alternatives[-1]}"
        selected_alternative = alternatives[-1]
        result = best_nutrition_match(
            selected_alternative,
            source,
            min_similarity,
            identity_name=selected_alternative,
        )
        result = dict(result)
        if result.get("match") is not None and not _sodium_qualifiers_match(
            str(name or ""), str(result.get("matched_name") or "")
        ):
            return {
                "match": None,
                "source_key": source,
                "similarity": None,
                "confidence": "none",
                "reason": "unsupported_nutrition_variant",
                "matched_name": result.get("matched_name"),
                "cleaned_query": cleaned,
                "nutrition_match_note": (
                    "No verified low-sodium composition row is available; "
                    "a regular-sodium alternative was not substituted."
                ),
            }
        result["cleaned_query"] = cleaned
        result["selected_alternative"] = selected_alternative
        if result.get("match") is not None:
            previous_reason = str(result.get("reason") or "")
            result["reason"] = ";".join(
                part for part in (
                    f"alternative_selected:{selected_alternative}", previous_reason
                ) if part
            )
            result["nutrition_match_note"] = (
                f"Nutrition calculated using '{selected_alternative}' from "
                f"alternatives: {', '.join(alternatives)}."
            )
        else:
            result["reason"] = "no_compatible_alternative"
            result["nutrition_match_note"] = (
                f"No compatible nutrition row found for alternatives: "
                f"{', '.join(alternatives)}."
            )
        return result

    alias_hit = _curated_alias_lookup(str(name or ""), cleaned)
    if alias_hit is not None:
        for alias_source_key, alias_food_id in (
            (source, alias_hit.get(source)),
            ("eu", alias_hit.get("eu_food_id")),
        ):
            if not alias_food_id:
                continue
            record = get_nutrition_candidate_by_source_id(
                _REGION_COLLECTIONS[alias_source_key], alias_food_id
            )
            if record is not None:
                if not _sodium_qualifiers_match(
                    str(name or ""), _candidate_name(record)
                ):
                    continue
                # No synthetic similarity — this is an exact curated pin, not a
                # ranked embedding match, so `distance`/`similarity` stay unset
                # (same as `match.get("distance")` above, which is already None
                # since a directly-fetched record carries no distance).
                return {
                    "match": record, "source_key": alias_source_key, "similarity": None,
                    "confidence": "curated", "reason": "alias",
                    "matched_name": _candidate_name(record), "cleaned_query": cleaned,
                }
        # Alias has no id that resolves in the index — fall through to the ES
        # pool rather than silently dropping the ingredient.

    q_tokens = _tokens(cleaned)
    q_class = food_class(identity)

    # Candidate retrieval below runs on raw query text, before any of the
    # token-level synonym expansion in `_tokens()` applies — so a query using
    # only the value-side of a _SYNONYMS pair (e.g. "cilantro", whose only
    # mapping is "coriander" -> "cilantro") can retrieve zero candidates
    # even though the reranker would have handled it fine had the right
    # candidate been retrieved at all. Append known synonyms to the text
    # actually sent to retrieval so this class of miss can't happen.
    _retrieval_extra = {
        syn for raw_tok in _TOKEN_RE.findall(cleaned)
        for syn in (_SYNONYMS.get(raw_tok), _SYNONYMS_REV.get(raw_tok))
        if syn and syn not in cleaned
    }
    for raw_tok in _TOKEN_RE.findall(cleaned):
        _retrieval_extra.update(_RETRIEVAL_EXPANSIONS.get(raw_tok, set()))
    retrieval_query = f"{cleaned} {' '.join(sorted(_retrieval_extra))}".strip()

    # 1) Gather the complete candidate pool for the selected region. Regional
    #    and EU hits compete in one reranking pass; EU is not a second-stage
    #    fallback. This lets a more precise regional row win without forcing a
    #    weak regional candidate over a stronger EU composition match.
    cands: list[dict] = []
    for src_key, fn in _candidate_pools(source):
        try:
            hits = fn(retrieval_query) or []
        except Exception:
            hits = []
        for c in hits:
            if isinstance(c, dict):
                c2 = dict(c)
                c2["_source_key"] = src_key
                cands.append(c2)

    if not cands:
        negligible = _intentionally_negligible_result(identity, source, cleaned)
        if negligible is not None:
            return negligible
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
        if (
            classes_compatible(q_class, food_class(_candidate_name(c)))
            or _is_generic_stock_powder_parent(identity, _candidate_name(c))
        )
        and animal_kinds_compatible(identity, _candidate_name(c))
        and ingredient_forms_compatible(cleaned, _candidate_name(c))
    ]
    if not cands:
        negligible = _intentionally_negligible_result(identity, source, cleaned)
        if negligible is not None:
            return negligible
        return {"match": None, "source_key": source, "similarity": None,
                "confidence": "none", "reason": "no_semantically_compatible_candidates",
                "matched_name": None, "cleaned_query": cleaned}

    names = [_candidate_name(c) for c in cands]
    corpus = [_tokens(n) for n in names]
    bm = _bm25_scores(q_tokens, corpus) if q_tokens else [0.0] * len(cands)
    q_set = set(q_tokens)
    # "cornstarch" (query, one word) vs "Corn starch" (table, two words) are
    # the same food but share zero tokens under plain set overlap, which
    # triggers the no-overlap penalty below even at high similarity (0.88+).
    # Add the space-free concatenation as an extra pseudo-token on both
    # sides so either spelling convention overlaps with the other.
    q_compound = "".join(q_tokens) if len(q_tokens) > 1 else None
    # raw tokenisation of the *original* name (no stopword/singular folding) —
    # used only for the cooking-state / processed-marker exemption.
    q_raw_words = set(_TOKEN_RE.findall(_ascii_fold(str(name or "").lower())))
    n_q = max(1, len(q_tokens))
    identity_groups = _identity_token_groups(identity)
    generic_average_head = (
        next(iter(identity_groups[0] & {"cheese", "oil"}), None)
        if len(identity_groups) == 1
        else None
    )
    max_rrf = max(
        (float(candidate.get("rrf_score") or 0.0) for candidate in cands),
        default=0.0,
    )

    def _base_score(c, cname, ctoks, bms):
        d = c.get("distance")
        sim = (1.0 - float(d)) if d is not None else 0.0
        ctok_set = set(ctoks)
        overlap = len(q_set & ctok_set)
        if overlap == 0:
            c_compound = "".join(ctoks) if len(ctoks) > 1 else None
            if (q_compound and q_compound in ctok_set) or (
                c_compound and c_compound in q_set
            ):
                overlap = 1
            # A single-word query that's a genuine prefix of a candidate
            # token (or vice versa) is very likely the same food under a
            # more/less specific name — "beet" vs "beetroot", "aubergine"
            # vs "aubergines". Found 2026-09-21: "beet"/"beets" retrieved
            # "Beetroot, raw" as the TOP candidate but scored 0.08 (below
            # the 0.30 floor) purely from the zero-overlap penalty, because
            # the compound-concatenation rescue above only fires for
            # multi-token queries. Length-gated at 4 chars on the shorter
            # side so short, unrelated words don't get credit for merely
            # prefixing an unrelated longer word (e.g. "pea" must not
            # credit "peanut" — "pea" is 3 chars, below the gate).
            # Also requires both sides to resolve to the SAME real food
            # class (not "other") -- length-gating alone isn't enough:
            # "pepperoni" is a genuine 6-char prefix-match of "pepper" in
            # "Chili pepper, raw", and "vegeta" of "vegetal" in a biscuit
            # record, both totally unrelated foods that happened to share a
            # word stem. Those only became wrong MATCHES (not just wrong
            # overlap credit) because of the separate "other" class always
            # being treated as compatible (see classes_compatible) -- until
            # that's addressed generally, gate this specific rescue on a
            # real class match so it can't combine with that gap. Found via
            # the same before/after sweep, 2026-09-21.
            elif len(q_tokens) == 1:
                qt = q_tokens[0]
                cname_class = food_class(cname)
                if (
                    q_class != "other"
                    and q_class == cname_class
                    and len(qt) >= 4
                    and any(
                        len(ct) >= 4 and (ct.startswith(qt) or qt.startswith(ct))
                        for ct in ctoks
                    )
                ):
                    overlap = 1
        rrf = (
            float(c.get("rrf_score") or 0.0) / max_rrf
            if max_rrf > 0.0
            else 0.0
        )
        pen = 0.0
        if overlap == 0 and sim < _HIGH_SIM_NO_OVERLAP:
            pen -= 0.5
        if (
            not classes_compatible(q_class, food_class(cname))
            and not _is_generic_stock_powder_parent(identity, cname)
        ):
            pen -= 1.0
        c_raw_words = set(_TOKEN_RE.findall(_ascii_fold(str(cname or "").lower())))
        if generic_average_head and "average" in c_raw_words:
            pen += 0.15
        if (_PROCESSED_MARKERS & c_raw_words) - q_raw_words:
            pen -= 0.12  # cooking-state / processed / branded marker the query didn't ask for
        elif "raw" in c_raw_words and not (_COOKING_STATES & q_raw_words):
            pen += 0.06  # state-less query -> nudge toward the raw/uncooked record
        if not _identity_matches(identity, cname):
            pen -= 0.35
        if not _colors_match(identity, cleaned, cname):
            pen -= 0.25
        if not _qualifiers_match(identity, cleaned, cname):
            pen -= 0.25
        if not _sodium_qualifiers_match(str(name or ""), cname):
            pen -= 1.0
        if not _cooking_method_matches(cleaned, cname):
            pen -= 0.25
        pen -= 0.25 * len(_form_conflicts(identity, cleaned, cname))
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
        ((*_base_score(c, cn, ct, bms), c, cn) for c, cn, ct, bms in zip(cands, names, corpus, bm)),
        key=lambda t: -t[0],
    )
    # A candidate preserving the requested form always beats a generic-parent
    # fallback, even when the fallback's embedding score is slightly higher.
    # Example: search smoked chicken first; only use raw chicken if no valid
    # smoked-chicken candidate exists.
    selected = next(
        (
            row for row in ranked
            if _selected_match_level(identity, cleaned, row[-1]) == "exact"
            and _sodium_qualifiers_match(str(name or ""), row[-1])
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                row for row in ranked
                if _selected_match_level(identity, cleaned, row[-1]) == "safe_parent"
                and _sodium_qualifiers_match(str(name or ""), row[-1])
            ),
            None,
        )
    if selected is None:
        negligible = _intentionally_negligible_result(identity, source, cleaned)
        if negligible is not None:
            return negligible
        _, top_sim, _, top_candidate, top_name = ranked[0]
        compound_needs_split = bool(
            re.search(r"\b(?:and|with)\b|[&,]", identity, re.I)
            and len(_identity_token_groups(identity)) > 1
        )
        return {"match": None,
                "source_key": top_candidate.get("_source_key", source),
                "similarity": top_sim, "confidence": "none",
                "reason": (
                    "compound_ingredient_requires_split"
                    if compound_needs_split else "top_candidate_incompatible"
                ),
                "nutrition_match_note": (
                    "Compound ingredient requires separate ingredient weights; "
                    "no single nutrition row was used."
                    if compound_needs_split else None
                ),
                "matched_name": top_name, "cleaned_query": cleaned}

    score, sim, overlap, c, cname = selected
    src_key = c.get("_source_key", source)
    selected_level = _selected_match_level(identity, cleaned, cname)

    if score < _WEAK_SCORE:
        return {"match": None, "source_key": src_key, "similarity": sim,
                "confidence": "none", "reason": f"below_floor:{score:.2f}",
                "matched_name": cname, "cleaned_query": cleaned}

    strong = (
        (score >= _STRONG_SCORE or (
            selected_level == "safe_parent" and score >= _WEAK_SCORE
        ))
        and sim >= float(min_similarity)
        and (overlap > 0 or sim >= _HIGH_SIM_NO_OVERLAP)
    )
    if strong:
        confidence = "strong"
        reason = "safe_parent_fallback" if selected_level == "safe_parent" else ""
    else:
        confidence = "weak"
        reason = f"weak:{score:.2f}"
    return {
        "match": c, "source_key": src_key, "similarity": sim,
        "confidence": confidence, "reason": reason or "",
        "nutrition_match_note": (
            f"Exact requested form was unavailable; nutrition calculated using "
            f"'{cname}' as a generic parent."
            if selected_level == "safe_parent" else None
        ),
        "matched_name": cname, "cleaned_query": cleaned,
    }
