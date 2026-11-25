"""Physical reference conversions, source preservation and food identity guards."""
import pytest

from recipe_wrangler.matching.portions.core import PortionFacets, PortionRecord, select_portion, rank_portions
from recipe_wrangler.matching.portions.external import external_records
from recipe_wrangler.matching.portions.sources import normalize_portion_name, product_identity_compatible, usda_records


def reference(**updates):
    data = dict(record_id='nz:1', food_id='ingredient:milk', source_food_id='nz:milk',
                food_name='Milk', unit='cup', grams_per_unit=250, facets=PortionFacets(),
                source='fixture', region='NZ', description='cup', original={}, unit_volume_ml=250)
    return PortionRecord(**dict(data, **updates))


@pytest.mark.parametrize('unit,volume', [('cup', 237), ('tablespoon', 15), ('ml', 1)])
def test_source_volume_is_not_replaced_by_application_volume(unit, volume):
    record = reference()
    result = select_portion([record], record.food_id, unit, PortionFacets(), unit_volumes={'cup':237,'tablespoon':15})
    assert result['record']['grams_per_unit'] == volume
    assert result['record']['reference_grams_per_unit'] == 250
    assert record.grams_per_unit == 250 and record.unit_volume_ml == 250


def test_rounding_equivalence_does_not_hide_real_density_disagreement():
    records = [reference(facets=PortionFacets(form='liquid'), grams_per_unit=244),
               reference(record_id='nz:2', facets=PortionFacets(form='liquid'), unit='quart', unit_volume_ml=1000, grams_per_unit=977)]
    result = rank_portions(records, records[0].food_id, 'ml', PortionFacets(preparation='raw'))
    assert result['record']['grams_per_unit'] == pytest.approx(.977)
    assert result['match_quality'] == 'approximate'
    records[1] = records[1].model_copy(update={'grams_per_unit':1100})
    assert rank_portions(records, records[0].food_id, 'ml', PortionFacets(preparation='raw'))['record'] is None


def test_mild_source_volume_disagreement_is_visible_only_in_ranked_mode():
    records = [reference(grams_per_unit=250), reference(record_id='nz:2', unit='tablespoon',
                unit_volume_ml=15, grams_per_unit=14.4)]
    assert select_portion(records, records[0].food_id, 'ml', PortionFacets())['record'] is None
    result = rank_portions(records, records[0].food_id, 'ml', PortionFacets())
    assert result['match_quality'] == 'approximate'
    assert result['record']['grams_per_unit'] == 1
    assert result['approximations'][0]['relative_density_spread'] > .01


def test_whole_nuts_precede_cut_forms_but_explicit_form_wins():
    whole = reference(facets=PortionFacets(form='whole'), grams_per_unit=140)
    sliced = reference(record_id='nz:2', facets=PortionFacets(form='slivered'), grams_per_unit=108)
    assert select_portion([sliced, whole], whole.food_id, 'cup', PortionFacets())['record']['grams_per_unit'] == 140
    assert select_portion([sliced, whole], whole.food_id, 'cup', PortionFacets(form='slivered'))['record']['grams_per_unit'] == 108


def test_compound_products_cannot_supply_plain_ingredient_identity():
    assert not product_identity_compatible('rolled oats', 'Rolls, dinner, oat bran')
    assert not product_identity_compatible('creme fraiche', 'Cookies, vanilla sandwich with creme filling')
    assert product_identity_compatible('bread', 'Bread, rye')
    assert product_identity_compatible('toast', 'Bread, wheat, toasted')
    assert product_identity_compatible('dinner roll', 'Rolls, dinner, oat bran')
    assert normalize_portion_name('Apple, domestic, without skin') == 'apple, , peeled'


