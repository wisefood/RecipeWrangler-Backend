"""Small-gain adaptation: gates, rules, card text, router, and exact reproduction of the 12 Nov 2026 results."""
import gzip
import json
from collections import Counter
import re
from fractions import Fraction
from pathlib import Path

import pytest

from recipe_wrangler.services.adaptation import nutrition_adaptation as sg
from recipe_wrangler.services.adaptation import nutrition_swap_rules as rules
from recipe_wrangler.services.adaptation.models import ComparisonSnapshot

A = Path(__file__).resolve().parents[1] / 'analysis/nutrition-adaptation-2026-11-11'
BASE = {'energy_kcal': 400.0, 'sugar_g': 10.0, 'saturated_fat_g': 6.0, 'sodium_mg': 800.0, 'fibre_g': 4.0, 'protein_g': 20.0}


def gate(before_grade, after_grade, **changes):
    after = dict(BASE, **changes)
    return sg.gates(BASE, after, 'Nutriscore_' + before_grade, 'Nutriscore_' + after_grade)


def test_a_staying_a_needs_ten_percent():
    assert gate('A', 'A', sodium_mg=700.0)['passed']          # 12.5%
    assert not gate('A', 'A', sodium_mg=750.0)['passed']      # 6% (owner, 16 Nov: A->A needs 10%)


def test_b_and_below_need_ten_percent_or_policy_floor_or_better_letter():
    assert gate('B', 'B', sodium_mg=700.0)['passed']          # 12.5%
    assert gate('B', 'B', saturated_fat_g=5.4)['passed']      # exactly 10%
    assert gate('B', 'B', sodium_mg=750.0)['passed']          # 6%: small gain counts for every grade (owner, 12 Nov)
    assert gate('B', 'A', fibre_g=4.5)['better_letter'] is True  # 12.5% more fibre credits the letter


def test_grade_drop_and_material_worsening_block():
    assert not gate('A', 'B', sodium_mg=600.0)['passed']
    worse = gate('B', 'B', sodium_mg=600.0, sugar_g=13.0)      # +30% sugar, +3 g: material
    assert not worse['passed'] and worse['material_worsening'][0]['metric'] == 'sugar_g'


def test_tiny_worsening_on_a_small_base_is_a_tradeoff_not_material():
    g = gate('A', 'A', sodium_mg=600.0, saturated_fat_g=6.4)   # +6.7% but only 0.4 g, below half the 1 g floor
    assert g['passed'] and g['tradeoffs_within_5pct'][0]['metric'] == 'saturated_fat_g'


def test_rewrite_keeps_recipe_units():
    r = sg.rewrite
    assert r({'operation': 'REDUCE_AMOUNT', 'source_line': '1 teaspoon of salt', 'reduction_fraction': 0.5, 'weight_after_g': 3, 'weight_before_g': 6})[0] == '½ teaspoon of salt'
    assert r({'operation': 'REDUCE_AMOUNT', 'source_line': '2 g Salt', 'reduction_fraction': 0.5, 'weight_after_g': 1, 'weight_before_g': 2})[0] == '1 g Salt'
    assert r({'operation': 'REDUCE_AMOUNT', 'source_line': '1 vegetable stock cube', 'reduction_fraction': 1 / 3, 'weight_after_g': 1, 'weight_before_g': 1.5})[0] == '⅔ vegetable stock cube'
    assert sg.parse('2 ½') == Fraction(5, 2) and sg.parse('0,2') == Fraction(1, 5)


def test_swap_rules_keep_type_and_state():
    assert rules.keeps_type('Yogurt, Greek style, plain', 'Yogurt Greek skimmed')
    assert not rules.keeps_type('Cheese, goats milk', 'Cheese, cow')
    assert not rules.compatible_state('Pasta, dry, raw', 'Pasta wholemeal boiled', 'wholemeal_pasta')
    assert rules.compatible_state('Pasta, dry, raw', 'Pasta, dry, wholewheat, raw', 'wholemeal_pasta')
    # a swap that also drops sodium by more than half is a different product, not the intended change
    assert rules.unearned('wholemeal_pasta', {'sodium_mg': 174, 'sugar_g': 1, 'saturated_fat_g': 0.1}, {'sodium_mg': 5}) == ['sodium_mg']


