"""Sustainability factors: reviewed ingredient name -> AGRIBALYSE 3.2 food_key rules (first match wins), kg CO2e/kg, no-preparation scenario (3 Nov release policy).
Note: AGRIBALYSE gives cod, haddock, pollock, whiting and plaice one shared proxy value (12.0), so fish-for-fish differences are indicative.
Fallback only for recipe totals: CONCITO for a few foods AGRIBALYSE lacks, then the role average (estimate). Never a swap source or target."""
import json, re
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'
KEYS = DATA / 'agribalyse_keys.json'
_REFS = dict(json.loads(KEYS.read_text()))
RULES = [  # (regex on lowercased name, food_key, note)
    (r'^(sea |table |rock |kosher )?salt\b|^salt,', 'pure sea salt no enrichment', ''),
    (r'black pepper|^pepper, black|white pepper|ground pepper|^pepper$', 'black pepper powder', ''),
    (r'tap water|boiling water|^water\b|water, municipal|cold water|hot water', 'tap water', ''),
    (r'extra virgin olive oil|olive oil|oil, olive', 'olive oil extra virgin', ''),
    (r'sunflower oil|oil, sunflower|high oleic', 'sunflower oil', ''),
    (r'rapeseed oil|oil, rapeseed|canola', 'rapeseed oil', ''),
    (r'vegetable oil|unsaturated oil|cooking oil|^oil$|^oil,', 'combined oil blended vegetable oils', 'approx'),
    (r'sesame oil', 'sesame oil', ''),
    (r'coconut oil', 'virgin coconut oil', ''),
    (r'low[- ]fat spread|light spread|margarine|baking fat|(?<!cheese )spread', 'vegetable fat margarine type 80 fat unsalted', 'approx'),
    (r'(?<!peanut )(?<!nut )butter(?!.*(bean|nut|milk))|butter,', 'butter 82 fat unsalted', ''),
    (r'lard|dripping', 'lard or pork fat', ''),
    (r'egg yolk', 'egg yolk raw', ''), (r'egg white', 'egg white raw', ''),
    (r'\beggs?\b|chicken egg', 'egg raw', ''),
    (r'parsley', 'parsley fresh', ''), (r'(?<!ground )coriander(?!.*(ground|seed|powder))', 'coriander fresh', ''), (r'basil', 'basil fresh', ''),
    (r'thyme', 'thyme fresh', ''), (r'rosemary', 'rosemary fresh', ''), (r'\bdill', 'dill fresh', ''), (r'\bmint', 'mint fresh', ''),
    (r'tarragon', 'tarragon fresh', ''), (r'marjoram', 'marjoram dried', ''), (r'oregano|mixed herbs|bay lea|\bbay\b|\bsage\b', 'provence herbs dried', 'approx'),
    (r'chive|spring onion|scallion', 'chive or spring onion fresh', ''),
    (r'leek(?!.*soup)', 'leek raw', ''),
    (r'red onion|onion|shallot', 'onion raw', ''),
    (r'garlic', 'garlic', ''), (r'ginger', 'ginger raw', ''),
    (r'sweet potato', 'sweet potato raw', ''),
    (r'potato', 'potato peeled raw', ''),
    (r'cherry tomato', 'tomato cherry raw', ''),
    (r'tomato (puree|paste)|tomato purée', 'tomato paste concentrated canned', ''),
    (r'tomato sauce|passata', 'tomato sauce for pizza base', 'approx'),
    (r'tomato|chopped tomatoes', 'tomato raw', ''),
    (r'courgette|zucchini', 'courgette or zucchini pulp and peel raw', ''),
    (r'aubergine|eggplant', 'eggplant raw', ''),
    (r'cucumber', 'cucumber pulp raw', ''), (r'celery', 'celery stalk raw', ''), (r'carrot', 'carrot raw', ''),
    (r'broccoli', 'broccoli raw', ''), (r'cauliflower', 'cauliflower raw', ''), (r'spinach', 'spinach raw', ''),
    (r'kale', 'curly kale raw', ''), (r'lettuce|salad leaves|rocket', 'lettuce raw', ''), (r'cabbage|sauerkraut', 'white cabbage raw', 'approx'),
    (r'beetroot', 'beetroot raw', ''), (r'parsnip', 'parsnip raw', ''), (r'turnip|swede', 'turnip peeled raw', ''),
    (r'pumpkin(?! seed)|squash', 'pumpkin raw', ''), (r'radish', 'radish raw', ''), (r'asparagus', 'asparagus green raw', ''),
    (r'brussels? sprout', 'white cabbage raw', ''),
    (r'(red|green|yellow|orange|bell|sweet)? ?pepper(s)?(, capsicum.*)?$|capsicum|bell pepper|red pepper|green pepper|yellow pepper', 'sweet pepper green yellow or red raw', ''),
    (r'chill?i(?!.*(powder|sauce))', 'chili pepper raw', ''),
    (r'chill?i powder|cayenne|paprika|smoked paprika|mild red pepper', 'paprika', ''),
    (r'cumin', 'cumin seed', ''), (r'cinnamon', 'cinnamon powder', ''), (r'turmeric', 'turmeric powder', ''),
    (r'curry powder|garam masala', 'curry powder', ''), (r'nutmeg', 'nutmeg', ''), (r'ground coriander|coriander seeds?|coriander powder', 'coriander seed', ''),
    (r'sweetcorn|sweet corn', 'sweet corn frozen raw', ''),
    (r'green beans?|french beans?', 'french bean frozen raw', ''), (r'kidney bean', 'red kidney bean dried', ''),
    (r'chick ?peas?|beans, chick', 'chick pea canned drained', ''), (r'lentil', 'lentil dried', ''),
    (r'\bpeas\b|frozen peas', 'garden peas frozen raw', ''), (r'\bbeans?\b|haricot|cannellini|butter bean', 'haricot bean dry', 'approx'),
    (r'lemon juice', 'lemon juice pure juice', ''), (r'lemon (peel|zest)|lime (peel|zest)', 'lemon zest raw', ''),
    (r'lime juice', 'lemon juice pure juice', 'approx'), (r'lemon', 'lemon pulp raw', ''), (r'lime', 'lime pulp raw', ''),
    (r'orange juice', 'orange juice home made', ''), (r'orange', 'orange pulp raw', ''),
    (r'apple', 'apple pulp and peel raw', ''), (r'banana', 'banana pulp raw', ''), (r'strawberr', 'strawberry raw', ''),
    (r'raspberr', 'raspberry raw', ''), (r'blueberr', 'blueberry raw', ''), (r'cherr', 'cherry pitted raw', ''), (r'pear', 'pear pulp and peel raw', ''),
    (r'raisin|sultana', 'raisin', ''), (r'dried apricot|apricot', 'apricot pitted dried', ''), (r'avocado', 'avocado pulp raw', ''),
    (r'walnut', 'walnut dried husked', ''), (r'almond', 'almond with peel', ''), (r'cashew', 'cashew nut grilled salted', 'approx'),
    (r'peanut butter', 'peanut butter or peanut paste', ''), (r'tahini', 'tahini sesame paste', ''), (r'pine nut', 'pine nuts', ''),
    (r'pumpkin seed|sunflower seed', 'sunflower seed', 'approx'), (r'sesame', 'sesame seed', ''),
    (r'honey', 'honey', ''), (r'maple syrup', 'syrup maple', ''), (r'brown sugar|light brown|dark brown|muscovado|demerara', 'sugar brown', ''),
    (r'sugar(?![- ]snap)|caster|castor|icing|powdered', 'sugar white', ''), (r'jam|marmalade', 'jam strawberry', 'approx'),
    (r'cocoa', 'cocoa powder without sugar powder instant non rehydrated', ''), (r'chocolate', 'milk chocolate bar', 'approx'),
    (r'baking powder|bicarbonate|raising agent|bread soda|baking soda', 'baking powder or raising agent', ''), (r'gelatin', 'gelatine dried', ''),
    (r'cornflour|corn flour|cornstarch|corn starch', 'maize corn starch', ''),
    (r'self[- ]raising', 'wheat flour self raising', ''), (r'wholemeal flour|whole ?wheat flour|wheat flour, wholemeal', 'wheat flour type 150', ''),
    (r'flour|t500|t55', 'wheat flour type 55 for pastry', ''),
    (r'porridge oats|rolled oats|oats|oat flakes|oatmeal', 'oat raw', ''),
    (r'breadcrumb|bread crumb', 'bread french bread baguette', 'approx'),
    (r'wholemeal bread|wholegrain bread|brown bread|bread, wholemeal|bread, brown|multigrain', 'bran grain bread', 'approx'),
    (r'tortilla|wrap', 'wheat tortilla wrap to be filled', ''), (r'pitta|pita', 'pita bread', ''),
    (r'bread(?! soda)|baguette|ciabatta|toast|bread roll|burger bun', 'bread french bread baguette', 'approx'),
    (r'puff pastry|filo|pastry', 'biscuit puff pastry', 'approx'),
    (r'penne|spaghetti|pasta|macaroni|noodle|lasagne|tagliatelle|fusilli', 'dried pasta raw', ''),
    (r'basmati', 'basmati rice raw', ''), (r'\brice\b(?! vinegar| wine| paper)', 'rice raw', ''), (r'couscous', 'couscous durum wheat semolina pre cooked with steam raw', ''),
    (r'quinoa', 'quinoa raw', ''), (r'bulgur', 'wheat bulgur raw', ''), (r'barley', 'barley whole raw', ''), (r'semolina', 'durum wheat semolina raw', ''),
    (r'semi[- ]skimmed milk|low[- ]fat milk|1[.,]5', 'milk semi skimmed uht', ''), (r'skimmed milk', 'milk skimmed uht', ''),
    (r'coconut milk', 'coconut milk or coconut cream', ''), (r'\bmilk\b|milk,', 'milk whole uht', ''),
    (r'single cream|light cream|cream, single|half[- ]fat (cream|cr[eè]me)|(cream|cr[eè]me)\b.*\bhalf[- ]fat|reduced[- ]fat cream|light sour cream|(low|reduced)[- ]fat cr[eè]me|cr[eè]me fra[iî]che,? (half|low|light|reduced)', 'liquid cream light 15 20 fat uht', ''),
    (r'sour cream|soured cream|cream, sour|cream sour|tejföl', 'liquid cream light 15 20 fat uht', 'approx: soured cream about 20% fat'),
    (r'creme fraiche|crème fraîche|cream', 'liquid cream 30 fat uht', ''),
    (r'greek yog|yoghurt|yogurt', 'yogurt fermented milk or dairy specialty plain', ''),
    (r'cottage|quark|cream cheese|soft cheese|curd', 'fresh cream cheese plain around 3 fat', 'approx'),
    (r'ricotta|queso fresco|fresh cheese|cheese, fresh', 'fresh cream cheese plain creamy around 8 fat', 'approx: fresh cheese'), (r'mozzarella', 'mozzarella cheese from cow s milk', ''), (r'feta', 'feta cheese from ewe s milk', ''), (r'parmesan|grana', 'parmesan cheese from cow s milk', ''),
    (r'cheddar|low-fat cheddar', 'cheddar cheese from cow s milk', ''), (r'emmental|gruy', 'emmental cheese from cow s milk', ''),
    (r'gouda|edam', 'gouda cheese from cow s milk', ''), (r'camembert|brie', 'camembert cheese from cow s milk', ''),
    (r'goats? cheese|cheese,? goat', 'cheese from goat s milk', ''), (r'blue cheese|stilton|gorgonzola', 'blue cheese from cow s milk', ''), (r'cheese', 'cheddar cheese from cow s milk', 'approx: generic cheese'),
    (r"(pig|pork)'?s? liver|liver,? (pig|pork)", 'liver pork raw', ''), (r'chicken liver|liver,? chicken', 'liver chicken raw', ''),
    (r'lambs?\'?s? liver|liver,? lamb', 'liver lamb raw', ''), (r'(calf|calves|veal)\'?s? liver|liver,? (calf|veal)', 'liver calf raw', ''),
    (r'salami', 'salami', ''), (r'anchov(y|ies)', 'common anchovy raw', ''),
    (r'minced beef|beef mince|ground beef', 'beef minced steak 15 fat raw', ''), (r'(?<!tomato )beef(?! tomato)', 'beef round raw', 'approx'), (r'beef tomato|tomato,? beef', 'tomato raw', ''),
    (r'lamb|mutton', 'lamb leg raw', ''), (r'veal', 'veal fillet raw', ''),
    (r'bacon|rasher|lardon', 'bacon back', ''), (r'ham', 'cooked ham choice', ''), (r'sausage', 'sausage meat pure pork raw', ''),
    (r'pork', 'pork loin raw', ''), (r'duck', 'duck meat raw', ''), (r'goose', 'goose meat raw', ''), (r'turkey', 'turkey meat raw', ''),
    (r'chicken breast', 'chicken breast without skin raw', ''), (r'chicken|poultry', 'chicken meat raw', ''),
    (r'salmon', 'salmon raw farmed', ''), (r'tuna', 'tuna raw', ''), (r'\bcod\b', 'cod raw', ''), (r'hake', 'european hake raw', ''), (r'saithe|coley', 'saithe raw', ''), (r'haddock', 'haddock raw', ''),
    (r'pollock|pollack', 'alaska pollock raw', ''), (r'whiting', 'whiting raw', ''), (r'plaice', 'european plaice raw', ''), (r'mackerel', 'mackerel raw', ''),
    (r'herring', 'atlantic herring raw', ''), (r'sardine|pilchard', 'european pilchard or sardine raw', ''), (r'mussel', 'mussel common raw', ''), (r'squid|calamari', 'squid raw', ''), (r'trout', 'trout farmed raw', ''),
    (r'prawn|shrimp', 'shrimp or prawn raw', ''),
    (r'stock cubes?|bouillon (cube|powder|granule)|gravy granules|stock powder|stock pot', 'aromatic stock cube for fish dehydrated', 'approx: stock cube'),
    (r'stock|broth|bouillon', 'tap water', 'approx: liquid stock counted as water (understates, so it cannot create a false gain)'),
    (r'soy sauce', 'soy sauce prepacked', ''), (r'mustard', 'mustard', ''), (r'ketchup', 'ketchup', ''), (r'mayonnaise|mayo', 'mayonnaise 70 fat and more', ''),
    (r'balsamic', 'vinegar balsamic', ''), (r'vinegar', 'vinegar', ''), (r'wine(?! vinegar)', 'sparkling fruit wine', 'approx: no plain wine in the catalogue'),
    (r'hummus', 'hummus', ''), (r'tofu', 'soybean whole grain', 'approx: no tofu record'),
    # 17 Nov gap rules (foods the Living Lab recipes use that had no factor)
    (r'blackberr', 'blackberry raw', ''), (r'cranberr', 'cranberry raw', ''), (r'nectarine', 'nectarine pulp and peel raw', ''),
    (r'bagel', 'bagel', ''), (r'crispbread', 'crispbread extruded and grilled', ''), (r'breadsticks?|grissini', 'grissini or bread stick', ''),
    (r'crackers?', 'salty snacks crackers plain', ''), (r'dandelion', 'dandelion raw', ''), (r'chicory', 'chicory raw', ''),
    (r'horseradish', 'horseradish fresh', ''), (r'cuttle ?fish', 'squid raw', 'approx: cuttlefish as squid'), (r'rabbit', 'rabbit meat raw', ''),
    (r'brownshrimp', 'shrimp or prawn raw', 'approx: brown shrimp'), (r'catfish|pangasius', 'trout farmed raw', 'approx: farmed freshwater fish'),
    (r'cloves?', 'cloves', ''), (r'agave', 'syrup agave', ''), (r'hamburger buns?|burger buns?|buns', 'rolls for hamburger hotdog buns prepacked', ''),
    (r'frankfurters?|hot ?dogs?', 'sausage meat pure pork raw', 'approx: frankfurter as pork sausage'),
    (r'muesli', 'muesli crunchy with fruits and or dried fruits grains not fortified with vitamins and chemical elements', 'approx'),
    (r'mixed nuts?', 'almond with peel', 'approx: mixed nuts as almond'), (r'poppy ?seeds?', 'sesame seed', 'approx'),
    (r'buttermilk', 'milk skimmed uht', 'approx: buttermilk as skimmed milk'), (r'^semi[- ]skimmed$', 'milk semi skimmed uht', ''),
    (r'boiled water|ice[- ]cubes?|^ice$', 'tap water', ''),
    (r'kiwi', 'kiwi fruit pulp and seeds raw', ''), (r'peach(?!.*dried)', 'peach pulp and peel raw', ''), (r'grapes?(?!fruit|seed)', 'grape raw', ''),
    (r'pineapple', 'pineapple pulp raw', ''), (r'chard', 'swiss chard raw', ''), (r'buckwheat flour', 'buckwheat flour', ''), (r'buckwheat', 'buckwheat whole raw', ''),
    (r'sugar[- ]snap|mange ?tout|snow peas?', 'garden peas frozen raw', 'approx: pods as garden peas'),
]
# Priority by what the matched record is: stock first (beef stock is not beef), then animal products, then everything else.
def _priority(rule):
    from recipe_wrangler.services.adaptation.food_roles import role_of
    role = role_of(rule[1])
    return 0 if role == 'stock' or 'stock' in rule[0] else 1 if role in ('red_meat', 'poultry', 'fish') else 2


