"""Culinary role of a food, shared by every adaptation facet (nutrition, sustainability, cost, time).

A substitution proposed by the graph, FoodOn or a database search is only sensible when the new food can do the same job in the
dish: oil for oil, cheese for cheese, one grain for another. `role_problems(source, target)` returns the reasons a swap is not
culinarily sensible; an empty list means the roles are compatible. Pure functions, no I/O."""
from __future__ import annotations

import re

# Order matters: the first matching role wins. Patterns run on the lower-cased food label.
ROLES: list[tuple[str, str]] = [
    ('ready_dish', r'\b(tapas|sandwich|pizza|burger|soup|salad|casserole|ready meal|pasty|pie|tart|cake|biscuit|cookie|pudding|dessert|ice cream|sorbet|'
                   r'quenelle|pâté|pate|terrine|croissant|brioche|muffin|crisps|snack|popcorn|stew|pancake|goulash|porridge|nugget|patty|patties|croquette|fritter|falafel|lasagn|dish|meal|marinated|stuffed|filled|in a crust|kebab)\b'),
    ('beverage', r'\b(infusion|tea|coffee|juice|nectar|drink|beverage|lemonade|cola|beer|wine|cocoa milk|milkshake|(?<!maple )(?<!golden )(?<!agave )syrup(?! (maple|golden|agave))|smoothie)\b'),
    ('baby_food', r'\b(baby (food|milk|formula|cereal|rice|porridge|meal)|infant|follow-on|formula|toddler|condensed|evaporated|powdered milk|milk powder)\b'),
    ('stock', r'\b(stock|broth|bouillon)\b'),
    ('oil', r'\boil\b|\bshortening\b|\bghee\b|\bdripping\b|\blard\b|\btallow\b'),
    ('spread_fat', r'\b(butter|margarine|spread)\b(?!\s*bean)|baking fat'),
    ('cream', r'\bcream\b(?! cheese)|cr[eè]me fra[iî]che|sour cream|mascarpone|tejf[oö]l'),
    ('yogurt', r'\by(o|og)h?urt\b|\bkefir\b|\bskyr\b|\bquark\b|fromage frais'),
    ('milk', r'\bmilk\b(?!\s*(chocolate|powder|bread|roll|loaf))'),
    ('cheese', r'\bcheese\b|\b(cheddar|mozzarella|feta|parmesan|gouda|edam|emmental|gruy[eè]re|halloumi|ricotta|brie|camembert|cottage)\b'),
    ('egg', r'\begg\b|\beggs\b|\byolk\b'),
    ('red_meat', r'\b(beef|veal|lamb(?!s? lettuce)|mutton|pork|venison|goat|steak|mince|minced|bacon|ham|sausage|salami|chorizo|pancetta|prosciutto|liver|offal)\b'),
    ('poultry', r'\b(chicken|turkey|duck|goose|poultry)\b'),
    ('shellfish', r'\b(barnacle|prawn|shrimp|crab|lobster|mussel|oyster|clam|scallop|squid|calamari|octopus)\b'),
    ('fish', r'\b(fish|salmon|tuna|cod|hake|trout|haddock|mackerel|sardine|anchov|herring|seabream|bass|plaice|sole|prawn|shrimp|crab|mussel|oyster|squid|calamari)\b'),
    ('legume', r'\b(lentil|chick ?pea|beans?|peas?|soy|soya|tofu|tempeh|lupin)\b'),
    ('rice_grain', r'\b(rice|quinoa|couscous|bulgur|barley|millet|polenta|semolina|oat|oats|spelt|buckwheat|farro|freekeh)\b'),
    ('pasta', r'\b(pasta|spaghetti|noodle|macaroni|penne|lasagne|tagliatelle|fusilli|gnocchi)\b'),
    ('bread', r'\b(bread|baguette|ciabatta|roll|bun|toast|wrap|tortilla|pitta|pita|naan|bagel|cracker|crispbread|rusk)\b'),
    ('flour', r'\bflour\b|\bstarch\b'),
    ('sweetener', r'\b(sugar|honey|syrup|molasses|treacle|sweetener|agave|jam|marmalade)\b'),
    ('nut_seed', r'\b(nut|nuts|almond|walnut|hazelnut|cashew|pecan|pistachio|peanut|seed|seeds|sesame|tahini)\b'),
    ('fruit', r'\b(apple|pear|banana|berry|berries|strawberr|raspberr|blueberr|cherry|cherries|grape|orange|lemon|lime|mango|pineapple|peach|plum|apricot|kiwi|melon|fig|date|raisin|prune|fruit)\b'),
    ('allium', r'\b(onion|shallot|leek|garlic|chive|scallion)\b'),
    ('herb_spice', r'\b(parsley|basil|coriander|thyme|rosemary|dill|mint|oregano|sage|pepper powder|paprika|cumin|cinnamon|turmeric|curry|nutmeg|ginger|spice|herb|salt|vinegar|mustard)\b'),
    ('potato_root', r'\b(potato|potatoes|carrot|parsnip|beetroot|turnip|swede|celeriac|radish|yam|cassava)\b'),
    ('vegetable', r'\b(tomato|cucumber|courgette|zucchini|aubergine|eggplant|pepper|capsicum|pumpkin|squash|spinach|kale|lettuce|rocket|cabbage|broccoli|cauliflower|'
                  r'greens|celery|asparagus|mushroom|champignon|sprout|chard|fennel|artichoke|vegetable|salad)\b'),
]
COMPILED = [(role, re.compile(rx.replace(')\\b', ')(?:e?s|ies)?\\b'))) for role, rx in ROLES]