def test_density_and_nz_native_volume_are_preserved_without_numeric_id_join():
    ausnut = [{'source_food_id':'F1','source_measure_id':'M1','food_name':'Milk',
               'quantity':1,'reference_kind':'density_g_per_ml','descriptors':['density'],
               'source_gram_amount_field':1.03,'source_volume_field':0}]
    nz = [{'source_food_id':'F1','food_name':'Crème fraiche','portion_description':'1 tablespoon (15mL)','grams':14.4}]
    records = list(external_records(ausnut, nz, [], []))
    assert len(records) == 2
    assert records[0].unit == 'ml' and records[0].grams_per_unit == 1.03
    assert records[0].original == ausnut[0]
    assert records[1].unit_volume_ml == 15 and records[1].grams_per_unit == 14.4
    assert records[0].food_id != records[1].food_id
    assert all(r.source_url and r.terms_url for r in records)


def test_external_crosswalk_requires_full_label_not_shared_ingredient():
    row = {'source_food_id':'F1','food_name':'Milk','portion_description':'1 cup (250mL)','grams':250}
    native = [{'usda_id':'1','food_name':'Milk, skim'}]
    assert list(external_records([], [row], [], native))[0].food_id == 'nz_concise2021:F1'
    native[0]['food_name'] = 'Milk'
    assert list(external_records([], [row], [], native))[0].food_id == 'usda_weights:1'


def test_count_aliases_keep_parts_and_no_fish_fillet_to_whole():
    rows = [{'usda_id':'egg','food_name':'Egg white raw', 'portions':[{'amount':1,'grams':33,'portion_desc':'large'}]},
            {'usda_id':'fish','food_name':'Perch raw','portions':[{'amount':1,'grams':64,'portion_desc':'fillet'}]}]
    egg, fish = list(usda_records(rows))
    assert egg.facets.part == 'white'
    assert rank_portions([egg], egg.food_id, 'piece', PortionFacets(size='medium', part='white'))['record']['grams_per_unit'] == 33
    assert rank_portions([fish], fish.food_id, 'piece', PortionFacets(size='medium'))['record'] is None


def test_fractional_measure_rounding_prefers_original_larger_amount():
    whole = reference(source_food_id='usda_weights:08120', grams_per_unit=81, original={'amount':1})
    fraction = reference(record_id='nz:2', source_food_id=whole.source_food_id,
                         grams_per_unit=27/.33, original={'amount':.33})
    assert select_portion([fraction, whole], whole.food_id, 'cup', PortionFacets())['record']['grams_per_unit'] == 81


def test_import_does_not_override_established_default_and_direct_spoon_wins():
    old = reference(source_food_id='usda_weights:1', unit='tablespoon', unit_volume_ml=15, grams_per_unit=15)
    imported = reference(record_id='nz:2', unit='tablespoon', unit_volume_ml=15, grams_per_unit=20)
    assert select_portion([imported, old], old.food_id, 'tablespoon', PortionFacets())['record']['grams_per_unit'] == 15
    cup = reference(record_id='usda:cup', source_food_id=old.source_food_id, grams_per_unit=244,
                    unit_volume_ml=237)
    assert rank_portions([cup, old], old.food_id, 'tablespoon', PortionFacets(preparation='raw'),
                         unit_volumes={'tablespoon':15})['record']['grams_per_unit'] == 15


@pytest.mark.parametrize('mode,expected', [('ranked',14.4), ('strict',0)])
def test_unverified_fat_percentage_is_explained_or_rejected(monkeypatch, mode, expected):
    from copy import deepcopy
    from recipe_wrangler.tools import ingredient_weight_tool as tool
    from recipe_wrangler.matching.portions import sources, runtime
    monkeypatch.setattr(tool, 'WEIGHT_PORTION_TABLE_ONLY', False)  # exercises the resolver path
    monkeypatch.setattr(sources, 'resolve_food_name', lambda *args:None)
    monkeypatch.setattr(tool, 'canonical_name_to_usda', lambda *args:None)
    monkeypatch.setattr(tool, '_weight_name_usda_link', lambda *args:None)
    monkeypatch.setattr(tool, '_embedding_usda_link', lambda *args, **kwargs:None)
    match = {'record':reference(food_name='Crème fraiche', grams_per_unit=14.4).model_dump(),
             'status':'matched','match_quality':'exact','match_score':1.0,'approximations':[]}
    monkeypatch.setattr(runtime, 'match_request', lambda *args:deepcopy(match))
    result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names':['creme fraiche, 28% fat'],
             'measurements':['1 tablespoon'], 'portion_inputs':[{'match_mode':mode}], 'return_details':True})
    assert result['weights'] == [expected]
    detail = result['details'][0]
    if mode == 'ranked':
        assert detail['approximate'] and detail['match_score'] == .9
        assert detail['approximation_reasons'][0]['reason'] == 'fat_percentage_not_verified'
    else:
        assert detail['error']


