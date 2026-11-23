"""Reviewed ingredient names pinned to a record, and reviewed values for blanks in records, on top of the release."""
import json
import re
from pathlib import Path

_SPRAY_OIL = ('cofid:17-686', 'Oil, vegetable, average')
_CURRY_PASTE = ('cofid:17-720', 'Curry paste')
_DRY_PASTA = ('ciqual:9810', 'Pasta, dry, regular, raw')
_SOURDOUGH = ('nevo:2788', 'Wheat sourdough bread wholemeal')
_APPLESAUCE = ('fineli:472', 'APPLESAUCE, UNSWEETENED')
_BROWN_RICE = ('cofid:11-869', 'Rice, brown, wholegrain, boiled in unsalted water')
_BROWN_SUGAR = ('ciqual:31017', 'Sugar, brown')

_NAMES = {
    'boiled water': ('matvaretabellen:1341', 'Water, tap'),
    'sea salt': ('matvaretabellen:1315', 'Salt, sea'),
    'lemongrass': ('efsa_czech:1041', 'Spices, lemon grass'),
    'sourdough bread': _SOURDOUGH,
    'sourdough': _SOURDOUGH,
    'applesauce': _APPLESAUCE,
    'unsweetened apple sauce': _APPLESAUCE,
    'apple puree': ('slv:1811', 'Apple purée unsweetened or artificial sweeteners'),
    'prosciutto': ('ciqual:28812', 'Dry-cured ham'),
    'chilli beans': ('nevo:5171', 'Beans chilli canned'),
    'gluten-free baking powder': ('cofid:17-355', 'Baking powder'),
    'liquid chicken stock': ('cofid:17-681', 'Stock, chicken, ready made, retail'),
    'instant polenta': ('ciqual:9614', 'Polenta or maize/corn semolina, pre-cooked, raw'),
    'steamed rice': ('ciqual:9104', 'Rice, white, cooked, no added salt'),
    'steamed brown rice': ('matvaretabellen:640', 'Rice, brown, long-grain, cooked'),
    'microwavable brown rice': _BROWN_RICE,
    'celery salt': ('ciqual:11044', 'Salt, with celery'),
    'toasted sesame seeds': ('cofid:14-844', 'Sesame seeds'),
    'dark brown sugar': _BROWN_SUGAR,
    'light muscovado sugar': _BROWN_SUGAR,
    'raw sugar': ('cofid:17-061', 'Sugar, Demerara'),
    'marjoram': ('nevo:1226', 'Marjoram dried'),
    'sponge fingers': ('nevo:260', 'Biscuit sponge fingers'),
    'black-eyed peas': ('nevo:5167', 'Beans black eyed canned'),
    'cremini mushrooms': ('cofid:13-505', 'Mushrooms, white, raw'),
    'dried breadcrumbs': ('nevo:1013', 'Bread crumbs'),
    'ciabatta': ('cofid:11-969', 'Bread, ciabatta'),
    'filo': ('matvaretabellen:1530', 'Filo pastry'),
    'saffron strands': ('ciqual:11039', 'Saffron'),
    'marmite': ('nevo:441', 'Yeast extract Marmite'),
    'rice paper wrappers': ('matvaretabellen:2688', 'Rice paper'),
    'wonton wrappers': ('matvaretabellen:1266', 'Wonton pastry'),
    'ginger paste': ('fineli:33022', 'SPICE PASTE, GINGER PASTE'),
    'pita pockets': ('cofid:11-974', 'Bread, pitta, white'),
    'rashers': ('cofid:19-497', 'Bacon rashers, back, raw'),
    'butter lettuce': ('nevo:46', 'Lettuce butterhead raw'),
    'vanilla bean': ('ciqual:11057', 'Vanilla, pod'),
    'spray oil': _SPRAY_OIL,
    'cooking spray': _SPRAY_OIL,
    'oil spray': _SPRAY_OIL,
    'vegetable spray oil': _SPRAY_OIL,
    'nonstick cooking spray': _SPRAY_OIL,
    'korma curry paste': _CURRY_PASTE,
    'korma paste': _CURRY_PASTE,
    'massaman curry paste': _CURRY_PASTE,
    'laksa paste': _CURRY_PASTE,
    'tandoori paste': _CURRY_PASTE,
    'pasta spirals': _DRY_PASTA,
    'rigatoni': _DRY_PASTA,
    'lasagna noodles': _DRY_PASTA,
}

REVIEWED_ALIASES = [{'alias': name, 'eu_food_id': record, 'food_name': label, 'note': 'reviewed 2026-11-05'}
                    for name, (record, label) in _NAMES.items()]