def test_pitfall_lines_are_detected():
    assert sg.pitfall('1 low salt vegetable stock cube', 'Stock cubes, vegetable')
    assert sg.pitfall('1 small tin of tomatoes', 'Tomato, round, raw')
    assert not sg.pitfall('1 small tin of tomatoes', 'Tomatoes, canned, whole contents')


def test_router_maps_region_and_returns_unsaved_result(monkeypatch):
    from recipe_wrangler.services.adaptation import router
    from recipe_wrangler.services.adaptation.schemas import SmallGainRequest
    seen = {}
    monkeypatch.setattr(sg, 'adapt_active_recipe', lambda rid, source, **_: seen.update(rid=rid, source=source) or {'unsaved': True})
    assert router.nutrition_adaptation(SmallGainRequest(region='IE'), recipe_id='abc') == {'unsaved': True}
    assert seen == {'rid': 'abc', 'source': 'irish'}


# --- reproduction of the 12 Nov results from captured live inputs (skipped where the analysis folder is absent) ---
needs_data = pytest.mark.skipif(not (A / 'records-2026-10-27-v11.json.gz').exists(), reason='analysis capture not present')


FROZEN = Path(__file__).resolve().parents[1] / 'evaluation' / 'adaptation' / 'results' / 'nutrition_results.json'


@needs_data
def test_packaged_module_reproduces_the_recorded_results():
    """The tracked frozen results (19 Nov 2026) are the reference; the capture under analysis/ supplies the inputs."""
    frozen = json.load(open(FROZEN))
    assert Counter(r['outcome'] for r in frozen.values()) == {'adapted': 284, 'enriched': 204, 'no_sensible_adaptation': 49}
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    cases = {c['recipe_id']: c for c in json.load(open(A / 'cases.json'))}
    wanted = {}  # one adapted recipe per operation/category plus no-adaptation ones
    for rid, old in frozen.items():
        if old['outcome'] == 'adapted':
            for c in old['changes']:
                wanted.setdefault(c.get('category') or c['operation'], rid)
        elif len(wanted) < 60:
            wanted.setdefault('none:' + old['baseline_grade'][-1], rid)
    assert len(wanted) > 20
    for rid in set(wanted.values()):
        screen = json.load(open(A / 'screens' / f'{rid}.json'))
        cap = json.load(gzip.open(A / 'inputs' / f'{rid}.json.gz', 'rt'))
        new = sg.adapt_snapshot(cases[rid], screen, ComparisonSnapshot.model_validate(cap['snapshot']),
                                cap['profile']['profile'], cap['source_recipe'], records)
        old = frozen[rid]
        assert new['outcome'] == old['outcome'], rid
        if new['outcome'] == 'adapted':
            keys = ('position', 'operation', 'category', 'route', 'source_line', 'matched_label', 'candidate_label', 'weight_before_g', 'weight_after_g')
            assert [{k: c.get(k) for k in keys} for c in new['adaptation']['changes']] == old['changes'], rid
            assert new['adaptation']['after_per_serving'] == old['after_per_serving'], rid
            lines = sg.ingredient_lines(new, ComparisonSnapshot.model_validate(cap['snapshot']))
            assert any(line['new'] for line in lines), rid


@needs_data
def test_baseline_that_does_not_reproduce_yields_no_changes():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    rid, case = next(iter({c['recipe_id']: c for c in json.load(open(A / 'cases.json'))}.items()))
    case = {'recipe_id': rid, 'region': 'irish', 'title': 't'}
    screen = json.load(open(A / 'screens' / f'{rid}.json'))
    cap = json.load(gzip.open(A / 'inputs' / f'{rid}.json.gz', 'rt'))
    cap['profile']['profile']['clean_totals']['energy_kcal'] += 50
    out = sg.adapt_snapshot(case, screen, ComparisonSnapshot.model_validate(cap['snapshot']), cap['profile']['profile'], cap['source_recipe'], records)
    assert (out.get('amount_recalculation_note') or '').startswith('recalculation_unavailable')
    ops = {c['operation'] for c in out.get('adaptation', {}).get('changes', [])}
    assert not ops & {'REDUCE_AMOUNT', 'SWAP_FOOD', 'INCREASE_VEG', 'COMBINED'}  # nothing recalculated on an unreproduced baseline