# Roles that may stand in for each other (symmetric). Everything else must keep its role.
COMPATIBLE = {frozenset(p) for p in [
    ('oil', 'spread_fat'), ('cream', 'yogurt'), ('cream', 'milk'), ('milk', 'yogurt'),
    ('red_meat', 'poultry'), ('red_meat', 'fish'), ('shellfish', 'fish'),
    ('bread', 'rice_grain'),
     ('cheese', 'yogurt'),
    ('flour', 'rice_grain'), ('flour', 'bread'),
]}
PRODUCE = {'vegetable', 'allium', 'fruit', 'herb_spice', 'potato_root', 'oil', 'egg', 'pasta', 'bread', 'flour', 'legume', 'stock', 'nut_seed'}  # swapping one produce item for another is not a nutrition or climate fix
NEVER_TARGET = {'ready_dish', 'beverage', 'baby_food'}
NOISE = {'raw', 'fresh', 'cooked', 'boiled', 'dried', 'frozen', 'average', 'standard', 'uncooked', 'peeled', 'whole', 'flesh', 'skin', 'and',
         'without', 'with', 'the', 'of', 'a', 'in', 'from', 'type', 'food', 'pulp', 'edible', 'part'}


SPECIAL = ('ready_dish', 'beverage', 'baby_food', 'stock')
# The head noun decides when an animal or plant word only describes the source of the food: 'chicken egg' is an egg, "goat's milk" is milk.
HEAD_NOUNS = [('shellfish', re.compile(r'\bbarnacles?\b')), ('egg', re.compile(r'\beggs?\b')), ('cheese', re.compile(r'\bcheese\b')), ('milk', re.compile(r"\bmilk\b(?! (bread|roll|loaf))")), ('nut_seed', re.compile(r'\b(peanut|nut|almond|cashew|seed|sunflower seed) butter\b')),
              ('legume', re.compile(r'\b(butter|broad|kidney|green|string|french) beans?\b')), ('oil', re.compile(r'\boil\b'))]


def role_of(label: str | None) -> str | None:
    """Prepared products, drinks, baby food and stocks are recognised anywhere in the label; otherwise the FIRST food word decides
    ('Tomato beef raw' is a tomato, 'Chicken, minced meat' is poultry)."""
    text = (label or '').casefold()
    for role, rx in COMPILED:
        if role in SPECIAL and rx.search(text):
            return role
    for role, rx in HEAD_NOUNS:
        if rx.search(text):
            return role
    rest = [(role, rx) for role, rx in COMPILED if role not in SPECIAL]
    for m in re.finditer(r'[a-zà-ž]+', text):
        for role, rx in rest:
            if rx.match(text, m.start()):
                return role
    for role, rx in rest:  # multi-word patterns that do not start at a word boundary
        if rx.search(text):
            return role
    return None


