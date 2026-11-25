"""Live sustainability adaptation of one recipe, with the user's allergens and diet applied.

Same engine as the stored rows (sustainability_adaptation.adapt) and the same AGRIBALYSE factors (CIQUAL code join, then the reviewed name
mapping in tools/sustainability_factors). The answer has the shape of a recipe_adaptations row, so the caller can show it like a stored one.
No model calls: the text is the changed ingredient line plus one code-written CO2e sentence; title and steps are not rewritten."""
import json
import re
from functools import lru_cache

from recipe_wrangler.tools import sustainability_factors as FM

from . import nutrition_adaptation_llm as L
from . import sustainability_adaptation as sa
from .food_roles import content_words
from .nutrition_adaptation import user_restriction_problems

VERSION = 'live'
SOURCE = 'AGRIBALYSE 3.2 reviewed name mapping'
COMPOSITE = re.compile(r'\b(dough|cakes?|soups?|pies?|stews?|casseroles?|filling|pizza|sandwich\w*|marinade)\b', re.I)
ALIASES = [  # target record label -> the name a cook uses
    (r'^(chicken|poultry),? .*\b(mince|minced)\b|^mince chicken|^poultry,? minced', 'chicken mince'),
    (r'\bturkey\b.*\bmince\b', 'turkey mince'),
    (r'^(chicken|poultry)\b|\bchicken\b', 'chicken'),
    (r'^turkey\b', 'turkey'),
    (r'\bhake\b', 'hake'),
    (r'\bsardines?\b', 'sardines'),
    (r'\bmargarine\b', 'margarine'),
    (r'\bmozzarella\b', 'mozzarella'),
    (r'\byogh?urt\b', 'Greek yogurt'),
    (r'\blentils?\b', 'cooked lentils'),
]
COUNT_LINE = re.compile(r'^\s*\d+\s+(?!(?:g|kg|ml|l|oz|lb|tbsp|tsp|tablespoons?|teaspoons?|cups?|x)\b)', re.I)
OLD_FOODS = re.compile(r'\b(beef|veal|lamb|mutton|pork|tuna|cod|haddock|pollock|whiting|plaice|prawns?|shrimps?|bacon|ham|salami|pepperoni|butter|cheddar|gouda|edam|emmental|gruy[eè]re|cheese|chicken|turkey|cream|cr[eè]me fra[iî]che)\b', re.I)
_POOLS = {}


@lru_cache(maxsize=1)
def _codes():
    """CIQUAL code -> [kg CO2e/kg, AGRIBALYSE name], no-preparation scenario (the 3 Nov release policy)."""
    return json.loads((FM.DATA / 'agribalyse_ciqual_codes.json').read_text())


def ciqual_code(candidate_id):
    cid = str(candidate_id or '')
    m = re.match(r'ciqual:(\d+)$', cid)
    if m:
        return m.group(1)
    if cid.startswith('catalogue:') and 'CIQUAL' in cid:
        m = re.search(r'"(\d+)"\]', cid)  # catalogue:["Anses ... CIQUAL ...","EFSA EU FCDB 2026","<code>"]...
        return m.group(1) if m else None
    return None


def factor_route(name, weight_g, region='eu', label='', candidate_id=''):
    """(kg CO2e/kg, source, route, record). Order: bare-pepper weight rule, CIQUAL code join, name rule, record-label rule, CONCITO, role average.
    Only 'agribalyse' sources may be swapped."""
    if name.strip().lower() in ('pepper', 'peppers'):  # a bare "pepper": over 10 g it is a bell pepper, otherwise ground black pepper
        key = 'sweet pepper green yellow or red raw' if weight_g > 10 else 'black pepper powder'
        return FM._REFS[key], 'agribalyse', 'pepper_weight_rule', key
    code = ciqual_code(candidate_id)
    if code and code in _codes():
        cf, rec = _codes()[code]
        return cf, 'agribalyse', 'ciqual_code_join', rec
    if COMPOSITE.search(name) or COMPOSITE.search(label):
        return None, None, 'composite_unscored', None  # a dough, cake or soup is not its main ingredient
    order = (('label_rule', label), ('name_rule', name)) if len(content_words(name)) <= 1 else (('name_rule', name), ('label_rule', label))
    for route, text in order:  # a bare word ('cheese') defers to the matcher's more specific record ('Cheese, cottage')
        if text:
            key = FM.map_name(text)[0]
            if key in FM._REFS:
                return FM._REFS[key], 'agribalyse', route, key
    cf = FM.concito_factor(name, region)
    if cf is not None:
        return cf, 'concito', 'concito_fallback', name
    role, cf = FM.role_average(name)
    return (cf, 'estimate', 'role_average', role) if cf is not None else (None, None, 'none', None)