def test_derived_products_do_not_replace_the_plain_ingredient():
    assert not product_identity_compatible('raisin', 'Bagels, cinnamon-raisin')
    assert not product_identity_compatible('nut, peanut', 'Sauce, peanut, made from coconut, water, sugar, peanuts')
    assert not product_identity_compatible('sesame seed, with hull', 'Salad dressing, sesame seed dressing, regular')
    assert product_identity_compatible('soy sauce', 'Sauce, soy')
    assert product_identity_compatible('peanut sauce', 'Sauce, peanut')


def test_spelling_aliases_for_soy_and_kiwifruit():
    assert 'soy beans' in normalize_portion_name('Soya beans, dried')
    assert normalize_portion_name('Kiwi fruit, with skin') == 'kiwifruit, with skin'


def test_unsweetened_coconut_flakes_map_to_dried_coconut_meat():
    assert normalize_portion_name('coconut flake') == 'desiccated coconut'
    assert normalize_portion_name('Coconut flakes, sweetened') == 'coconut flakes, sweetened'


def test_babyfood_never_replaces_a_plain_ingredient():
    assert not product_identity_compatible('brown rice', 'Babyfood, peas and brown rice')
    assert normalize_portion_name('kiwi') == 'kiwifruit'
    assert normalize_portion_name('2 kiwis') == '2 kiwifruit'


def test_nz_whole_fruit_bracket_weight_becomes_a_with_skin_record_on_the_reviewed_identity():
    from recipe_wrangler.matching.portions.external import external_records
    row = {'source_food_id': 'L1267', 'food_name': 'Kiwifruit, Zespri Green Kiwifruit, flesh & seeds, raw',
           'portion_description': '1 fruit (113 g)', 'grams': 93}
    records = list(external_records([], [row], [], []))
    by_peel = {r.facets.peel: r for r in records}
    assert by_peel['without_peel'].grams_per_unit == 93
    assert by_peel['with_peel'].grams_per_unit == 113
    assert {r.food_id for r in records} == {'usda_weights:09148'}


def test_bakery_and_dish_compounds_never_replace_the_plain_ingredient():
    assert not product_identity_compatible('apple', 'Croissants, apple')
    assert not product_identity_compatible('blueberries', 'Muffins, blueberry')
    assert not product_identity_compatible('apple', 'Pie, apple')
    assert product_identity_compatible('apple pie', 'Pie, apple')


def test_mixed_dishes_and_blends_never_replace_a_single_ingredient():
    assert not product_identity_compatible('spaghetti', "DENNY'S, spaghetti and meatballs")
    assert not product_identity_compatible('sugar', 'Sweeteners, for baking, contains sugar and sucralose')
    assert product_identity_compatible('salt and pepper', 'Salt and pepper mix')


def test_common_recipe_names_map_to_their_reference_food():
    assert normalize_portion_name('flour') == 'wheat flour, white, all-purpose'
    assert normalize_portion_name('plain flour') == 'wheat flour, white, all-purpose'
    assert normalize_portion_name('courgette') == 'zucchini'
    assert normalize_portion_name('red bell pepper') == 'red sweet pepper'
    assert normalize_portion_name('penne') == 'pasta'
    assert normalize_portion_name('whole wheat penne') == 'whole wheat penne'


def test_usda_cut_wording_with_and_is_not_a_dish():
    assert product_identity_compatible('whole goose', 'Goose, domesticated, meat and skin, raw')
    assert product_identity_compatible('chicken thigh', 'Chicken, broilers or fryers, thigh, meat and skin, raw')