def content_words(label: str | None) -> frozenset[str]:
    words = {re.sub(r's$', '', w) for w in re.findall(r'[a-zà-ž]+', (label or '').casefold())}
    return frozenset(w for w in words if w not in NOISE and len(w) > 2)


_TOKEN_ROLES = {}
_GROUP = {'red_meat': 'animal', 'poultry': 'animal', 'fish': 'animal', 'milk': 'dairy', 'cream': 'dairy', 'yogurt': 'dairy', 'cheese': 'dairy',
          'potato_root': 'produce', 'vegetable': 'produce', 'allium': 'produce', 'fruit': 'produce', 'oil': 'fat', 'spread_fat': 'fat'}
_NOT_FOOD_TOKEN = {'minced', 'mince', 'raw', 'fat', 'light', 'low', 'lean', 'plain', 'whole', 'dark', 'meat', 'flesh', 'skin'}


def is_compound(label: str | None) -> bool:
    """A record that names two or more different kinds of food (carrot + oil, milk + beetroot, bread + carrot) is a prepared dish, not a food."""
    groups = set()
    for token in re.findall(r'[a-zà-ž]+', (label or '').casefold()):
        if token in _NOT_FOOD_TOKEN or len(token) < 3:
            continue
        if token not in _TOKEN_ROLES:
            _TOKEN_ROLES[token] = role_of(token)
        role = _TOKEN_ROLES[token]
        if role and role not in ('ready_dish', 'beverage', 'baby_food', 'stock'):
            groups.add(_GROUP.get(role, role))
    return len(groups) >= 2


# Nutrition swaps stay inside a family; the nutrition gates then decide whether the swap is worth it. First match wins.
FAMILIES = [(f, re.compile(rx)) for f, rx in [
    ('sprouts', r'(lentil|bean|alfalfa|pea|mung) sprouts?'),
    ('tomato', r'tomato'),
    ('summer_squash', r'cucumber|courgette|zucchini'),
    ('winter_squash', r'pumpkin|squash|marrow'),
    ('nightshade', r'pepper|capsicum|aubergine|eggplant'),
    ('leafy', r'spinach|kale|rocket|arugula|lettuce|chard|cress|greens|pak choi'),
    ('brassica', r'broccoli|cauliflower|romanesco|cabbage|sprout|kohlrabi'),
    ('garlic', r'garlic'),
    ('leek', r'leek'),
    ('onion', r'onion|shallot|scallion|chive'),
    ('potato', r'potato|yam|cassava'),
    ('beet', r'beetroot'),
    ('carrot_parsnip', r'carrot|parsnip'),
    ('turnip_radish', r'turnip|swede|celeriac|radish'),
    ('lentil', r'lentil'),
    ('chickpea', r'chick ?pea'),
    ('green_bean', r'(runner|green|string|french|flat|broad) beans?|beans?,? (runner|green|string|french|flat|broad)'),
    ('bean', r'bean|lupin'),
    ('pea', r'\bpeas?\b'),
    ('soy', r'tofu|tempeh|soy'),
    ('grain', r'rice|quinoa|couscous|bulgur|barley|millet|polenta|semolina|oat|spelt|buckwheat|farro|freekeh'),
    ('pasta', r'pasta|spaghetti|noodle|macaroni|penne|tagliatelle|fusilli'),
    ('sweetener', r'sugar|honey|syrup|molasses|treacle|agave'),
    ('citrus', r'lemon|lime|orange|grapefruit|mandarin|clementine'),
    ('berry', r'berry|berries|currant|cranberr'),
    ('tree_fruit', r'apple|pear|peach|plum|apricot|cherry|cherries|nectarine'),
    ('mushroom', r'mushroom|champignon'),
    ('nut_seed', r'nut|almond|walnut|hazelnut|cashew|pecan|pistachio|peanut|seed|sesame|tahini'),
    ('oil', r'\boil\b|shortening|ghee|dripping|lard|tallow'),
]]
VARIETY = {'red', 'yellow', 'white', 'green', 'brown', 'black', 'purple', 'golden', 'sweet', 'imported', 'norwegian', 'young', 'baby', 'small', 'large'}
ARTIFICIAL_SWEETENER = re.compile(r'sweeten|aspart|stevia|saccharin|sucralose|cyclamate', re.I)
OFFAL_SOURCE = re.compile(r'\b(liver|kidney|kidneys|heart|tripe|offal)\b', re.I)
WITH_MARKER = re.compile(r'(?:\bw/|\bw\b|\bwith\b)', re.I)
GAME = re.compile(r'\b(venison|roebuck|deer|wild boar|elk|moose|reindeer|pheasant|partridge|hare)\b', re.I)
PROCESSED_MEAT = re.compile(r'\b(bacon|ham|sausages?|salami|chorizo|pepperoni|frankfurters?|hot ?dogs?|cured|smoked meat|prosciutto|pancetta)\b', re.I)
VAGUE_TARGET = re.compile(r'\b(mixed|various|assorted)\b|\b(oil|fat|margarine|spread)\b.*\baverage\b|\baverage\b.*\b(oil|fat|margarine|spread)\b', re.I)


