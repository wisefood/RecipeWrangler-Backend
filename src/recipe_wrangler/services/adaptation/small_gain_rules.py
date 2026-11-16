"""Curated culinary swaps (small-gain adaptation) between foods: same role in the dish, a real released record as the target.

Each rule: name, source-line regex, baseline-label regex, target-label regex, mass factor,
metric used to pick the median eligible record, reason, optional recipe-context exclusion.
"""
import re

COMPOSITE = r'\b(hazelnut|toffee|honey|chocolate|cherr\w*|peach|apricot|raspberr\w*|berr\w*|lemon|banana|mango|coffee|caramel|sweeten\w*|muesli|cereal|lactose|organic|pilau|risotto|homemade|recipe|egg and|and potato|with|salad|soup|pie|casserole|sandwich|ready|meal|dish|w/|pizza|lasagne|pudding|cake|biscuit|sauce|dressing|flavou?red|fruit|vanilla|strawberr|drink|dessert)\b'
def NOT(exclude, alternatives):
    # Exclusion must guard every alternative, not only the first.
    return rf'^(?!.*{exclude})(?:.*?)(?:{alternatives})'

RULES = [
    ('wholemeal_pasta', r'\b(pasta|spaghetti|penne|macaroni|tagliatelle|fusilli|linguine|noodles?|rigatoni|farfalle)\b',
     NOT(r'(wholemeal|whole ?wheat|wholegrain|egg|gluten|rice|corn|buckwheat|filled|stuffed|tortellini|ravioli)', r'\b(pasta|spaghetti|macaroni|noodles)\b'),
     r'(?=.*\b(pasta|spaghetti|macaroni)\b)(?=.*\b(wholemeal|whole ?wheat|wholegrain|whole grain)\b)', 1.0, 'fibre_g',
     'use wholemeal pasta in place of white pasta', r'\b(lasagne)\b'),
    ('brown_rice', r'\brice\b', NOT(r'(brown|unpolished|wholegrain|whole grain|vermicelli|noodle|paper|flake|crispies|puffed|wild|pudding|flour|noodle|cake|paper|wine|vinegar|milk|basmati brown|parboiled brown|glutinous|arborio|risotto)', r'\brice\b'),
     r'(?=.*\brice\b)(?=.*\bbrown\b)', 1.0, 'fibre_g', 'use brown rice in place of white rice',
     r'\b(risotto|pudding|sushi|paella|rizi|rice cake|congee)\b'),
    ('wholemeal_bread', r'\b(bread|loaf|slices?|toast|bap|baguette|ciabatta|roll)\b',
     NOT(r'(wholemeal|whole ?wheat|wholegrain|brown|rye|crumb|soda|naan|pitta|pita|tortilla|wrap|gluten|malt|fruit)', r'\bbread\b.*\bwhite\b|\bwhite\b.*\bbread\b|^bread\b'),
     r'(?=.*\bbread\b)(?=.*\b(wholemeal|whole ?wheat|wholegrain)\b)(?!.*\b(flour|mix|gluten|dough|crumbs?|crisp|crispbread|rusks?|crackers?|toast)\b)', 1.0, 'fibre_g', 'use wholemeal bread in place of white bread', None),
    ('half_wholemeal_flour', r'\bflour\b', NOT(r'(wholemeal|whole ?wheat|wholegrain|brown|rye|corn|rice|potato|almond|gluten|self)', r'\bflour\b.*\b(white|plain|wheat)\b|^flour\b'),
     r'(?=.*\bflour\b)(?=.*\b(wholemeal|whole ?wheat|wholegrain)\b)(?!.*\b(rye|spelt|bread|pasta)\b)', 0.5, 'fibre_g',
     'replace half the white flour with wholemeal flour', None),
    ('wholewheat_couscous', r'\bcous ?cous\b', NOT(r'(wholemeal|whole ?wheat|wholegrain)', r'\bcous ?cous\b'),
     r'(?=.*\bcous ?cous\b)(?=.*\b(wholemeal|whole ?wheat|wholegrain)\b)', 1.0, 'fibre_g', 'use wholewheat couscous', None),
    ('cream_to_reduced_creme_fraiche', r'\b(double|whipping|heavy|single|cooking)?\s*cream\b',
     NOT(r'(ice|sour|cheese|cracker|soup|of |fra[iî]che|coconut|custard|spray|reduced|light|half|low)', r'\bcream\b'),
     r'(?=.*\bcr[eè]me fra[iî]che\b)(?=.*\b(half|reduced|light|low|lite|\d+ ?%)\b)', 1.0, 'saturated_fat_g',
     'stir in reduced-fat crème fraîche instead of cream', r'\b(whip|whisk\w* .*cream|pavlova|meringue|mousse|ice cream|panna cotta|truffle|ganache)'),
    ('sour_cream_to_yogurt', r'\b(sour cream|soured cream|tejföl|kisla smetana|smetana)\b',
     NOT(r'(light|reduced|low|half)', r'\b(sour|soured) ?cream|cream,? sour|tejföl\b'),
     r'(?=.*\by(o|og)h?urt\b)(?=.*\b(natural|plain|greek|low fat|fat free|0 ?%|1\.5 ?%|2 ?%)\b)', 1.0, 'saturated_fat_g',
     'use plain low-fat yogurt in place of sour cream (stir in off the heat)', r'\b(cake|bake|cheesecake|gibanica)\b'),
    ('semi_skimmed_milk', r'\bmilk\b', NOT(r'(skim|semi|low|1[.,]5|0[.,]5|condensed|evaporated|powder|dried|coconut|soy|soya|oat|almond|rice|butter|choc|goat|human|breast|yog|egg|cheese|cream|sauce|pudding|porridge|bread|scrambled|custard)', r'^milk\b|\bmilk, (whole|full|pasteuri|uht|fresh|cow)|\bwhole milk$|^(whole|full[- ]fat) milk\b'),
     r'(?=.*\bmilk\b)(?=.*\b(semi[- ]?skimmed|1\.5 ?%|1\.6 ?%|1 ?%|low[- ]fat|reduced[- ]fat)\b)', 1.0, 'saturated_fat_g',
     'use semi-skimmed milk in place of whole milk', None),
    ('reduced_fat_cheddar', r'\bcheddar\b', NOT(r'(reduced|half|low|light)', r'\bcheddar\b'),
     r'(?=.*\bcheddar\b)(?=.*\b(reduced|half|low|light|lite)\b)', 1.0, 'saturated_fat_g', 'use reduced-fat cheddar', None),
    ('light_mozzarella', r'\bmozzarella\b', NOT(r'(reduced|half|low|light)', r'\bmozzarella\b'),
     r'(?=.*\bmozzarella\b)(?=.*\b(reduced|half|low|light|lite)\b)', 1.0, 'saturated_fat_g', 'use light mozzarella', None),
    ('light_cream_cheese', r'\bcream cheese|soft cheese\b', NOT(r'(reduced|half|low|light)', r'\b(cream|soft) cheese\b'),
     r'(?=.*\b(cream|soft) cheese\b)(?=.*\b(reduced|half|low|light|lite|medium)\b)', 1.0, 'saturated_fat_g', 'use light cream cheese', None),
    ('lean_beef_mince', r'\bmince|minced|ground\b', NOT(r'(lean|extra|5 ?%)', r'\bbeef\b.*\bmince|\bmince.*\bbeef\b|\bminced beef\b|\bground beef\b'),
     r'(?=.*\bbeef\b)(?=.*\b(mince|minced|ground)\b)(?=.*\b(lean|extra lean|5 ?%|10 ?%)\b)', 1.0, 'saturated_fat_g', 'use lean beef mince', None),
    ('reduced_fat_mayo', r'\bmayo', NOT(r'(reduced|light|low|half)', r'\bmayo'),
     r'(?=.*\bmayo)(?=.*\b(reduced|light|low|lite|half)\b)', 1.0, 'saturated_fat_g', 'use reduced-fat mayonnaise', None),
    ('light_coconut_milk', r'\bcoconut milk\b', NOT(r'(reduced|light|low|lite|half)', r'\bcoconut milk\b'),
     r'(?=.*\bcoconut milk\b)(?=.*\b(reduced|light|low|lite|half)\b)', 1.0, 'saturated_fat_g', 'use light coconut milk', None),
    ('lean_bacon', r'\b(bacon|rashers?)\b', NOT(r'(lean|reduced|medallion|trimmed)', r'\bbacon\b'),
     r'(?=.*\bbacon\b)(?=.*\b(lean|reduced[- ]fat|medallion|fat trimmed|trimmed)\b)', 1.0, 'saturated_fat_g', 'use lean back bacon', None),
    ('reduced_salt_stock', r'\b(stock|bouillon)\b', NOT(r'(reduced|low|no added)', r'\b(stock|bouillon)\b'),
     r'(?=.*\b(stock|bouillon)\b)(?=.*\b(reduced|low)[- ]?(salt|sodium)\b)', 1.0, 'sodium_mg', 'use a reduced-salt stock (same form)', None),
    ('reduced_salt_soy', r'\bsoy\b', NOT(r'(reduced|low|light)', r'\bsoy sauce\b'),
     r'(?=.*\bsoy sauce\b)(?=.*\b(reduced|low|less)[- ]?(salt|sodium)\b)', 1.0, 'sodium_mg', 'use reduced-salt soy sauce', None),
    ('no_salt_canned_pulses', r'\b(tins?|tinned|cans?|canned)\b', NOT(r'(no added|unsalted|without salt|reduced salt)', r'\b(chick ?peas?|beans?|lentils?|sweetcorn|corn|peas)\b.*\b(canned|tinned|drained)\b|\b(canned|tinned)\b.*\b(chick ?peas?|beans?|lentils?|sweetcorn|corn|peas)\b'),
     r'(?=.*\b(canned|tinned)\b)(?=.*\b(no added salt|unsalted|without salt|reduced salt)\b)', 1.0, 'sodium_mg',
     'use the no-added-salt tinned version', r'\bbaked beans\b'),
    ('reduced_sugar_salt_baked_beans', r'\bbaked beans\b', NOT(r'(reduced|low|no added)', r'\bbaked beans\b'),
     r'(?=.*\bbaked beans\b)(?=.*\b(reduced|low)\b)', 1.0, 'sugar_g', 'use reduced sugar and salt baked beans', None),
    ('wholemeal_wrap', r'\b(wraps?|tortillas?)\b', NOT(r'(wholemeal|whole ?wheat|wholegrain|corn|maize|crisp|chip)', r'\b(tortilla|wrap)s?\b'),
     r'(?=.*\b(tortilla|wrap)s?\b)(?=.*\b(wholemeal|whole ?wheat|wholegrain)\b)', 1.0, 'fibre_g', 'use wholemeal wraps/tortillas', None),
    ('low_fat_yogurt', r'\by(o|og)h?urt\b', NOT(r'(low|fat[- ]free|0 ?%|light|reduced|skimmed|flavou?red|fruit|vanilla|drink|soya|coconut)', r'\by(o|og)h?urt\b'),
     r'(?=.*\by(o|og)h?urt\b)(?=.*\b(low[- ]fat|fat[- ]free|0 ?%|light|reduced[- ]fat|skimmed|1\.5 ?%|2 ?%)\b)', 1.0, 'saturated_fat_g',
     'use low-fat plain yogurt', None),
    ('half_fat_creme_fraiche', r'\bcr[eè]me fra[iî]che\b', NOT(r'(half|light|reduced|low|lite)', r'\bcr[eè]me fra[iî]che\b'),
     r'(?=.*\bcr[eè]me fra[iî]che\b)(?=.*\b(half|reduced|light|low|lite)\b)', 1.0, 'saturated_fat_g', 'use half-fat crème fraîche', None),
    ('light_sour_cream', r'\b(sour cream|soured cream|tejföl|smetana)\b', NOT(r'(light|reduced|low|half)', r'\b(sour|soured) ?cream|cream,? sour|tejföl\b'),
     r'(?=.*\b(sour|soured) ?cream|.*cream,? sour)(?=.*\b(light|reduced|low|lite|half|10 ?%|12 ?%)\b)', 1.0, 'saturated_fat_g',
     'use light sour cream', None),
    ('reduced_fat_cheese', r'\b(cheese|edam|gouda|emmental|gruy[eè]re|trappist|tilsit|eidam|feta)\b',
     NOT(r'(reduced|light|half|low|lite|cottage|cream cheese|soft|parmesan|grana|mozzarella|cheddar|spread|processed|quark|curd|ricotta|mascarpone|goat)',
         r'\bcheese\b|\b(edam|gouda|emmental|gruy[eè]re|trappist|tilsit|eidam|feta)\b'),
     r'(?=.*\b(cheese|edam|gouda|emmental|gruy[eè]re|trappist|tilsit|eidam|feta)\b)(?=.*\b(reduced|light|half|low|lite|30 ?\+|20 ?\+|17 ?%|\d+ ?% fat)\b)',
     1.0, 'saturated_fat_g', 'use a reduced-fat version of the same cheese', None),
    ('reduced_fat_sausage', r'\bsausages?\b', NOT(r'(reduced|lean|low|light|vegetarian|vegan|roll)', r'\bsausages?\b'),
     r'(?=.*\bsausages?\b)(?=.*\b(reduced[- ]fat|low[- ]fat|lean|light)\b)', 1.0, 'saturated_fat_g', 'use reduced-fat sausages', None),
    ('lean_mince_other', r'\b(mince|minced|ground)\b', NOT(r'(lean|extra|5 ?%|beef)', r'\b(pork|lamb|turkey|chicken)\b.*\b(mince|minced|ground)\b|\b(mince|minced|ground)\b.*\b(pork|lamb|turkey|chicken)\b'),
     r'(?=.*\b(mince|minced|ground)\b)(?=.*\b(lean|extra lean|5 ?%|10 ?%|low[- ]fat|reduced[- ]fat)\b)', 1.0, 'saturated_fat_g', 'use lean mince of the same meat', None),
    ('light_spread_for_spreading', r'\b(butter|margarine|spread)\b', NOT(r'(light|reduced|low|peanut|nut|almond|cocoa|chocolate)', r'\b(butter|margarine|spread)\b'),
     r'(?=.*\b(spread|margarine)\b)(?=.*\b(light|reduced[- ]fat|low[- ]fat|lite|\d\d ?% fat)\b)(?!.*\b(cheese|chocolate|hazelnut|peanut)\b)', 1.0,
     'saturated_fat_g', 'spread a light spread instead of butter', 'SPREAD_ONLY'),
    ('butter_to_oil_for_frying', r'\b(butter|margarine|lard|ghee|dripping)\b',
     NOT(r'(peanut|nut|almond|cocoa|shea|bean|cheese|chocolate)', r'\b(butter|lard|ghee|dripping)\b'),
     r'^(oil,? )?(rapeseed|olive|canola)\b.*\boil\b|^oil,? (rapeseed|olive|canola)\b', 0.8, 'saturated_fat_g',
     'fry in rapeseed/olive oil (about 80% of the butter weight, same fat) instead of butter', 'BAKED_OR_NOT_FRYING'),
]
ORDER_FIRST = ('light_sour_cream', 'half_fat_creme_fraiche')
RULES = sorted(RULES, key=lambda r: r[0] not in ORDER_FIRST)
RULES = [dict(zip(('name', 'line', 'base', 'target', 'mass_factor', 'pick_metric', 'reason', 'exclude_context'), r)) for r in RULES]

