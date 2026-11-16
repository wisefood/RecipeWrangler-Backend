"""Small-gain adaptation: gates, rules, card text, router, and exact reproduction of the 12 Nov 2026 results."""
import gzip
import json
from fractions import Fraction
from pathlib import Path

import pytest

from recipe_wrangler.services.adaptation import small_gain as sg
from recipe_wrangler.services.adaptation import small_gain_rules as rules
from recipe_wrangler.services.adaptation.models import ComparisonSnapshot

A = Path(__file__).resolve().parents[1] / 'analysis/nutrition-adaptation-2026-11-11'
BASE = {'energy_kcal': 400.0, 'sugar_g': 10.0, 'saturated_fat_g': 6.0, 'sodium_mg': 800.0, 'fibre_g': 4.0, 'protein_g': 20.0}


def gate(before_grade, after_grade, **changes):
    after = dict(BASE, **changes)
    return sg.gates(BASE, after, 'Nutriscore_' + before_grade, 'Nutriscore_' + after_grade)


def test_a_grade_small_gain_counts_but_not_below_five_percent():
    assert gate('A', 'A', sodium_mg=750.0)['passed']          # 6%
    assert not gate('A', 'A', sodium_mg=770.0)['passed']      # 3.75%


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
    monkeypatch.setattr(sg, 'adapt_active_recipe', lambda rid, source: seen.update(rid=rid, source=source) or {'unsaved': True})
    assert router.small_gain(SmallGainRequest(region='IE'), recipe_id='abc') == {'unsaved': True}
    assert seen == {'rid': 'abc', 'source': 'irish'}


# --- reproduction of the 12 Nov results from captured live inputs (skipped where the analysis folder is absent) ---
needs_data = pytest.mark.skipif(not (A / 'records-2026-10-27-v11.json.gz').exists(), reason='analysis capture not present')


@needs_data
def test_packaged_module_reproduces_the_recorded_results():
    records = json.load(gzip.open(A / 'records-2026-10-27-v11.json.gz', 'rt'))
    cases = {c['recipe_id']: c for c in json.load(open(A / 'cases.json'))}
    wanted = {}  # one adapted recipe per operation/category plus no-adaptation ones
    for rid in cases:
        old = json.load(open(A / 'results' / f'{rid}.json'))
        if old['outcome'] == 'adapted':
            for c in old['adaptation']['changes']:
                wanted.setdefault(c.get('category') or c['operation'], rid)
        elif len(wanted) < 60:
            wanted.setdefault('none:' + old['baseline']['grade'][-1], rid)
    assert len(wanted) > 20
    for rid in set(wanted.values()):
        screen = json.load(open(A / 'screens' / f'{rid}.json'))
        cap = json.load(gzip.open(A / 'inputs' / f'{rid}.json.gz', 'rt'))
        new = sg.adapt_snapshot(cases[rid], screen, ComparisonSnapshot.model_validate(cap['snapshot']),
                                cap['profile']['profile'], cap['source_recipe'], records)
        old = json.load(open(A / 'results' / f'{rid}.json'))
        assert new['outcome'] == old['outcome'], rid
        if new['outcome'] == 'adapted':
            assert new['adaptation']['changes'] == old['adaptation']['changes'], rid
            assert new['adaptation']['after_per_serving'] == old['adaptation']['after_per_serving'], rid
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