_DATA = json.loads((Path(__file__).parent / 'reviewed_nutrition.json').read_text())
RECORD_FILLS = _DATA['fills']
APPROXIMATE_NAMES = {row['alias']: row['approximate'] for row in _DATA['aliases'] if row.get('approximate')}
REVIEWED_SELECTIONS = {row['alias']: row for row in _DATA['aliases']}
# The gated engine rescues unresolved lines only through REVIEWED_SELECTIONS; the owner-reviewed names above
# reached the legacy matcher alone and stayed unresolved, so they join the fallback (it never overrides a match).
for _name,(_record,_label) in _NAMES.items():
    REVIEWED_SELECTIONS.setdefault(_name,{'alias':_name,'eu_food_id':_record,'food_name':_label,'note':'reviewed 2026-11-05'})
for _name in ('chilli flakes', 'chili flakes', 'dried chilli flakes', 'dried chili flakes',
              'red chilli flakes', 'red chili flakes', 'crushed red pepper flakes'):
    REVIEWED_SELECTIONS[_name] = {
        **REVIEWED_SELECTIONS['crushed chillies'], 'alias': _name,
        'approximate': 'Dried chilli flakes estimated using the reviewed chilli powder record; grind and blend may differ.',
    }


# Correct ingredient-level fallbacks; these are estimates, never substitutions.
for _name, _identifier, _label, _reason in (
    ('cranberry sauce', 'nevo:2885', 'Cranberry compote sweetened',
     'Sweetened cranberry compote used as a released sauce proxy; brand, texture and sugar content may differ.'),
    ('chicken gravy granules', 'cofid:17-724', 'Gravy instant granules',
     'Dry instant gravy granules used as a generic gravy proxy; chicken flavour and product sodium may differ. Never uses made-up gravy for dry weight.'),
    ('golden caster sugar', 'nevo:374', 'Sugar castor brown',
     'Golden caster sugar estimated using brown castor sugar; exact refining and moisture may differ.'),
    ('coriander powder', 'matvaretabellen:909', 'Coriander seeds',
     'Ground coriander uses the seed composition, not dried coriander leaves; grinding is not a cooking-state conversion.'),
    ('natural peanut butter', 'nevo:5275', 'Peanut butter 100% peanuts wo salt',
     'Uses the 100% peanut, no-added-salt record; natural product composition may vary.'),
    ('lean rashers', 'cofid:19-646', 'Bacon rashers, back, fat trimmed, raw',
     'Lean rashers estimated using raw fat-trimmed back bacon, not untrimmed streaky bacon.'),
    ('parsley and thyme', 'ciqual:11014', 'Parsley, fresh',
     'Small fresh herb mixture estimated using fresh parsley; the unspecified mixture ratio remains unknown.'),
    ('lemongrass', 'efsa_czech:1041', 'Spices, lemon grass',
     'Uses the as-described lemon-grass record, not dried lemon-grass powder; exact source preparation state is unspecified.'),
):
    REVIEWED_SELECTIONS[_name] = {'alias': _name, 'eu_food_id': _identifier,
        'food_name': _label, 'approximate': _reason, 'note': 'reviewed 2026-11-10'}
REVIEWED_SELECTIONS['cheese spread triangle'] = {
    **REVIEWED_SELECTIONS['cheese spread triangle'], 'low_fat': 'cofid:12-537'}