# --- loose similar-food search and protein role swaps ---
def rec(label, identity, state, sat, kcal=100.0, part=None, dataset='ds', cid=None):
    v = {'energy_kcal': kcal, 'sugar_g': 0.0, 'saturated_fat_g': sat, 'sodium_mg': 50.0, 'fibre_g': 0.0, 'protein_g': 20.0}
    return {'collection': 'nutritional_ingredients_eu', 'candidate_id': cid or label, 'label': label, 'dataset': dataset, 'v': v,
            'f': {'base_identity': identity, 'preparation_state': state, 'food_part': part}}


def test_similar_targets_accept_any_part_of_the_same_animal_without_a_part_check():
    base = {m: 0.0 for m in sg.GUARDED} | {'saturated_fat_g': 10.0, 'energy_kcal': 280.0, 'sodium_mg': 60.0}
    pool = [rec('Lamb, loin, raw', 'lamb', 'raw', 10.0), rec('Lamb, shoulder, raw', 'lamb', 'raw', 4.0), rec('Lamb, leg, raw', 'lamb', 'raw', 3.0, part='leg'),
            rec('Lamb, leg, raw', 'lamb', 'raw', 3.0, dataset='copy', cid='dup'),                      # duplicate value
            rec('Liver, lamb, raw', 'lamb', 'raw', 1.0), rec('Lamb, fat, raw', 'lamb', 'raw', 1.0),   # offal and pure fat never count
            rec('Beef, rump, raw', 'beef', 'raw', 1.0), rec('Lamb, braised', 'lamb', 'cooked', 1.0)]  # other food / other state
    target, cross = sg.similar_targets(pool, 'lamb', 'raw', 'Lamb, loin, raw', base, 'ds', 'irish', 'saturated_fat_g', 1.0)
    assert target['label'] in ('Lamb, shoulder, raw', 'Lamb, leg, raw') and cross is False


def test_similar_targets_ignore_tiny_gains_and_keep_the_baseline_descriptors():
    base = {m: 0.0 for m in sg.GUARDED} | {'saturated_fat_g': 1.0}
    assert sg.similar_targets([rec('Onion, frozen, raw', 'onion', 'raw', 0.8)], 'onion', 'raw', 'Onion, raw', base, 'ds', 'irish', 'saturated_fat_g', 0.5)[0] is None
    assert not rules.keeps_descriptors('Chicken, breast, without skin, raw', 'Chicken, breast, raw')
    assert rules.keeps_descriptors('Chicken, breast, without skin, raw', 'Chicken inner breast fillet skinless boneless raw')


def test_role_targets_pick_a_poultry_breast_record_for_a_fatty_meat():
    pool = [rec('Turkey breast skinless boneless raw', 'turkey', 'raw', 0.3, part='breast'), rec('Turkey, thigh, raw', 'turkey', 'raw', 2.0, part='thigh'),
            rec('Chicken, breast, with skin, raw', 'chicken', 'raw', 3.0, part='breast'), rec('Chicken breast skinless raw', 'chicken', 'raw', 0.7, part='breast')]
    spec_c, spec_t = rules.ROLE_SWAPS[0]['to']  # chicken first, turkey second
    assert sg.role_targets(pool, spec_t, 'irish')['label'] == 'Turkey breast skinless boneless raw'
    assert sg.role_targets(pool, spec_c, 'irish')['label'] == 'Chicken breast skinless raw'


