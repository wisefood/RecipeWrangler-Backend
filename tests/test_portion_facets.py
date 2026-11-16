"""Identity, explicit facet constraints, ambiguity and source-preserving adapters."""
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from recipe_wrangler.matching.portions.core import PortionFacets, PortionInput, PortionRecord, infer_facets, select_portion
from recipe_wrangler.matching.portions.sources import european_records, usda_records, identity_key
from recipe_wrangler.matching.portions.runtime import match_request
from recipe_wrangler.tools import ingredient_weight_tool as tool


@pytest.fixture(autouse=True)
def no_external_lookups(monkeypatch):
    monkeypatch.setattr(tool, 'WEIGHT_PORTION_TABLE_ONLY', False)  # these tests exercise the resolver path
    monkeypatch.setattr('recipe_wrangler.matching.portions.sources.resolve_food_name', lambda *args: None)
    monkeypatch.setattr(tool, '_weight_name_usda_link', lambda *args: None)
    monkeypatch.setattr(tool, '_embedding_usda_link', lambda *args, **kwargs: None)
    monkeypatch.setattr('recipe_wrangler.matching.portions.sources.usda._unit_volumes_ml',
                        lambda *args: {'cup': 237, 'tablespoon': 15, 'teaspoon': 5})


def record(**updates):
    data = dict(record_id='fixture:1', food_id='ingredient:potato', source_food_id='fixture:potato',
                food_name='potato', unit='piece', grams_per_unit=70, facets=PortionFacets(size='medium', preparation='raw', peel='without_peel'),
                source='fixture', region='NL', description='medium raw peeled', original={})
    data.update(updates)
    return PortionRecord(**data)


def test_food_id_is_required_before_facet_equivalence():
    result = select_portion([record(food_id='ingredient:banana')], 'ingredient:potato', 'piece', PortionFacets(size='medium'))
    assert result['status'] == 'unresolved'
    assert result['rejections'] == {'food_identity_mismatch': 1}


@pytest.mark.parametrize('field,value', [('preparation', 'boiled'), ('peel', 'with_peel'), ('form', 'sliced'), ('size', 'large'), ('part', 'fillet')])
def test_every_explicit_facet_must_be_known_and_compatible(field, value):
    result = select_portion([record()], 'ingredient:potato', 'piece', PortionFacets(**{field: value}))
    assert result['status'] == 'unresolved'
    assert any(key.startswith(field) for key in result['rejections'])


def test_region_preference_cannot_override_facets():
    incompatible = record(facets=PortionFacets(size='large'), region='NL')
    compatible = record(record_id='fixture:2', region='US')
    result = select_portion([incompatible, compatible], 'ingredient:potato', 'piece', PortionFacets(size='medium'))
    assert result['record']['record_id'] == 'fixture:2'


def test_european_preference_applies_after_compatibility():
    result = select_portion([record(region='US', grams_per_unit=173), record()], 'ingredient:potato', 'piece', PortionFacets(size='medium'))
    assert result['record']['grams_per_unit'] == 70


def test_conflicting_equal_priority_records_abstain():
    result = select_portion([record(), record(record_id='fixture:2', grams_per_unit=80)], 'ingredient:potato', 'piece', PortionFacets())
    assert result['status'] == 'ambiguous' and result['record'] is None


def test_no_silent_medium_assumption_for_piece():
    result = select_portion(european_records(), 'ingredient:potato', 'piece', PortionFacets(peel='without_peel'))
    assert result['status'] == 'ambiguous'


def test_usda_adapter_retains_native_ids_amount_and_unknown_facets():
    rows = [{'usda_id': '09040', 'food_name': 'Bananas, raw', 'portions': [
        {'amount': 2, 'grams': 200, 'portion_desc': 'medium'},
        {'amount': 1, 'grams': 123, 'portion_desc': 'extra large'},
        {'amount': 1, 'grams': 100, 'portion_desc': 'cup, chopped or sliced'},
    ]}]
    first, extra, ambiguous = list(usda_records(rows))
    assert first.food_id == 'ingredient:banana'
    assert first.source_food_id == 'usda_weights:09040'
    assert first.grams_per_unit == 100 and first.original['amount'] == 2
    assert first.facets.size == 'medium' and first.facets.peel is None
    assert 'unsupported_size_scale' in extra.blockers
    assert 'ambiguous_form' in ambiguous.blockers
    assert list(usda_records([{'usda_id': 'other', 'food_name': 'x', 'portions': []}, *rows]))[0].record_id == first.record_id
    assert identity_key('323793') == 'usda_weights:323793'  # Never assume an FDC ID crosswalk.