STATE_GROUPS = {
    'cooked': r'\b(boiled|cooked|fried|roast(ed)?|baked|grilled|stewed|steamed|braised|microwaved|poached|toasted)\b',
    'canned': r'\b(canned|tinned|in brine|in water|drained)\b',
    'concentrate': r'\b(cubes?|powder|granules|concentrate|paste|dry mix|dried)\b',
    'made_up': r'\b(dissolved|made up|prepared|diluted|reconstituted|ready to (use|eat)|liquid)\b',
}


def states(label):
    return frozenset(k for k, rx in STATE_GROUPS.items() if re.search(rx, label, re.I))


def compatible_state(base_label, target_label, rule):
    base, target = states(base_label), states(target_label)
    if rule in ('wholemeal_pasta', 'brown_rice', 'half_wholemeal_flour', 'wholewheat_couscous'):
        # "dried" means uncooked for grains, never a concentrate.
        base, target = base - {'concentrate'}, target - {'concentrate'}
    return base == target


# Words in the source line that mean the recipe already asks for the healthier target (swap would only repair the match).
ALREADY = r'\b(wholemeal|whole ?wheat|wholegrain|whole grain|brown|unpolished|lean|trimmed|semi[- ]?skimmed|skimmed|low[- ]?(salt|fat|sodium)|reduced|light|lite|half[- ]?fat|no added|unsalted|1\.5 ?%)'
# Nutrients each swap is meant to change; any other score nutrient falling by >50% means a different product.
INTENDED = {'fibre': {'fibre_g', 'saturated_fat_g', 'energy_kcal'}, 'fat': {'saturated_fat_g', 'energy_kcal'},
            'salt': {'sodium_mg'}, 'sugar_salt': {'sugar_g', 'sodium_mg'}}