def test_rank_prefers_the_strongest_single_change_over_a_combination():
    def cand(grade_after, floors, n, gain):
        g = {'meaningful_reductions': [{}] if floors else [], 'improvements': [{'relative': gain}], 'passes_strict_meaningful_nutrient_v1': False}
        base = {'grade_after': grade_after, 'gates': g, 'score_before': 1, 'score_after': 0}
        return dict(base, operation='COMBINED', changes=[0] * n) if n > 1 else dict(base, operation='SWAP_FOOD')
    assert sg.rank(cand('Nutriscore_A', False, 1, 0.5)) > sg.rank(cand('Nutriscore_A', False, 1, 0.1))     # swap beats token cut
    assert sg.rank(cand('Nutriscore_A', False, 1, 0.2)) > sg.rank(cand('Nutriscore_A', False, 3, 0.9))     # simple beats a bundle
    assert sg.rank(cand('Nutriscore_A', True, 3, 0.3)) > sg.rank(cand('Nutriscore_A', False, 1, 0.9))      # but floors justify a bundle


def test_without_skin_is_not_treated_as_skin_on():
    pool = [rec('Chicken, breast, without skin, raw', 'chicken', 'raw', 0.7, part='breast'), rec('Chicken, breast, with skin, raw', 'chicken', 'raw', 3.0, part='breast')]
    spec = rules.ROLE_SWAPS[0]['to'][0]
    assert sg.role_targets(pool, spec, 'irish')['label'] == 'Chicken, breast, without skin, raw'
    assert not sg._clean(pool[1], '')


def test_raw_meat_records_with_brine_level_sodium_are_never_candidates():
    brined = rec('Chicken, breast, without skin, raw', 'chicken', 'raw', 0.35, part='breast'); brined['v']['sodium_mg'] = 250.0
    plain = rec('Chicken breast skinless raw', 'chicken', 'raw', 0.7, part='breast'); plain['v']['sodium_mg'] = 60.0
    spec = rules.ROLE_SWAPS[0]['to'][0]
    assert sg.role_targets([brined, plain], spec, 'irish')['label'] == 'Chicken breast skinless raw'
    assert sg.role_targets([brined], spec, 'irish') is None


def test_negated_skin_and_bone_phrases_do_not_exclude_a_record():
    for label in ('Chicken, breast, without bone and skin, raw', 'Chicken breast fillet raw w/o skin'):
        assert sg._clean(rec(label, 'chicken', 'raw', 0.3, part='breast'), ''), label
    assert not sg._clean(rec('Chicken, breast, meat and skin, raw', 'chicken', 'raw', 2.1, part='breast'), '')
    assert not sg._clean(rec('Chicken, breast, with bone, raw', 'chicken', 'raw', 0.3, part='breast'), '')


def test_cut_drops_the_stale_imperial_amount():
    ch = {'operation': 'REDUCE_AMOUNT', 'source_line': '50g / 2 oz. of low-fat spread', 'reduction_fraction': 1 / 3}
    assert sg.rewrite(ch)[0] == '33g of low-fat spread'


def test_bone_in_cuts_and_mince_are_recognised():
    assert sg.bone_in('8 lean lamb cutlets') and sg.bone_in('Chicken, leg quarter, raw, weighed with bone')
    assert not sg.bone_in('4 chicken breasts, boneless') and not sg.bone_in('400g diced pork')
    assert sg.MINCED.search('Veal, mince, raw') and not sg.MINCED.search('Veal shoulder, raw')


def test_low_sodium_salt_row_matches_plain_salt_only():
    import re
    from recipe_wrangler.services.adaptation import nutrition_swap_rules as r
    rule = next(x for x in r.RULES if x['name'] == 'low_sodium_salt')
    assert re.search(rule['base'], 'Salt, white (sea, igneous or rock), no fortification', re.I)
    assert not any(re.search(rule['base'], x, re.I) for x in ('Salt low sodium LoSalt', 'Salted butter', 'Celery salt'))
    assert re.search(rule['target'], 'Salt low sodium LoSalt', re.I) and not re.search(rule['target'], 'Salt, table', re.I)
    assert rule['mass_factor'] == 0.75 and r.TIPS['low_sodium_salt']