def test_explicit_contradictions_and_unknown_fields_are_not_silently_accepted():
    _, blockers = infer_facets('raw boiled potato')
    assert 'ambiguous_preparation' in blockers
    with pytest.raises(ValidationError):
        PortionInput(facets={'invented': 'yes'})


@pytest.mark.parametrize('name,measurement,portion_input,grams', [
    ('potato', '2 pieces', {'food_id': 'ingredient:potato', 'facets': {'size': 'medium', 'peel': 'without_peel', 'preparation': 'raw'}}, 140),
    ('banana', '2 medium', {'facets': {'peel': 'without_peel'}}, 260),
    ('potato', '2 pieces', {'facets': {'size': 'medium', 'preparation': 'boiled'}}, 0),
    ('banana', '1 medium', {'food_id': 'ingredient:potato'}, 0),
    ('banana', '2 pieces', {}, 260),  # unsized count = medium, recorded as an assumption
    ('potato', '2 medium', {'facets': {'size': 'large'}}, 0),
    ('potato', '', {'facets': {'size': 'medium'}}, 0),
    ('banana', '100 g', {'facets': {'size': 'medium'}}, 100),
])
def test_real_tool_accepts_facets_and_never_guesses_after_no_match(name, measurement, portion_input, grams):
    with patch.object(tool, 'canonical_name_to_usda', return_value=None), patch('recipe_wrangler.matching.portions.sources._native_food_rows', return_value={}):
        result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': [name], 'measurements': [measurement], 'portion_inputs': [portion_input], 'return_details': True})
    assert result['weights'] == [grams]
    if grams == 0:
        assert result['details'][0]['error']
        assert result['details'][0]['weight_grams'] is None
    else:
        assert not result['details'][0]['error']
    assert 'facet_matching' in result['details'][0]


def test_wrong_food_source_id_cannot_bypass_identity_validation():
    with patch.object(tool, 'weight_food_name', return_value='Bananas, raw'):
        result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['potato'], 'measurements': ['1 medium'], 'portion_inputs': [{'food_id': 'usda_weights:09040'}], 'return_details': True})
    assert result['weights'] == [0]
    assert 'unvalidated_food_id' in result['details'][0]['facet_matching']['blockers']


def test_specific_source_id_constraints_cannot_be_overridden():
    with patch.object(tool, 'weight_food_name', return_value='Apples, raw, with skin'), patch.object(tool, '_usda_link_mismatch_reason', return_value=None), patch.object(tool, '_candidate_has_lexical_identity', return_value=True), patch('recipe_wrangler.matching.portions.sources._native_food_rows', return_value={}):
        result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['apple'], 'measurements': ['1 medium'], 'portion_inputs': [{'food_id': 'usda_weights:09003', 'facets': {'peel': 'without_peel'}}], 'return_details': True})
    assert result['weights'] == [0]
    assert 'conflicting_input_peel' in result['details'][0]['facet_matching']['blockers']
    assert result['details'][0]['match_score'] == 0
    assert not result['details'][0]['approximate']


def test_parallel_arrays_must_align():
    with pytest.raises(ValueError, match='equal lengths'):
        tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['banana'], 'measurements': ['1 medium'], 'portion_inputs': []})


def test_fndds_codes_remain_distinct_and_can_match_by_validated_native_id():
    row = {'usda_id': None, 'food_code': '11000000', 'food_name': 'Milk, human',
           'portions': [{'amount': 1, 'grams': 246, 'portion_desc': 'cup'}]}
    records = list(usda_records([row]))
    assert records[0].food_id == 'fndds_weights:11000000'
    assert records[0].source_food_id == 'fndds_weights:11000000'
    with patch('recipe_wrangler.matching.portions.sources._native_food_rows', return_value={'fndds_weights:11000000': [row]}):
        result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['human milk'], 'measurements': ['2 cups'], 'portion_inputs': [{'food_id': 'fndds_weights:11000000'}], 'return_details': True})
    assert result['weights'] == [492]
    assert result['details'][0]['portion_match']['source_food_id'] == 'fndds_weights:11000000'