KIND = {'wholemeal_pasta': 'fibre', 'brown_rice': 'fibre', 'wholemeal_bread': 'fibre', 'half_wholemeal_flour': 'fibre',
        'wholewheat_couscous': 'fibre', 'reduced_salt_stock': 'salt', 'reduced_salt_soy': 'salt', 'no_salt_canned_pulses': 'salt',
        'reduced_sugar_salt_baked_beans': 'sugar_salt'}
FLOOR = {'sodium_mg': 100, 'sugar_g': 2, 'saturated_fat_g': 1}


def unearned(rule, before, after):
    intended = INTENDED[KIND.get(rule, 'fat')]
    return [m for m, floor in FLOOR.items() if m not in intended and (before.get(m) or 0) >= floor and (after.get(m) or 0) < 0.5 * before[m]]

# Type words that must carry from the baseline record to the target (no goat->cow, smoked->fresh, pork->beef, gouda->edam).
KEEP = r'\b(greek|goats?|sheeps?|ewes?|buffalo|soya|soy|coconut|smoked|cured|pork|beef|lamb|chicken|turkey|veal|cheddar|edam|gouda|emmental|feta|gruy[eè]re|trappist|tilsit|eidam|basmati|jasmine|spaghetti)\b'


def keeps_type(base_label, target_label):
    import re as _re
    return all(_re.search(r'\b' + w + r'\b', target_label, _re.I) for w in {m.lower() for m in _re.findall(KEEP, base_label, _re.I)})