def test_oil_and_butter_cut_rules_match_the_right_foods():
    import re
    rules = {c: (lab, line) for c, lab, line, *_ in sg.AMOUNT_RULES}
    assert re.search(rules['oil'][0], 'Oil, sunflower, refined', re.I) and re.search(rules['oil'][0], 'Extra virgin olive oil', re.I)
    assert not re.search(rules['oil'][0], 'Tuna in oil, canned', re.I) and not re.search(rules['butter'][0], 'Peanut butter, smooth', re.I)
    assert re.search(rules['butter'][0], 'Butter, 80% fat minimum, unsalted', re.I)


def test_protected_foods():
    for label, kind in [('Eggs, chicken, whole, raw', 'egg'), ('Apples, eating, raw', 'fruit'), ('Salmon, raw', 'oily_fish'), ('Mackerel, smoked', 'oily_fish'),
                        ('Avocado, raw', 'avocado'), ('Walnuts, kernel only', 'nuts_seed'.replace('nuts_seed', 'nuts_seeds')), ('Almonds, raw', 'nuts_seeds')]:
        assert sg.protected_food(label) == kind, label
    for label in ('Peanut butter, smooth', 'Tuna in oil, canned', 'Cod, raw', 'Olive oil', 'Tahini paste', 'Chicken breast'):
        assert sg.protected_food(label) is None, label


@needs_data
def test_every_new_curated_row_resolves_to_a_real_released_record():
    import gzip, json
    from recipe_wrangler.services.adaptation import nutrition_swap_rules as r
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    high = {m: 9999.0 for m in ('energy_kcal', 'saturated_fat_g', 'sugar_g', 'sodium_mg')} | {'fibre_g': 0, 'protein_g': 0}
    bases = {'skinless_poultry': 'Chicken, leg quarter, raw, meat and skin', 'tuna_in_water': 'Tuna in oil, canned, drained', 'pork_sausage_to_poultry': 'Frankfurter',
             'ketchup_reduced_sugar': 'Tomato ketchup', 'jam_reduced_sugar': 'Jam, fruit',
             'feta_to_cottage': 'Cheese, Feta', 'canned_chickpeas_to_cooked': 'Chickpeas, canned, drained', 'canned_lentils_to_cooked': 'Lentils, red, canned, drained',
             'canned_beans_to_cooked': 'Kidney beans, canned, drained', 'pastry_to_filo': 'Pastry, puff, retail, uncooked'}
    for name, base in bases.items():
        rule = next(x for x in r.RULES if x['name'] == name)
        assert re.search(rule['base'], base, re.I), name
        target, why = sg.pick_target(records, rule, high, None, 'irish', base)
        assert target, (name, why)


def test_addition_gate_is_per_serving_not_score():
    base = dict(energy_kcal=200.0, sugar_g=10.0, saturated_fat_g=1.0, sodium_mg=100.0, fibre_g=2.0, protein_g=4.0)
    yogurt = {'saturated_fat_g': 1.0, 'sodium_mg': 62.0}
    def c(after, grams=100, food=yogurt):
        return {'before_per_serving': base, 'after_per_serving': dict(base, **after), 'grams_per_serving': grams, 'added_per_100g': food}
    assert sg.add_gates(c({'protein_g': 14.6, 'sodium_mg': 224.0, 'energy_kcal': 308.0}, grams=200), 'protein')['passed']  # 200 g yogurt
    assert not sg.add_gates(c({'protein_g': 7.0}), 'protein')['passed']                      # +3 g protein is too little
    assert sg.add_gates(c({'fibre_g': 5.0, 'energy_kcal': 240.0}, grams=80), 'fruit_veg')['passed']
    assert not sg.add_gates(c({'fibre_g': 5.0}, grams=40), 'fruit_veg')['passed']             # less than a portion
    assert not sg.add_gates(c({'fibre_g': 5.0, 'energy_kcal': 400.0}), 'fibre')['passed']      # +200 kcal
    assert not sg.add_gates(c({'fibre_g': 5.0}, food={'saturated_fat_g': 4.0, 'sodium_mg': 10.0}), 'fibre')['passed']  # not a low-fat food