def test_region_eu_keeps_reviewed_european_preference():
    result = select_portion([record(region='US', grams_per_unit=173), record()], 'ingredient:potato', 'piece', PortionFacets(size='medium'), region='EU')
    assert result['record']['region'] == 'NL'


def test_state_node_forwards_structured_facets():
    from recipe_wrangler.schemas import RecipeState
    state = RecipeState(ingredient_names=['potato'], measurements=['2 pieces'],
                        portion_inputs=[{'food_id': 'ingredient:potato', 'facets': {'size': 'medium', 'peel': 'without_peel'}}])
    with patch('recipe_wrangler.matching.portions.sources._native_food_rows', return_value={}):
        state = tool.Ingredient_Weight_Node(state)
    assert state.weights == [140]
    detail = state.pipeline_trace['weight_calculation']['details'][0]
    assert detail['facet_matching']['record']['source_food_id'] == 'nevo:1'


def test_cooked_native_identity_is_not_replaced_by_a_raw_family_default():
    row = {'usda_id': '11367', 'food_name': 'Potatoes, boiled, cooked without skin',
           'portions': [{'amount': 1, 'grams': 167, 'portion_desc': 'medium'}]}
    with patch.object(tool, 'canonical_name_to_usda', return_value={'usda_id': '11367', 'canonical': row['food_name']}), patch.object(tool, 'weight_food_name', return_value=row['food_name']), patch('recipe_wrangler.matching.portions.sources._native_food_rows', return_value={'11367': [row]}):
        result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['potato boiled peeled'], 'measurements': ['1 medium'], 'portion_inputs': [{'facets': {'preparation': 'boiled'}}], 'return_details': True})
    assert result['weights'] == [167]
    assert result['details'][0]['portion_match']['food_id'] == 'usda_weights:11367'


@pytest.mark.parametrize('description,blocker', [
    ('cup, quartered or chopped', 'ambiguous_form'),
    ('cup, not packed', 'unsupported_packing_negation'),
])
def test_alternative_forms_and_negated_packing_remain_blocked(description, blocker):
    records = list(usda_records([{'usda_id': '09003', 'food_name': 'Apple, raw',
                                'portions': [{'amount': 1, 'grams': 125, 'portion_desc': description}]}]))
    assert blocker in records[0].blockers
    assert select_portion(records, 'ingredient:apple', 'cup', PortionFacets())['status'] == 'unresolved'


def test_metric_volume_conversion_keeps_native_grams_and_profile():
    source = record(unit='cup', grams_per_unit=237, unit_volume_ml=237,
                    volume_profile='existing_usda_unit_volume_profile', facets=PortionFacets(form='whole'))
    result = select_portion([source], 'ingredient:potato', 'ml', PortionFacets(form='whole'))
    assert result['record']['grams_per_unit'] == 1
    assert result['record']['unit'] == 'ml'
    assert result['record']['reference_unit'] == 'cup'
    assert result['record']['reference_grams_per_unit'] == 237
    assert result['record']['unit_conversion']['reference_volume_ml'] == 237
    assert result['record']['volume_profile'] == 'existing_usda_unit_volume_profile'


def test_metric_conversion_without_a_known_profile_is_rejected():
    assert select_portion([record(unit='cup')], 'ingredient:potato', 'ml', PortionFacets())['status'] == 'unresolved'


def test_unrequested_form_prefers_whole_over_chopped_but_requested_form_is_strict():
    whole = record(unit='cup', facets=PortionFacets(form='whole'))
    chopped = record(record_id='fixture:2', unit='cup', grams_per_unit=100, facets=PortionFacets(form='chopped'))
    assert select_portion([chopped, whole], 'ingredient:potato', 'cup', PortionFacets())['record']['grams_per_unit'] == 70
    assert select_portion([chopped, whole], 'ingredient:potato', 'cup', PortionFacets(form='chopped'))['record']['grams_per_unit'] == 100


def test_authoritative_total_mass_in_name_precedes_outer_count():
    result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['(600g) chicken thighs'], 'measurements': ['4 medium'], 'portion_inputs': [{}], 'return_details': True})
    assert result['weights'] == [600]
    assert result['details'][0]['match_type'] == 'direct_mass'


