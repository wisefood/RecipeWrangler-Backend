"""Small-gain nutrition adaptation: amount cuts, curated food swaps, extra vegetables, combinations.

Every candidate is recalculated from the saved profile detail rows with the baseline's own arithmetic; the baseline
totals, grade and fruit/vegetable/legume share must reproduce exactly first, otherwise nothing is proposed.
Deterministic: no model calls, no writes. Gates follow the owner's 12 Nov 2026 direction (see docs/ADAPTATION.md).
"""
import itertools
import json
import math
import re
from collections import Counter
from fractions import Fraction

from .models import ComparisonSnapshot
from .nutrition_benefit import MIN_REDUCTION, MIN_RELATIVE_REDUCTION, ROUNDING_TOLERANCE, assess_nutrition_benefit
from .objective_approval import production_score_context
from recipe_wrangler.utils.fruit_vegetable_content import fruits_veg_legumes_percent, is_fruit_vegetable_legume_or_nut
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values

from . import small_gain_rules as swap_rules
from .food_roles import role_problems

GRAPH_POSITIONS_PER_METRIC = 3
TARGETS = ('saturated_fat_g', 'sodium_mg', 'sugar_g', 'energy_kcal')
GUARDED = ('energy_kcal', 'sugar_g', 'saturated_fat_g', 'sodium_mg', 'fibre_g', 'protein_g')
REPORTED = ('energy_kcal', 'sugar_g', 'saturated_fat_g', 'sodium_mg', 'fibre_g', 'protein_g')
MATERIAL = 0.05  # same bound as the codebase's bounded-trade-off policy (prototype_nutrient_v1)
MATERIAL_ABS = {'energy_kcal': 25.0, 'sugar_g': 2.5, 'saturated_fat_g': 0.5, 'sodium_mg': 50.0, 'fibre_g': 1.0, 'protein_g': 1.0}
SMALL_GAIN = 0.05  # A->A: a real but small gain on any score nutrient counts (owner, 12 Nov)
TOTAL_KEYS = {'energy_kcal': 'energy_kcal', 'protein_g': 'protein_g', 'carbs_g': 'carbohydrate_g', 'fat_g': 'fat_g',
              'sugar_g': 'sugar_g', 'saturated_fat_g': 'saturated_fat_g', 'sodium_mg': 'sodium_mg', 'fibre_g': 'fibre_g'}
SCORE_MAP = {'energy_kcal': ('energy', 4.184), 'sugar_g': ('sugar', 1), 'saturated_fat_g': ('saturated_fats', 1),
             'sodium_mg': ('sodium', 1), 'fibre_g': ('fibers', 1), 'protein_g': ('proteins', 1)}
GRADE = {'Nutriscore_' + g: i for i, g in enumerate('ABCDE')}

BAKED = re.compile(r'\b(cakes?|buns?|scones?|muffins?|biscuits?|cookies?|pastry|pastries|tarts?|tartlets?|loaf|brownies?|crumble|flapjacks?|'
                   r'shortbread|pancakes?|crêpes?|crepes?|waffles?|strudel|gingerbread|torte|meringues?|pogača|potica|kalács|pogácsa|'
                   r'rétes|gibanica|lepinja|quiche|soda bread|brown bread|wholemeal bread|banana bread|bread rolls?|focaccia|pizza)\b', re.I)
NUMERIC = re.compile(r'(\d|½|¼|¾|⅓|⅔|\bone\b|\btwo\b|\bthree\b|\bhalf\b)', re.I)

# category: (label regex, source-line regex, reduction fraction, reason)
# Owner, 12 Nov: no cuts of salt, oil/fat, cheese, cream or processed meat; a few grams less is invisible to a cook.
# Those foods get swaps instead (small_gain_rules: lower-fat cheese, half-fat creme fraiche, lean bacon, light spread...).
AMOUNT_RULES = [
    ('sugar', r'^(sugar|honey|syrup|golden syrup|maple syrup|treacle|caster sugar|brown sugar|icing sugar|demerara)\b',
     r'\b(sugar(?! ?snap)|honey|syrup|treacle)\b', 0.25,
     'use a quarter less sugar/honey; sweetness and texture stay recognisable'),
    ('sauce', r'\b(mayonnaise|mayo|ketchup|brown sauce|barbecue sauce|bbq sauce|sweet chilli|salad cream|dressing|pesto|hoisin|teriyaki)\b',
     r'\b(mayonnaise|mayo|ketchup|brown sauce|barbecue|bbq|sweet chilli|salad cream|dressing|pesto|hoisin|teriyaki)\b', 1 / 3,
     'use a third less of the sauce/dressing'),
    ('sweet_spread', r'\b(jam|marmalade|chocolate spread|hazelnut spread|nutella|lemon curd|conserve)\b',
     r'\b(jam|marmalade|chocolate spread|hazelnut spread|nutella|lemon curd|conserve)\b', 1 / 3, 'use a third less jam/sweet spread'),
    ('chocolate', r'^(?!.*\b(spread|drink|cake|biscuit|milk powder|cocoa powder)\b).*\bchocolate\b', r'\bchocolate\b(?!.*\bspread\b)', 0.25,
     'use a quarter less chocolate'),
    ('nut_butter', r'\b(peanut|nut|almond|cashew) butter\b', r'\b(peanut|nut|almond|cashew) butter\b', 1 / 3, 'use a third less nut butter'),
    ('salty_condiment', r'^(?!.*\b(dissolved|made up|prepared|diluted|ready to use|liquid|home ?made|reconstituted)\b).*\b(soy sauce|fish sauce|stock cubes?|bouillon (cube|powder|granules)|cubes?|gravy granules|stock powder|granules|concentrate)\b',
     r'\b(soy sauce|fish sauce|stock cube|bouillon|gravy granules|stock powder|cube)\b', 0.33,
     'use a third less of the salty seasoning (stock cube/soy/fish sauce)'),
]

# Known matcher pitfalls: (source pattern, matched-label pattern that contradicts it)
PITFALLS = [
    (r'\b(tins?|tinned|canned)\b|\b(a|one|\d+|x)\s*(\d+\s*(g|ml)\s*)?cans?\b', None, r'\b(canned|tinned|can|drained|coconut milk|condensed|evaporated|passata|baked beans)\b', 'source says tinned, record is not canned'),
    (r'\b(cooked|leftover)\b', None, r'\b(cooked|boiled|roast\w*|baked|fried|grilled|stewed|braised|steamed|poached)\b', 'source says cooked, record is raw'),
    (r'\b(low[- ]fat|reduced[- ]fat|half[- ]fat|skimmed|semi[- ]skimmed|fat[- ]free|lean)\b|\b(light|lite) (cream|mayo\w*|cheese|spread|butter|cr[eè]me|coconut|sour cream|cream cheese)', None,
     r'\b(low|reduced|half|light|lite|skimmed|fat[- ]free|lean|diet)\b|\d+(\.\d+)? ?%', 'source says lower-fat, record is regular'),
    (r'\blamb\b.*\bleg\b|\bleg of lamb\b', None, r'\bleg\b', 'lamb leg matched to a different cut'),
    (r'\bpork\b.*\bloin\b', r'\bbelly\b', None, 'pork loin matched to belly'),
    (r'\bbacon\b', r'\bham\b', None, 'bacon matched to ham'),
    (r'\bcottage cheese\b', r'\bmascarpone\b', None, 'cottage cheese matched to mascarpone'),
    (r'\bbutter ?beans?\b.*\b(tin|can)', None, r'\b(canned|tinned|can|boiled)\b', 'tinned butter beans matched raw'),
    (r'\bstock\b(?!.*\b(cube|powder|granule|concentrate|pot|paste)\b)', r'\b(cube|concentrate|powder|granule|dry|dried|paste|bouillon)\b',
     None, 'liquid stock matched to a concentrate'),
    (r'\bcream\b(?!.*ice)', r'\bice ?cream\b', None, 'cream matched to ice cream'),
    (r'\b(parma|prosciutto|serrano|cured|air[- ]dried) ham\b|\bprosciutto\b', r'\b(raw|gammon|boiled|cooked)\b', None, 'cured ham matched to a raw or cooked ham'),
    (r'\b(low|reduced)[- ]?(salt|sodium)\b', None, r'\b(low|reduced|less)[- ]?(salt|sodium)\b|\bunsalted\b', 'source says low-salt, record is regular'),
]


def is_baked(title, steps, snapshot):
    mass = sum(i.quantity.weight_g for i in snapshot.ingredients)
    flour = sum(i.quantity.weight_g for i in snapshot.ingredients if re.search(r'\bflour\b', i.original_text or '', re.I))
    return bool(BAKED.search(title)) or bool(re.search(r'\b(bake|oven)\b', steps, re.I) and flour >= 0.15 * mass)