@needs_data
def test_healthy_snack_that_lacks_protein_is_enriched():
    case = next(c for c in json.loads((A / 'cases.json').read_text()) if c['title'].startswith('Apple, almond butter'))
    screen = json.load(open(A / 'screens' / f"{case['recipe_id']}.json"))
    cap = json.load(gzip.open(A / 'inputs' / f"{case['recipe_id']}.json.gz", 'rt'))
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    r = sg.adapt_snapshot(case, screen, ComparisonSnapshot.model_validate(cap['snapshot']), cap['profile']['profile'], cap['source_recipe'], records)
    assert r['outcome'] == 'enriched' and r['diagnosis']['strategy'] == 'enrich'
    assert 'grade_after' not in r['enrichment'] and r['enrichment']['gates']['passed']
    assert 'yogurt' not in r['enrichment']['candidate_label'].lower()  # the recipe is vegan: the diet gate keeps dairy out


@needs_data
def test_descriptor_route_finds_the_healthier_version_of_the_same_food():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    high = {'energy_kcal': 400.0, 'saturated_fat_g': 20.0, 'sugar_g': 20.0, 'sodium_mg': 900.0, 'fibre_g': 0.5, 'protein_g': 5.0}
    found = lambda label: {g: t['label'] for g, t, _ in sg.descriptor_targets(records, label, 'irish', high)}
    pasta = found('Pasta, white, dried, raw')
    assert 'wholegrain' in pasta and re.search(r'pasta', pasta['wholegrain'], re.I)
    milk = found('Milk, whole, pasteurised, average')
    assert 'lower-fat' in milk and re.search(r'milk', milk['lower-fat'], re.I)
    assert not found('Pasta, wholewheat, dried, raw').get('wholegrain')  # already the healthier version


def test_regional_table_first_then_eu():
    pool = [{'collection': 'nutritional_ingredients_eu', 'dataset': 'cofid', 'id': 1}, {'collection': 'nutritional_ingredients_irish', 'dataset': 'irish', 'id': 2},
            {'collection': 'nutritional_ingredients_eu', 'dataset': 'fineli', 'id': 3}]
    assert [r['id'] for r in sg.regional_first(pool, 'irish')[0]] == [2]
    assert [r['id'] for r in sg.regional_first(pool, 'hungarian', 'fineli')[0]] == [3] and sg.regional_first(pool, 'hungarian', 'fineli')[1]
    assert sorted(r['id'] for r in sg.regional_first(pool, 'eu')[0]) == [1, 3]   # EU mode uses the EU collection only


@needs_data
def test_descriptor_route_keeps_the_same_food():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    high = {'energy_kcal': 600.0, 'saturated_fat_g': 60.0, 'sugar_g': 20.0, 'sodium_mg': 900.0, 'fibre_g': 0.5, 'protein_g': 5.0}
    labels = lambda label: [t['label'] for _, t, _ in sg.descriptor_targets(records, label, 'irish', high)]
    assert not any(re.search(r'water|drink|juice', l, re.I) for l in labels('Coconut, desiccated'))
    assert not any(re.search(r'porridge', l, re.I) for l in labels('Oats, porridge, raw'))
    assert all(re.search(r'bacon', l, re.I) for l in labels('Pork, bacon, cooked'))
    assert sg._dataset('catalogue:["CoFID","2021","11-718"]extra') == 'CoFID'
    assert not re.search(sg.swap_rules.ADDITIONS[3][3], 'Oil, vegetable, average', re.I)


def test_flower_honey_is_cut_and_pepperoni_has_a_row():
    label_rx = next(lab for cat, lab, *_ in sg.AMOUNT_RULES if cat == 'sugar')
    assert re.search(label_rx, 'FLOWER HONEY', re.I) and not re.search(label_rx, 'Peas, sugar snap', re.I)
    rule = next(x for x in sg.swap_rules.RULES if x['name'] == 'pepperoni_to_lean_ham')
    assert re.search(rule['target'], 'Ham, turkey breast', re.I) and not re.search(rule['target'], 'Ham, cured', re.I)
    assert re.search(sg.swap_rules.COMPOSITE, 'MOUSSAKA, PORK-BEEF MINCE, POTATO', re.I)