def test_medium_carrot_uses_real_regular_carrot_with_explained_score():
    import json
    from pathlib import Path
    from recipe_wrangler.matching.portions.sources import usda_records
    rows = json.loads((Path(__file__).resolve().parents[1] / 'data/processed/usda/usda-weights-v2.json').read_text())
    row = next(r for r in rows if r.get('usda_id') == '11124')
    with patch.object(tool, 'weight_food_name', return_value=row['food_name']), patch('recipe_wrangler.matching.portions.sources._native_food_rows', return_value={'11124': [row]}):
        result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['carrot'], 'measurements': ['2 medium'], 'portion_inputs': [{'food_id': 'usda_weights:11124'}], 'return_details': True})
    detail = result['details'][0]
    assert result['weights'] == [120]
    assert detail['match_score'] == 0.85
    assert detail['approximate']
    assert detail['match_type'] == 'faceted_portion_approximate'
    assert detail['portion_match']['description'] == 'regular carrot'
    assert detail['portion_match']['facets']['size'] is None
    assert detail['approximation_reasons'][0]['reason'] == 'regular_whole_item_used_for_medium'
    assert detail['facet_matching']['score_kind'] == 'heuristic_compatibility'
    assert detail['portion_match']['original']['grams'] == 60


def test_strict_mode_is_available_without_regular_to_medium_assumption():
    row = {'usda_id': '11124', 'food_name': 'Carrots, raw', 'portions': [{'portion_desc': 'regular carrot', 'amount': 1, 'grams': 60}]}
    with patch.object(tool, 'weight_food_name', return_value=row['food_name']), patch('recipe_wrangler.matching.portions.sources._native_food_rows', return_value={'11124': [row]}):
        result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['carrot'], 'measurements': ['1 medium'], 'portion_inputs': [{'food_id': 'usda_weights:11124', 'match_mode': 'strict'}], 'return_details': True})
    assert result['weights'] == [0]
    assert result['details'][0]['error']


def test_ranked_matching_never_replaces_a_compatible_exact_match():
    from recipe_wrangler.matching.portions.core import rank_portions
    exact = record(region='US', facets=PortionFacets(size='medium', form='whole'))
    regular = record(record_id='fixture:regular', region='NL', description='regular potato', facets=PortionFacets(form='whole'))
    result = rank_portions([regular, exact], 'ingredient:potato', 'piece', PortionFacets(size='medium'))
    assert result['record']['record_id'] == exact.record_id
    assert result['match_quality'] == 'exact' and result['match_score'] == 1.0


@pytest.mark.parametrize('field,value', [('preparation', 'boiled'), ('form', 'sliced'), ('peel', 'with_peel'), ('part', 'fillet'), ('preservation', 'frozen')])
def test_approximation_never_ignores_conflicting_food_facets(field, value):
    from recipe_wrangler.matching.portions.core import rank_portions
    source = record(facets=PortionFacets(preparation='raw', form='whole', peel='without_peel', preservation='fresh'))
    query = PortionFacets(size='medium', **{field: value})
    assert rank_portions([source], 'ingredient:potato', 'piece', query)['record'] is None


def test_adjacent_size_proxy_is_scored_and_never_rescaled():
    from recipe_wrangler.matching.portions.core import rank_portions
    source = record(grams_per_unit=50, facets=PortionFacets(size='small', form='whole'))
    result = rank_portions([source], 'ingredient:potato', 'piece', PortionFacets(size='medium'))
    assert result['match_score'] == 0.7 and result['match_quality'] == 'approximate'
    assert result['record']['grams_per_unit'] == 50
    assert result['approximations'][0]['reason'] == 'adjacent_size_proxy'
    assert rank_portions([source], 'ingredient:potato', 'piece', PortionFacets(size='large'))['record'] is None


def test_unknown_soft_facet_can_be_reported_without_ignoring_known_contradictions():
    from recipe_wrangler.matching.portions.core import rank_portions
    source = record(facets=PortionFacets(size='medium', form='whole'))
    result = rank_portions([source], 'ingredient:potato', 'piece', PortionFacets(size='medium', preparation='raw'))
    assert result['match_score'] == 0.9
    assert result['approximations'][0]['reference'] is None
    source = source.model_copy(update={'facets': PortionFacets(size='medium', form='whole', preparation='boiled')})
    assert rank_portions([source], 'ingredient:potato', 'piece', PortionFacets(size='medium', preparation='raw'))['record'] is None


