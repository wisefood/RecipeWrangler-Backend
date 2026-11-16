"""Adapters retain native IDs; crosswalks are explicit, never numeric-ID equality."""
from functools import lru_cache
import re
import hashlib
import json
import unicodedata

from recipe_wrangler.tools.reviewed_european_portions import _PORTIONS, _DEFAULT_BASIS, _ALIASES
from recipe_wrangler.utils import weigh_calculation_usda_ as usda
from .core import PortionFacets, PortionRecord, infer_facets

# Reviewed generic raw-food identities. Facets remain separate from this family key.
USDA_FOOD_KEYS = {
    '09003': 'apple', '09004': 'apple', '09040': 'banana', '09252': 'pear',
    '09200': 'orange', '09218': 'mandarin', '09037': 'avocado',
    '11352': 'potato', '11205': 'cucumber', '11206': 'cucumber',
}

# Source measures that are not a standard unit of the food (snack slices, crustless slices, single-serve sachets).
UNSUPPORTED_SCALE_RE = re.compile(r'\b(?:extra|very)\s+(?:small|large)\b|\bjumbo\b|\bsnack[- ]size\b|\bmini(?:ature)?\b|'
                                  r'\bbite[- ]size\b|\bshaved\b|\bcrust not eaten\b|\bsachet\b|\bsingle serve\b', re.I)

COUNT_UNITS = {'fruit': 'piece', 'whole': 'piece', 'potato': 'piece', 'cucumber': 'piece',
               'avocado': 'piece', 'apple': 'piece', 'pear': 'piece', 'carrot': 'piece',
               'almond': 'nut', 'nuts': 'nut', 'nut': 'nut', 'egg': 'egg',
               'head': 'head', 'fillet': 'fillet', 'leaf': 'leaf'}


def normalize_portion_name(name: str) -> str:
    text = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    replacements = {r'\bblack currants?\b': 'blackcurrant', r'\bcowberry\b': 'lingonberry',
                    r'\bbutter milk\b': 'buttermilk', r'\bsour milk,?\s*buttermilk\b': 'buttermilk',
                    r'\boats,?\s*rolled,?\s*oat flakes\b': 'rolled oats',
                    r'\bdomestic\b': '', r'\bwithout skin\b': 'peeled',
                    r'\bsoya\b': 'soy', r'\bkiwi fruits?\b': 'kiwifruit', r'\bkiwis?\b': 'kiwifruit',
                    r'^coconut flakes?$': 'desiccated coconut', r'^liquid honey$': 'honey', r'^ground almonds$': 'almond meal', r'\bsmoked paprika\b': 'paprika',
                    r'^(?:plain |all[- ]purpose )?flour$': 'wheat flour, white, all-purpose',
                    r'\bcourgettes?\b': 'zucchini', r'\bbell peppers?\b': 'sweet pepper',
                    r'^(?:penne|fusilli|rigatoni|farfalle|linguine|tagliatelle|macaroni|spaghetti)$': 'pasta'}
    for pattern, value in replacements.items(): text = re.sub(pattern, value, text)
    return ' '.join(text.split())


def product_identity_compatible(name: str, label: str) -> bool:
    """Do not resolve an ingredient to a compound product that merely contains it."""
    words = set(re.findall(r'[a-z]+', normalize_portion_name(name)))
    source = set(re.findall(r'[a-z]+', normalize_portion_name(label)))
    compounds = {'cookie', 'cookies', 'roll', 'rolls', 'bread', 'cake', 'cakes',
                 'sandwich', 'sandwiches', 'pudding', 'pancake', 'pancakes',
                 'biscuit', 'biscuits', 'waffle', 'waffles',
                 'bagel', 'bagels', 'sauce', 'sauces', 'dressing', 'dressings', 'babyfood',
                 'croissant', 'croissants', 'muffin', 'muffins', 'pie', 'pies', 'tart', 'tarts', 'strudel'}
    if words & {'toast', 'breadcrumbs'}: words.add('bread')
    products = lambda tokens: {word.rstrip('s') for word in tokens & compounds}
    if products(source) - products(words):
        return False
    # "spaghetti and meatballs", "sugar and sucralose": a second food makes it a dish or blend.
    filler = {'and', 'with', 'added', 'vitamin', 'a', 'd', 'regular', 'quick', 'contains', 'or',
              'meat', 'skin', 'flesh', 'seeds', 'bone', 'fat', 'lean'}  # USDA cut wording, not dishes
    for segment in normalize_portion_name(label).split(','):
        if re.search(r'\band\b', segment) and not re.search(r'\band\b', normalize_portion_name(name)):
            if set(re.findall(r'[a-z]+', segment)) - words - filler:
                return False
    return True


