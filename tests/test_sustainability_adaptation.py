import pytest

pytest.importorskip('recipe_wrangler.services.adaptation.nutrition_adaptation', reason='needs the nutrition adaptation modules')
import re

from recipe_wrangler.services.adaptation import sustainability_swap_rules as rules
from recipe_wrangler.services.adaptation.food_roles import role_problems
from recipe_wrangler.services.adaptation.sustainability_adaptation import local_problems


def blocked(src, tgt, baked=False):
    return bool(role_problems(src, tgt, lenient=True) + local_problems(src, tgt, baked))


@pytest.mark.parametrize('src,tgt', [  # bad pairs from the 13 Nov sustainability run
    ('Cream sour', 'Coconut milk, light, canned'),
    ('Cheese, Cheddar type, hard', 'Cheese, processed, plain'),
    ('Cod raw', 'Tuna, flesh only, raw'),
    ('Fish, cooked (average)', 'Poultry, cooked (average)'),
    ('Bacon fat smoked prepared', 'CHICKEN'),
    ('Liver pig raw', 'Turkey, flesh only, raw'),
    ('Beef, shoulder', 'Venison, meat only, raw'),
    ('Milk semi-skimmed', 'CURD MILK, PLAIN, 2.5% FAT'),
    ('Cheese, Cheddar type, half fat', 'Yogurt, Greek style, plain'),
    ('Rice, white, Italian Arborio, raw', 'Couscous, plain, raw'),
    ('Rice, white, basmati, raw', 'Flour, chapati, brown'),
    ('Bacon rashers, back, raw', 'Turkey, flesh only, raw'),
    ('Beef, stewing steak, raw', 'Cod, flesh only, raw'),
    ('Butter, 80% fat minimum, unsalted', 'Olive oil, extra virgin'),
    ('Tortilla, wheat, soft', 'Barley'),
    ('RICE, unpolished', 'Oat flakes'),
    ('Rice, white, Italian Arborio, raw', 'RICE, QUINOA AND MULTIGRAIN MIX'),
    ('Cream sour', 'MILK Pasteurized 1,5-1,8%'),
    ('Cuttle fish', 'Sardine'),
    ('Cheese, Halloumi', 'Edam cheese, from cow\'s milk'),
    ('Whipping cream, liquid', 'Breast milk replacement from 6 months'),
    ('Beef, raw', 'Chicken, breast, casseroled, meat only'),
    ('Gilthead seabream, wild, raw', 'Plant-based ham, prepared'),
    ('Duck, raw, meat, fat and skin', 'Fat chicken'),
    ('Chicken, breast, without skin, raw', 'Chicken fat'),
    ('Chicken, breast, without skin, raw', 'Rillettes of goose'),
    ('Veal, ribs', 'Chicken frankfurter'),
    ('Yogurt, Greek style, plain', 'Water kefir or Tibicos, prepacked'),
    ('Agave syrup', 'Sugar, icing'),
    ('Beef, raw', 'Smoked minced lard'),
    ('Pork, tenderloin, raw', 'Brain, pork, raw'),
    ('Tuna, canned in brine, drained', 'Sardines, canned in brine, drained'),  # fish for fish is a table row, never a search result
    ('Pork, tenderloin, raw', 'Pork, back fat, rind less, raw'),
    ('European bass, raw', 'Sardine'),
    ('Butter, unsalted', 'Margarine low-fat (39%)'),
    ('Goose, domesticated, meat only, raw', 'Turkey fat'),
    ('Cod, raw', 'Beef, raw'),
    ('Rice, white, basmati, raw', 'Wheat, bulgur, raw'),
    ('Butter, 80% fat minimum, unsalted', 'Croutons, for spreads, prepacked'),
    ('Cheese, Feta', 'Cheese, cottage, plain'),
    ('Cod raw', 'HAKE, HOKI, RAW'),
    ('Haddock, raw', 'Mackerel, raw'),
    ('Agave syrup', 'Jam, reduced sugar'),
    ('Yogurt, Greek style, plain', 'milk, 1.5%'),
    ('Yogurt, low fat, plain', 'Cheese, skimmed milk, matured'),
    ('Butter, 80% fat minimum, unsalted', 'Grapeseed oil'),
])
def test_bad_pairs_blocked(src, tgt):
    assert blocked(src, tgt)


def test_butter_goes_to_margarine_only():
    assert blocked('Butter, unsalted', 'Sunflower oil', baked=False)
    assert blocked('Butter, unsalted', 'Sunflower oil', baked=True)
    assert not blocked('Butter, unsalted', 'Margarine, 80% fat', baked=False)