def item_factor_for(name, weight_g, region, label, candidate_id):
    return factor_route(name, weight_g, region, label, candidate_id)[:2]


def label_factor_for(label, candidate_id=None):
    """Targets need an AGRIBALYSE factor (code join or reviewed rule); role averages are for sources only."""
    code = ciqual_code(candidate_id)
    if code and code in _codes():
        return _codes()[code][0]
    return FM._REFS.get(FM.map_name(label)[0])


def cook_label(label, source_line):
    for rx, name in ALIASES:
        if re.search(rx, label, re.I):
            if name == 'chicken' and COUNT_LINE.match(source_line or ''):
                return 'chicken piece' if re.match(r'\s*1\s', source_line or '') else 'chicken pieces'
            if name == 'sardines' and re.search(r'\b(tins?|cans?|canned|tinned)\b', source_line or '', re.I):
                return 'tinned sardines'
            return name
    return label


def half_line(old, weight_after_g):
    """Half the meat stays, the same weight of cooked lentils is added: '225g chicken fillets, diced, plus 225g cooked lentils'."""
    lead = L.QTY_PREFIX.match(old).group(1)
    rest = re.sub(r'\([^)]*\)', '', old[len(lead):])
    rest = re.sub(r'^\s*/\s*[\d.,½¼¾⅓⅔ ]+\s*(oz|lb|lbs|g)\.?\s*(of\s+)?', '', rest)  # the second unit of '500g / 17 1/2 oz.' would go stale
    rest = re.sub(r'^\s*(of\s+)?((small|medium|large)\s+)?', '', rest.strip(), flags=re.I)
    rest = re.sub(r'\s+,', ',', re.sub(r'\s+', ' ', rest)).strip(' ,.')
    g = f'{max(5, round(weight_after_g / 5) * 5)}g'
    return f'{g} {rest}, plus {g} cooked lentils'


def co2_sentence(change, before, after):
    """What is replaced by what, and the measured CO2e difference (code, never a model)."""
    m = OLD_FOODS.search((change.get('source_line') or '') + ' ' + (change.get('matched_label') or ''))
    old, new = (m.group(1).lower() if m else L.food_word(change.get('source_line') or change['matched_label'])), change['candidate_label']
    per_kg = round((1 - change['to_cf'] / change['from_cf']) * 100)
    cut = f"cuts this recipe's footprint by {round((before - after) / before * 100)}% ({before - after:.2f} kg CO2e per serving)"
    if change.get('half_lentils'):
        return f"We replace half of the {old} with {new} because {new} have {per_kg}% lower CO2e per kg, which {cut}."
    return f"We replace the {old} with {new} because it has {per_kg}% lower CO2e per kg, which {cut}."


def _row(case, region, status, reason_code=None, **extra):
    return dict(recipe_id=case['recipe_id'], mode='sustainability', region=region, version=VERSION, source='live', status=status,
                is_adapted=status == 'adapted', reason_code=reason_code, original=None, adapted=None, swap=None, benefit=None,
                provenance={'source': SOURCE}, **extra)