RULES.sort(key=_priority)  # stable: the original order is kept inside each class
# whole-word matching (plural 's' allowed): 'bread' must not match 'breaded', 'ham' must not match 'hamburger'
COMPILED = [(re.compile(r'(?<![a-z])(?:' + rx + r')(?:e?s|ies)?(?![a-z])'), key, note) for rx, key, note in RULES]


def map_name(name):
    """Liquid-stock rules first (beef stock is not beef); generic catch-alls last; then the first rule (list order, specific first)
    whose food type matches the name's own ("mozzarella, from cow's milk" is cheese, not milk); else the first rule that matches."""
    n = name.lower().strip()
    from recipe_wrangler.services.adaptation.food_roles import role_of
    n = re.sub(r'\b(in|with|fried in|roasted in|cooked in)\s+(\w+\s+)?oil\b|\b(without|no|no added)\s+(added\s+)?sugar\b', ' ', n)  # cooking medium, not the food
    oil = re.search(r'\b([a-z]+) oil\b', n)  # '<plant> oil' is that oil, whatever the plant word would otherwise map to (avocado oil is not avocado)
    if oil and (oil.group(1) + ' oil') in _REFS:
        return oil.group(1) + ' oil', ''
    if re.search(r'\b(turkey|chicken|poultry) (ham|salami|bacon|sausages?)\b|\b(ham|salami|bacon|sausages?),? (turkey|chicken|poultry)\b|poultry sausage', n):
        return 'turkey cooked ham in slices', 'approx: processed poultry product as AGRIBALYSE turkey ham'
    if re.search(r'\by(o|og)h?urt\b', n):  # food_roles reads "yoghurt, skimmed milk" as milk; yogurt names take the yogurt rule
        return 'yogurt fermented milk or dairy specialty plain', ''
    if re.search(r'ketchup', n):
        return 'ketchup', ''
    hits = [(i, key, note) for i, (rx, key, note) in enumerate(COMPILED) if rx.search(n)]
    if not hits:
        return None, None
    stock = [h for h in hits if 'stock' in RULES[h[0]][0] or _key_role(h[1]) == 'stock']
    if stock:
        return stock[0][1], stock[0][2]
    own = 'herb_spice' if SPICE_PEPPER.search(n) else role_of(n)  # food_roles reads "black pepper" as a vegetable
    specific = [h for h in hits if 'generic' not in (h[2] or '') and _key_role(h[1]) == own]
    hits = [h for h in hits if 'generic' not in (h[2] or '')] if specific else hits  # catch-all ('cheese') only without a specific rule of the same type
    best = next((h for h in hits if own and _key_role(h[1]) == own), hits[0])  # within a type, list order: specific rules come first
    return best[1], best[2]