@pytest.mark.parametrize('src,tgt', [
    ('Beef, stewing steak, raw', 'Chicken, breast, meat only, raw'),
    ('Lamb leg raw', 'Turkey, flesh only, raw'),
    ('Cheese, Cheddar type, hard', 'Cheese, mozzarella, fresh'),
    ('Prawns, cooked', 'Mussels, boiled'),
])
def test_realistic_swaps_allowed(src, tgt):
    assert not blocked(src, tgt)


def test_table_targets():
    by = {r['name']: r for r in rules.RULES}
    assert re.search(by['red_meat_to_poultry']['target'], 'Chicken, breast, meat only, raw', re.I)
    assert not re.search(by['red_meat_to_poultry']['target'], 'Chicken, skin, raw', re.I)
    assert 'butter_to_oil' not in by and 'white_fish_to_hake_saithe' not in by


def test_processed_meat_has_poultry_versions():
    by = {r['name']: r for r in rules.RULES}
    assert re.search(by['ham_to_poultry_ham']['target'], 'Turkey ham', re.I) and not re.search(by['ham_to_poultry_ham']['target'], 'Plant-based ham', re.I)
    assert re.search(by['bacon_to_poultry_bacon']['target'], 'Cooked turkey meat, bacon, smoked', re.I)
    assert re.search(by['salami_to_poultry_salami']['target'], 'Turkey salami', re.I)
    assert {'ham_to_poultry_ham', 'bacon_to_poultry_bacon', 'salami_to_poultry_salami'} == rules.ALLOW_PROCESSED
    assert not re.search(by['ham_to_poultry_ham']['source'], 'Dry-cured ham \"Karst type\"', re.I) and re.search(by['ham_to_poultry_ham']['source'], 'Ham, cooked', re.I)


def test_bone_in_edible_fraction():
    assert rules.edible_fraction('Lamb cutlets') == 0.75 and rules.edible_fraction('Pork loin chops, bone-in') == 0.75
    assert rules.edible_fraction('Veal, ribs') == 0.67 and rules.edible_fraction('Pork spare ribs') == 0.70
    assert rules.edible_fraction('leg of lamb on the bone') == 0.82 and rules.edible_fraction('Beef rib steak') == 0.77 and rules.edible_fraction('Beef short ribs') == 0.71


def test_cod_to_hake_is_indicative():
    by = {r['name']: r for r in rules.RULES}
    assert 'white_fish_to_hake' in rules.INDICATIVE and re.search(by['white_fish_to_hake']['target'], 'HAKE, HOKI, RAW', re.I)


def test_why_no_suggestion():
    from recipe_wrangler.services.adaptation.sustainability_adaptation import WHY, why_no_suggestion
    assert why_no_suggestion([{'co2_share': 0.5, 'skipped': 'protected:egg'}], {}) == 'main_food_protected'
    assert why_no_suggestion([{'co2_share': 0.5}], {'below_co2_floor': 1}) == 'saving_too_small'
    assert why_no_suggestion([{'co2_share': 0.5}], {'allergen_or_diet': 1}) == 'alternatives_add_allergen'
    assert why_no_suggestion([{'co2_share': 0.5}], {}) == 'no_lower_impact_option'
    assert set(WHY) >= {'already_low_impact', 'saving_too_small', 'alternatives_add_allergen'}


def test_poultry_target_is_plain_meat():
    import re
    from recipe_wrangler.services.adaptation.sustainability_swap_rules import POULTRY_CUT
    for bad in ('Chicken pieces, coated, takeaway', 'Chicken breasts in casing', 'Chicken slices', 'Chicken cordon bleu raw', 'Chicken fillet in batter raw'):
        assert not re.search(POULTRY_CUT, bad, re.I), bad
    for good in ('Chicken, meat, average, raw', 'Chicken fillet raw', 'Turkey meat'):
        assert re.search(POULTRY_CUT, good, re.I), good


def test_line_without_a_stated_amount_is_not_a_hotspot_candidate():
    from recipe_wrangler.services.adaptation.sustainability_adaptation import STATED_AMOUNT
    for line in ('creme fraiche', 'Black pepper, to taste'):
        assert not STATED_AMOUNT.search(line)
    for line in ('1 tin of tuna', '2 x 200g cod', 'a tin of tuna', '½ tablespoon basil', '450g beef'):
        assert STATED_AMOUNT.search(line)
