"""Original text survives quantity repair without pretending old matches saw it."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from recipe_wrangler.matching.quantities import recipe_inputs, repair_profile
from recipe_wrangler.matching.quality import finalize_profile
from recipe_wrangler.repositories.matching_store import insert_run
from scripts.composition.repair_original_ingredient_context import propose,recover_raw_context


def test_missing_context_requires_one_to_one_source_line_binding():
    from scripts.composition.repair_original_ingredient_context import recover_missing_context
    originals,bindings=recover_missing_context(['milk','milk powder'],['',''],['10 g milk powder'])
    assert originals==['',''] and all(row['status']=='unresolved' for row in bindings)
    originals,_=recover_missing_context(['milk'],[''],['10 g milk','10 g milk'])
    assert originals==['']
    originals,_=recover_missing_context(['Cheese'],[''],['100 g Cheese, Feta'])
    assert originals==['100 g Cheese, Feta']


def test_missing_context_never_overwrites_existing_wording():
    from scripts.composition.repair_original_ingredient_context import recover_missing_context
    originals,_=recover_missing_context(['milk'],['100g milk'],['100 g milk'])
    assert originals==['100g milk']


def test_review_rematching_cannot_save_a_profile(case,tmp_path,monkeypatch):
    import json
    from scripts.composition import repair_original_ingredient_context as workflow
    from recipe_wrangler.matching import runtime,quality
    profile,inputs=case
    active={'profile':profile,'recipe_id':'recipe','nutrition_source':'irish','run_id':'run','release_id':'release'}
    monkeypatch.setattr(workflow.store,'deployment',lambda:{'release_id':'release','mode':'new'})
    monkeypatch.setattr(workflow.store,'active_profile',lambda *a,**k:active)
    matcher=Mock();matcher.release_id='release'
    matcher.calculate.return_value={'details':[],'quantity_input_hash':inputs['quantity_input_hash']}
    monkeypatch.setattr(runtime,'engine',lambda *a:matcher)
    monkeypatch.setattr(quality,'finalize_profile',lambda value:value)
    path=tmp_path/'inputs.json';path.write_text(json.dumps({'recipe':inputs}))
    report=workflow.inspect(path,['recipe'],['irish'],tmp_path/'report.json',rematch=True)
    assert 'recipe_id' not in matcher.calculate.call_args.kwargs
    assert report['database_writes']==0 and report['activated'] is False


@pytest.fixture
def case():
    inputs=recipe_inputs(['onion','chicken thighs'],[150.,600.],
        ['1 large onion, diced','600g skinless boneless chicken thighs'])
    inputs['serves']=6.
    profile={'title':'Curry','serves':6.,'mechanism_version':'old','quantity_input_hash':inputs['quantity_input_hash'],
        'details':[{'position':i,'ingredient':name,'name':name,'weight_g':quantity['weight_g'],
            'original_ingredient_text':'','observed_facts':{'marker':'old'},'selected_candidate_id':'food-'+str(i),
            'values_per_100g':{'energy_kcal':100.}}
            for i,(name,quantity) in enumerate(zip(inputs['names'],inputs['quantities'],strict=True))]}
    return profile,inputs


def test_repair_preserves_every_original_line_even_without_numeric_mass_evidence(case):
    profile,inputs=case
    before=deepcopy((profile,inputs))
    result=repair_profile(profile,inputs,'irish','new')
    assert inputs['quantities'][0]['quantity_evidence'] is None
    assert [d['original_ingredient_text'] for d in result['details']]==inputs['originals']
    assert result['source_context_rematch_positions']==[0,1]
    assert result['details'][0]['source_context_previous']['original_ingredient_text']==''
    assert result['details'][0]['observed_facts']==profile['details'][0]['observed_facts']
    assert result['details'][0]['source_context_status']=='requires_rematching'
    assert (profile,inputs)==before


def test_text_already_seen_by_matcher_does_not_request_another_rematch(case):
    profile,inputs=case
    for detail,line in zip(profile['details'],inputs['originals'],strict=True):
        detail.update(original_ingredient_text=line,source_context_status='matched_with_original_text')
    result=repair_profile(profile,inputs,'irish','new')
    assert result['source_context_rematch_positions']==[]


def test_repeated_quantity_repair_cannot_clear_pending_semantic_rematch(case):
    profile,inputs=case
    once=repair_profile(profile,inputs,'irish','new')
    twice=repair_profile(once,inputs,'irish','new')
    assert twice['source_context_rematch_positions']==[0,1]
    with pytest.raises(ValueError,match='matching must be rerun'):finalize_profile(twice)
    connection=Mock()
    with pytest.raises(ValueError,match='Cannot activate'):
        insert_run(connection,'recipe','irish','release','new',inputs,twice,activate=True)
    connection.execute.assert_not_called()


def test_repair_does_not_erase_existing_source_text_when_canonical_context_is_missing(case):
    profile,inputs=case
    profile['details'][0]['original_ingredient_text']='1 large onion, diced'
    empty=recipe_inputs(inputs['names'],inputs['weights'],['','']);empty['serves']=6.
    with pytest.raises(ValueError,match='Cannot discard'):repair_profile(profile,empty,'irish','new')


@pytest.mark.parametrize('issue',['originals','hash','length','position'])
def test_corrupt_canonical_context_is_rejected(case,issue):
    profile,inputs=case
    if issue=='originals':inputs['originals'][0]='different text'
    elif issue=='hash':inputs['quantity_input_hash']='changed'
    elif issue=='length':inputs['names'].pop()
    else:profile['details'][0]['position']=5
    with pytest.raises(ValueError):repair_profile(profile,inputs,'irish','new')


def test_real_repair_proposal_is_bound_to_the_exact_active_occurrence_vector(case):
    profile,inputs=case
    active={'profile':profile,'recipe_id':'recipe','nutrition_source':'irish','run_id':'run'}
    result=propose(active,inputs)
    assert result['status']=='requires_rematching'
    assert result['before_with_text']==0 and result['after_with_text']==2
    assert [c['position'] for c in result['changes']]==[0,1]


@pytest.mark.parametrize('issue',['hash','name','mass','serves'])
def test_proposals_cannot_rebind_a_different_profile(case,issue):
    profile,inputs=case
    if issue=='hash':profile['quantity_input_hash']='other'
    elif issue=='name':profile['details'][0]['ingredient']='garlic'
    elif issue=='mass':profile['details'][0]['weight_g']=30
    else:profile['serves']=2
    with pytest.raises(ValueError):propose({'profile':profile,'recipe_id':'r','nutrition_source':'irish','run_id':'run'},inputs)


def test_raw_recovery_preserves_whole_lines_and_alternatives(case):
    profile,inputs=case
    empty=recipe_inputs(inputs['names'],inputs['weights'],['','']);empty['serves']=6.
    profile['quantity_input_hash']=empty['quantity_input_hash']
    active={'profile':profile,'recipe_id':'r','nutrition_source':'irish','run_id':'run'}
    lines=['1 large onion, diced','1 boneless uncooked chicken thigh (about 4 ounces or ½ cup)']
    # Use the exact plural canonical name so this tests binding, not synonyms.
    lines[1]=lines[1].replace('chicken thigh','chicken thighs')
    revised,ledger=recover_raw_context(active,empty,[{'title':'Curry','ingredients':lines}])
    assert revised['originals']==lines
    assert revised['quantities'][1]['quantity_evidence'] is None
    assert ledger['previous_quantity_input_hash']==empty['quantity_input_hash']
    assert ledger['revised_quantity_input_hash']!=empty['quantity_input_hash']
    assert all(b['status']=='unique_archived_source_line' for b in ledger['bindings'])


def test_duplicate_raw_recipe_or_ambiguous_source_lines_are_not_guessed(case):
    profile,inputs=case
    empty=recipe_inputs(inputs['names'],inputs['weights'],['','']);empty['serves']=6.
    profile['quantity_input_hash']=empty['quantity_input_hash']
    active={'profile':profile,'recipe_id':'r','nutrition_source':'irish','run_id':'run'}
    row={'title':'Curry','ingredients':['1 onion, diced','1 onion, sliced']}
    with pytest.raises(ValueError,match='uniquely'):recover_raw_context(active,empty,[row,row])
    revised,ledger=recover_raw_context(active,empty,[row])
    assert revised['originals']==['','']
    assert all(b['status']=='unresolved' for b in ledger['bindings'])


def test_foodhero_normalization_does_not_split_internal_quantities_or_fraction_ranges():
    import importlib
    module=importlib.import_module('scripts.import.foodhero.transform_foodhero_recipes_json')
    lines=['1 boneless uncooked chicken thigh (about 4 ounces or ½ cup)',
        '¾ to 1½ teaspoons cayenne pepper','¼ teaspoon salt or 2 pieces cooked turkey bacon']
    assert module.normalize_recipe('Soup',{'ingredients':lines})['ingredients']==lines


def test_graph_lines_fill_blank_context_regardless_of_graph_order(monkeypatch):
    from recipe_wrangler.matching import source_context
    from scripts.composition.repair_original_ingredient_context import recover_missing_context
    monkeypatch.setattr(source_context,'run_query',lambda query,params:[
        {'rid':'r1','lines':['1 tsp ground paprika','1500g Pork, shoulder']}])
    lines=source_context.graph_source_lines(['r1'])
    originals,_=recover_missing_context(['Pork','paprika'],['',''],lines['r1'])
    assert originals==['1500g Pork, shoulder','1 tsp ground paprika']


def test_backfill_binds_lines_with_and_without_frozen_evidence():
    from scripts.composition.backfill_matching_profiles import bind_source_lines
    lines=['1 tsp ground paprika','1500g Pork, shoulder']
    originals,frozen=bind_source_lines(['Pork','paprika'],['',''],None,lines)
    assert originals==['1500g Pork, shoulder','1 tsp ground paprika'] and frozen is None
    evidence={'occurrences':[{'parsed_name':'Pork','original_text':''},
                             {'parsed_name':'paprika','original_text':'kept as frozen'}]}
    _,frozen=bind_source_lines(['Pork','paprika'],['',''],evidence,lines)
    assert [o['original_text'] for o in frozen['occurrences']]==['1500g Pork, shoulder','kept as frozen']
    assert evidence['occurrences'][0]['original_text']==''


def test_backfill_keeps_corrected_weights_when_frozen_evidence_is_stale():
    from scripts.composition.backfill_matching_profiles import bind_source_lines
    evidence={'occurrences':[
        {'parsed_name':'chopped tomatoes','saved_weight_g':5.0,'original_text':'1 can chopped tomatoes','quantity_evidence':None},
        {'parsed_name':'onion','saved_weight_g':150.0,'original_text':'','quantity_evidence':None}]}
    originals,frozen=bind_source_lines(['chopped tomatoes','onion'],['',''],evidence,['1 onion, diced'],
                                       weights=[400.0,150.0])
    assert frozen is None
    assert originals==['1 can chopped tomatoes','1 onion, diced']


def test_backfill_keeps_corrected_names_when_frozen_evidence_is_stale():
    from scripts.composition.backfill_matching_profiles import bind_source_lines
    evidence={'occurrences':[
        {'parsed_name':'Pepper','saved_weight_g':100.0,'original_text':'100 g Pepper, capsicum, yellow, raw','quantity_evidence':None},
        {'parsed_name':'onion','saved_weight_g':150.0,'original_text':'150 g onion','quantity_evidence':None}]}
    originals,frozen=bind_source_lines(['capsicum pepper','onion'],['',''],evidence,[],weights=[100.0,150.0])
    assert frozen is None
    assert originals==['100 g Pepper, capsicum, yellow, raw','150 g onion']


def test_archived_scrape_replaces_fragmented_graph_lines_by_unique_title(tmp_path):
    from scripts.composition.backfill_matching_profiles import archived_source_lines
    raw=tmp_path/'raw.json'
    raw.write_text('{"a":{"title":"Agua Fresca","ingredients":["1 to 2 Tablespoons lime juice"]},'
                   '"b":{"title":"Twin","ingredients":["x"]},"c":{"title":"Twin","ingredients":["y"]}}')
    rows=[{'recipe_id':'1','source':'FoodHero','title':'agua fresca '},
          {'recipe_id':'2','source':'FoodHero','title':'Twin'},
          {'recipe_id':'3','source':'MyPlate','title':'Agua Fresca'}]
    assert archived_source_lines(rows,{'FoodHero':raw})=={'1':['1 to 2 Tablespoons lime juice']}
