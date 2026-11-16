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
    ('baby_food', r'\b(baby|infant|follow-on|formula|toddler|condensed|evaporated|powdered milk|milk powder)\b'),
    ('stock', r'\b(stock|broth|bouillon)\b'),
    ('oil', r'\boil\b|\bshortening\b|\bghee\b|\bdripping\b|\blard\b|\btallow\b'),
    ('spread_fat', r'\b(butter|margarine|spread)\b(?!\s*bean)|baking fat'),
    ('cream', r'\bcream\b(?! cheese)|cr[eè]me fra[iî]che|sour cream|mascarpone|tejf[oö]l'),
    ('yogurt', r'\by(o|og)h?urt\b|\bkefir\b|\bskyr\b|\bquark\b|fromage frais'),
    ('milk', r'\bmilk\b(?!\s*(chocolate|powder))'),
    ('cheese', r'\bcheese\b|\b(cheddar|mozzarella|feta|parmesan|gouda|edam|emmental|gruy[eè]re|halloumi|ricotta|brie|camembert|cottage)\b'),
    ('egg', r'\begg\b|\beggs\b|\byolk\b'),
    ('red_meat', r'\b(beef|veal|lamb|mutton|pork|venison|goat|steak|mince|minced|bacon|ham|sausage|salami|chorizo|pancetta|prosciutto|liver|offal)\b'),
    ('poultry', r'\b(chicken|turkey|duck|goose|poultry)\b'),
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
                  r'celery|asparagus|mushroom|champignon|sprout|chard|fennel|artichoke|vegetable|salad)\b'),
]
COMPILED = [(role, re.compile(rx.replace(')\\b', ')(?:e?s|ies)?\\b'))) for role, rx in ROLES]

# Roles that may stand in for each other (symmetric). Everything else must keep its role.
COMPATIBLE = {frozenset(p) for p in [
    ('oil', 'spread_fat'), ('cream', 'yogurt'), ('cream', 'milk'), ('milk', 'yogurt'),
    ('red_meat', 'poultry'), ('red_meat', 'fish'), ('poultry', 'fish'),
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
HEAD_NOUNS = [('egg', re.compile(r'\beggs?\b')), ('cheese', re.compile(r'\bcheese\b')), ('milk', re.compile(r"\bmilk\b")), ('nut_seed', re.compile(r'\b(peanut|nut|almond|cashew|seed|sunflower seed) butter\b')),
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


def role_problems(source: str | None, target: str | None) -> list[str]:
    """Reasons the target food cannot take the source food's place in a dish; empty when the roles are compatible."""
    src, tgt = role_of(source), role_of(target)
    problems = []
    if is_compound(target) and not is_compound(source):
        problems.append('target_is_a_prepared_dish')
    if tgt in NEVER_TARGET and src != tgt:
        problems.append(f'target_is_{tgt}')
    if src is None or tgt is None:
        return problems + ['role_unknown']
    if src != tgt and frozenset((src, tgt)) not in COMPATIBLE:
        problems.append(f'role_mismatch:{src}->{tgt}')
    if src == 'sweetener' and tgt == 'sweetener' and not re.search(r'sweeten|aspart|stevia|saccharin|sucralose', (target or '').casefold()):
        problems.append('sugar_for_other_sugar')
    if src == tgt and src in PRODUCE:
        problems.append('produce_for_other_produce')
    return problems