def family_of(label: str | None) -> str | None:
    text = (label or '').casefold()
    return next((f for f, rx in FAMILIES if rx.search(text)), None)


def role_problems(source: str | None, target: str | None, lenient: bool = False) -> list[str]:
    """Reasons the target food cannot take the source food's place in a dish; empty when the roles are compatible.
    lenient (nutrition): a swap inside one family (sugar for syrup, lentils for lentils) is left to the nutrition gates;
    the same food under another record name and vague average/mixed records are rejected."""
    src, tgt = role_of(source), role_of(target)
    problems = []
    if is_compound(target) and not is_compound(source):
        problems.append('target_is_a_prepared_dish')
    if tgt in NEVER_TARGET and src != tgt:
        problems.append(f'target_is_{tgt}')
    if src is None or tgt is None:
        return problems + ['role_unknown']
    same_family = False
    if lenient:
        same_family = family_of(source) is not None and family_of(source) == family_of(target)
        a, b = content_words(source) - VARIETY, content_words(target) - VARIETY
        if a <= b or b <= a:
            problems.append('same_food_other_record')
        if ARTIFICIAL_SWEETENER.search(target or ''):
            problems.append('sweetener_needs_amount_scaling')
        if OFFAL_SOURCE.search(source or '') and not OFFAL_SOURCE.search(target or ''):
            problems.append('offal_to_muscle_meat')
        if re.search(r'parmesan|pecorino|grana', (source or '').casefold()) and tgt != 'cheese':
            problems.append('topping_cheese_to_other_food')
        if WITH_MARKER.search(target or '') and not WITH_MARKER.search(source or ''):
            problems.append('target_is_a_prepared_dish')
        if VAGUE_TARGET.search(target or ''):
            problems.append('vague_target_record')
        if all(re.search(r'lentil', x or '', re.I) and not re.search(r'canned|tinned|boiled|\bcooked|sprout', x or '', re.I) for x in (source, target)):
            problems.append('dry_lentil_variety_swap')  # red/green/puy: the records differ by table, the foods do not (owner, 19 Nov)
        if src == 'poultry' and tgt == 'red_meat':
            problems.append('poultry_to_red_meat')  # WCRF: replace red meat with poultry, not the reverse
        if re.search(r'(^|,\s*)fat\b', source or '', re.I) and not re.search(r'(^|,\s*)fat\b', target or '', re.I):
            problems.append('fat_trimming_is_not_meat')
        if GAME.search(target or ''):
            problems.append('game_meat_target')  # venison or roebuck is not a realistic everyday swap
        if PROCESSED_MEAT.search(source or '') and PROCESSED_MEAT.search(target or ''):
            problems.append('processed_to_processed_meat')
    if src != tgt and frozenset((src, tgt)) not in COMPATIBLE and not same_family:
        problems.append(f'role_mismatch:{src}->{tgt}')
    if not same_family and src == 'sweetener' and tgt == 'sweetener' and not re.search(r'sweeten|aspart|stevia|saccharin|sucralose', (target or '').casefold()):
        problems.append('sugar_for_other_sugar')
    if not same_family and src == tgt and src in PRODUCE:
        problems.append('produce_for_other_produce')
    return problems