SPICE_PEPPER = re.compile(r'\b(black|white|ground|cayenne|chill?i|mild red|pink) pepper|pepper,? (black|white|ground)|pepper\b.*\bground\b')
_KEY_ROLES = {}


def _key_role(key):
    if key not in _KEY_ROLES:
        from recipe_wrangler.services.adaptation.food_roles import role_of
        _KEY_ROLES[key] = role_of(key)
    return _KEY_ROLES[key]


# Generalised fallback for names no rule covers: the average factor of the catalogue's plain records with the same culinary role
# (fruit -> fruit average, vegetable -> vegetable average ...), clearly marked as an estimate.
_ROLE_AVG = None


def role_average(name):
    global _ROLE_AVG
    from recipe_wrangler.services.adaptation.food_roles import role_of
    if _ROLE_AVG is None:
        acc = {}
        for key, cf in json.loads(KEYS.read_text()):
            role = role_of(key)
            if role and role not in ('ready_dish', 'baby_food', 'stock') and (role == 'beverage' or key.endswith(' raw') or ' raw ' in key):
                acc.setdefault(role, []).append(cf)
        _ROLE_AVG = {r: sum(v) / len(v) for r, v in acc.items() if len(v) >= 3}
    role = role_of(name)
    return (role, _ROLE_AVG[role]) if role in _ROLE_AVG else (None, None)