def pitfall(source, label):
    found = []
    for src, bad, need, why in PITFALLS:
        if re.search(src, source, re.I) and ((bad and re.search(bad, label, re.I)) or (need and not re.search(need, label, re.I))):
            found.append(why)
    return found


DIET_GROUPS = ('vegan', 'vegetarian', 'pescatarian')


def restriction_problems(parts, line_texts):
    """Hard safety gate: a change may not add an allergen the original recipe does not already contain, nor put a food that a
    diet excludes into a recipe that respected it. Detection is lexical, so an allergen that is only 'possible' on the new food
    counts as added. Amount cuts add no food. Returns a list of reasons, empty when safe."""
    from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names
    from recipe_wrangler.utils.consumer_suitability import dietary_keyword_evidence
    new_foods = [p['candidate_label'] for p in parts if p.get('candidate_label') and p.get('operation') != 'REDUCE_AMOUNT']
    if not new_foods:
        return []
    line_texts = list(line_texts) + [p['matched_label'] for p in parts if p.get('matched_label')]  # the replaced food counts as already in the recipe
    have = {r['allergen'] for r in detect_allergen_evidence_from_names(line_texts)}
    reasons = [f"{label}: adds allergen {a}" for label in new_foods
               for a in sorted({r['allergen'] for r in detect_allergen_evidence_from_names([label])} - have)]
    for group in DIET_GROUPS:
        if any(dietary_keyword_evidence(t, group)[1] == 'contains' for t in line_texts):
            continue  # the original recipe already breaks this diet
        reasons += [f'{label}: not {group}' for label in new_foods if dietary_keyword_evidence(label, group)[1] == 'contains']
    return reasons


def score(values_total, mass, fvl, food_type):
    values = {label: values_total[k] / mass * 100 * f for k, (label, f) in SCORE_MAP.items()}
    values['fruit_percentage'] = fvl
    return compute_nutri_score_breakdown_from_values(values, food_type)


def totals(snapshot, weights):
    out = dict.fromkeys(TOTAL_KEYS, 0.0)
    for item in snapshot.ingredients:
        if item.composition is None:
            return None
        for k in TOTAL_KEYS:
            v = item.composition.values_per_100g.get(k)
            if v is None and k in GUARDED:
                return None  # a score nutrient is missing: no recalculation
            out[k] = None if v is None or out[k] is None else out[k] + v * weights[item.position] / 100
    return out


def gates(before, after, grade_before, grade_after):
    """User criteria: meaningful_nutrient_v1 benefit floors OR better letter; no material worsening."""
    tradeoffs, material = [], []
    for m in GUARDED:
        change = after[m] - before[m]
        worse = change < 0 if m in ('fibre_g', 'protein_g') else change > 0
        if worse and abs(change) > ROUNDING_TOLERANCE[m] + 1e-12:
            # Material = >5% relative AND above half the policy's absolute floor (not a fraction of a gram on a tiny base).
            (material if abs(change) > before[m] * MATERIAL + 1e-12 and abs(change) > MATERIAL_ABS[m] else tradeoffs).append(
                {'metric': m, 'delta_per_serving': round(change, 4), 'relative': round(change / before[m], 4) if before[m] else None})
    meaningful = []
    for m in TARGETS:
        reduction = before[m] - after[m]
        if before[m] > 0 and reduction + 1e-9 >= MIN_REDUCTION[m] and reduction / before[m] + 1e-12 >= MIN_RELATIVE_REDUCTION:
            meaningful.append({'metric': m, 'reduction_per_serving': round(reduction, 3), 'relative': round(reduction / before[m], 4)})
    worse_letter = GRADE[grade_after] > GRADE[grade_before]
    # Every real improvement (beyond rounding): lower negatives, higher fibre/protein.
    improvements = []
    for m in GUARDED:
        gain = (after[m] - before[m]) if m in ('fibre_g', 'protein_g') else (before[m] - after[m])
        if gain > ROUNDING_TOLERANCE[m] + 1e-12 and before[m] > 0:
            improvements.append({'metric': m, 'gain_per_serving': round(gain, 4), 'relative': round(gain / before[m], 4)})
    # A letter flip only counts with a real change: >=10% (the policy's relative floor) on any score nutrient,
    # lower negatives or higher fibre/protein - not a grade-boundary flip from a trivial edit.
    better_letter = GRADE[grade_after] < GRADE[grade_before] and any(x['relative'] + 1e-12 >= MIN_RELATIVE_REDUCTION for x in improvements)
    # Owner rule (12 Nov): an A recipe staying A counts with a small real gain (>=5% on any score nutrient).
    # Owner, 12 Nov night: "even a small change in nutrition" counts -> >=5% real gain on any score nutrient, every grade.
    a_grade_small_gain = any(x['relative'] >= SMALL_GAIN for x in improvements)
    # Owner, 12 Nov night ("not too strict"): B and below count a real >=10% gain on any score nutrient, even under the absolute floor.
    relative_gain = grade_before != 'Nutriscore_A' and any(x['relative'] + 1e-12 >= MIN_RELATIVE_REDUCTION for x in improvements)
    passed = (bool(meaningful) or better_letter or a_grade_small_gain or relative_gain) and not material and not worse_letter
    # Strict check under the named policy, for reporting (rounding-only guards on all eight nutrients).
    strict = any(assess_nutrition_benefit(before, after, m, {'nutri_score': grade_before}, {'nutri_score': grade_after},
                                          policy='meaningful_nutrient_v1')['status'] == 'approved_input_objective' for m in TARGETS)
    reasons = [] if passed else (['nutrition_grade_regression'] if worse_letter else []) + \
        (['material_worsening:' + x['metric'] for x in material]) + \
        ([] if meaningful or better_letter or a_grade_small_gain or relative_gain else ['no_meaningful_improvement'])
    return {'passed': passed, 'meaningful_reductions': meaningful, 'better_letter': better_letter,
            'a_grade_small_gain': a_grade_small_gain and not meaningful and not better_letter,
            'relative_gain_only': relative_gain and not meaningful and not better_letter, 'improvements': improvements,
            'tradeoffs_within_5pct': tradeoffs, 'tradeoff_note': 'non-material: <=5% or below half the absolute floor', 'material_worsening': material,
            'passes_strict_meaningful_nutrient_v1': strict, 'reasons': reasons}


def per_serving(total, serves):
    return {k: None if total[k] is None else total[k] / serves for k in TOTAL_KEYS}


def instructions_text(recipe):
    steps = recipe.get('instructions') or []
    return ' / '.join(s if isinstance(s, str) else json.dumps(s) for s in steps) if isinstance(steps, list) else str(steps)


def mentions(text, word):
    # The sentence naming the fat plus the next one ("Add the oil. Roast for 50 minutes").
    parts = re.split(r'(?<=[.!?])\s+|\n| / ', text)
    return [' '.join(parts[n:n + 2]) for n, s in enumerate(parts) if re.search(r'\b' + re.escape(word), s, re.I)]


def pick_target(records, rule, base_values, dataset, region, base_label):
    """Median eligible released record for the rule (same dataset preferred) - never the most favourable one."""
    pool = [r for r in records if r['collection'] in ('nutritional_ingredients_eu', 'nutritional_ingredients_' + region)
            and re.search(rule['target'], r['label'] or '', re.I)
            and (rule['name'] == 'reduced_sugar_salt_baked_beans' or not re.search(swap_rules.COMPOSITE, r['label'], re.I))
            and swap_rules.compatible_state(base_label, r['label'], rule['name']) and swap_rules.keeps_type(base_label, r['label'])
            and all(isinstance((r['v'] or {}).get(m), (int, float)) for m in GUARDED)]
    same = [r for r in pool if (r['dataset'] or '').casefold() == (dataset or '').casefold()]
    pool = sorted(same or pool, key=lambda r: (r['v'][rule['pick_metric']], r['candidate_id']))
    if not pool:
        return None, 'no_eligible_released_record'
    target = pool[(len(pool) - 1) // 2]
    better = target['v'][rule['pick_metric']] > base_values.get(rule['pick_metric'], 0) if rule['pick_metric'] in ('fibre_g', 'protein_g') \
        else target['v'][rule['pick_metric']] < base_values.get(rule['pick_metric'], 0)
    return (target, None) if better else (None, 'median_record_not_better_on_' + rule['pick_metric'])



def _facets(record):
    return record.get('f') or {}


def _dedupe(rows):
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r['candidate_id'], r['collection'])):  # order-independent: duplicates resolve the same way every run
        key = (re.sub(r'\W+', ' ', (r['label'] or '').lower()).strip(), tuple(round(r['v'].get(m) or 0, 2) for m in GUARDED))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _clean(r, base_label):
    label = r['label'] or ''
    bad = set(re.findall(swap_rules.SIMILAR_EXCLUDE, swap_rules.scan_label(label), re.I)) - set(re.findall(swap_rules.SIMILAR_EXCLUDE, swap_rules.scan_label(base_label), re.I))
    brined = (_facets(r).get('base_identity') in swap_rules.ANIMAL and _facets(r).get('preparation_state') == 'raw'
              and isinstance((r['v'] or {}).get('sodium_mg'), (int, float)) and r['v']['sodium_mg'] > swap_rules.BRINED_SODIUM_MG)
    return (not re.search(swap_rules.COMPOSITE, swap_rules.scan_label(label), re.I) and not bad and not brined
            and all(isinstance((r['v'] or {}).get(m), (int, float)) for m in GUARDED))