# ---- loose "similar food" search: same food any part, and role swaps between proteins ----
# Exclusions apply to a candidate label only when the baseline label does not have them (mince stays mince).
SIMILAR_EXCLUDE = (r'\b(liver|kidneys?|heart|brain|tongue|lung|tripe|sweetbreads?|offal|suet|tallow|dripping|skin|bones?|stock|broth|'
                   r'pate|pâté|sausages?|burgers?|mince|minced|dried|smoked|cured|canned|tinned|soup|stew|curry|pie|gravy|sauce|spread|egg|white|yolk|roe|caviar)\b'
                   r'|(^|,\s*)fat\b|\bfat only\b')
# Fatty or red meats (and their identities) that a leaner poultry breast can stand in for.
ROLE_SWAPS = [
    {'name': 'protein_to_poultry', 'from': ('lamb', 'mutton', 'beef', 'pork', 'veal', 'venison', 'duck', 'goose'),
     'to': (('chicken', 'breast', r'skinless|(without|w/o)\b[^,]*skin|no skin|meat only'), ('turkey', 'breast', r'')),
     'reason': 'use lean poultry breast in place of the fattier meat (changes the dish; shorten the cooking time)'},
]

# Descriptors that make the baseline food what it is: a "similar" candidate must keep each one the baseline label has.
DESCRIPTORS = r'\b(skinless|without skin|lean|skimmed|semi[- ]?skimmed|low[- ]?fat|reduced[- ]?fat|wholemeal|whole ?wheat|wholegrain|brown|unpolished|wild|no added \w+|unsalted|egg white|yolk|whole)\b'


def keeps_descriptors(base_label, target_label):
    import re as _re
    need = {m.lower() for m in _re.findall(DESCRIPTORS, base_label, _re.I)}
    got = {m.lower() for m in _re.findall(DESCRIPTORS, target_label, _re.I)}
    alias = {'skinless': 'without skin', 'without skin': 'skinless'}
    return all(d in got or alias.get(d) in got for d in need)

# Raw plain meat and poultry carry about 40-110 mg sodium per 100 g; far more means brine or a seasoned product, not the cut.
ANIMAL = {'lamb', 'mutton', 'beef', 'pork', 'veal', 'venison', 'duck', 'goose', 'chicken', 'turkey', 'rabbit'}
BRINED_SODIUM_MG = 150


def scan_label(label: str) -> str:
    """Label with negated phrases removed ("without bone and skin", "w/o skin"), so they are not read as skin-on or bone-in."""
    import re as _re
    return _re.sub(r'\b(without|w/o|no)\s+[a-z ]+?(?=,|$)', '', label or '', flags=_re.I)
