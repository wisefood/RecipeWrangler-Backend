"""Sustainability (CO2e) adaptation, mirroring the nutrition rework (16 Nov 2026).

Diagnosis: only ingredients carrying >= HOTSPOT_SHARE of the recipe's CO2e are changed; protected foods never are.
Candidates: the curated table (sustainability_swap_rules) and a related-food search over the composition records, both judged on CO2e saving
per serving (floors), the culinary role filter (food_roles, lenient) and the allergen/diet gate. Nutrition is deliberately not judged: sustainability
is its own facet (owner, 18 Nov). Deterministic, no model calls, no writes.
CO2e factors are injected (item_factor, label_factor; both get the matched record's candidate id so a code join can come first) so the reviewed AGRIBALYSE mapping stays outside the package until signed off."""
import re

from . import nutrition_swap_rules as nswap
from . import sustainability_swap_rules as rules
from .food_roles import GAME, PROCESSED_MEAT, is_compound, role_of, role_problems
from .nutrition_adaptation import MINCED, OFFAL, bone_in, instructions_text, is_baked, protected_food, regional_first, restriction_problems

HOTSPOT_SHARE = 0.20      # owner, 17 Nov: same as nutrition's main-source share
MIN_SHARE = 0.10          # saving >= 10% of the recipe's CO2e ...
MIN_PER_SERVING = 0.05    # ... and >= 0.05 kg CO2e per serving
LOWER_RATIO = 0.7         # search targets: factor <= 70% of the source's
MIN_SCORED_SHARE = 0.95   # >= 95% of the recipe mass must have a factor
PLANT_PROTEIN = True    # owner, 17 Nov: half of a chicken/turkey/meat line replaced by the same weight of cooked lentils, in stewed or curried dishes only
PULSE_DISH = re.compile(r'stew|soup|curry|chilli|chili|casserole|hotpot|tagine|goulash|risotto|pilaf|dahl|bolognese|ragu|rogan|jalfrezi|korma|tikka|biryani|bake|pie\b|cottage|shepherd|broth|pottage|paprikash|ragout|sauce', re.I)
PULSE = re.compile(r'\blentils?\b.*\b(boiled|cooked|stewed)\b|\b(boiled|cooked|stewed)\b.*\blentils?\b', re.I)
SKIP_SOURCE_ROLES = ('ready_dish', 'beverage', 'baby_food', 'stock')
LOW_IMPACT_PLANT = ('vegetable', 'allium', 'fruit', 'potato_root', 'herb_spice', 'legume', 'nut_seed')  # never swapped for climate
HARD_CHEESE = re.compile(r'\b(cheddar|gouda|edam|eidam|emmental|gruy[eè]re|parmesan|grana|pecorino|leicester|halloumi|tilsit|trappist|hard|mozzarella)\b', re.I)
FRESH_CHEESE = re.compile(r'\b(cottage|ricotta|quark|cream cheese|curd|fresh|t[uú]r[oó]|skuta|feta|mascarpone|soft)\b', re.I)
RICE_DISH = re.compile(r'\b(rice|risotto|paella|biryani|pilaf|pilau|sushi|congee|jambalaya|kedgeree)\b', re.I)  # rice is the dish
STRUCTURAL_CREAM = re.compile(r'\b(whip\w*|ice cream|mousse|panna cotta|roulade|pavlova|cheesecake)\b', re.I)
CEPHALOPOD = re.compile(r'\b(cuttle ?fish|squid|calamari|octopus)\b', re.I)
SPREAD = re.compile(r'\b(spread\w*|sandwich\w*|toast\w*|bread and butter|buttered)\b', re.I)
COOKED = re.compile(r'\b(boiled|cooked|fried|roast\w*|baked|grilled|stewed|steamed|braised|casserol\w*|poached|microwaved)\b', re.I)
RAW_DISH = re.compile(r'\b(tartare|tartar|carpaccio|sushi|sashimi|ceviche|tataki)\b', re.I)
COMMON_OIL = re.compile(r'\b(sunflower|rapeseed|canola|vegetable)\b', re.I)
GRILLING_CHEESE = re.compile(r'\b(halloumi|paneer|grilling cheese)\b', re.I)
SPECIALTY_RICE = re.compile(r'\b(arborio|carnaroli|risotto|paella|sushi|pudding rice)\b', re.I)
WHITE_FISH = re.compile(rules.WHITE_FISH, re.I)
PREPARED_MEAT_TARGET = re.compile(r'\b(rillettes?|p[aâ]t[eé]s?|terrine|nuggets?|goujons?|fingers?|burgers?|patt(y|ies)|meat ?balls?|spreads?|pastes?|breaded|battered|crumbed|in casing|coated|takeaway|slices|cordon|bleu|kiev|stuffed|in batter|gravy|sauce|paupiette|brawn|skewers?|hamburg\w*|smoked|corned|almods?|fried|luncheon|brined)\b', re.I)
FERMENTED_DRINK = re.compile(r'\b(kefir|tibicos|probiotic|drink|water)\b', re.I)
OFFAL_TARGET = re.compile(r'\b(brains?|lungs?|spleen|tripe|tongue|sweetbreads?|hearts?|livers?|kidneys?|trotters?|pigs? feet|tail|cheeks?|blood|marrow|gizzards?|giblets?)\b', re.I)
ANIMAL_FAT_TARGET = re.compile(r'\b(lard|tallow|dripping|suet|(back|belly|pork|beef) fat|fat ?back|rind(less)?)\b', re.I)
LOW_FAT_SPREAD = re.compile(r'low[- ]?fat|\blight\b|reduced|\b[1-5]\d ?%', re.I)
SWEETENER = re.compile(r'\b(sugar|honey|syrup|agave|molasses|sweetener)\b', re.I)  # a sweetener for another sweetener is not a climate swap (agave to white sugar)
STATED_AMOUNT = re.compile(r'\d|[½¼¾⅓⅔⅛]|\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten|half|dozen)\b', re.I)
ICING_SUGAR = re.compile(r'\b(icing|powdered|confectioner\w*)\b', re.I)