def identity_key(usda_id: str) -> str:
    food = USDA_FOOD_KEYS.get(usda_id)
    return 'ingredient:' + food if food else 'usda_weights:' + usda_id


def reviewed_name_key(name: str) -> str | None:
    words = {_ALIASES.get(w, w) for w in re.findall(r'[a-z]+', normalize_portion_name(name))}
    foods = words & _DEFAULT_BASIS.keys()
    modifiers = {'raw', 'fresh', 'ripe', 'small', 'medium', 'large', 'whole',
                 'peeled', 'unpeeled', 'with', 'without', 'peel', 'skin',
                 'chopped', 'sliced', 'diced', 'ground', 'mashed', 'shredded',
                 'cooked', 'boiled', 'baked', 'fried', 'roasted', 'steamed',
                 'dried', 'frozen', 'canned', 'pickled', 'smoked'}
    if len(foods) == 1 and not words - foods - modifiers:
        return 'ingredient:' + next(iter(foods))
    return None


def european_records() -> list[PortionRecord]:
    result = []
    for (food, peel, size), (code, label, desc, grams) in _PORTIONS.items():
        result.append(PortionRecord(
            record_id=f'rivm:2026-2.0:{code}:{peel}:{size}', food_id='ingredient:' + food,
            source_food_id='nevo:' + code, food_name=label, unit='piece',
            grams_per_unit=grams,
            facets=PortionFacets(size=size, preparation='raw', form='whole', peel=peel, part='whole'),
            source='Portie-online versie 2026/2.0, RIVM, Bilthoven.', region='NL',
            description=desc, original={'NEVO-code': code, 'NEVO-naam': label,
                                       'Maat': desc, 'Hoeveelheid': grams, 'Eenheid': 'gram'},
            excluded_parts={'apple': ('core',), 'pear': ('core',), 'avocado': ('pit',),
                            'cucumber': ('waste',)}.get(food, ()),
        ))
    return result


def usda_records(rows, unit_volumes: dict | None = None):
    """Normalize all valid native portions without inventing absent facets."""
    for row in rows:
        code = str(row.get('usda_id') or '')
        fndds = str(row.get('food_code') or '') if not code else ''
        if not code and not fndds: continue
        native_id = 'usda_weights:' + code if code else 'fndds_weights:' + fndds
        food_id = identity_key(code) if code else native_id
        label = str(row.get('food_name') or '')
        for original in row.get('portions') or []:
            normalized = usda._normalized_portion(original)
            if normalized is None: continue
            desc = str(normalized['portion_desc'])
            facets, blockers = infer_facets(label + ', ' + desc)
            size_facets, size_blockers = infer_facets(desc)
            values = facets.model_dump()
            values['size'] = size_facets.size
            if code == '08120': values['preparation'] = 'raw'  # Reviewed plain dry rolled oats.
            blockers = [b for b in blockers if b != 'ambiguous_size'] + size_blockers
            unit = usda._portion_unit(desc)
            # "Regular carrot" is a count of a whole carrot, not a size assertion.
            regular = re.fullmatch(r'regular ([a-z]+)', desc.lower().strip())
            food_words = {usda._normalize_token(w) for w in re.findall(r'[a-z]+', label.lower())} - {'raw', 'fresh', 'cooked', 'regular', 'whole'}
            if (regular and usda._normalize_token(regular.group(1)) in food_words) or desc.lower().strip() == 'regular':
                unit = 'piece'
            elif (usda._normalize_token(unit) in food_words
                  or usda._normalize_token(unit) == usda._normalize_token(re.sub(r'[^a-z]', '', label.split(',')[0].lower()))):
                unit = 'piece'  # "pepper, large" on Peppers, "sweetpotato" on Sweet potato = one whole item
            if re.search(r'\bice\b', desc.lower()) and 'ice' not in food_words:
                blockers.append('ice_measure')  # "cup ice" weighs the frozen form, not the liquid
            unit = COUNT_UNITS.get(unit, unit)
            if unit in {'small', 'medium', 'large', 'piece'}:
                unit = 'piece'
            if unit in {'piece', 'egg', 'nut', 'head', 'fillet', 'leaf'} and values['form'] is None:
                values['form'] = 'whole'
            if UNSUPPORTED_SCALE_RE.search(desc):
                blockers.append('unsupported_size_scale')
            try:
                fingerprint = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()[:16]
                yield PortionRecord(
                    record_id=f'{native_id}:{fingerprint}', food_id=food_id,
                    source_food_id=native_id, food_name=label,
                    unit=unit, grams_per_unit=normalized['grams_per_unit'],
                    facets=PortionFacets(**values), source='Existing USDA weights snapshot; release lineage unverified',
                    region='US', description=desc, original=dict(original), blockers=tuple(sorted(set(blockers))),
                    unit_volume_ml=(unit_volumes or {}).get(unit),
                    volume_profile='existing_usda_unit_volume_profile' if (unit_volumes or {}).get(unit) else None,
                )
            except ValueError:
                continue