# Reviewed 2026-11-11 (held-recipe reprofile): stated forms the plain match ignored, and parser names of reviewed foods.
for _name, _identifier, _label in (
    ('canned tuna', 'catalogue:["CoFID","2021","16-416"]', 'Tuna, canned in brine, drained'),
    ('canned tomatoes', 'catalogue:["Dutch national food composition database NEVO, 2025","EFSA EU FCDB 2026","2293"]', 'Tomatoes tinned'),
    ('canned sweetcorn', 'catalogue:["CoFID","2021","13-529"]', 'Sweetcorn kernels, canned in water, drained'),
    ('canned chickpeas', 'catalogue:["CoFID","2021","13-670"]', 'Beans, chick peas, canned, re-heated, drained'),
    ('ground paprika', 'catalogue:["Slovenian Food Composition Database 2025","EFSA EU FCDB 2026","97b9e5dd-6a81-4b9f-81d7-ceca916b1617"]', 'MILD RED PEPPER Ground'),
    ('grated low-fat cheese', 'cofid:12-348', 'Cheese, Cheddar type, half fat'),
    ('cooked turkey', 'catalogue:["CoFID","2021","18-361"]', 'Turkey, meat, average, roasted'),
    ('cooked prawns', 'catalogue:["CoFID","2021","16-384"]', 'Prawns, standard, purchased cooked'),
    ('cooked potatoes', 'catalogue:["CoFID","2021","13-490"]', 'Potatoes, old, boiled in unsalted water, flesh only'),
    ('dairy-free margarine', 'catalogue:["Serbian Food Composition Database 2025","EFSA EU FCDB 2026","0005386"]', 'Vegan sunflower spread'),
    ('dried pasta', 'catalogue:["Anses. 2025.\u00a0CIQUAL\u00a0French food composition table","EFSA EU FCDB 2026","9810"]', 'Pasta, dry, regular, raw'),
    ('wholegrain cracker-bread', 'catalogue:["Dutch national food composition database NEVO, 2025","EFSA EU FCDB 2026","1779"]', 'Crispbread wholemeal'),
    ('cold, unsalted butter', 'catalogue:["Irish composition table","retained_snapshot_sha256:282f69999a05a25e343ef14b713a40d411f336d398f15af55f75f8164a1b57b0","IE00264"]', 'Butter, unsalted'),
    ('cold, low-fat buttermilk', 'catalogue:["Dutch national food composition database NEVO, 2025","EFSA EU FCDB 2026","289"]', 'Buttermilk'),
):
    REVIEWED_SELECTIONS[_name] = {'alias': _name, 'eu_food_id': _identifier, 'food_name': _label, 'note': 'reviewed 2026-11-11'}
REVIEWED_SELECTIONS['evaporated fat-free milk'] = {
    'alias': 'evaporated fat-free milk', 'eu_food_id': 'usda_sr_legacy:01097', 'food_name': 'Milk, canned, evaporated, nonfat',
    'note': 'reviewed 2026-11-11; values per 100 g from local USDA SR Legacy NUT_DATA (NDB 01097)',
    'public': {'usda_sr_legacy:01097': {'food_name': 'Milk, canned, evaporated, nonfat', 'source': 'usda_sr_legacy:01097',
        'values': {'energy_kcal': 78.0, 'protein_g': 7.55, 'fat_g': 0.2, 'saturated_fat_g': 0.121, 'carbs_g': 11.35,
                   'sugar_g': 11.35, 'fibre_g': 0.0, 'sodium_mg': 115.0}}}}
REVIEWED_SELECTIONS['pittas or flatbreads'] = {**REVIEWED_SELECTIONS['pittas'], 'alias': 'pittas or flatbreads'}
REVIEWED_SELECTIONS['low-fat cheese spread triangle'] = {**REVIEWED_SELECTIONS['cheese spread triangle'], 'alias': 'low-fat cheese spread triangle'}

# Records excluded from retrieval and completeness swaps, with the reason.
_ICELANDIC_CHICKEN = 'Raw chicken breast at 250 mg sodium/100 g; other tables give 43-76 mg (likely an injected product).'
BLOCKED_RECORDS = {
    'catalogue:["Icelandic Food Composition Database version 5.0 2025","EFSA EU FCDB 2026","223"]': _ICELANDIC_CHICKEN,
    'efsa_iceland:223': _ICELANDIC_CHICKEN,
}


def reviewed_record_compatible(name: str, original: str, source: dict) -> bool:
    """A reviewed proxy cannot discard an explicit product-defining restriction."""
    requested = name + ' ' + original
    label = source.get('label') or ''
    # Lactose-free products may contain milk; the names are not interchangeable.
    if re.search(r'\bdairy[- ]free\b', requested, re.I) and not re.search(
            r'\bdairy[- ]free\b|\bvegan\b', label, re.I):
        return False
    if re.search(r'\bfat[- ]free\b|\bskimmed\b', requested, re.I) and not re.search(
            r'\bfat[- ]free\b|\bskimmed\b|\bnonfat\b', label, re.I):
        return False
    if re.search(r'\b(?:low|reduced)[- ]fat\b', requested, re.I) and re.search(
            r'\bcheese\b|\bmilk\b|\bmargarine\b', name, re.I) and not re.search(
            r'\b(?:low|reduced|half)[- ]fat\b|\bfat[- ]free\b|\bskimmed\b|\blight\b', label, re.I):
        return False
    # Bare pulse weights are not evidence that they were weighed dry.
    if re.fullmatch(r'(?:chickpeas|kidney beans)', name.strip(), re.I) and not re.search(
            r'\b(?:dried|dry|uncooked|raw)\b', requested, re.I) and re.search(
            r'\b(?:dried|dry|uncooked|raw)\b', label, re.I):
        return False
    return True