def test_same_score_different_weights_stays_ambiguous_and_exposes_candidates():
    from recipe_wrangler.matching.portions.core import rank_portions
    first = record(record_id='fixture:regular1', description='regular potato', facets=PortionFacets(form='whole'))
    second = first.model_copy(update={'record_id': 'fixture:regular2', 'grams_per_unit': 80})
    result = rank_portions([first, second], 'ingredient:potato', 'piece', PortionFacets(size='medium'))
    assert result['status'] == 'ambiguous' and result['record'] is None
    assert len(result['alternatives']) == 2
    assert result['match_score'] == 0.85


def test_regular_carrot_is_a_whole_count_without_asserting_medium_size():
    row = {'usda_id': '11124', 'food_name': 'Carrots, raw', 'portions': [{'portion_desc': 'regular carrot', 'amount': 1, 'grams': 60}]}
    source = list(usda_records([row]))[0]
    assert source.unit == 'piece' and source.facets.form == 'whole'
    assert source.facets.size is None


def test_dried_request_is_never_relaxed_to_a_record_without_preservation():
    from recipe_wrangler.matching.portions.core import rank_portions
    raw_apple = record(food_id='ingredient:apple', unit='piece', facets=PortionFacets(preparation='raw', form='whole'))
    assert rank_portions([raw_apple], 'ingredient:apple', 'piece', PortionFacets(preservation='dried'))['record'] is None
    assert rank_portions([raw_apple], 'ingredient:apple', 'piece', PortionFacets(preservation='fresh'))['record'] is not None


def test_bare_piece_description_is_not_a_whole_item_size_proxy():
    from recipe_wrangler.matching.portions.core import rank_portions
    floret = record(food_id='usda_weights:11090', unit='piece', description='piece', grams_per_unit=10,
                    facets=PortionFacets(preparation='raw', form='whole'))
    assert rank_portions([floret], 'usda_weights:11090', 'piece', PortionFacets(size='medium'))['record'] is None
    regular = record(food_id='usda_weights:11124', unit='piece', description='regular carrot', grams_per_unit=60,
                     facets=PortionFacets(preparation='raw', form='whole'))
    assert rank_portions([regular], 'usda_weights:11124', 'piece', PortionFacets(size='medium'))['record'] is not None


def test_cut_descriptions_are_not_whole_item_size_proxies():
    from recipe_wrangler.matching.portions.core import rank_portions
    for desc in ('piece bite-size', 'slice', 'wedge', 'chunk'):
        cut = record(food_id='ausnut2023:F005299', unit='piece', description=desc, grams_per_unit=10,
                     facets=PortionFacets(preparation='raw', form='whole'))
        assert rank_portions([cut], 'ausnut2023:F005299', 'piece', PortionFacets(size='large'))['record'] is None


def test_whole_piece_description_is_still_a_valid_size_proxy():
    from recipe_wrangler.matching.portions.core import rank_portions
    whole = record(food_id='usda_weights:11238', unit='piece', description='piece whole', grams_per_unit=19,
                   facets=PortionFacets(preparation='raw', form='whole'))
    assert rank_portions([whole], 'usda_weights:11238', 'piece', PortionFacets(size='large'))['record'] is not None


def test_usda_edible_portion_satisfies_without_peel_for_volume_but_not_pieces():
    from recipe_wrangler.matching.portions.core import rank_portions
    cup = record(food_id='usda_weights:09226', source_food_id='usda_weights:09226', unit='cup', grams_per_unit=140, unit_volume_ml=237,
                 facets=PortionFacets(preparation='raw', form='pieces'))
    assert rank_portions([cup], 'usda_weights:09226', 'cup', PortionFacets(peel='without_peel', preparation='raw', form='pieces'))['record'] is not None
    assert rank_portions([cup], 'usda_weights:09226', 'cup', PortionFacets(peel='with_peel', preparation='raw', form='pieces'))['record'] is None
    piece = record(food_id='usda_weights:09226', source_food_id='usda_weights:09226', unit='piece', grams_per_unit=140,
                   facets=PortionFacets(preparation='raw', form='whole'))
    assert rank_portions([piece], 'usda_weights:09226', 'piece', PortionFacets(peel='without_peel', preparation='raw', form='whole'))['record'] is None