BONE_IN = re.compile(r'\b(on the bone|bone[- ]in|with bone|chops?|cutlets?|ribs?|drumsticks?|wings?|leg quarters?)\b', re.I)
BONELESS = re.compile(r'\b(boneless|without bones?|off the bone|bone removed|deboned)\b', re.I)
MINCED = re.compile(r'\b(mince|minced|ground)\b', re.I)


def bone_in(text):
    """Same-weight swaps from a bone-in cut to a boneless one overstate the edible meat, so they are not offered."""
    return bool(BONE_IN.search(text or '')) and not BONELESS.search(text or '')


def similar_targets(records, identity, state, base_label, base_per100, dataset, region, metric, min_gain_per100, source_text=''):
    """Same food in the same state, any part or product: median of the clearly better distinct records.

    "Clearly better" = at least 15% lower and lower by the policy's per-serving floor, so database variants of the
    same plant food (onion vs frozen onion) never count. The baseline's own descriptors (skinless, lean, brown) stay."""
    pool = _dedupe([r for r in records if r['collection'] in ('nutritional_ingredients_eu', 'nutritional_ingredients_' + region)
                    and _facets(r).get('base_identity') == identity and _facets(r).get('preparation_state') == state
                    and _clean(r, base_label) and swap_rules.keeps_descriptors(base_label, r['label'] or '')
                    and r['v'][metric] < 0.85 * base_per100[metric] and base_per100[metric] - r['v'][metric] >= min_gain_per100
                    and (r['label'] or '') != base_label
                    and not (MINCED.search(base_label + ' ' + source_text) and not MINCED.search(r['label'] or ''))  # a minced food stays minced
                    and not bone_in(base_label + ' ' + source_text)])
    same = [r for r in pool if (r['dataset'] or '').casefold() == (dataset or '').casefold()]
    use = sorted(same or pool, key=lambda r: (r['v'][metric], r['candidate_id']))
    return (use[(len(use) - 1) // 2], not same) if use else (None, False)


def role_targets(records, spec, region):
    identity, part, label_rx = spec
    pool = _dedupe([r for r in records if r['collection'] in ('nutritional_ingredients_eu', 'nutritional_ingredients_' + region)
                    and _facets(r).get('base_identity') == identity and _facets(r).get('food_part') == part
                    and _facets(r).get('preparation_state') == 'raw' and _clean(r, '')
                    and (not label_rx or re.search(label_rx, r['label'] or '', re.I))])
    use = sorted(pool, key=lambda r: (r['v']['saturated_fat_g'], r['candidate_id']))
    return use[(len(use) - 1) // 2] if use else None


def make_evaluator(snapshot, profile):
    """Baseline recalculation shared by every facet (nutrition, sustainability, cost, time). Returns (ns, error): ns holds
    evaluate(edits, record) which recomputes the six score nutrients and the Nutri-Score for edits {position: [(weight_g, values_per_100g or None)]},
    plus details, mass, base, base_score, context and zero_filled. error is set when the baseline does not reproduce exactly."""
    # Recalculate from the saved profile detail rows: the exact contributions production summed (incl. reviewed fills).
    context = production_score_context(snapshot, profile)
    if context is None:
        return None, 'recalculation_unavailable:no_score_context'
    details = {d.get('position', n): d for n, d in enumerate(profile['details'])}
    mass = sum(d['weight_g'] for d in details.values())
    # Production sums a missing score nutrient as zero (totals reproduce that way); follow it and flag it.
    base = {k: sum(d.get(k) or 0.0 for d in details.values()) if k in GUARDED or all(d.get(k) is not None for d in details.values()) else None
            for k in TOTAL_KEYS}
    zero_filled = sorted({k for d in details.values() for k in GUARDED if d.get(k) is None})
    saved = profile['clean_totals']
    if any(base[k] is None for k in GUARDED) or not math.isclose(mass, context.total_weight_g, rel_tol=1e-9) or not all(
            math.isclose(base[k], saved[v], rel_tol=1e-6, abs_tol=1e-6) for k, v in TOTAL_KEYS.items() if saved.get(v) is not None and base[k] is not None):
        return None, 'recalculation_unavailable:baseline_totals_not_reproduced'
    base_score = score(base, mass, context.fruit_veg_legume_percent, context.food_type)
    if not math.isclose(fruits_veg_legumes_percent(profile['details']), context.fruit_veg_legume_percent, rel_tol=1e-6, abs_tol=1e-6):
        return None, 'recalculation_unavailable:fvl_share_not_reproduced'
    if base_score['nutri_score'] != profile['nutri_score']['nutri_score']:
        return None, 'recalculation_unavailable:baseline_grade_not_reproduced'
    def evaluate(edits, record):
        """edits: {position: [(weight_g, values_per_100g or None=same food scaled)]}; FVL membership kept as baseline."""
        total = dict(base)
        new_mass, fvl_mass = mass, context.fruit_veg_legume_percent * mass / 100
        for pos, parts in edits.items():
            d = details[pos]
            for k in TOTAL_KEYS:
                if total[k] is None:
                    continue
                added = [(d.get(k) or 0.0) * w / d['weight_g'] if values is None else values.get(k) and values[k] * w / 100
                         for w, values in parts]
                total[k] = None if any(a is None for a in added) and k not in GUARDED else total[k] - (d.get(k) or 0.0) + sum(a or 0.0 for a in added)
            new_mass += sum(w for w, _ in parts) - d['weight_g']
            if is_fruit_vegetable_legume_or_nut(d):
                fvl_mass += sum(w for w, _ in parts) - d['weight_g']
        after_score = score(total, new_mass, fvl_mass / new_mass * 100, context.food_type)
        b, a = per_serving(base, snapshot.serves), per_serving(total, snapshot.serves)
        return dict(record, before_per_serving=b, after_per_serving=a,
                    grade_before=base_score['nutri_score'], grade_after=after_score['nutri_score'],
                    score_before=base_score['score'], score_after=after_score['score'],
                    gates=gates(b, a, base_score['nutri_score'], after_score['nutri_score']))
    return dict(evaluate=evaluate, details=details, mass=mass, base=base, base_score=base_score, context=context, zero_filled=zero_filled), None


def change_candidates(case, snapshot, profile, recipe, records, more_veg=False):
    """Amount reductions and food swaps on single occurrences, then combinations; baseline arithmetic only."""
    ns, error = make_evaluator(snapshot, profile)
    if ns is None:
        return [], error
    evaluate, details, mass, base, base_score, context, zero_filled = (ns[k] for k in ('evaluate', 'details', 'mass', 'base', 'base_score', 'context', 'zero_filled'))
    title, steps = recipe.get('title') or case['title'], instructions_text(recipe)
    baked = is_baked(title, steps, snapshot)
    out, valid = [], []

    for item in snapshot.ingredients:
        if item.position not in details or not math.isclose(details[item.position]['weight_g'], item.quantity.weight_g, rel_tol=1e-9):
            continue
        d = details[item.position]
        label = (item.composition.provenance.get('candidate') or {}).get('document', '') if item.composition else \
            (d.get('matched_nutritional_ingredient') or d.get('name') or '')
        line = item.original_text or ''
        if d['weight_g'] <= 0:
            continue
        if more_veg:  # fallback pass: bulk out a savoury dish with half as much again of a measured vegetable/pulse
            if (is_fruit_vegetable_legume_or_nut(d) and d['weight_g'] >= 30 and NUMERIC.search(line)
                    and not re.search(VEG_EXCLUDE, label + ' ' + line, re.I) and not baked and not re.search(SWEET_DISH, title, re.I)
                    and not pitfall(line, label)):
                record = {'position': item.position, 'source_line': line, 'matched_label': label, 'category': 'more_veg',
                          'operation': 'INCREASE_VEG', 'weight_before_g': d['weight_g'], 'weight_after_g': round(d['weight_g'] * 1.5, 2),
                          'reduction_fraction': -0.5, 'reason': 'add half as much again of this vegetable/pulse to bulk out the dish'}
                out.append(evaluate({item.position: [(d['weight_g'] * 1.5, None)]}, record))
            continue
        per100 = {k: (d.get(k) or 0.0) * 100 / d['weight_g'] for k in GUARDED}  # effective values incl. reviewed fills
        common = ['match_pitfall:' + p for p in pitfall(line, label)]
        # --- amount reductions
        for category, label_re, line_re, fraction, why in AMOUNT_RULES:
            if not (re.search(label_re, label, re.I) and re.search(line_re, line, re.I)):
                continue
            record = {'position': item.position, 'source_line': line, 'matched_label': label, 'category': category,
                      'operation': 'REDUCE_AMOUNT', 'weight_before_g': item.quantity.weight_g}
            skip = list(common)
            if not NUMERIC.search(line) or re.search(r'\b(to taste|pinch|dash|optional|to serve|for dusting|for greasing)\b', line, re.I):
                skip.append('amount_not_explicitly_measured')
            if re.search(r'\b(tablespoons?|tbsp|teaspoons?|tsp|cups?|g|ml)\b.*\b(and|plus|\+)\s*(\d|½|¼|¾|a |one )', line, re.I):
                skip.append('compound_amount_line:baseline_weight_unverified')
            if re.search(r'\b(low|reduced)[- ]?(salt|sodium|fat|sugar)\b|\bunsalted\b|\blight\b', line, re.I) and category == 'salty_condiment':
                skip.append('already_reduced_product')
            if category in ('chocolate', 'sauce', 'sweet_spread', 'nut_butter') and \
                    re.search(re.escape(re.search(line_re, line, re.I).group(0)), title, re.I):
                fraction, why = 0.2, why.split(';')[0].replace('a third', 'a fifth').replace('a quarter', 'a fifth').replace('about a fifth', 'a fifth') + \
                    ' (it is the dish identity, so only a fifth)'
            if category == 'sugar' and re.search(r'\b(jam|meringue|caramel|toffee|fudge|syrup cake|marmalade|candied|preserve)\b', title, re.I):
                skip.append('sugar_is_structural_or_preservative')
            if item.quantity.weight_g * fraction < 0.5:
                skip.append('reduction_too_small_to_matter')
            if skip:
                out.append(dict(record, status='rejected_culinary_or_match', reasons=skip))
            else:
                edit = {item.position: [(item.quantity.weight_g * (1 - fraction), None)]}
                record.update(weight_after_g=round(item.quantity.weight_g * (1 - fraction), 2), reduction_fraction=round(fraction, 3), reason=why)
                valid.append((edit, record))
                out.append(evaluate(edit, record))
            break
        # --- food swaps (same role, real released target record)
        for rule in swap_rules.RULES:
            if not (re.search(rule['line'], line, re.I) and re.search(rule['base'], label, re.I)):
                continue
            record = {'position': item.position, 'source_line': line, 'matched_label': label, 'category': rule['name'],
                      'operation': 'SWAP_FOOD', 'weight_before_g': item.quantity.weight_g}
            skip = list(common)
            ctx = rule['exclude_context']
            if ctx == 'SPREAD_ONLY':
                uses = [u for w in ('butter', 'margarine', 'spread') for u in mentions(steps, w)]
                if not uses or not all(re.search(r'\b(spread|serve|serving|top|toast)', u, re.I)
                                       and not re.search(r'\b(fry|saut|flour|dough|mix|beat|cream|rub|melt|heat|bake|roux|grease)', u, re.I) for u in uses):
                    skip.append('role_mismatch:light spread only where the fat is spread on bread')
            elif ctx == 'BAKED_OR_NOT_FRYING':
                uses = mentions(steps, re.search(r'butter|margarine|lard|ghee|dripping', label, re.I).group(0))
                if baked or re.search(r'\b(confit|rillettes?|rilette|submerge\w*|seal\w*|potted)\b', title + ' ' + steps, re.I) or not any(re.search(r'\b(fry|fried|frying|saut|soften|sweat|brown|sear|melt\w* .*pan|heat)', u, re.I) for u in uses) \
                        or any(re.search(r'\b(flour|dough|pastry|rub|cream|beat|sauce|spread|serve|confit|submerge|seal|set\b)', u, re.I) for u in uses):
                    skip.append('role_mismatch:butter not used only for frying')
            elif ctx and re.search(ctx, title + ' ' + steps, re.I):
                skip.append('role_mismatch:' + rule['name'] + '_not_suitable_in_this_dish')
            if re.search(swap_rules.ALREADY, line, re.I):
                skip.append('repairs_baseline_only:source_line_already_asks_for_this')
            target, why_not = (None, None) if skip else pick_target(records, rule, per100, item.composition.source_dataset if item.composition else None, case['region'], label)
            if why_not:
                skip.append(why_not)
            if target:
                skip += ['target_differs_beyond_swap:' + m for m in swap_rules.unearned(rule['name'], per100, target['v'])]
            if skip:
                out.append(dict(record, status='rejected_culinary_or_match', reasons=skip))
                if all(x.startswith(('no_eligible_released_record', 'median_record_not_better', 'role_mismatch')) for x in skip):
                    continue  # e.g. no light sour cream record: try yogurt next
                break
            w = item.quantity.weight_g
            if rule['name'] == 'half_wholemeal_flour':
                edit = {item.position: [(w * 0.5, None), (w * 0.5, target['v'])]}
            else:
                edit = {item.position: [(w * rule['mass_factor'], target['v'])]}
            record.update(candidate_label=target['label'], candidate_id=target['candidate_id'], candidate_collection=target['collection'],
                          candidate_dataset=target['dataset'], weight_after_g=round(w * rule['mass_factor'], 2), reason=rule['reason'],
                          per_100g={'from': {k: round(per100[k], 3) for k in GUARDED},
                                    'to': {k: target['v'].get(k) for k in GUARDED}})
            valid.append((edit, record))
            out.append(evaluate(edit, record))
            break
    if more_veg:
        return out, None
    # --- loose similar-food search for the biggest contributors: same food any part, and lean-protein role swaps
    shares = {}
    for pos, d in details.items():
        shares[pos] = max(((d.get(m) or 0) / (base[m] or 1)) for m in TARGETS)
    for item in sorted(snapshot.ingredients, key=lambda i: -shares.get(i.position, 0))[:4]:
        d = details.get(item.position)
        if d is None or d['weight_g'] < 40 or shares[item.position] < 0.15 or item.composition is None:
            continue
        label = (item.composition.provenance.get('candidate') or {}).get('document', '')
        line = item.original_text or ''
        if pitfall(line, label) or not NUMERIC.search(line) or re.search(r'(^|,\s*)fat\b', label, re.I):
            continue  # unmeasured, known pitfall, or a baseline that matched a pure-fat record
        facts = item.composition.properties
        identity = facts['base_identity'].value if 'base_identity' in facts and facts['base_identity'].evidence.status == 'stated' else None
        state = facts['preparation_state'].value if 'preparation_state' in facts and facts['preparation_state'].evidence.status == 'stated' else None
        if not identity or not state:
            continue
        per100 = {k: (d.get(k) or 0.0) * 100 / d['weight_g'] for k in GUARDED}
        proposals = []
        for metric in ('saturated_fat_g', 'sodium_mg', 'energy_kcal'):  # sugar differences between plant varieties are noise
            if per100[metric] > 0:
                target, cross = similar_targets(records, identity, state, label, per100, item.composition.source_dataset, case['region'], metric,
                                                   MIN_REDUCTION[metric] * snapshot.serves * 100 / d['weight_g'], line)
                if target is not None:
                    proposals.append(('similar_part', target, {'cross_database': cross}, 'use a leaner/lighter cut or version of the same food (' + metric.replace('_g', '').replace('_mg', '') + ')'))
        for rs in swap_rules.ROLE_SWAPS:
            if identity in rs['from'] and state == 'raw' and not MINCED.search(label + ' ' + line) and not bone_in(label + ' ' + line):  # a whole breast is not minced meat
                for spec in rs['to']:
                    target = role_targets(records, spec, case['region'])
                    if target is not None:
                        proposals.append(('protein_swap', target, {'changes_dish_identity': True}, rs['reason'] + ': ' + spec[0] + ' ' + spec[1]))
        done = set()
        for category, target, flags, why in proposals:
            if target['candidate_id'] in done:
                continue
            done.add(target['candidate_id'])
            record = {'position': item.position, 'source_line': line, 'matched_label': label, 'category': category, 'operation': 'SWAP_FOOD',
                      'weight_before_g': item.quantity.weight_g, 'weight_after_g': round(item.quantity.weight_g, 2), 'candidate_label': target['label'],
                      'candidate_id': target['candidate_id'], 'candidate_collection': target['collection'], 'candidate_dataset': target['dataset'],
                      'reason': why, **flags, 'per_100g': {'from': {k: round(per100[k], 3) for k in GUARDED}, 'to': {k: target['v'].get(k) for k in GUARDED}}}
            edit = {item.position: [(item.quantity.weight_g, target['v'])]}
            valid.append((edit, record))
            out.append(evaluate(edit, record))
    # --- every line of one reduction category together (one culinary instruction)
    groups = {}
    for edit, record in valid:
        if record['operation'] == 'REDUCE_AMOUNT':
            groups.setdefault(record['category'], []).append((edit, record))
    combined = []
    for category, entries in groups.items():
        if len(entries) > 1:
            edit = {k: v for e, _ in entries for k, v in e.items()}
            combined.append((edit, {'position': [r['position'] for _, r in entries], 'category': category, 'operation': 'REDUCE_AMOUNT',
                                    'source_line': ' + '.join(r['source_line'] for _, r in entries),
                                    'matched_label': ' + '.join(r['matched_label'] for _, r in entries),
                                    'weight_before_g': [r['weight_before_g'] for _, r in entries],
                                    'weight_after_g': [r['weight_after_g'] for _, r in entries],
                                    'reduction_fraction': entries[0][1]['reduction_fraction'],
                                    'reason': entries[0][1]['reason'] + ' (every ' + category + ' line)'}))
    for edit, record in combined:
        out.append(evaluate(edit, record))
    # --- combinations of up to three helpful changes on different ingredients
    helpful = []
    for edit, record in valid + combined:
        single = next(c for c in out if c.get('position') == record['position'] and c.get('category') == record['category']
                      and c.get('operation') == record['operation'] and 'gates' in c)
        g = single['gates']
        if not g['material_worsening'] and single['score_after'] <= single['score_before'] and g['improvements']:
            helpful.append((single['score_before'] - single['score_after'], len(g['improvements']), edit, record))
    helpful.sort(key=lambda h: (-h[0], -h[1]))
    helpful = helpful[:8]
    for size in (2, 3):
        for combo in itertools.combinations(helpful, size):
            if len({p for _, _, e, _ in combo for p in e}) != sum(len(e) for _, _, e, _ in combo):
                continue  # each ingredient edited once
            edit = {k: v for _, _, e, _ in combo for k, v in e.items()}
            out.append(evaluate(edit, {'operation': 'COMBINED', 'category': '+'.join(r['category'] for *_, r in combo),
                                       'position': [r['position'] for *_, r in combo],
                                       'source_line': ' + '.join(r['source_line'] for *_, r in combo),
                                       'changes': [r for *_, r in combo]}))
    return out, ('baseline_sums_missing_score_nutrients_as_zero:' + ','.join(zero_filled)) if zero_filled else None


VEG_EXCLUDE = (r'\b(garlic|chill?i|ginger|herb|parsley|coriander|basil|mint|dill|thyme|rosemary|lemon|lime|olive|nut|seed|almond|walnut|'
               r'cashew|peanut|raisin|sultana|date|dried|juice|zest|oil|coconut|apple|pear|banana|berr\w*|grape|orange|peach|apricot|'
               r'cherr\w*|plum|melon|kiwi|mango|pineapple|fig|potato|paste|pur[eé]e|ketchup|sauce|soup|tinned tomato|chutney|pickle)')
SWEET_DISH = r'\b(cake|bun|scone|muffin|biscuit|cookie|crumble|pudding|smoothie|dessert|sorbet|ice cream|mousse|fool|jam|compote|' \
             r'porridge|muesli|granola|pancake|sweet|fruit salad|snack)'


SWAP_ROLE_BLOCKS = [
    # (candidate label regex, recipe context regex, reason)
    (r'\b(light|reduced|low)\b.*\bbutter\b|\bbutter\b.*\b(light|reduced|low)\b|\bspread\b', 'BAKED', 'reduced-fat spread is not a baking/pastry fat'),
    (r'\b(single|light|reduced|half)\b.*\bcream\b|\bcream\b.*\b(single|light|reduced|half)\b', r'\bwhip', 'lower-fat cream will not whip'),
    (r'\b(skimmed|semi|low|fat[- ]free|reduced)\b', r'\b(custard|béchamel|bechamel|panna cotta|ice cream)\b', None),
]
OFFAL = re.compile(r'\b(liver|heart|gizzard|kidney|tongue|tripe|offal|skin|bone|carcass|eviscerated|mechanically)\b', re.I)
PROCESSED_TARGET = re.compile(r'\b(casserole|ready meal|soup|sauce|pie|pizza|sandwich|with|w/)\b', re.I)


def swap_review(case, screen, recipe, baked):
    occ = {o['position']: o for o in screen['occurrences']}
    out = []
    title = recipe.get('title') or case['title']
    for c in screen['candidates']:
        if c['comparison_status'] != 'supported_input_comparison':
            continue
        o = occ[c['position']]
        line, base_label, label = o['source_line'] or '', o['matched_label'] or '', c['label']
        reasons = ['baseline_match_pitfall:' + p for p in pitfall(line, base_label)]
        reasons += ['candidate_match_pitfall:' + p for p in pitfall(line, label) if p not in ' '.join(reasons)]
        claim = re.compile(r'\b(low[- ]fat|reduced[- ]fat|half[- ]fat|light|skimmed|semi[- ]skimmed|fat[- ]free|lean|'
                           r'low[- ](salt|sodium)|reduced[- ](salt|sodium)|no added (salt|sugar)|unsalted|reduced sugar)\b', re.I)
        if claim.search(line) and not claim.search(base_label) and claim.search(label):
            reasons.append('repairs_baseline_only:source_already_states_variant')
        if re.search(r'\b(boiled|cooked|roast\w*|fried|baked)\b', base_label, re.I) and re.search(r'without salt|no added salt|unsalted', label, re.I) \
                and not re.search(r'without salt|no added salt|unsalted', base_label, re.I):
            reasons.append('repairs_baseline_only:salted_cooking_record_is_a_matcher_choice_not_a_recipe_change')
        if re.search(r'\begg', base_label, re.I) and re.search(r'\b(yolk|white|whole)\b', base_label + ' ' + label, re.I) \
                and re.sub(r'\W', '', base_label.lower()) != re.sub(r'\W', '', label.lower()):
            reasons.append('role_mismatch:whole egg, yolk and white do different jobs')
        if re.search(r'\b(light|reduced|low)\b.*\b(butter|margarine|spread)\b|\b(butter|margarine)\b.*\b(light|reduced|low)\b', label, re.I) \
                and not re.search(r'\b(light|reduced|low)', base_label, re.I):
            uses = mentions(instructions_text(recipe), 'butter') + mentions(instructions_text(recipe), 'margarine')
            spread_only = uses and all(re.search(r'\b(spread|serve|serving|drizzle|melt\w* over|pour\w* over|top|brush)', u, re.I)
                                       and not re.search(r'\b(fry|saut|flour|semolina|dough|mix|beat|cream|rub|breadcrumb|roux)', u, re.I) for u in uses)
            if not spread_only:
                reasons.append('role_mismatch:light butter/spread is only for spreading or serving, not frying, doughs or cooking')
        if c.get('route') == 'graph_foodon':  # graph/FoodOn relatives are only "related"; the target must do the same job in the dish
            reasons += ['role:' + p for p in role_problems(base_label, label)]
        if OFFAL.search(label) and not OFFAL.search(base_label):
            reasons.append('different_food_part_offal_or_skin')
        if PROCESSED_TARGET.search(label) and not PROCESSED_TARGET.search(base_label):
            reasons.append('target_is_a_composite_dish')
        for label_re, ctx, why in SWAP_ROLE_BLOCKS:
            if why and re.search(label_re, label, re.I) and (baked if ctx == 'BAKED' else re.search(ctx, instructions_text(recipe) + title, re.I)):
                reasons.append('role_mismatch:' + why)
        # Production approval reasons other than the numeric floors/guards re-judged below still block.
        numeric = ('nutrient_regression:', 'target_reduction_below_', 'no_target_nutrient_improvement')
        approval = c.get('approval') or {}
        # carbs/fat are not Nutri-Score inputs; their incomparability is recorded, not blocking, when all six score nutrients compare.
        score_complete = all(isinstance((c.get(side) or {}).get(m), (int, float)) for side in ('before_per_serving', 'after_per_serving') for m in GUARDED)
        unassessed = [m for m in ('carbs_g', 'fat_g') if (c.get('before_per_serving') or {}).get(m) is None or (c.get('after_per_serving') or {}).get(m) is None]
        tolerated = {'complete_comparable_guard_nutrients_required', 'complete_comparable_guard_nutrients_required'} if score_complete else set()
        blocking = [r for r in (approval.get('reasons') or []) if not r.startswith(numeric) and r not in tolerated]
        reasons += ['production_evidence:' + r for r in blocking]
        if approval.get('status') not in ('approved_input_objective', 'rejected') and blocking:
            reasons += ['production_approval_status:' + str(approval.get('status'))]
        b, a = c['before_per_serving'], c['after_per_serving']
        if not score_complete or not c.get('grade_before'):
            reasons.append('score_nutrients_not_comparable')
            g = {'passed': False, 'reasons': ['score_nutrients_not_comparable']}
        else:
            g = gates(b, a, c['grade_before'], c['grade_after'])
        out.append({'position': c['position'], 'source_line': line, 'matched_label': base_label, 'operation': c['operation'],
                    'route': c['route'], 'candidate_label': label, 'candidate_id': c['candidate_id'], 'collection': c['collection'],
                    'target_metric': c['metric'], 'before_per_serving': b, 'after_per_serving': a,
                    'grade_before': c.get('grade_before'), 'grade_after': c.get('grade_after'),
                    'score_before': c.get('score_before'), 'score_after': c.get('score_after'),
                    'production_approval': (c.get('approval') or {}).get('status'),
                    'production_reasons': (c.get('approval') or {}).get('reasons'),
                    'unassessed_non_score_nutrients': unassessed, 'gates': g, 'status': 'rejected_culinary_or_match' if reasons else ('qualifies' if g['passed'] else 'rejected_nutrition'),
                    'reasons': reasons or g['reasons'],
                    'reason': f"swap {base_label} for {label} at the same amount"})
    return out


def n_changes(c):
    return len(c['changes']) if c.get('operation') == 'COMBINED' else 1


def rank(c):
    # Best letter, then meeting the policy floors, then FEWEST changes, then the larger total gain: the strongest single change wins
    # and extra changes are added only when they reach a better letter or the floors.
    g = c['gates']
    gain = sum(x['relative'] for x in g['improvements'])
    return (-GRADE[c['grade_after']], bool(g['meaningful_reductions']), -n_changes(c), round(gain, 2),
            (c['score_before'] or 0) - (c['score_after'] or 0), g['passes_strict_meaningful_nutrient_v1'])


def fmt(w):
    return '/'.join(f'{x:.1f}' for x in w) if isinstance(w, list) else f'{w:.1f}'


def compact(ps):
    return {k: None if ps.get(k) is None else round(ps[k], 3) for k in REPORTED}



def describe_change(c):
    """One plain sentence for a change, for reviewers and for the LLM candidate pack."""
    op, cat = c.get('operation'), c.get('category')
    line = c.get('source_line') or ''
    if op in ('REDUCE_AMOUNT', 'INCREASE_VEG'):
        b, a = c.get('weight_before_g'), c.get('weight_after_g')
        verb = 'Add more of' if op == 'INCREASE_VEG' else 'Use less of'
        return f"{verb} \"{line}\" ({fmt(b)} g -> {fmt(a)} g)"
    if op == 'SWAP_FOOD':
        extra = ' (changes the dish)' if c.get('changes_dish_identity') else ''
        return f"Replace \"{line}\" ({c.get('matched_label')}) with {c.get('candidate_label')}, same weight{extra}"
    return f"Replace \"{line}\" ({c.get('matched_label')}) with {c.get('candidate_label')}"


def first_food_word(label):
    m = re.match(r"\s*([A-Za-z]+)", label or '')
    return m.group(1).lower() if m else ''


def candidate_pack(qualifying, limit=10, max_combinations=3):
    """Distinct qualifying candidates, each with its before/after table, for the LLM choice step.

    Single changes first (rank order), then at most `max_combinations` bundles, so a bundle of the same swap plus an oil cut
    never crowds out a genuinely different option."""
    ranked = sorted(qualifying, key=rank, reverse=True)
    ordered = [c for c in ranked if c['operation'] != 'COMBINED'] + [c for c in ranked if c['operation'] == 'COMBINED'][:max_combinations]
    pack, seen = [], set()
    for c in ordered:
        parts = c['changes'] if c['operation'] == 'COMBINED' else [c]
        # one option per food per ingredient: six mozzarella records are one choice for the cook
        sig = tuple(sorted((x.get('category') or x['operation'], str(x.get('position')), first_food_word(x.get('candidate_label')) or str(x.get('source_line'))) for x in parts))
        if sig in seen:
            continue
        seen.add(sig)
        keys = ('position', 'operation', 'category', 'source_line', 'matched_label', 'candidate_label', 'candidate_id', 'weight_before_g',
                'weight_after_g', 'reduction_fraction', 'reason', 'cross_database', 'changes_dish_identity')
        changes = [{k: x.get(k) for k in keys if x.get(k) is not None} for x in parts]
        g = c['gates']
        pack.append({'id': len(pack) + 1, 'summary': '; '.join(describe_change(x) for x in parts), 'changes': changes,
                     'flags': {'changes_dish_identity': any(x.get('changes_dish_identity') for x in parts),
                               'cross_database': any(x.get('cross_database') for x in parts)},
                     'before_per_serving': compact(c['before_per_serving']), 'after_per_serving': compact(c['after_per_serving']),
                     'grade_before': c['grade_before'], 'grade_after': c['grade_after'],
                     'tier': 'policy_floors' if g['meaningful_reductions'] else 'better_letter' if g['better_letter'] else 'gain_10pct' if g.get('relative_gain_only') else 'small_gain',
                     'tradeoffs': [t['metric'] for t in g['tradeoffs_within_5pct']]})
        if len(pack) == limit:
            break
    return pack


def gate_restrictions(candidates, texts):
    for c in candidates:
        if c.get('status') == 'qualifies':
            bad = restriction_problems(c['changes'] if c['operation'] == 'COMBINED' else [c], texts)
            if bad:
                c.update(status='rejected_restriction', reasons=['restriction:' + b for b in bad])


def adapt_snapshot(case, screen, snapshot, profile, recipe, records):
    """case: {recipe_id, region, title}; screen: production swap candidates and occurrences (see screen_production_swaps)."""
    title, steps = recipe.get('title') or case['title'], instructions_text(recipe)
    baked = is_baked(title, steps, snapshot)
    swaps = swap_review(case, screen, recipe, baked)
    amounts, amount_note = change_candidates(case, snapshot, profile, recipe, records)
    candidates = swaps + amounts
    for c in amounts:
        if 'gates' in c and c.get('status') is None:
            c['status'] = 'qualifies' if c['gates']['passed'] else 'rejected_nutrition'
            c['reasons'] = c['gates']['reasons']
    # A letter-only improvement is trusted only when no occurrence anywhere shows a known match pitfall.
    recipe_pitfalls = [{'position': i.position, 'source_line': i.original_text, 'matched_label': lab, 'pitfalls': found}
                       for i in snapshot.ingredients
                       for lab in [(i.composition.provenance.get('candidate') or {}).get('document', '') if i.composition else
                                   next((d.get('matched_nutritional_ingredient') or d.get('name') or '' for d in profile['details']
                                         if d.get('position') == i.position), '')]
                       for found in [pitfall(i.original_text or '', lab)] if found]
    for c in candidates:
        if c.get('status') == 'qualifies':
            c['gates']['letter_flip_only'] = c['gates']['better_letter'] and not c['gates']['meaningful_reductions']
            if c['gates']['letter_flip_only'] and recipe_pitfalls:
                c.update(status='rejected_nutrition', reasons=['letter_only_gain_with_baseline_match_pitfall'])
    texts = [i.original_text or i.name for i in snapshot.ingredients]
    gate_restrictions(candidates, texts)
    qualifying = [c for c in candidates if c.get('status') == 'qualifies']
    if not qualifying and amount_note is None or not qualifying and (amount_note or '').startswith('baseline_sums'):
        veg, _ = change_candidates(case, snapshot, profile, recipe, records, more_veg=True)
        for c in veg:
            c['status'] = 'qualifies' if c['gates']['passed'] else 'rejected_nutrition'
            c['reasons'] = c['gates']['reasons']
            c['gates']['letter_flip_only'] = c['gates']['better_letter'] and not c['gates']['meaningful_reductions']
        gate_restrictions(veg, texts)
        candidates += veg
        qualifying = [c for c in veg if c['status'] == 'qualifies']
    # Food-part scan returns at most 500 rows; exactly 500 for one target means truncated retrieval.
    per_target = Counter((c['position'], c['metric']) for c in screen['candidates'] if c['route'] == 'food_part_variant')
    incomplete = [f"route_error:{e['route']}" for e in screen['route_errors']] + \
        [f'food_part_scan_truncated:position_{k[0]}' for k, n in per_target.items() if n >= 500]
    baseline = {'per_serving': compact({k: v for k, v in profile['clean_totals_per_serving'].items() if k in REPORTED}),
                'grade': profile['nutri_score']['nutri_score'], 'serves': snapshot.serves}
    result = dict(case, live_run_id=screen['live_run_id'], baseline=baseline, amount_recalculation_note=amount_note,
                  routes_evaluated=screen['routes_evaluated'] + ['amount_reduction_rules', 'curated_food_swaps (swap_rules.py)', 'combinations_up_to_3'],
                  routes_not_evaluated=screen.get('routes_not_evaluated', []),
                  route_errors=screen['route_errors'],
                  candidate_counts=dict(Counter(c.get('status') for c in candidates)),
                  swap_screen_counts=dict(Counter(c['comparison_status'] for c in screen['candidates'])),
                  swap_before_matches_saved=all(all(math.isclose(c['before_per_serving'][k], profile['clean_totals_per_serving'][k],
                      rel_tol=1e-6, abs_tol=1e-6) for k in REPORTED if c['before_per_serving'][k] is not None
                      and profile['clean_totals_per_serving'].get(k) is not None) for c in swaps))
    result['baseline_match_pitfalls'] = recipe_pitfalls
    result['candidate_pack'] = candidate_pack(qualifying)
    if qualifying:
        best = max(qualifying, key=rank)
        pit_positions = {x['position'] for x in recipe_pitfalls}
        used = {p for c in (best['changes'] if best['operation'] == 'COMBINED' else [best])
                for p in (c['position'] if isinstance(c['position'], list) else [c['position']])}
        assert not used & pit_positions, ('adapted change on a pitfall line', case['recipe_id'], used & pit_positions)
        keys = ('position', 'operation', 'category', 'source_line', 'matched_label', 'candidate_label', 'candidate_id', 'candidate_dataset',
                'weight_before_g', 'weight_after_g', 'reduction_fraction', 'reason', 'route', 'per_100g', 'cross_database', 'changes_dish_identity')
        parts = best['changes'] if best['operation'] == 'COMBINED' else [best]
        result.update(outcome='adapted', adaptation={
            'changes': [{k: c.get(k) for k in keys if c.get(k) is not None} for c in parts],
            'before_per_serving': compact(best['before_per_serving']), 'after_per_serving': compact(best['after_per_serving']),
            'grade_before': best['grade_before'], 'grade_after': best['grade_after'],
            'score_before': best['score_before'], 'score_after': best['score_after'], 'gates': best['gates'],
            'calculation': 'production compare_raw_part (same-amount product/part swap)' if best['operation'] in ('REPLACE_PRODUCT_VARIANT', 'REPLACE_INGREDIENT')
            else 'analysis recalculation from the baseline snapshot (baseline totals, grade and FVL share reproduced exactly first)'},
            alternatives=[{k: c.get(k) for k in ('operation', 'category', 'source_line', 'candidate_label', 'weight_after_g', 'grade_after')}
                          | {'meaningful': c['gates']['meaningful_reductions']} for c in sorted(qualifying, key=rank, reverse=True)[1:6]])
    if not qualifying and baseline['grade'][-1] in 'BCDE':
        near = [c for c in candidates if 'improvements' in c.get('gates', {}) and not c['gates']['material_worsening']
                and any(x['relative'] >= MIN_RELATIVE_REDUCTION and x['metric'] in TARGETS for x in c['gates']['improvements'])]
        if near:
            nb = max(near, key=lambda c: max(x['relative'] for x in c['gates']['improvements'] if x['metric'] in TARGETS))
            result['near_miss_below_absolute_floor'] = {
                'source_line': nb.get('source_line'), 'operation': nb['operation'], 'category': nb.get('category'),
                'improvements': nb['gates']['improvements'], 'grade_after': nb['grade_after'],
                'note': '>=10% cut on a score nutrient but under the absolute per-serving floor; not counted as adapted'}
    if qualifying:
        pass  # outcome already set above
    elif incomplete:
        result.update(outcome='failed', reason='incomplete_search', incomplete_search=sorted(set(incomplete)))
    else:
        why = Counter(r.split(':')[0] for c in candidates for r in (c.get('reasons') or []))
        result.update(outcome='no_sensible_adaptation', reason=(
            'No candidate passed. Evaluated: production product/part swaps, amount reductions, curated food swaps and '
            'combinations of up to three. Graph (Neo4j/FoodOn) substitutions were not evaluated. Rejection tally: '
            + (', '.join(f'{k} x{v}' for k, v in why.most_common(6)) or 'no candidates retrieved')
            + ('' if amount_note is None else f'; {amount_note}')))
    result['rejected_examples'] = [{k: c.get(k) for k in ('operation', 'source_line', 'matched_label', 'candidate_label', 'category', 'status', 'reasons')}
                                   for c in candidates if c.get('status') != 'qualifies'][:12]
    return result


# ---- recipe card (deterministic text for the edited ingredient lines) ----
UNICODE = {'½': Fraction(1, 2), '¼': Fraction(1, 4), '¾': Fraction(3, 4), '⅓': Fraction(1, 3), '⅔': Fraction(2, 3)}
AMOUNT = re.compile(r'^\s*(?P<num>(\d+[.,]\d+|\d+\s*[½¼¾⅓⅔]|\d+/\d+|\d+|[½¼¾⅓⅔]))')
NAMES = {
    'wholemeal_pasta': 'wholemeal pasta', 'brown_rice': 'brown rice', 'wholemeal_bread': 'wholemeal bread',
    'half_wholemeal_flour': 'half white, half wholemeal flour', 'wholewheat_couscous': 'wholewheat couscous',
    'cream_to_reduced_creme_fraiche': 'reduced-fat crème fraîche', 'sour_cream_to_yogurt': 'plain low-fat yogurt',
    'semi_skimmed_milk': 'semi-skimmed milk', 'reduced_fat_cheddar': 'reduced-fat cheddar', 'light_mozzarella': 'light mozzarella',
    'light_cream_cheese': 'light cream cheese', 'lean_beef_mince': 'lean beef mince (5% fat)', 'reduced_fat_mayo': 'reduced-fat mayonnaise',
    'light_coconut_milk': 'light coconut milk', 'lean_bacon': 'lean back bacon', 'reduced_salt_stock': 'reduced-salt stock',
    'reduced_salt_soy': 'reduced-salt soy sauce', 'no_salt_canned_pulses': 'no-added-salt tinned version',
    'reduced_sugar_salt_baked_beans': 'reduced sugar and salt baked beans', 'butter_to_oil_for_frying': 'rapeseed or olive oil',
}
STEP_NOTES = {
    'sour_cream_to_yogurt': 'Stir the yogurt in off the heat so it does not split.',
    'cream_to_reduced_creme_fraiche': 'Add the crème fraîche at the end and heat gently without boiling hard.',
    'butter_to_oil_for_frying': 'Fry in the oil wherever the method says to melt or heat the butter.',
    'brown_rice': 'Brown rice needs longer to cook (about 25–30 minutes); follow the packet.',
    'wholemeal_pasta': 'Cook the wholemeal pasta according to the packet; it may need 1–2 minutes longer.',
    'protein_swap': 'This changes the dish: lean poultry breast cooks faster and dries out more easily than red meat, so cook it until just done and check early.',
    'similar_part': 'Check the cooking time: a different cut or version may cook faster or slower.',
    'more_veg': 'Prepare and cook the extra vegetables with the rest; allow a few more minutes if needed.',
}


def parse(num):
    num = num.replace(',', '.').strip()
    if num in UNICODE:
        return Fraction(UNICODE[num])
    m = re.match(r'(\d+)\s*([½¼¾⅓⅔])$', num)
    if m:
        return int(m.group(1)) + UNICODE[m.group(2)]
    return Fraction(num).limit_denominator(100)


def nice(value, line):
    if re.search(r'^\s*[\d.,/½¼¾⅓⅔ ]+\s*(g|ml|kg|l)\b', line, re.I):  # metric amounts stay decimal
        return str(round(float(value))) if value >= 5 else f'{float(value):.1f}'.rstrip('0').rstrip('.')
    for denominator in (1, 2, 3, 4, 8):
        f = Fraction(value).limit_denominator(denominator)
        if abs(float(f) - float(value)) <= 0.07 * float(value):
            whole, rest = divmod(f.numerator, f.denominator)
            frac = {Fraction(1, 2): '½', Fraction(1, 4): '¼', Fraction(3, 4): '¾', Fraction(1, 3): '⅓',
                    Fraction(2, 3): '⅔', Fraction(1, 8): '⅛', Fraction(3, 8): '⅜'}.get(Fraction(rest, f.denominator), '')
            if rest and not frac:
                continue
            return (f'{whole} ' if whole and frac else str(whole) if whole else '') + frac if rest else str(whole)
    return f'{float(value):.1f}'.rstrip('0').rstrip('.')


def rewrite(change):
    line = change['source_line']
    if change['operation'] in ('REDUCE_AMOUNT', 'INCREASE_VEG'):
        keep = 1 - change['reduction_fraction']
        m = AMOUNT.match(line)
        if m:
            rest = re.sub(r'\s*\((?:about|approx\.?|roughly)[^)]*\)', '', line[m.end():], flags=re.I)  # a second unit would go stale after the cut
            rest = re.sub(r'^(\s*(?:g|ml|kg|l))\s*/\s*[\d.,¼½¾⅓⅔ ]+\s*(?:fl\.? ?oz|oz|lb|pints?)\.?', r'\1', rest, flags=re.I)  # '50 g / 2 oz.' -> '33 g'
            return nice(parse(m.group('num')) * Fraction(keep).limit_denominator(100), line) + rest, None
        return line, f"use about {round(change['weight_after_g'], 1)} g instead of {round(change['weight_before_g'], 1)} g"
    name = NAMES.get(change.get('category')) or change.get('candidate_label')
    return f'{line}  →  use **{name}**', None



def ingredient_lines(result, snapshot):
    """Old and new ingredient line for every ingredient; new is empty when unchanged."""
    changed = {}
    for ch in result['adaptation']['changes']:
        for pos in ch['position'] if isinstance(ch['position'], list) else [ch['position']]:
            changed[pos] = ch
    lines = []
    for item in snapshot.ingredients:
        text = item.original_text or item.name
        ch = changed.get(item.position)
        if ch is None:
            lines.append({'old': text, 'new': ''})
            continue
        new, note = rewrite(dict(ch, source_line=text, weight_before_g=item.quantity.weight_g,
                                 weight_after_g=item.quantity.weight_g * (1 - ch.get('reduction_fraction', 0))))
        if ch['operation'] not in ('REDUCE_AMOUNT', 'INCREASE_VEG'):
            new = 'use ' + new.replace('**', '').split('  →  use ')[-1]
        lines.append({'old': text, 'new': new + (f' ({note})' if note else '')})
    return lines


# ---- live entry point (read-only) ----
_RECORDS: dict[str, list] = {}


def release_records(release_id: str) -> list[dict]:
    """All released composition records (label, dataset, per-100 g values), cached per process."""
    if release_id not in _RECORDS:
        from sqlalchemy import text
        from recipe_wrangler.utils.nutrition_postgres import get_engine
        with get_engine().connect() as connection, connection.begin():
            connection.execute(text('SET TRANSACTION READ ONLY'))
            connection.execute(text("SET LOCAL statement_timeout='60s'"))
            rows = connection.execute(text("""select collection, candidate_id, candidate->>'document' as label,
                annotation->'source'->>'dataset' as dataset, annotation->'values_per_100g' as v, annotation->'text_annotation'->'facets' as f
                from matching_compositions c join matching_releases r using (release_id)
                where release_id=:r and r.status='ready'"""), {'r': release_id}).mappings().all()
        _RECORDS[release_id] = [dict(r) for r in rows]
    return _RECORDS[release_id]


def screen_production_swaps(snapshot, profile, targets_per_metric=10):
    """Production product/part variants through compare_raw_part and the approval function (as in the 11 Nov screen)."""
    from .comparison import compare_raw_part
    from .comparison_store import ComparisonLoadError, _composition
    from .nutritional_variants import nutritional_variant_records
    from .models import NutrientObjective
    from .objective_approval import approve_nutrition_candidate
    from .production_candidates import food_part_candidate_records, make_substitution_edit, production_candidate_records
    context = production_score_context(snapshot, profile)
    if context is None:
        raise ComparisonLoadError('production_score_context_unavailable')
    out = {'candidates': [], 'route_errors': [], 'routes_evaluated': ['nutritional_variants x4 metrics', 'food_part_variants', 'graph_foodon'],
           'occurrences': [{'position': i.position, 'name': i.name, 'source_line': i.original_text, 'weight_g': i.quantity.weight_g,
                            'matched_label': i.composition.provenance.get('candidate', {}).get('document') if i.composition else None,
                            'matched_id': i.composition.ref.candidate_id if i.composition else None} for i in snapshot.ingredients]}
    seen, part_rows, graph_rows = set(), {}, {}
    for metric in TARGETS:
        ranked = sorted((i for i in snapshot.ingredients if i.composition is not None and (i.composition.values_per_100g.get(metric) or 0) > 0),
                        key=lambda i: -i.composition.values_per_100g[metric] * i.quantity.weight_g)
        for rank, item in enumerate(ranked[:targets_per_metric]):
            routes = [('nutritional_variant', lambda: nutritional_variant_records(snapshot, item.position, metric)),
                      ('food_part_variant', lambda: part_rows.setdefault(item.position, food_part_candidate_records(snapshot, item.position)[0]))]
            if rank < GRAPH_POSITIONS_PER_METRIC:  # graph/FoodOn retrieval is slow: only the biggest contributors
                routes.append(('graph_foodon', lambda: graph_rows.setdefault(item.position, production_candidate_records(snapshot, item.position)[0])))
            for route, lookup in routes:
                try:
                    rows = lookup()
                except Exception as error:
                    out['route_errors'].append({'position': item.position, 'metric': metric, 'route': route, 'error': f'{type(error).__name__}: {error}'[:300]})
                    continue
                for row in rows:
                    key = (item.position, row['collection'], row['candidate_id']) + (() if route == 'graph_foodon' else (metric,))
                    if key in seen:
                        continue
                    seen.add(key)
                    c = {'position': item.position, 'metric': metric, 'route': route, 'collection': row['collection'],
                         'candidate_id': row['candidate_id'], 'label': row['candidate']['document']}
                    try:
                        composition = _composition(row)
                        edit = make_substitution_edit(snapshot, item.position, composition, nutritional_metric=metric)
                        comparison = compare_raw_part(snapshot, edit, composition)
                        c.update(operation=edit.operation, comparison_status=comparison.status,
                                 reasons=comparison.blockers + comparison.missing_evidence)
                        if comparison.status == 'supported_input_comparison':
                            approval = approve_nutrition_candidate(snapshot, comparison, NutrientObjective(metric=metric, direction='decrease'),
                                                                   context, nutrition_policy='meaningful_nutrient_v1')
                            c.update(before_per_serving=comparison.before['complete_totals_per_serving'],
                                     after_per_serving=comparison.after['complete_totals_per_serving'],
                                     approval={k: approval.get(k) for k in ('status', 'reasons')},
                                     grade_before=(approval.get('before') or {}).get('nutri_score'),
                                     grade_after=(approval.get('after') or {}).get('nutri_score'),
                                     score_before=(approval.get('before') or {}).get('score'),
                                     score_after=(approval.get('after') or {}).get('score'))
                    except ComparisonLoadError as error:
                        c.update(comparison_status='blocked' if error.blocked else 'needs_evidence', reasons=[error.reason])
                    out['candidates'].append(c)
    return out


def adapt_active_recipe(recipe_id: str, nutrition_source: str) -> dict:
    """Adapt one recipe in its regional profile. Read-only; returns an unsaved result with a deterministic card."""
    from recipe_wrangler.repositories import matching_store as store
    from recipe_wrangler.tools import fetch_recipe_info as fetch
    from .comparison_store import ComparisonLoadError, load_active_snapshot
    from .enhancement_pipeline import _verify_source_binding
    case = {'recipe_id': recipe_id, 'region': nutrition_source, 'title': recipe_id}
    try:
        before = store.active_profile(recipe_id, nutrition_source, include_provenance=True)
        if not before:
            raise ComparisonLoadError('no_active_profile')
        snapshot = load_active_snapshot(recipe_id, nutrition_source, allow_input_estimates=True)
        if snapshot.run_id != before['run_id']:
            raise ComparisonLoadError('snapshot_run_differs')
        recipe = fetch.fetch_recipe_info_by_id(recipe_id)
        _verify_source_binding(snapshot, recipe, before['profile'])
        case['title'] = recipe.get('title') or recipe_id
        screen = screen_production_swaps(snapshot, before['profile'])
        screen['live_run_id'] = before['run_id']
        result = adapt_snapshot(case, screen, snapshot, before['profile'], recipe, release_records(snapshot.release_id))
        if store.active_profile(recipe_id, nutrition_source, include_provenance=True) != before:
            raise ComparisonLoadError('active_profile_changed_during_execution')
    except ComparisonLoadError as error:
        return dict(case, outcome='failed', reason=error.reason, unsaved=True)
    result['unsaved'] = True
    if result['outcome'] == 'adapted':
        result['card'] = {'ingredients': ingredient_lines(result, snapshot),
                          'notes': list(dict.fromkeys(STEP_NOTES[c['category']] for c in result['adaptation']['changes'] if c.get('category') in STEP_NOTES)),
                          'instructions': recipe.get('instructions')}
    return result