def low_salt_name(name, original):
    """The reviewed record for stock or soy sauce stated as low or reduced salt, else None."""
    if re.search(r'\blosalt\b|\blow[- ]sodium salt\b', original or '', re.I) and re.search(r'\bsalt\b', name, re.I):
        return 'low sodium salt'  # the parser often keeps only 'salt' from 'low-sodium salt (LoSalt)'
    if not re.search(r'\b(?:low|reduced)[- ](?:salt|sodium)\b', f'{name} {original}', re.I):
        return None
    text = name.casefold()
    if 'soy' in text:
        return 'reduced salt soy sauce'
    if not re.search(r'\bstock\b|\bbouillon\b|\bbroth\b', text):
        return None
    if re.search(r'\b(?:powder|paste|granules?|concentrate)\b', text):
        return 'low salt stock powder'
    if 'cube' in text:
        return f"reduced salt {'chicken' if 'chicken' in text else 'vegetable'} stock cube"
    return 'low salt stock'


def preferred_name(name, region):
    """Reviewed names that replace a working match where the matched record is a known outlier or ignores the stated fat level."""
    key = ' '.join(name.casefold().split())
    return key if region in {'irish', 'eu'} and key in {'wholemeal flour', 'light cream cheese'} else None


_COOKED = re.compile(r'\b(?:steamed|microwav\w*|ready[- ]to[- ](?:heat|eat)|pre-?cooked|leftover|pouch(?:es)?)\b|\bcooked\b(?=.*\b(?:rice|quinoa|couscous|pasta|noodles?|spaghetti|macaroni|penne)\b)|\bcups?\b.*\bcooked\b', re.I)
_FRESH_NOODLES = re.compile(r'\b(?:shelf-|fresh\b.*\b)(?:fresh\b.*)?noodles?\b|\bnoodles?,? fresh\b', re.I)
_COOKED_FOODS = (
    (r'\brice noodles?\b|\bvermicelli\b', 'cooked rice noodles'),
    (r'\begg noodles?\b', 'cooked egg noodles'),
    (r'\bnoodles?\b', 'cooked noodles'),
    (r'\b(?:brown|wholegrain)\b.*\brice\b', 'cooked brown rice'),
    (r'\bbasmati\b', 'cooked basmati rice'),
    (r'\brice\b', 'cooked white rice'),
    (r'\bquinoa\b', 'cooked quinoa'),
    (r'\bcouscous\b', 'cooked couscous'),
    (r'\b(?:wholemeal|whole[- ]wheat|wholegrain)\b.*\b(?:pasta|spaghetti|macaroni|penne)\b', 'cooked wholemeal pasta'),
    (r'\b(?:pasta|spaghetti|macaroni|penne|shells)\b', 'cooked pasta'),
)
_COOKED_STAPLES = (
    (r'\bturkey\b', 'cooked turkey'),
    (r'\b(?:prawns?|shrimps?)\b', 'cooked prawns'),
    (r'\bpotato(?:es)?\b', 'cooked potatoes'),
)
_CANNED_FOODS = (
    (r'\btuna\b', 'canned tuna'),
    (r'\btomato(?:es)?\b(?!.*\b(?:pur[eé]e|paste|soup|ketchup|sauce)\b)', 'canned tomatoes'),
    (r'\b(?:sweet ?corn|corn kernels)\b', 'canned sweetcorn'),
    (r'\bchick ?peas?\b', 'canned chickpeas'),
)
_LEAN_FOODS = (
    (r'\bbeef\b.*\bmince\b|\bmince\b.*\bbeef\b|\bminced beef\b', 'lean beef mince'),
    (r'\blamb\b.*\bmince\b|\bminced lamb\b', 'lean lamb mince'),
    (r'\blamb\b', None),  # the lean lamb record gave no gain on HealthyFoods panels
    (r'\bpork\b', 'lean pork'),
    (r'\b(?:rump|sirloin|steaks?)\b', 'lean beef steak'),
    (r'\bbeef\b', 'lean beef'),
)
_BONE_FOODS = (
    (r'\blamb\b.*\bcutlets?\b', 'lamb cutlets with bone', 'trimmed lamb cutlets with bone'),
    (r'\blamb\b.*\bchops?\b', 'lamb chops with bone', 'trimmed lamb chops with bone'),
    (r'\blamb\b.*\bshanks?\b', 'lamb shanks with bone', None),
    (r'\bpork\b.*\b(?:chops?|cutlets?)\b', 'pork chops with bone', 'trimmed pork chops'),
    (r'\bwhole\b.*\bchicken\b', 'whole chicken with bone', None),
)