# Fallback source: used only when AGRIBALYSE has no record, only to complete the recipe total of small ingredients.
# Never the swapped food or a swap target (different method, see evaluation/sustainability/README.md). Market: GB for Irish, FR otherwise.
CONCITO_CSV = DATA / 'concito-v1.2-public-table.csv'
CONCITO_RULES = [  # (regex on lowercased name, CONCITO product)
    (r'mushroom|champignon|chanterelle|porcini|cep\b', 'Mushroom, raw'), (r'\bwine\b|\bport\b|sherry|marsala|vermouth', 'Wine, red'),
    (r'olives?', 'Olives, green, pickled, canned'), (r'pesto', 'Pesto'), (r'pickle|gherkin', 'Gherkin, pickled'),
    (r'^wheat$|wheat grain|wheat kernels', 'Wheat, kernels, whole/cracked'),
]
_CONCITO = None


def concito_factor(name, region):
    global _CONCITO
    if _CONCITO is None:
        import csv
        _CONCITO = {}
        for x in csv.DictReader(open(CONCITO_CSV, encoding='utf-8')):
            _CONCITO[(x['product'], x['market'])] = float(x['total_kg_co2e_per_kg'])
    n = name.lower()
    for rx, product in CONCITO_RULES:
        if re.search(rx, n):
            return _CONCITO.get((product, 'GB' if str(region).lower() in ('irish', 'ie') else 'FR'))
    return None


COMPOSITE = re.compile(r'\b(dough|cakes?|soups?|pies?|stews?|casseroles?|filling|pizza|sandwich\w*|marinade)\b', re.I)


def factor_for(name, weight_g=0.0, region=None):
    """(kg CO2e/kg, source, record) for an ingredient name, or (None, None, None). Source is 'agribalyse', 'concito' or 'estimate'."""
    n = (name or '').strip()
    if not n:
        return None, None, None
    if n.lower() in ('pepper', 'peppers'):  # a bare "pepper": over 10 g it is a bell pepper, otherwise ground black pepper
        key = 'sweet pepper green yellow or red raw' if weight_g > 10 else 'black pepper powder'
        return _REFS[key], 'agribalyse', key
    if COMPOSITE.search(n):
        return None, None, None  # a dough, cake or soup is not its main ingredient
    key = map_name(n)[0]
    if key in _REFS:
        return _REFS[key], 'agribalyse', key
    cf = concito_factor(n, region)
    if cf is not None:
        return cf, 'concito', n
    role, cf = role_average(n)
    return (cf, 'estimate', role) if cf is not None else (None, None, None)
