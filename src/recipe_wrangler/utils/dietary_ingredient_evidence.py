"""Exact, attributed ingredient knowledge; no network or generative inference."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata


def normalize(name: str) -> str:
    text = unicodedata.normalize('NFKC', name).casefold().replace('’', "'")
    return re.sub(r'\s+', ' ', re.sub(r'[-_]', ' ', text)).strip()


# Name equivalences only, not similar foods or product formulations.
_ALIASES = {
    'grapeseed oil': 'grape seed oil',
    "confectioners' sugar": 'icing sugar',
    'confectioners sugar': 'icing sugar',
    "confectioner's sugar": 'icing sugar',
    'soymilk': 'soy milk',
    'soya milk': 'soy milk',
    'popped popcorn': 'popcorn',
}
_PREPARATION = re.compile(
    r'^(?:(?:fresh|raw|organic|frozen|dried|dry|ground|chopped|diced|sliced|minced|'
    r'grated|crushed|peeled|canned|tinned|drained|rinsed|unsalted|roasted)[ -]+)+'
)


_LEADING_QUANTITY = frozenset(
    "or about approx approximately a an one two three few some large small medium big heaped level packed generous "
    "handful handfuls bunch bunches sprig sprigs pinch pinches dash dashes tin tins can cans packet packets of".split())
_TRAILING_PREP = re.compile(
    r"(?:[ ,]+(?:cubed|chopped|diced|sliced|minced|grated|shredded|peeled|halved|quartered|crushed|drained|rinsed|softened|"
    r"melted|beaten|trimmed|torn|thawed|optional|to (?:taste|serve)|for (?:serving|garnish|frying|greasing)|"
    r"in (?:water|brine|oil|juice|natural juice|spring water)))+$")


def _food_token(token: str) -> str:
    from recipe_wrangler.utils.food_identity import _IRREGULAR, _SYNONYMS
    token = _IRREGULAR.get(token) or (token[:-1] if token.endswith("s") and len(token) > 3 else token)
    return _SYNONYMS.get(token, token)


def clean_ingredient_name(name: str) -> str:
    """Parser residue only: leading quantity words, trailing preparation, and synonym pairs ("eggplant aubergine").

    Used for positive evidence only; blocking rules still read the original name. Never rewrites the stored name.
    """
    text = _TRAILING_PREP.sub("", normalize(name))
    tokens = text.split()
    while len(tokens) > 1 and (tokens[0] in _LEADING_QUANTITY or re.fullmatch(r"\d+(?:[./]\d+)?", tokens[0])):
        tokens.pop(0)
    kept: list[str] = []
    for token in tokens:
        if kept and _food_token(token) == _food_token(kept[-1]):
            continue
        kept.append(token)
    return " ".join(kept) or normalize(name)


def canonical_ingredient_name(name: str) -> str:
    """Apply only the reviewed spelling equivalences shared with local rules."""
    key = normalize(name)
    return _ALIASES.get(key, key)


@lru_cache(maxsize=1)
def _dataset() -> dict:
    return json.loads(Path(__file__).with_name('data').joinpath('dietary_ingredients.json').read_text())


@lru_cache(maxsize=1)
def _index() -> dict:
    data = _dataset()
    index = {}
    for entry in data['entries']:
        for name in entry['names']:
            key = normalize(name)
            if key in index and index[key] != entry:
                index[key] = None  # A shared alias cannot establish identity.
            else:
                index[key] = entry
    return index


def ingredient_diet_evidence(name: str, group: str) -> dict | None:
    """Return an exact-name property and its original evidence roots.

    Do not strip arbitrary adjectives, quantities, warnings or compound tails.
    Pescatarian is not inferred from OFF vegetarian values.
    """
    if group not in {'vegan', 'vegetarian'}:
        return None
    key = normalize(name)
    index = _index()
    if key not in index:
        key = _PREPARATION.sub('', key)
    key = canonical_ingredient_name(key)
    entry = index.get(key)
    if entry is None:
        return None
    property_ = entry[group]
    if property_['value'] is None:
        return None
    return {'status': {'yes': 'suitable', 'no': 'not_suitable', 'maybe': 'unknown'}[property_['value']],
            'ingredient_id': entry['id'], 'property_sources': property_['sources'],
            'source_provenance': dict(_dataset()['source']),
            'source': 'open_food_facts_ingredients'}