@lru_cache(maxsize=1)
def _native_food_rows():
    result = {}
    for row in usda._weight_rows(str(usda.DEFAULT_WEIGHTS)):
        key = str(row['usda_id']) if row.get('usda_id') else 'fndds_weights:' + str(row.get('food_code'))
        result.setdefault(key, []).append(row)
    return result


def records_for_food(food_id: str) -> list[PortionRecord]:
    from .external import external_for
    records = [r for r in european_records() if r.food_id == food_id]
    records.extend(external_for(food_id))
    if food_id.startswith('ingredient:'):
        codes = [code for code in USDA_FOOD_KEYS if identity_key(code) == food_id]
    elif food_id.startswith('usda_weights:'):
        codes = [food_id.split(':', 1)[1]]
    elif food_id.startswith('fndds_weights:'):
        codes = [food_id]
    else:
        return records
    rows = [row for code in codes for row in _native_food_rows().get(code, [])]
    if rows:
        volumes = usda._unit_volumes_ml(str(usda.DEFAULT_UNIT_VOLUMES))
        records.extend(usda_records(rows, volumes))
    return records


def native_food_label(food_id: str) -> str | None:
    key = food_id.split(':', 1)[1] if food_id.startswith('usda_weights:') else food_id
    rows = _native_food_rows().get(key, [])
    labels = {row.get('food_name') for row in rows if row.get('food_name')}
    if not labels:
        from .external import external_for
        labels = {r.food_name for r in external_for(food_id)}
    return next(iter(labels)) if len(labels) == 1 else None


def label_key(name: str) -> tuple[str, ...]:
    text = normalize_portion_name(name)
    text = re.sub(r'\b\d+(?:\.\d+)?\s*%\s*fat\b', '', text)
    text = re.sub(r'\(includes foods for usda.*?\)', '', text)
    text = re.sub(r'\b(?:currants?, european black|black currants?)\b', 'blackcurrant', text)
    return tuple(sorted({usda._normalize_token(w) for w in re.findall(r'[a-z]+', text)
                         if w not in {'raw', 'fresh'}}))


@lru_cache(maxsize=1)
def _label_index():
    result = {}
    for key, rows in _native_food_rows().items():
        native = key if key.startswith('fndds_weights:') else 'usda_weights:' + key
        for row in rows:
            result.setdefault(label_key(row['food_name']), set()).add(native)
    from .external import external_index
    for native, rows in external_index()[1].items():
        for row in rows:
            result.setdefault(label_key(row.food_name), set()).add(native)
    return result


def resolve_food_name(name: str) -> dict | None:
    """Reviewed lexical synonyms precede exact full-label lookup, without embeddings."""
    normalized = normalize_portion_name(name)
    alias = {'rolled oats': 'usda_weights:08120', 'blackcurrant': 'usda_weights:09083',
             'buttermilk': 'fndds_weights:11115300', 'milk': 'usda_weights:01077'}.get(normalized)
    ids = {alias} if alias else _label_index().get(label_key(name), set())
    # Full-name matches from independent sources can share an already verified identity.
    from .external import external_identity
    canonical = {external_identity(i) or i for i in ids}
    if len(canonical) == 1 and len(ids) > 1:
        ids = {sorted(ids, key=lambda i: (not i.startswith('usda_weights:'), i))[0]}
    if len(ids) != 1:
        return None
    native = next(iter(ids))
    label = native_food_label(native)
    if not label: return None
    result = {'canonical': label, 'match_source': 'reviewed_alias' if alias else 'exact_source_label'}
    if alias == 'usda_weights:08120': result['identity_query'] = 'rolled oats dry'
    if native.startswith('usda_weights:'): result['usda_id'] = native.split(':', 1)[1]
    else: result['native_food_id'] = native
    return result


def default_european_peel(food_id: str) -> str | None:
    return _DEFAULT_BASIS.get(food_id.removeprefix('ingredient:'))
