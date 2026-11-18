"""Curated lower-CO2e swaps (owner, 17 Nov 2026). Each row: name, source regex (matched label + line), target regex (composition record
label), mass factor (target grams per source gram), context ('baked' / 'not_baked' / None), reason. Targets are real composition records
picked regional table first; every row is still judged by the same recalculation and gates as search candidates."""
import re

RED_MEAT = r'\b(beef|veal|lamb|mutton|goat|pork)\b'
POULTRY_CUT = r'^(chicken|turkey)\b(?!.*\b(skin|liver|heart|gizzard|wings?|drumsticks?|mince|minced|ground|balls?|fried|breaded|nuggets?|sausages?|rolls?|burgers?|smoked|ham|coated|takeaway|casing|battered|crumbed|slices?|weighed|frankfurter|strips|gyros|schnitzel|cordon|bleu|kiev|stuffed|filled|batter|breadcrumbs?)\b)'
WHITE_FISH = r'\b(cod|haddock|pollock|pollack|whiting|plaice|sole|hake|saithe|coley|ling)\b'

RULES = [dict(zip(('name', 'source', 'target', 'mass_factor', 'context', 'reason'), r)) for r in [
    ('red_meat_to_poultry', RED_MEAT, POULTRY_CUT, 1.0, None,
     'poultry has a much lower climate footprint than beef, lamb, veal or pork'),
    ('red_mince_to_poultry_mince', RED_MEAT, r'^(chicken|turkey)\b.*\b(mince|minced|ground)\b', 1.0, None,
     'chicken or turkey mince has a much lower climate footprint than red-meat mince'),
    ('butter_to_margarine', r'\bbutter\b', r'^(margarine|vegetable fat spread|fat spread)\b', 1.0, None,
     'a plant margarine has a far lower footprint than butter and works for baking and spreading'),
    ('cream_to_light_cream', r'\b(double|whipping|heavy|whipped)\b.*\bcream\b|\bcream\b.*\b(double|whipping|heavy|3[0-9] ?%)',
     r'\bcream\b.*\b(single|light|half|1[0-9] ?%|20 ?%)|\b(single|light|half)\b.*\bcream\b', 1.0, None,
     'lower-fat cream needs less milk fat, so it has a lower footprint'),
    ('hard_cheese_to_mozzarella', r'\b(cheddar|gouda|edam|eidam|emmental|gruy[eè]re|red leicester|trappist|tilsit)\b', r'^(cheese,? )?mozzarella\b', 1.0, None,
     'mozzarella melts the same way and has a lower footprint than aged hard cheese'),
    ('ham_to_poultry_ham', r'^(?!.*\b(cured|prosciutto|parma|serrano|jam[oó]n|bayonne|karst|smoked|speck)\b).*\bham\b', r'^(turkey ham|chicken ham|ham,? (turkey|chicken))\b', 1.0, None,
     'turkey or chicken ham has a lower footprint than pork ham (about a quarter less)'),
    ('bacon_to_poultry_bacon', r'\b(bacon|rashers?)\b', r'(turkey|chicken)\b.*\bbacon\b|\bbacon,? (turkey|chicken)\b', 1.0, None,
     'turkey bacon has a lower footprint than pork bacon (changes the taste)'),
    ('salami_to_poultry_salami', r'\b(salami|pepperoni)\b', r'(turkey|chicken|poultry)\b.*\b(salami|pepperoni)\b|\b(salami|pepperoni),? (turkey|chicken|poultry)\b', 1.0, None,
     'turkey salami has a lower footprint than pork salami'),
    ('white_fish_to_hake', r'\b(cod|haddock|pollock|pollack|whiting|plaice)\b', r'^(hake|saithe|coley)\b|\b(hake|saithe|coley),', 1.0, None,
     'hake is a close relative that cooks the same way (fish pie, fillets, battered); indicative saving, because AGRIBALYSE gives cod, haddock, pollock, whiting and plaice one shared value'),
    ('tuna_to_small_oily_fish', r'\btuna\b', r'\b(sardines?|pilchards?|mackerel|herring)\b', 1.0, None,
     'sardines, mackerel and herring have about half the footprint of tuna and are rich in omega-3'),
    ('prawns_to_mussels', r'\b(prawns?|shrimps?)\b', r'^mussels?\b', 1.0, None,
     'mussels have about a quarter of the footprint of prawns'),
]]
WHIP = re.compile(r'\bwhip', re.I)

SOURCE_ROLE = {'red_meat_to_poultry': ('red_meat',), 'red_mince_to_poultry_mince': ('red_meat',), 'butter_to_margarine': ('spread_fat',),
               'cream_to_light_cream': ('cream',), 'hard_cheese_to_mozzarella': ('cheese',), 'tuna_to_small_oily_fish': ('fish',), 'white_fish_to_hake': ('fish',), 'ham_to_poultry_ham': ('red_meat',), 'bacon_to_poultry_bacon': ('red_meat',), 'salami_to_poultry_salami': ('red_meat',), 'prawns_to_mussels': ('shellfish',)}  # the food's own role, so composites that merely name it are skipped
ALLOW_PROCESSED = {'ham_to_poultry_ham', 'bacon_to_poultry_bacon', 'salami_to_poultry_salami'}  # cured pork to the poultry version of the same product
INDICATIVE = {'white_fish_to_hake'}  # the saving rests on one shared AGRIBALYSE proxy value for several white fish; shown as indicative

# Edible fraction of bone-in red meat (1 - refuse), first match wins. Source: ReFED, "U.S. Grocery Retail Food Inedible Parts Factors" (2023),
# which takes the item refuse from the USDA National Nutrient Database for Standard Reference: pork chops bone-in 25%, spareribs 30%, blade chops 26%;
# lamb rib chop 25%, leg chop 15%, shoulder chop 23%, neck chop 32%, category average 18%; veal rib 35%, loin chop 30%, shank 29%; beef steaks 23%.
BONE_EDIBLE = [
    (r'\bveal\b.*\b(ribs?|chops?|cutlets?)\b|\b(ribs?|chops?|cutlets?)\b.*\bveal\b', 0.67),
    (r'\bveal\b', 0.71),
    (r'\b(pork|spare)\b.*\bribs?\b|\bribs?\b.*\bpork\b|\bspare ?ribs?\b', 0.70),
    (r'\b(lamb|mutton)\b.*\b(chops?|cutlets?|ribs?|rack|neck)\b|\b(chops?|cutlets?|ribs?|rack|neck)\b.*\b(lamb|mutton)\b', 0.75),
    (r'\b(lamb|mutton)\b', 0.82),
    (r'\bpork\b.*\b(chops?|cutlets?|steaks?|shoulder|blade)\b|\b(chops?|cutlets?)\b.*\bpork\b', 0.75),
    (r'\bbeef\b.*\bribs?\b(?!\s*(steak|eye))|\bribs?\b(?!\s*(steak|eye)).*\bbeef\b', 0.71),
    (r'\bbeef\b|\bsteaks?\b|\bt-bone\b', 0.77),
]
BONE_EDIBLE_DEFAULT = 0.80


def edible_fraction(text):
    """Edible share of a bone-in red-meat line: a swap target is weighed as edible meat, so 500 g of chops becomes about 375 g of chicken."""
    return next((f for rx, f in BONE_EDIBLE if re.search(rx, text or '', re.I)), BONE_EDIBLE_DEFAULT)