def local_problems(src_label, tgt_label, solid_fat):
    """Sustainability-only culinary guards on top of food_roles (17 Nov review of the 13 Nov suggestions)."""
    src, tgt = role_of(src_label), role_of(tgt_label)
    out = []
    if re.search(r'\baverage\b|\bprocessed\b|\bimitation\b|\banalogue\b', tgt_label, re.I):
        out.append('vague_or_processed_target')
    if re.search(r'plant[- ]based|vegetarian|vegan|meat[- ]?free|meat[- ]?less|imitation|substitute|alternative|analogue', tgt_label, re.I):
        out.append('plant_based_or_imitation_target')
    fat_record = r'^(fat|skin)\b|^[a-z]+,?\s+(fat|skin|dripping|lard|tallow|suet)\b'  # 'Chicken fat', 'Goose fat', 'Skin only'
    if re.search(fat_record, tgt_label, re.I) and not re.search(fat_record, src_label, re.I):
        out.append('fat_or_skin_target')
    if src == 'fish' and tgt == 'red_meat':
        out.append('fish_to_red_meat')
    if tgt in ('red_meat', 'poultry', 'fish') and PREPARED_MEAT_TARGET.search(tgt_label) and not PREPARED_MEAT_TARGET.search(src_label):
        out.append('prepared_meat_target')  # rillettes, pate, nuggets, breaded: a prepared product, not the plain meat
    if src in ('red_meat', 'poultry', 'fish') and not PROCESSED_MEAT.search(src_label) and PROCESSED_MEAT.search(tgt_label):
        out.append('fresh_meat_to_processed_meat')  # frankfurter, bacon, ham for fresh meat
    if src in ('yogurt', 'cream', 'milk') and FERMENTED_DRINK.search(tgt_label) and not FERMENTED_DRINK.search(src_label):
        out.append('fermented_drink_target')  # kefir and water kefir are drinks
    if OFFAL_TARGET.search(tgt_label) and not OFFAL_TARGET.search(src_label):
        out.append('offal_target')
    if ANIMAL_FAT_TARGET.search(tgt_label) and not ANIMAL_FAT_TARGET.search(src_label):
        out.append('animal_fat_target')  # lard, back fat: fat, not meat
    if src == 'spread_fat' and LOW_FAT_SPREAD.search(tgt_label) and not re.search(r'low|light|reduced|spreadable|\b[1-5]\d ?%', src_label, re.I):
        out.append('low_fat_spread_for_butter')  # butter is cooked and baked with; a 35-40% spread is not a substitute
    if src == 'fish' and tgt == 'fish':
        out.append('fish_swap_only_via_table')
    if src == 'sweetener' and ICING_SUGAR.search(tgt_label) and not ICING_SUGAR.search(src_label):
        out.append('icing_sugar_target')
    if re.search(r'\bolive oil\b|\boil,? olive\b', tgt_label, re.I):
        out.append('olive_oil_target')  # owner, 16 Nov: no olive-oil advice
    if re.search(r'\b(breast milk|milk replacement|formula|infant)\b', tgt_label, re.I):
        out.append('infant_food_target')
    if src == 'bread' and tgt != 'bread':
        out.append('bread_to_other_food')
    if bool(COOKED.search(src_label)) != bool(COOKED.search(tgt_label)):
        out.append('cooked_state_differs')  # raw and cooked records differ in weight per portion
    if tgt == 'oil' and src != 'oil' and not COMMON_OIL.search(tgt_label):
        out.append('uncommon_oil')
    if src == 'yogurt' and tgt in ('milk', 'cheese'):
        out.append('yogurt_to_milk_or_cheese')
    if src == 'sweetener' and re.search(r'\b(jam|marmalade|jelly|preserve|chocolate|spread)\b', tgt_label, re.I) and not re.search(r'\b(jam|marmalade|jelly|preserve)\b', src_label, re.I):
        out.append('sweetener_to_preserve')
    if src == 'cream' and tgt == 'milk':
        out.append('cream_to_milk')
    if CEPHALOPOD.search(src_label) and not CEPHALOPOD.search(tgt_label):
        out.append('squid_to_other_seafood')
    if GRILLING_CHEESE.search(src_label):
        out.append('grilling_cheese')
    if re.search(r'\bcoconut\b', tgt_label, re.I) and not re.search(r'\bcoconut\b', src_label, re.I):
        out.append('coconut_for_dairy')
    if src == 'red_meat' and tgt == 'fish':
        out.append('red_meat_to_fish')
    if src == 'milk' and (tgt != 'milk' or re.search(r'\b(curd|kefir|buttermilk|sour|soured|fermented)\b', tgt_label, re.I)):
        out.append('milk_to_other_dairy')
    if src == 'fish' and WHITE_FISH.search(src_label) and not WHITE_FISH.search(tgt_label):
        out.append('white_fish_to_other_fish')
    if PROCESSED_MEAT.search(src_label) and tgt in ('red_meat', 'poultry'):
        out.append('processed_meat_to_fresh_meat')
    if src == 'spread_fat' and tgt == 'oil':
        out.append('butter_to_oil_dropped')  # owner, 17 Nov: butter goes to margarine only (same factor, works in mash, baking, spreading)
    if src == 'spread_fat' and not re.search(r'\b(margarine|butter|fat spread|vegetable fat|oil|ghee|lard)\b', tgt_label, re.I):
        out.append('target_is_not_a_fat')  # "croutons, for spreads" is not a spread
    if src == 'rice_grain':
        out.append('grain_swaps_dropped')  # owner, 17 Nov
    if re.search(r'\bfeta\b', src_label, re.I):
        out.append('feta_is_identity')  # owner, 17 Nov: feta to cottage cheese dropped
    if WHITE_FISH.search(src_label):
        out.append('white_fish_proxy_group')  # cod, haddock, pollock, whiting, plaice share one AGRIBALYSE value (12.0); owner: cod and hake are the same, so no swap
    if src == 'cheese' and (tgt != 'cheese' or HARD_CHEESE.search(src_label) and not HARD_CHEESE.search(tgt_label)
                            or FRESH_CHEESE.search(src_label) and not FRESH_CHEESE.search(tgt_label)):
        out.append('cheese_to_other_texture')
    if src == 'rice_grain' and re.search(r'\b(flakes?|flour|mix|porridge|muesli)\b', tgt_label, re.I):
        out.append('grain_to_flakes_or_mix')
    if src == 'rice_grain' and (tgt != 'rice_grain' or SPECIALTY_RICE.search(src_label) and not re.search(r'\brice\b', tgt_label, re.I)):
        out.append('grain_to_other_kind')
    return out