def test_dried_request_matches_raw_record_only_for_dry_staples():
    from recipe_wrangler.matching.portions.core import rank_portions
    lentils = record(food_id='usda_weights:16069', source_food_id='usda_weights:16069', food_name='Lentils, raw', unit='cup', grams_per_unit=192,
                     unit_volume_ml=237, facets=PortionFacets(preparation='raw'))
    assert rank_portions([lentils], 'usda_weights:16069', 'cup', PortionFacets(preservation='dried'))['record'] is not None
    apple = record(food_id='ingredient:apple', source_food_id='usda_weights:09003', food_name='Apples, raw', unit='cup', grams_per_unit=110,
                   unit_volume_ml=237, facets=PortionFacets(preparation='raw'))
    assert rank_portions([apple], 'ingredient:apple', 'cup', PortionFacets(preservation='dried'))['record'] is None


def test_relaxed_candidates_prefer_pieces_over_mashed_when_form_unstated():
    from recipe_wrangler.matching.portions.core import rank_portions
    kw = dict(food_id='usda_weights:09226', source_food_id='usda_weights:09226', unit='cup', unit_volume_ml=237)
    diced = record(record_id='a', grams_per_unit=145, facets=PortionFacets(preparation='raw', form='pieces'), **kw)
    mashed = record(record_id='b', grams_per_unit=230, facets=PortionFacets(preparation='raw', form='mashed'), **kw)
    result = rank_portions([mashed, diced], 'usda_weights:09226', 'cup', PortionFacets(peel='without_peel'))
    assert result['record']['grams_per_unit'] == 145


def test_bare_count_is_one_medium_piece_with_recorded_assumption():
    medium = record(food_id='ingredient:potato', unit='piece', grams_per_unit=70,
                    facets=PortionFacets(size='medium', preparation='raw', form='whole', peel='with_peel'))
    small = record(record_id='fixture:2', food_id='ingredient:potato', unit='piece', grams_per_unit=40,
                   facets=PortionFacets(size='small', preparation='raw', form='whole', peel='with_peel'))
    with patch('recipe_wrangler.matching.portions.runtime.records_for_food', return_value=[medium, small]):
        result = match_request('potato', None, PortionInput())
    assert result['record']['grams_per_unit'] == 70
    assert {'facet': 'size', 'value': 'medium', 'policy': 'unsized_count_defaults_to_medium'} in result['assumptions']
    with patch('recipe_wrangler.matching.portions.runtime.records_for_food', return_value=[medium, small]):
        assert match_request('potato', 'small', PortionInput())['record']['grams_per_unit'] == 40


def test_unstated_shape_uses_median_of_same_source_shape_variants():
    from recipe_wrangler.matching.portions.core import rank_portions
    kw = dict(food_id='usda_weights:20420', source_food_id='usda_weights:20420', unit='cup', unit_volume_ml=237)
    shapes = [record(record_id=f's{g}', grams_per_unit=g, description=f'cup {d}', facets=PortionFacets(preservation='dried'), **kw)
              for g, d in [(91, 'spaghetti'), (122, 'elbows'), (95, 'penne'), (81, 'farfalle'), (64, 'shells')]]
    result = rank_portions(shapes, 'usda_weights:20420', 'cup', PortionFacets())
    assert result['record']['grams_per_unit'] == 91
    assert result['match_quality'] == 'approximate'
    assert result['approximations'][0]['reason'] == 'unspecified_variant_median'


def test_count_written_inside_the_name_is_parsed():
    out = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['2 potatoes'], 'measurements': [''],
                                                   'portion_inputs': [{'match_mode': 'ranked'}], 'return_details': True})
    assert out['details'][0]['parsed_quantity'] == '2'


def test_reviewed_portion_table_supplies_the_food_and_bypasses_resolver(tmp_path, monkeypatch):
    import json
    table = tmp_path / 'table.json'
    table.write_text(json.dumps({'entries': [{'name': 'Skim Milk', 'food_id': 'usda_weights:01085',
                                              'label': 'Milk, nonfat, fluid', 'review': 'corrected'}]}))
    monkeypatch.setattr(tool, 'REVIEWED_PORTION_FOODS_PATH', table)
    tool._reviewed_portion_table.cache_clear()
    link = tool._reviewed_portion_link('skim  milk')
    assert link['usda_id'] == '01085' and link['match_source'] == 'reviewed_portion_table'
    assert tool._reviewed_portion_link('almond milk') is None
    tool._reviewed_portion_table.cache_clear()