@needs_data
def test_descriptor_route_word_traps():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    high = {'energy_kcal': 600.0, 'saturated_fat_g': 60.0, 'sugar_g': 20.0, 'sodium_mg': 900.0, 'fibre_g': 0.5, 'protein_g': 5.0}
    labels = lambda label: [t['label'] for _, t, _ in sg.descriptor_targets(records, label, 'irish', high)]
    assert not any('light meat' in l.lower() for l in labels('Chicken, whole, raw'))
    assert all(re.search(r'minc', l, re.I) for l in labels('Beef, mince, raw'))
    assert not any(re.search(r'fillet steak', t['label'], re.I) for _, t, _ in sg.descriptor_targets(records, 'Beef, raw', 'irish', high, None, '450g of minced beef'))
    assert not any(re.search(r'without (added )?salt|unsalted', l, re.I) for l in labels('Beef, boiled'))
    assert labels('Pork, fat, average, raw') == []
    assert re.search(sg.swap_rules.SIMILAR_EXCLUDE, 'Gizzard chicken raw', re.I)


def test_enrichment_card_is_written_in_code_without_a_grade():
    e = {'addition': 'a side portion of mixed vegetables', 'grams_per_serving': 80,
         'gates': {'gap': 'fruit_veg', 'gain_per_serving': {'fibre_g': 3.3, 'protein_g': 1.8, 'energy_kcal': 50}}}
    card = sg.enrichment_card(e)
    assert card['serve_with'] == 'Serve with a side portion of mixed vegetables (80 g per person).'
    assert '+3.3 g fibre' in card['benefit'] and '+1 portion of fruit or vegetables' in card['benefit'] and 'grade' not in card['benefit'].lower()


def test_cook_name_and_option_text():
    assert sg.cook_name('Beef, mince, raw, extra lean') == 'beef mince, extra lean'
    assert sg.cook_name('Turkey breast skinless boneless raw') == 'turkey breast skinless boneless'
    salt = {'operation': 'SWAP_FOOD', 'source_line': '2 g Salt', 'matched_label': 'Salt', 'candidate_label': 'Salt low sodium LoSalt', 'weight_before_g': 2.0, 'weight_after_g': 1.5}
    assert '1.5 g instead of 2.0 g' in sg.describe_change(salt) and 'same weight' not in sg.describe_change(salt)


@needs_data
def test_descriptor_route_guards_against_a_different_food():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    high = {'energy_kcal': 600.0, 'saturated_fat_g': 60.0, 'sugar_g': 90.0, 'sodium_mg': 900.0, 'fibre_g': 0.5, 'protein_g': 5.0}
    labels = lambda label, line='': [t['label'] for _, t, _ in sg.descriptor_targets(records, label, 'irish', high, None, line)]
    assert not any(re.search(r'jam|syrup', l, re.I) for l in labels('Sugar, white'))                 # sugar is not jam
    assert not any(re.search(r'rye|chapati|rice', l, re.I) for l in labels('Flour, wheat, white, self raising'))  # wheat flour is not rye flour
    assert not any(re.search(r'breast|fillet', l, re.I) for l in labels('Chicken, whole, raw'))        # a whole bird is not a fillet
    assert not any(re.search(r'breast|fillet', l, re.I) for l in labels('Chicken slices'))
    assert not any(re.search(r'cottage', l, re.I) for l in labels('Cheese, Mozzarella, fresh'))        # mozzarella is not cottage cheese
    salt = {'energy_kcal': 0.0, 'saturated_fat_g': 0.0, 'sugar_g': 0.0, 'sodium_mg': 39100.0, 'fibre_g': 0.0, 'protein_g': 0.0}
    assert any('LoSalt' in t['label'] for _, t, _ in sg.descriptor_targets(records, 'Salt, white (sea, igneous or rock), iodine added, no fluride added', 'irish', salt))  # found without the table