def _pool(records, region, label_factor):
    """Single-food records of the regional table and the EU collection that have a CO2e factor."""
    keep = ('nutritional_ingredients_eu', 'nutritional_ingredients_' + region)
    out = []
    for r in records:
        label = r.get('label') or ''
        if r['collection'] not in keep or not label or is_compound(label) or (r.get('f') or {}).get('compound'):
            continue
        f = label_factor(label, r.get('candidate_id'))
        if f is not None:
            out.append(dict(r, cf=f))
    return out


def adapt(case, snapshot, recipe, records, item_factor, label_factor, use_table=True, use_search=True, pool=None, allergens=(), diet=None):
    """One recipe. item_factor(name, weight_g, label, candidate_id) returns (kg CO2e/kg, source) or (None, None); label_factor(label, candidate_id) returns kg CO2e/kg or None.
    Only the primary source ('agribalyse') may be the swapped food; fallback factors (CONCITO, role averages) just complete the total.
    allergens/diet: the user's own restrictions; a target that hits them is rejected whether or not the recipe already has the food."""
    region, serves = case['region'], snapshot.serves or 1
    out = dict(recipe_id=case['recipe_id'], title=case['title'], region=region, serves=serves)
    items = []
    for item in snapshot.ingredients:
        w = item.quantity.weight_g or 0
        label_ = (item.composition.provenance.get('candidate') or {}).get('document', '') if item.composition else ''
        cid = (item.composition.provenance.get('candidate_id') or '') if item.composition else ''
        cf, src = item_factor(item.name or item.original_text or '', w, label_, cid)
        items.append((item, w, cf, src))
    mass = sum(w for _, w, _, _ in items) or 1
    scored = sum(w for _, w, cf, _ in items if cf is not None)
    out['fallback_mass_share'] = round(sum(w for _, w, cf, src in items if cf is not None and src != 'agribalyse') / mass, 3)
    out['scored_mass_share'] = round(scored / mass, 3)
    if scored / mass < MIN_SCORED_SHARE:
        return dict(out, status='not_scored', reason='less_than_95pct_of_mass_has_a_co2_factor')
    total = sum(w / 1000 * cf for _, w, cf, _ in items if cf is not None)
    out.update(co2_total=round(total, 4), co2_per_serving=round(total / serves, 4))
    if total <= 0:
        return dict(out, status='not_scored', reason='zero_total')
    title = recipe.get('title') or case['title']
    baked = is_baked(title, instructions_text(recipe), snapshot)
    texts = [i.original_text or i.name or '' for i, _, _, _ in items]
    pool = pool if pool is not None else _pool(records, region, label_factor)
    hotspots, cands, rejected, rej_examples = [], [], {}, []

    def judge(item, w, cf, label, r, mass_factor, route, reason, partial=False, allow=(), edible=1.0):
        new_w = w * mass_factor
        saved = w / 1000 * 0.5 * (cf - r['cf']) if partial else w / 1000 * cf * edible - new_w / 1000 * r['cf']  # edible: bone is not meat, so compare edible with edible  # partial: half the line stays, half becomes the target
        why = []
        if saved < MIN_SHARE * total or saved / serves < MIN_PER_SERVING:
            why.append('below_co2_floor')
        if not partial:
            why += [p for p in role_problems(label, r['label'], lenient=True) + local_problems(label, r['label'], solid_fat) if p not in allow]
        if restriction_problems([{'operation': 'SWAP_FOOD', 'candidate_label': r['label'], 'matched_label': label}], texts, allergens, diet):
            why.append('allergen_or_diet')
        if why:
            for k in why:
                rejected[k] = rejected.get(k, 0) + 1
            if len(rej_examples) < 12:
                rej_examples.append(dict(from_label=label, to_label=r['label'], route=route, reasons=why))
            return False
        cands.append(dict(position=item.position, source_line=item.original_text or item.name, from_label=label, to_label=r['label'], route=route,
                          reason=reason, indicative=route.removeprefix('table:') in rules.INDICATIVE, edible_fraction=edible if edible != 1.0 else None, to_dataset=r.get('dataset'), weight_before_g=round(w, 1), weight_after_g=round(w * 0.5 if partial else new_w, 1), partial=partial,
                          from_cf=cf, to_cf=r['cf'], saved_per_serving=round(saved / serves, 4), saved_share=round(saved / total, 3),
                          changes_dish_identity=bool(re.search(r'\b' + re.escape((re.findall(r'[a-z]{3,}', label.lower()) or ['~'])[0]), title.lower()))))
        return True

    for item, w, cf, src in items:
        if cf is None or w <= 0 or w / 1000 * cf < HOTSPOT_SHARE * total:
            continue
        label = (item.composition.provenance.get('candidate') or {}).get('document', '') if item.composition else ''
        line = item.original_text or ''
        spot = dict(position=item.position, line=line, label=label, co2_share=round(w / 1000 * cf / total, 3))
        hotspots.append(spot)
        if src != 'agribalyse':
            spot['skipped'] = 'fallback_factor'; continue
        if not STATED_AMOUNT.search(line):
            spot['skipped'] = 'amount_not_stated'; continue  # the weight of a line like 'creme fraiche' is a guess, so is any saving
        if protected_food(label):
            spot['skipped'] = 'protected:' + protected_food(label); continue
        if role_of(label) in SKIP_SOURCE_ROLES:
            spot['skipped'] = 'prepared_product_or_drink'; continue
        if role_of(label) in LOW_IMPACT_PLANT:
            spot['skipped'] = 'low_impact_plant_food'; continue
        if re.search(nswap.COMPOSITE, label, re.I) or is_compound(label) or re.search(r'\b(pastry|dough|crust|biscuits?|crackers?|pie)\b', label, re.I):
            spot['skipped'] = 'prepared_product_or_drink'; continue
        if RAW_DISH.search(title):
            spot['skipped'] = 'eaten_raw'; continue
        if role_of(label) == 'rice_grain' and RICE_DISH.search(title):
            spot['skipped'] = 'rice_is_the_dish'; continue
        if role_of(label) == 'cream' and STRUCTURAL_CREAM.search(title + ' ' + instructions_text(recipe)):
            spot['skipped'] = 'cream_is_structural'; continue
        ref_cf = min(cf, label_factor(label, (item.composition.provenance.get('candidate_id') or '') if item.composition else '') or cf)  # the record's own factor too: a name/record mapping mismatch is not a saving
        solid_fat = baked or bool(SPREAD.search(label + ' ' + line + ' ' + title))  # butter for baking or spreading needs margarine, not oil
        bone = bone_in(label + ' ' + line)
        if bone and role_of(label) != 'red_meat':
            spot['skipped'] = 'bone_in'; continue  # bone-in poultry is already the low-impact meat
        edible = rules.edible_fraction(label + ' ' + line) if bone else 1.0
        if role_of(label) in ('red_meat', 'poultry') and re.search(r'(^|,\s*)(fat|skin|trimmings?)\b', label, re.I):
            spot['skipped'] = 'fat_trimming'; continue  # a fat or skin line is not meat
        dataset = item.composition.source_dataset if item.composition else None
        minced = bool(MINCED.search(label + ' ' + line))

        def fits(r):
            return (bool(MINCED.search(r['label'])) == minced and not OFFAL.search(r['label']) and not GAME.search(r['label'])
                    and not (SWEETENER.search(label) and SWEETENER.search(r['label']))
                    and not re.search(r'\b(goose|geese|duck|wild|quail|pheasant|pigeon|ostrich|rabbit)\b', r['label'], re.I)  # not a stand-in for beef, pork or lamb
                    and not re.search(nswap.COMPOSITE, r['label'], re.I) and nswap.compatible_state(label, r['label'], ''))

        if use_table:
            for rule in rules.RULES:
                processed_ok = rule['name'] in rules.ALLOW_PROCESSED
                if not re.search(rule['source'], label + ' ' + line, re.I) or (PROCESSED_MEAT.search(label + ' ' + line) and not processed_ok):
                    continue
                if role_of(label) not in rules.SOURCE_ROLE[rule['name']]:
                    continue  # "puff pastry, pure butter" is pastry, not butter
                if rule['context'] and (rule['context'] == 'baked') != solid_fat:
                    continue
                if rule['name'] == 'cream_to_light_cream' and rules.WHIP.search(instructions_text(recipe)):
                    continue
                if rule['name'] == 'red_meat_to_poultry' and minced:
                    continue
                if bone and rule['name'] != 'red_meat_to_poultry':
                    continue  # bone-in red meat: only the poultry swap, weighed as edible meat
                targets = [r for r in pool if re.search(rule['target'], r['label'], re.I) and role_of(r['label']) and fits(r)]  # a record with no recognised role (coley) must not stop the EU fallback
                for r in sorted(targets, key=lambda r: (not r['collection'].endswith(region), r['cf'], 'raw' not in r['label'].lower(), r['label'])):  # regional table first, EU if the regional records all fail the gates; equal factors: plain raw meat before cooked or prepared records
                    note = f" Weighed as edible meat: {edible:.0%} of the bone-in weight (USDA refuse via ReFED)." if bone else ''
                    if judge(item, w, ref_cf, label, r, rule['mass_factor'] * edible, 'table:' + rule['name'], rule['reason'] + note, edible=edible,
                             allow=(('processed_to_processed_meat', 'processed_meat_to_fresh_meat') if processed_ok else ()) + (('white_fish_proxy_group',) if rule['name'] in rules.INDICATIVE else ()) + ('fish_swap_only_via_table',)):
                        break
        if bone:
            continue
        if PLANT_PROTEIN and PULSE_DISH.search(title) and role_of(label) in ('poultry', 'red_meat') and not PROCESSED_MEAT.search(label + ' ' + line) and not OFFAL.search(label):
            tier = regional_first([r for r in pool if PULSE.search(r['label'])], region, dataset)[0]
            for r in sorted(tier, key=lambda r: (r['cf'], r['label']))[:3]:
                if judge(item, w, ref_cf, label, r, 0.5, 'table:half_meat_half_lentils', 'half of the meat replaced by the same weight of cooked lentils', partial=True):
                    break
        if use_search:
            src_role = role_of(label)
            hits = [r for r in pool if r['cf'] <= LOWER_RATIO * ref_cf and role_of(r['label']) is not None
                    and (r.get('f') or {}).get('base_identity') and fits(r) and not role_problems(label, r['label'], lenient=True)
                    and not local_problems(label, r['label'], solid_fat)]
            if src_role:
                tier = regional_first(hits, region, dataset)[0]
                by_word = {}
                for r in tier:
                    fw = re.match(r'\s*([A-Za-z]+)', r['label'])
                    by_word.setdefault(fw.group(1).lower() if fw else r['label'], []).append(r)
                for _, options in sorted(by_word.items(), key=lambda kv: min(r['cf'] for r in kv[1]))[:8]:
                    for r in sorted(options, key=lambda r: (r['cf'], r['label']))[:10]:  # ponytail: 10 per food word; salted variants can fill a smaller cap
                        if judge(item, w, ref_cf, label, r, 1.0, 'search:related_food', 'a related food with a clearly lower footprint'):
                            break
    cands.sort(key=lambda c: -c['saved_per_serving'])
    seen, uniq = set(), []
    for c in cands:
        k = (c['position'], (re.findall(r'[a-z]+', c['to_label'].lower()) or [''])[0])
        if k not in seen:
            seen.add(k); uniq.append(c)
    out.update(hotspots=hotspots, rejected=rejected, rejected_examples=rej_examples, candidates=uniq[:8])
    if not uniq:
        out['why_no_suggestion'] = why_no_suggestion(hotspots, rejected)
    return dict(out, status='adapted' if uniq else 'no_candidate')


from .sustainability_reasons import WHY  # noqa: E402,F401  (kept here: the offline runs read sa.WHY)


def why_no_suggestion(hotspots, rejected):
    open_spots = [h for h in hotspots if 'skipped' not in h]
    if not open_spots:
        skip = (max(hotspots, key=lambda h: h['co2_share'])['skipped'] if hotspots else 'low_impact_plant_food').split(':')[0]
        return {'low_impact_plant_food': 'already_low_impact', 'protected': 'main_food_protected', 'fallback_factor': 'footprint_estimate_only',
                'rice_is_the_dish': 'main_food_defines_the_dish', 'cream_is_structural': 'main_food_defines_the_dish', 'eaten_raw': 'main_food_defines_the_dish',
                'bone_in': 'no_lower_impact_option', 'fat_trimming': 'no_lower_impact_option', 'amount_not_stated': 'amount_not_stated'}.get(skip, 'prepared_product')
    if 'allergen_or_diet' in rejected:
        return 'alternatives_add_allergen'
    if 'below_co2_floor' in rejected:
        return 'saving_too_small'
    return 'no_lower_impact_option'