def _cup(**kw):
    base = dict(food_id='usda_weights:x', source_food_id='usda_weights:x', unit='cup', unit_volume_ml=237)
    base.update(kw)
    return record(**base)


def test_unstated_cut_form_takes_median_of_same_source_cut_variants():
    from recipe_wrangler.matching.portions.core import rank_portions
    chopped = _cup(record_id='a', grams_per_unit=89, description='cup, chopped', facets=PortionFacets(preparation='raw', form='chopped'))
    shredded = _cup(record_id='b', grams_per_unit=70, description='cup, shredded', facets=PortionFacets(preparation='raw', form='shredded'))
    result = rank_portions([chopped, shredded], 'usda_weights:x', 'cup', PortionFacets())
    assert result['record']['grams_per_unit'] == 70 and result['match_quality'] == 'approximate'


def test_leaf_part_and_ground_spice_and_egg_part_sizes_relax_only_where_safe():
    from recipe_wrangler.matching.portions.core import rank_portions
    herb = _cup(grams_per_unit=60, description='cup chopped', food_name='Parsley, fresh', facets=PortionFacets(preparation='raw'))
    assert rank_portions([herb], 'usda_weights:x', 'cup', PortionFacets(part='leaf'))['record'] is not None
    spice = record(food_id='usda_weights:x', unit='teaspoon', grams_per_unit=2.1, food_name='Spices, cumin seed',
                   description='tsp, whole', facets=PortionFacets(form='whole'))
    assert rank_portions([spice], 'usda_weights:x', 'teaspoon', PortionFacets(form='ground'))['record'] is not None
    fruit = record(food_id='usda_weights:y', unit='piece', grams_per_unit=150, food_name='Apples, raw', facets=PortionFacets(form='whole'))
    assert rank_portions([fruit], 'usda_weights:y', 'piece', PortionFacets(form='ground'))['record'] is None
    white = record(food_id='usda_weights:z', unit='piece', grams_per_unit=33, food_name='Egg, white, raw, fresh',
                   description='large', facets=PortionFacets(size='large', part='white', preparation='raw'))
    assert rank_portions([white], 'usda_weights:z', 'piece', PortionFacets(size='medium', part='white'))['record']['grams_per_unit'] == 33


def test_unit_naming_the_food_counts_pieces():
    breast = record(food_id='usda_weights:05062', unit='piece', grams_per_unit=272, food_name='Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw',
                    facets=PortionFacets(form='whole'), description='piece')
    with patch('recipe_wrangler.matching.portions.runtime.records_for_food', return_value=[breast]):
        result = match_request('chicken breasts', 'breasts', PortionInput(food_id=None), resolved_usda_id='05062')
    assert result['record']['grams_per_unit'] == 272


def test_bare_piece_guard_still_blocks_vegetable_cuts():
    from recipe_wrangler.matching.portions.core import rank_portions
    floret = record(food_id='usda_weights:11090', unit='piece', description='piece', grams_per_unit=10,
                    food_name='Broccoli, raw', facets=PortionFacets(preparation='raw', form='whole'))
    assert rank_portions([floret], 'usda_weights:11090', 'piece', PortionFacets(size='medium'))['record'] is None


def test_plural_egg_parts_are_detected():
    assert infer_facets('egg whites')[0].part == 'white'
    assert infer_facets('egg yolks')[0].part == 'yolk'


def _rec(rid, unit, grams, desc='', food_name='Garlic, raw', **facets):
    return record(record_id=rid, food_id='usda_weights:q', source_food_id='usda_weights:q', unit=unit, grams_per_unit=grams,
                  description=desc, food_name=food_name, facets=PortionFacets(**facets))


def test_item_word_units_resolve_against_the_records_that_exist():
    clove = _rec('c', 'clove', 3, 'clove', preparation='raw', part='clove')
    with patch('recipe_wrangler.matching.portions.runtime.records_for_food', return_value=[clove]):
        assert match_request('garlic cloves', 'cloves', PortionInput(), resolved_usda_id='q')['record']['grams_per_unit'] == 3
    ear = _rec('e', 'ear', 90, 'ear', preparation='raw')
    with patch('recipe_wrangler.matching.portions.runtime.records_for_food', return_value=[ear]):
        assert match_request('sweetcorn', 'cobs', PortionInput(), resolved_usda_id='q')['record']['grams_per_unit'] == 90
    rasher = _rec('r', 'slice', 28, 'slice', preparation='raw')
    with patch('recipe_wrangler.matching.portions.runtime.records_for_food', return_value=[rasher]):
        assert match_request('bacon', 'rashers', PortionInput(), resolved_usda_id='q')['record']['grams_per_unit'] == 28
    bulb = _rec('b', 'piece', 234, 'bulb', food_name='Fennel, bulb, raw', form='whole', preparation='raw')
    with patch('recipe_wrangler.matching.portions.runtime.records_for_food', return_value=[bulb]):
        assert match_request('fennel', 'bulb', PortionInput(), resolved_usda_id='q')['record']['grams_per_unit'] == 234