@needs_data
def test_descriptor_route_keeps_bread_kind_raising_agent_and_dishes_out():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    high = {'energy_kcal': 600.0, 'saturated_fat_g': 60.0, 'sugar_g': 20.0, 'sodium_mg': 900.0, 'fibre_g': 0.5, 'protein_g': 5.0}
    labels = lambda label, line='': [t['label'] for _, t, _ in sg.descriptor_targets(records, label, 'irish', high, None, line)]
    assert not any(not re.search(r'ciabatta', l, re.I) for l in labels('Bread, ciabatta'))
    assert not any(not re.search(r'self[- ]raising', l, re.I) for l in labels('Flour, wheat, white, self raising'))
    assert labels('Scones, cheese, homemade') == []
    assert not any(re.search(r'\bdips?\b', l, re.I) for l in labels('Cream, single'))


@needs_data
def test_descriptor_route_ignores_a_lighter_version_with_an_implausible_fibre_jump():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    smooth = {'energy_kcal': 607.0, 'saturated_fat_g': 12.78, 'sugar_g': 6.7, 'sodium_mg': 350.0, 'fibre_g': 6.6, 'protein_g': 22.8}
    assert not any(re.search(r'peanut butter light', t['label'], re.I) for _, t, _ in sg.descriptor_targets(records, 'Peanut butter, smooth', 'irish', smooth))


@needs_data
def test_a_nut_butter_is_never_swapped_for_whole_nuts():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    smooth = {'energy_kcal': 607.0, 'saturated_fat_g': 12.78, 'sugar_g': 6.7, 'sodium_mg': 350.0, 'fibre_g': 6.6, 'protein_g': 22.8}
    assert not [t['label'] for _, t, _ in sg.descriptor_targets(records, 'Peanut butter, smooth', 'irish', smooth) if not re.search(r'butter|paste', t['label'], re.I)]


@needs_data
def test_a_vague_average_fish_record_is_not_swapped_for_a_specific_fish():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    base = next(r for r in records if r['label'] == 'Fish, cooked (average)')
    facets = base.get('annotation', {}).get('facets', {}) if isinstance(base.get('annotation'), dict) else {}
    per100 = {'energy_kcal': 142.0, 'sugar_g': 0.0, 'saturated_fat_g': 1.18, 'sodium_mg': 98.4, 'fibre_g': 0.0, 'protein_g': 22.5}
    assert sg.similar_targets(records, facets.get('base_identity') or 'fish', facets.get('preparation_state'), 'Fish, cooked (average)', per100, None, 'irish', 'energy_kcal', 10.0) == (None, False)


def test_nice_picks_the_closest_kitchen_fraction():
    assert sg.nice(2.25, 'tablespoons of oil') == '2 ¼' and sg.nice(0.75, 'tablespoons of oil') == '¾' and sg.nice(1.5, 'cups') == '1 ½'


@needs_data
def test_irish_wholemeal_pasta_is_priced_with_the_record_the_profiler_selects():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    rule = next(r for r in rules.RULES if r['name'] == 'wholemeal_pasta')
    target, why = sg.pick_target(records, rule, {'fibre_g': 3.0, 'energy_kcal': 350.0, 'saturated_fat_g': 0.5, 'sugar_g': 3.0, 'sodium_mg': 5.0, 'protein_g': 12.0}, None, 'irish', 'Pasta, white, dried, raw')
    assert target['label'] == 'Pasta, dry, wholewheat, raw'


def test_a_swap_on_a_ham_line_is_blocked_because_the_profiler_cannot_tell_hams_apart():
    screen = {'occurrences': [{'position': 1, 'source_line': '200g cooked ham, shredded', 'matched_label': 'Ham, cooked'}],
              'candidates': [{'comparison_status': 'supported_input_comparison', 'position': 1, 'label': 'Ham, turkey, with spices', 'before_per_serving': BASE, 'after_per_serving': BASE, 'operation': 'SWAP_FOOD', 'route': 'direct', 'candidate_id': 'x', 'changes': [], 'collection': 'c', 'grade_after': 'Nutriscore_A', 'grade_before': 'Nutriscore_B', 'metric': 'saturated_fat_g'}]}
    out = sg.swap_review({'title': 'Ham pie'}, screen, {'title': 'Ham pie'}, False)
    assert any('unverifiable_swap' in r for r in out[0]['reasons'])