def adapt_recipe(case, snapshot, recipe, records, region, item_factor=None, label_factor=None, pool=None, allergens=(), diet=None):
    """case: {recipe_id, title, region: nutrition source}; region: IE, HU, SI or EU. Returns a recipe_adaptations-shaped dict."""
    applied = {'allergens': list(allergens), 'diet': diet}
    lines = [i.original_text or i.name or '' for i in snapshot.ingredients]
    own = user_restriction_problems(lines, allergens, diet)
    if own:  # a swap cannot make the recipe safe for this user, so none is offered
        return _row(case, region, 'not_suitable_for_user', 'recipe_breaks_user_restriction', offending=own, applied_restrictions=applied)
    source = case['region']
    out = sa.adapt(case, snapshot, recipe, records, item_factor or (lambda n, w, lab, cid: item_factor_for(n, w, source, lab, cid)),
                   label_factor or label_factor_for, pool=pool, allergens=allergens, diet=diet)
    if out['status'] == 'not_scored':
        return _row(case, region, 'not_scored', out['reason'], applied_restrictions=applied)
    if out['status'] != 'adapted':
        return _row(case, region, 'no_suggestion', out.get('why_no_suggestion'), applied_restrictions=applied)
    c = out['candidates'][0]
    name = cook_label(c['to_label'], c['source_line'])
    old = next(i for i in snapshot.ingredients if i.position == c['position'])
    old_text = old.original_text or old.name
    change = {'position': c['position'], 'operation': 'SWAP_FOOD', 'category': 'co2_swap', 'source_line': c['source_line'], 'matched_label': c['from_label'],
              'candidate_label': name, 'record_label': c['to_label'], 'half_lentils': c['partial'], 'weight_before_g': c['weight_before_g'],
              'weight_after_g': c['weight_after_g'], 'reason': c['reason'], 'from_cf': c['from_cf'], 'to_cf': c['to_cf']}
    if c['partial']:
        new_text = half_line(old_text, c['weight_after_g'])
    else:
        new_text = f'{old_text}  →  use {name}'  # ponytail: no rewrite without a model; the original amount stays visible (a tin or count line cannot be re-stated safely in code)
    before = out['co2_per_serving']
    after = round(before - c['saved_per_serving'], 4)
    steps = [L.STEP_NUMBER.sub('', s) for s in L.flat_steps(recipe.get('instructions'))]
    row = _row(case, region, 'adapted', applied_restrictions=applied)
    row.update(
        original=dict(title=out['title'], lines=lines, steps=steps),
        adapted=dict(title=out['title'], lines=[new_text if i.position == c['position'] else t for i, t in zip(snapshot.ingredients, lines)],
                     steps=steps, changed_positions=[c['position']]),
        swap=change,
        benefit={'facet': 'sustainability', 'tip': co2_sentence(change, before, after), 'tip_source': 'code',
                 'metric': {'name': 'co2e_kg_per_serving', 'unit': 'kg CO2e', 'before': before, 'after': after, 'change': round(-c['saved_per_serving'], 3)},
                 'swap': [{'from': L.food_word(c['from_label'] or c['source_line']), 'to': L.food_word(name), 'from_record': c['from_label'], 'to_record': name}],
                 'notes': []},
        provenance={'source': SOURCE, 'text': 'code_line_only: title and steps are not rewritten',
                    'flags': {'changes_dish_identity': c['changes_dish_identity'], 'indicative': c['indicative'], 'cross_database': False},
                    'co2_per_serving_before': before})
    return row


def adapt_active_recipe(recipe_id, region, allergens=(), diet=None):
    """Load the recipe's active regional profile (as the nutrition route does) and adapt it. Read-only."""
    from recipe_wrangler.repositories import matching_store as store
    from recipe_wrangler.tools import fetch_recipe_info as fetch

    from .comparison_store import ComparisonLoadError, load_active_snapshot
    from .enhancement_pipeline import _verify_source_binding
    from .nutrition_adaptation import release_records
    from .service import _region_to_source
    source = _region_to_source(region)
    case = {'recipe_id': recipe_id, 'region': source, 'title': recipe_id}
    try:
        before = store.active_profile(recipe_id, source, include_provenance=True)
        if not before:
            raise ComparisonLoadError('no_active_profile')
        snapshot = load_active_snapshot(recipe_id, source, allow_input_estimates=True)
        if snapshot.run_id != before['run_id']:
            raise ComparisonLoadError('snapshot_run_differs')
        recipe = fetch.fetch_recipe_info_by_id(recipe_id)
        _verify_source_binding(snapshot, recipe, before['profile'])
        case['title'] = recipe.get('title') or recipe_id
        records = release_records(snapshot.release_id)
        key = (snapshot.release_id, source)
        if key not in _POOLS:
            _POOLS[key] = sa._pool(records, source, label_factor_for)
        return adapt_recipe(case, snapshot, recipe, records, region.upper(), pool=_POOLS[key], allergens=allergens, diet=diet)
    except ComparisonLoadError as error:
        return _row(case, region.upper(), 'failed', error.reason)