def test_near_identical_same_source_pieces_take_lower_median():
    from recipe_wrangler.matching.portions.core import rank_portions
    a = record(record_id='a', food_id='usda_weights:d', source_food_id='usda_weights:d', unit='piece', grams_per_unit=122,
               description='piece', food_name='Chicken, drumstick, raw', facets=PortionFacets(form='whole', preparation='raw'))
    b = a.model_copy(update={'record_id': 'b', 'grams_per_unit': 130})
    assert rank_portions([a, b], 'usda_weights:d', 'piece', PortionFacets(size='medium'))['record']['grams_per_unit'] == 122


def test_whole_wheat_and_whole_milk_are_not_the_whole_form():
    assert infer_facets('whole wheat bread')[0].form is None
    assert infer_facets('whole grain mustard')[0].form is None
    assert infer_facets('whole milk')[0].form is None
    assert infer_facets('whole chicken')[0].form == 'whole'


def test_whole_bulb_counts_as_unsized_whole_item_and_skin_variants_within_ten_percent_take_median():
    from recipe_wrangler.matching.portions.core import rank_portions
    bulb = record(food_id='usda_weights:b', unit='piece', grams_per_unit=234, description='bulb', food_name='Fennel, bulb, raw',
                  facets=PortionFacets(preparation='raw', form='whole', part='bulb'))
    assert rank_portions([bulb], 'usda_weights:b', 'piece', PortionFacets(size='medium'))['record']['grams_per_unit'] == 234
    skin = record(record_id='s', food_id='usda_weights:d', source_food_id='usda_weights:d', unit='piece', grams_per_unit=130,
                  description='drumstick with skin', food_name='Chicken, drumstick, raw',
                  facets=PortionFacets(preparation='raw', form='whole', peel='with_peel'))
    bare = skin.model_copy(update={'record_id': 'n', 'grams_per_unit': 122, 'facets': PortionFacets(preparation='raw', form='whole', peel='without_peel')})
    assert rank_portions([skin, bare], 'usda_weights:d', 'piece', PortionFacets(size='medium'))['record']['grams_per_unit'] == 122


def test_table_only_mode_gives_no_weight_to_unlisted_names_and_keeps_listed_ones(tmp_path, monkeypatch):
    import json
    table = tmp_path / 'table.json'
    table.write_text(json.dumps({'entries': [{'name': 'banana', 'food_id': 'ingredient:banana', 'label': 'Banana', 'review': 'confirmed'}]}))
    monkeypatch.setattr(tool, 'REVIEWED_PORTION_FOODS_PATH', table)
    tool._reviewed_portion_table.cache_clear()
    monkeypatch.setattr(tool, 'WEIGHT_PORTION_TABLE_ONLY', True)
    monkeypatch.setattr(tool, 'canonical_name_to_usda', lambda n: pytest.fail('resolver must not be consulted'))
    monkeypatch.setattr(tool, '_embedding_usda_link', lambda *a, **k: pytest.fail('embedding link must not be consulted'))
    out = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['kaffir lime leaves'], 'measurements': ['2 leaves'],
                                                   'portion_inputs': [{'match_mode': 'ranked'}], 'return_details': True})
    assert out['weights'] == [0.0]
    assert out['details'][0]['facet_matching']['blockers'] == ['not_in_reviewed_portion_table']
    tool._reviewed_portion_table.cache_clear()


def test_table_only_mode_still_resolves_built_in_generic_foods(monkeypatch):
    monkeypatch.setattr(tool, 'WEIGHT_PORTION_TABLE_ONLY', True)
    out = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['banana'], 'measurements': ['2 medium'],
                                                   'portion_inputs': [{'match_mode': 'ranked'}], 'return_details': True})
    assert out['weights'][0] > 0