def test_plain_milk_is_whole_cow_milk_and_bare_regular_is_a_piece():
    from recipe_wrangler.matching.portions.sources import resolve_food_name
    assert resolve_food_name('milk')['usda_id'] == '01077'
    rows = [{'usda_id': '11821', 'food_name': 'Peppers, sweet, red, raw',
             'portions': [{'portion_seq': 1, 'amount': 1.0, 'portion_desc': 'regular', 'grams': 120.0, 'grams_per_unit': 120.0}]}]
    assert [r.unit for r in usda_records(rows)] == ['piece']


def test_unit_naming_the_food_itself_is_a_piece():
    rows = [{'usda_id': '11951', 'food_name': 'Peppers, sweet, yellow, raw',
             'portions': [{'portion_seq': 1, 'amount': 1.0, 'portion_desc': 'pepper, large (3-3/4" long, 3" dia)', 'grams': 186.0, 'grams_per_unit': 186.0}]}]
    assert [r.unit for r in usda_records(rows)] == ['piece']


def test_ice_measure_is_not_the_liquid_and_joined_food_unit_is_a_piece():
    rows = [{'usda_id': '11507', 'food_name': 'Sweet potato, raw, unprepared',
             'portions': [{'portion_seq': 1, 'amount': 1.0, 'portion_desc': 'sweetpotato, 5" long', 'grams': 130.0, 'grams_per_unit': 130.0}]},
            {'food_code': '94000100', 'food_name': 'Water, tap',
             'portions': [{'portion_seq': 1, 'amount': 1.0, 'portion_desc': 'cup ice', 'grams': 110.0, 'grams_per_unit': 110.0}]}]
    records = list(usda_records(rows))
    assert records[0].unit == 'piece'
    assert 'ice_measure' in records[1].blockers
    assert normalize_portion_name('smoked paprika') == 'paprika'


def test_external_unit_naming_the_food_is_a_piece():
    row = {'source_food_id': 'F006328', 'source_measure_id': 1, 'food_name': 'Parsnip, peeled, raw',
           'descriptors': ['parsnip'], 'reference_kind': 'portion', 'quantity': 1,
           'source_gram_amount_field': 72.0, 'source_volume_field': None}
    assert [r.unit for r in external_records([row], [], [], [])] == ['piece']


def test_recipe_wording_aliases_and_external_item_units():
    assert normalize_portion_name('liquid honey') == 'honey'
    assert normalize_portion_name('ground almonds') == 'almond meal'
    row = {'source_food_id': 'A45', 'food_name': 'Bread, pita, wholemeal', 'portion_description': '1 pita', 'grams': 85}
    assert [r.unit for r in external_records([], [row], [], [])] == ['piece']


def test_snack_size_and_shaved_slices_are_not_the_default_slice():
    rows = [{'usda_id': '18069', 'food_name': 'Bread, white',
             'portions': [{'portion_seq': 1, 'amount': 1.0, 'portion_desc': 'slice, snack-size', 'grams': 10.0, 'grams_per_unit': 10.0},
                          {'portion_seq': 2, 'amount': 1.0, 'portion_desc': 'slice', 'grams': 29.0, 'grams_per_unit': 29.0}]}]
    by_desc = {r.description: r for r in usda_records(rows)}
    assert 'unsupported_size_scale' in by_desc['slice, snack-size'].blockers
    assert not by_desc['slice'].blockers
    ham = [{'usda_id': '1', 'food_name': 'Ham', 'portions': [{'portion_seq': 1, 'amount': 1.0, 'portion_desc': 'slice shaved', 'grams': 12.0, 'grams_per_unit': 12.0}]}]
    assert 'unsupported_size_scale' in list(usda_records(ham))[0].blockers


def test_crust_not_eaten_and_single_serve_measures_are_not_standard_portions():
    from recipe_wrangler.matching.portions.sources import UNSUPPORTED_SCALE_RE
    for desc in ('slice, crust not eaten', 'cup or sachet single serve', 'slice, snack-size', 'slice shaved', 'jumbo'):
        assert UNSUPPORTED_SCALE_RE.search(desc), desc
    for desc in ('slice', 'cup', 'slice regular', 'medium'):
        assert not UNSUPPORTED_SCALE_RE.search(desc), desc