def _unspecified_food_name(text, matched):
    """Reviewed record where a generic line landed on a record the panels show is the wrong kind of food."""
    if re.search(r'\b(?:white )?fish fillets?\b|\bwhite ?fish\b', text) and not re.search(r'smoked|salmon|tuna|fingers|sauce|stock|\boil\b', text):
        return 'white fish'
    if re.search(r'\bsalmon\b', text) and not re.search(r'smoked|cann|\btin|wild|pink|\bred\b|sockeye|sauce', text) and re.search(r'wild|without skin|^salmon$|fresh', matched, re.I):
        return 'farmed salmon'
    if 'passata' in text and re.search(r'pur[eé]e', matched, re.I):
        return 'passata'
    if re.search(r'\bpaprika\b|\bred pepper\b.*\bground\b|\bground\b.*\bred pepper\b', text) and re.search(r'capsicum|sweet pepper|bell pepper', matched, re.I):
        return 'ground paprika'
    if re.search(r'\bgrated\b', text) and re.search(r'\b(?:low|reduced)[- ]fat\b', text) and 'cheese' in text and re.search(r'cream|soft|spread', matched, re.I):
        return 'grated low-fat cheese'
    if re.search(r'\b(?:tagliatelle|fettuccine|linguine|spaghetti|pasta)\b', text) and re.search(r'carbonara|prepacked|ready[- ]meal', matched, re.I):
        return 'dried pasta'
    if re.search(r'(?:low|reduced)[- ]fat (?:margarine|spread)|margarine.*low[- ]fat', text) and re.search(r'baking fat|^margarine\b', matched, re.I):
        return 'low fat spread'
    if re.search(r'\boranges?\b', text) and not re.search(r'juice|zest|peel|marmalade|rind', text) and re.search(r'^oranges?,? raw', matched, re.I):
        return 'oranges'
    return None


def stated_state_name(name, original, matched=''):
    """Reviewed record when the line states cooked, lean or bone-in food but the match ignores it."""
    text = f'{name} {original}'.casefold()
    food = name.casefold()
    unspecified = _unspecified_food_name(text, matched)
    if unspecified:
        return unspecified
    if re.search(r'\b(?:cooked|leftover|roast(?:ed)?)\b', original, re.I) and not re.search(r'\buncooked\b', original, re.I) \
            and re.search(r'\b(?:raw|fresh)\b', matched, re.I):
        cooked = next((key for pattern, key in _COOKED_STAPLES if re.search(pattern, food)), None)
        if cooked:
            return cooked
    if re.search(r'\b(?:tins?|tinned|cans?|canned)\b', original, re.I) and not re.search(r'\b(?:can|canned|tin|tinned|brine|drained|jar)', matched, re.I):
        canned = next((key for pattern, key in _CANNED_FOODS if re.search(pattern, food)), None)
        if canned:
            return canned
    if re.search(r'\b(?:uncooked|dried|dry|courgette|zucchini|vegetable)\b', text) or re.search(r'\b(?:cooked|boiled|steamed|fresh)\b', matched, re.I):
        pass
    else:
        if _COOKED.search(original) or (_FRESH_NOODLES.search(original) and 'noodle' in food):
            key = next((key for pattern, key in _COOKED_FOODS if re.search(pattern, food)), None)
            if key:
                return key
    if not re.search(r'\b(?:bone(?:d|less)|skinless|skin removed|fillets?|meat\b)', text):
        trimmed = re.search(r'\b(?:lean|trimmed)\b|\bfat removed\b', original, re.I)
        for pattern, key, trimmed_key in _BONE_FOODS:
            if re.search(pattern, food):
                return trimmed_key if trimmed and trimmed_key else key
        if re.search(r'\bbone[- ]in\b|\bon the bone\b', original, re.I) and 'chicken' in food:
            return 'chicken pieces with bone'
        if re.search(r'\bbone[- ]in\b', original, re.I) and 'pork' in food:
            return 'pork chops with bone'
    if re.search(r'\b(?:sausages?|meatballs?|schnitzels?|burgers?|patt(?:y|ies))\b', food):
        return None
    if re.search(r'\blean\b', original, re.I) and not re.search(r'\d\s*%|\blean\b', matched, re.I):
        if re.search(r'\d\s*%', original):
            return None
        return next((key for pattern, key in _LEAN_FOODS if re.search(pattern, food)), None)
    return None


def reviewed_public_record(candidate_id):
    """The reviewed public-source row (outside the release) a selection points to, else None."""
    for row in REVIEWED_SELECTIONS.values():
        if candidate_id in (row.get('public') or {}):
            return row['public'][candidate_id]
    return None
