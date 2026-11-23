import asyncio
from unittest.mock import Mock
import pytest

from recipe_wrangler.tools import nutrition_match as nm
from recipe_wrangler.matching import runtime
from recipe_wrangler.matching.runtime import MatchingEngine
from recipe_wrangler.matching.source_suitability import material_scope_conflicts


def test_backend_is_request_local_and_restored():
    async def task(label):
        provider=Mock()
        provider.query.return_value=[{'id':label}]
        with nm.matching_backend(provider,{}):
            await asyncio.sleep(0)
            return nm._candidate_pools('eu')[0][1]('milk')[0]['id']
    async def run():return await asyncio.gather(task('one'),task('two'))
    assert asyncio.run(run())==['one','two']
    assert nm._BACKEND.get() is None


@pytest.mark.parametrize('name,label,rule',[
    ('brown rice','Rice, Jasmin, cooked','product.rice_subtype_lost'),
    ('black rice','Rice, cooked, with salt','product.rice_subtype_lost'),
    ('milk','MILK, GOAT MILK','product.unsupported_milk_source'),
    ('milk','Milk human','product.unsupported_milk_source'),
])
def test_complete_identity_is_not_displaced_by_requested_state(name,label,rule):
    assert rule in material_scope_conflicts({'input_text':name,'facets':{}},{'input_text':label,'facets':{}})


def test_runtime_keeps_unknown_totals_and_source_quantities_separate():
    engine=object.__new__(MatchingEngine);engine.release_id='test'
    engine.resolve=Mock(return_value={'decision':{'confidence':'none'},'calculation':{
        'kind':'unresolved','values_per_100g':{},'assumptions':[],'blockers':['unknown']},
        'source_facts':None,'observed_facts':{'facets':{}},'leading_alternatives':[]})
    result=engine.calculate(title='Test',ingredient_names=['milk'],weights=[200],
        original_texts=['1 cup milk'],source='eu',serves=2)
    assert result['clean_totals']['energy_kcal'] is None
    assert result['supported_partial_totals']['energy_kcal']==0
    assert result['details'][0]['weight_g']==pytest.approx(247.2)
    assert result['details'][0]['original_weight_g']==200
    engine.resolve.assert_called_once_with('milk','eu','1 cup milk')


def test_coconut_milk_quantity_is_identical_across_regional_catalogues():
    engine=object.__new__(MatchingEngine);engine.release_id='test'
    engine.resolve=Mock(return_value={'decision':{'confidence':'none'},'calculation':{
        'kind':'unresolved','values_per_100g':{},'assumptions':[],'blockers':['unknown']},
        'source_facts':None,'observed_facts':{'facets':{}},'leading_alternatives':[]})
    profiles=[engine.calculate(title='Thai carrot soup with crispy tofu',
        ingredient_names=['coconut milk'],weights=[2224.8],
        original_texts=['½ cup reduced-fat coconut milk, plus extra to serve'],source=region)
        for region in ('eu','irish','hungarian','slovenian')]
    assert [p['details'][0]['weight_g'] for p in profiles]==pytest.approx([123.6]*4)
    assert len({p['quantity_input_hash'] for p in profiles})==1


def test_live_quantity_cannot_borrow_prepared_dish_mass_for_component():
    from recipe_wrangler.matching.quantities import resolve_quantity
    assert resolve_quantity('spaghetti',75,'200g Spaghetti bolognese, homemade')['weight_g']==75


def test_runtime_rejects_truncated_ingredient_weights():
    engine=object.__new__(MatchingEngine)
    with pytest.raises(ValueError,match='lengths'):
        engine.calculate(title='Test',ingredient_names=['milk'],weights=[],source='eu')


def test_match_detail_endpoint_checks_visibility_before_reading_postgres(monkeypatch):
    from recipe_wrangler.api.routers import catalog
    from recipe_wrangler.repositories import matching_store
    visible=Mock(return_value={'recipe_id':'recipe'})
    monkeypatch.setattr(catalog,'catalog_get',visible)
    monkeypatch.setattr(matching_store,'active_profile',lambda *a:None)
    monkeypatch.setattr(matching_store,'latest_profile',lambda *a:{'run_id':'run','profile':{'details':[{'observed_facts':{'facets':{'processes':['fried']}}}]}})
    result=catalog.catalog_nutrition_matches('recipe','eu','caller')
    visible.assert_called_once_with('recipe','caller')
    assert result['active'] is False
    assert result['profile']['details'][0]['observed_facts']['facets']['processes']==['fried']


def _engine_returning(decision,kind,values,source_facts=None):
    engine=object.__new__(MatchingEngine);engine.release_id='test'
    engine.resolve=Mock(return_value={'decision':decision,'calculation':{'kind':kind,'values_per_100g':values,
        'assumptions':[],'blockers':[]},'source_facts':source_facts,'observed_facts':{'facets':{}},'leading_alternatives':[]})
    return engine


def _totals(engine):
    return engine.calculate(title='T',ingredient_names=['x','y'],weights=[10,10],original_texts=['',''],source='eu',serves=1)


def test_large_blank_in_matched_record_blanks_only_that_nutrient_and_is_listed():
    engine=_engine_returning({'match':{'id':'a'},'confidence':'strong'},'retained_legacy_estimate',{'protein_g':10.0},
        {'label':'Paprika','native_id':'a','source_key':'eu'})
    result=_totals(engine)
    assert result['clean_totals']['energy_kcal'] is None
    assert result['clean_totals']['protein_g']==2
    assert 'energy_kcal' in result['details'][0]['blank_nutrients_as_zero']
    assert 'protein_g' not in result['details'][0]['blank_nutrients_as_zero']


def test_reviewed_fill_supplies_blank_nutrient_and_records_its_source(monkeypatch):
    monkeypatch.setitem(runtime.RECORD_FILLS,'a',{'energy_kcal':{'value':300.0,'source':'other:1'},'protein_g':{'value':99.0,'source':'other:1'}})
    engine=_engine_returning({'match':{'id':'a'},'confidence':'strong'},'retained_legacy_estimate',{'protein_g':10.0},
        {'label':'Paprika','native_id':'a','source_key':'eu'})
    result=_totals(engine)
    assert result['clean_totals']['energy_kcal']==60
    assert result['clean_totals']['protein_g']==2  # a stated value is never overwritten
    assert result['details'][0]['nutrients_filled']=={'energy_kcal':'other:1'}
    assert 'energy_kcal' not in result['details'][0]['blank_nutrients_as_zero']


def test_approximate_alias_is_flagged_on_the_line(monkeypatch):
    monkeypatch.setitem(runtime.APPROXIMATE_NAMES,'mirin','Sweet rice wine proxy')
    engine=_engine_returning({'match':{'id':'a'},'confidence':'strong'},'retained_legacy_estimate',{'protein_g':10.0},
        {'label':'Paprika','native_id':'a','source_key':'eu'})
    result=engine.calculate(title='T',ingredient_names=['Mirin'],weights=[10],original_texts=[''],source='eu',serves=1)
    assert result['details'][0]['approximate_match']=='Sweet rice wine proxy'


@pytest.mark.parametrize('decision',[{'confidence':'none','intentionally_ignored':True},
    {'confidence':'none','reason':'non_food'}])
def test_ignored_and_non_food_lines_do_not_blank_totals(decision):
    result=_totals(_engine_returning(decision,'unresolved',{}))
    assert result['clean_totals']['energy_kcal']==0
    assert result['details'][0]['nutrition_intentionally_ignored'] is True


def test_unmatched_food_still_blanks_totals():
    result=_totals(_engine_returning({'confidence':'none','reason':'top_candidate_incompatible'},'unresolved',{}))
    assert result['clean_totals']['energy_kcal'] is None
    assert result['details'][0]['blank_nutrients_as_zero']==[]


def test_reviewed_public_source_food_reports_citation_and_state_variant(monkeypatch):
    monkeypatch.setattr(runtime,'occurrence_key',lambda facts:'key')
    row={'alias':'soba','food_name':'Soba, dry','eu_food_id':'usda_sr_legacy:1','cooked':'usda_sr_legacy:2','public':{
        'usda_sr_legacy:1':{'source':'usda_sr_legacy:1','food_name':'Soba, dry','values':{'energy_kcal':336.0}},
        'usda_sr_legacy:2':{'source':'usda_sr_legacy:2','food_name':'Soba, cooked','values':{'energy_kcal':99.0}}}}
    engine=object.__new__(MatchingEngine)
    monkeypatch.setitem(runtime.REVIEWED_SELECTIONS,'soba',row)
    dry=engine._reviewed_selection('soba','100g soba','eu',{},None,None)
    cooked=engine._reviewed_selection('soba','200g cooked soba','eu',{},None,None)
    assert dry['source_facts']['native_id']=='usda_sr_legacy:1' and dry['calculation']['values_per_100g']['energy_kcal']==336
    assert cooked['source_facts']['label']=='Soba, cooked' and dry['decision']['reason']=='reviewed_alias'


def test_reviewed_non_food_counts_as_ignored(monkeypatch):
    monkeypatch.setattr(runtime,'occurrence_key',lambda facts:'key')
    monkeypatch.setitem(runtime.REVIEWED_SELECTIONS,'skewers',{'alias':'skewers','non_food':True})
    decision=object.__new__(MatchingEngine)._reviewed_selection('skewers','8 skewers','eu',{},None,None)['decision']
    assert decision['reason']=='non_food'


class _Policy:
    def source(self,collection,candidate):
        return {'label':'Beans, dried','values_per_100g':{'energy_kcal':330.0},'native_id':'dried'}


class _Provider:
    def by_id(self,collection,identifier):
        return {'id':identifier}


def test_reviewed_dry_record_refuses_a_stated_tin_or_cooked_weight(monkeypatch):
    monkeypatch.setattr(runtime,'occurrence_key',lambda facts:'key')
    monkeypatch.setitem(runtime.REVIEWED_SELECTIONS,'beans',{'alias':'beans','eu_food_id':'dried'})
    engine=object.__new__(MatchingEngine)
    assert engine._reviewed_selection('beans','400g tin beans','eu',{},_Provider(),_Policy()) is None
    assert engine._reviewed_selection('beans','200g cooked beans','eu',{},_Provider(),_Policy()) is None
    assert engine._reviewed_selection('beans','100g beans','eu',{},_Provider(),_Policy())['decision']['reason']=='reviewed_alias'


def test_reviewed_selection_only_applies_when_matching_failed(monkeypatch):
    monkeypatch.setattr(runtime,'occurrence_key',lambda facts:'key')
    monkeypatch.setitem(runtime.REVIEWED_SELECTIONS,'beans',{'alias':'beans','eu_food_id':'dried'})
    engine=object.__new__(MatchingEngine)
    reviewed={'decision':{'reason':'reviewed'},'source_facts':None}
    engine._reviewed_selection=Mock(return_value=reviewed)
    engine._match=Mock(return_value={'decision':{},'calculation':{'kind':'direct_match'},'observed_facts':{}})
    assert MatchingEngine.resolve.__wrapped__(engine,'beans','eu','')['calculation']['kind']=='direct_match'
    engine.annotator=engine.records=engine.profiles=engine.foodex=engine.annotations=engine.source_cache=None
    engine._match=Mock(return_value={'decision':{},'calculation':{'kind':'unresolved'},'observed_facts':{}})
    assert MatchingEngine.resolve.__wrapped__(engine,'beans','eu','')==reviewed


@pytest.mark.parametrize('name,label,expected',[
    ('greek yogurt','Yogurt, low fat, plain',True),
    ('low fat Greek yogurt','Yogurt, low fat, plain',True),
    ('reduced-fat Greek-style yoghurt','Yogurt, low fat, plain',True),
    ('low fat Greek yogurt','Yogurt, low fat, toffee',False),
    ('Greek-style yoghurt','Yoghurt low fat',True),
    ('french dressing','Dressing, basic, low fat',True),
    ('greek yogurt','Yogurt, low fat, toffee',False),
    ('french dressing','Dressing, thousand island, low fat',False),
    ('greek yogurt','Yogurt, whole milk, plain',False),
    ('coconut milk','Milk, semi-skimmed, UHT',False),
    ('coconut milk','Milk, low fat',False),
    ('salad cream','Cream, low fat',False),
])
def test_stated_fat_level_may_drop_the_variety_only_for_a_plain_record(name,label,expected):
    from recipe_wrangler.matching.source_suitability import plain_fat_variant
    assert plain_fat_variant(name,label) is expected


@pytest.mark.parametrize('supported,conflicts,incomplete,accepted', [
    (False,False,False,True), (True,False,False,False),
    (False,True,False,False), (False,False,True,False),
])
def test_source_line_low_fat_cheddar_retrieval(monkeypatch,supported,conflicts,incomplete,accepted):
    from recipe_wrangler.matching import runtime
    observed={'input_text':'cheddar cheese','facets':{'fat_level':'low'},
        'occurrence_recovery':{'conflicts':['alternative'] if conflicts else [],
            'recovered':[{'field':'fat_level'}]}}
    def facts(label,fat,missing=False):
        return {'label':label,'text_annotation':{'input_text':label,'facets':{'fat_level':fat}},
            'values_per_100g':{m:None if missing and m=='protein_g' else 1 for m in runtime.METRICS}}
    before={'decision':{},'calculation':{'kind':'automatic_estimate'},'observed_facts':observed,
        'evaluation_key':'original-key','source_facts':facts('Cheese Cheddar', 'low' if supported else None)}
    after={'decision':{},'calculation':{'kind':'automatic_estimate'},'observed_facts':observed,
        'evaluation_key':'retry-key','source_facts':facts('Cheese, Cheddar type, half fat','reduced',incomplete)}
    engine=object.__new__(MatchingEngine)
    engine._match=Mock(side_effect=[before,after])
    result=MatchingEngine.resolve.__wrapped__(engine,'cheddar cheese','irish','30g of low-fat cheddar cheese, grated')
    if accepted:
        assert result is not after
        assert result['source_facts']==after['source_facts']
        assert after['evaluation_key']=='retry-key'
        assert engine._match.call_args.args==('low fat cheddar cheese','irish','30g of low-fat cheddar cheese, grated')
        assert result['evaluation_key']=='original-key'
    else:
        assert result is before


@pytest.mark.parametrize('name,original,expected',[
    ('low salt vegetable stock','800ml / 1 ½ pint of low salt vegetable stock','low salt stock'),
    ('beef stock','600 ml beef stock or 2 low salt stock cubes','low salt stock'),
    ('vegetable stock cube','1 low salt vegetable stock cube dissolved','reduced salt vegetable stock cube'),
    ('chicken stock cube','½ chicken stock cube (reduced sodium)','reduced salt chicken stock cube'),
    ('soy sauce','1 tablespoon of reduced-salt soy sauce','reduced salt soy sauce'),
    ('vegetable stock','850ml vegetable stock',None),
    ('butter','50g reduced-salt butter',None),
])
def test_low_salt_name_only_reads_stated_low_salt_stock_and_soy(name,original,expected):
    from recipe_wrangler.matching.reviewed_aliases import low_salt_name
    assert low_salt_name(name,original)==expected


def test_low_salt_records_are_cited_and_lower_than_the_standard_records():
    from recipe_wrangler.matching.reviewed_aliases import REVIEWED_SELECTIONS
    stock=REVIEWED_SELECTIONS['low salt stock'];soy=REVIEWED_SELECTIONS['reduced salt soy sauce']
    assert stock['public'][stock['eu_food_id']]['values']['sodium_mg']==pytest.approx(109.6,abs=.1)
    assert soy['public'][soy['eu_food_id']]['values']['sodium_mg']==pytest.approx(3585)
    assert 'nevo:1883' in stock['approximate'] and '1924/2006' in soy['approximate']


def test_resolve_uses_low_salt_record_over_a_working_standard_match(monkeypatch):
    monkeypatch.setattr('recipe_wrangler.matching.runtime.occurrence_key',lambda facts:'key')
    engine=object.__new__(MatchingEngine)
    engine.annotator=engine.records=engine.profiles=engine.foodex=None
    engine.annotations=engine.source_cache={}
    plain={'decision':{'match':{'id':'fineli:29026'},'confidence':'curated'},'observed_facts':{},
        'calculation':{'kind':'retained_legacy_estimate','values_per_100g':{'sodium_mg':543.8}}}
    engine._match=Mock(return_value=plain)
    low=engine.resolve('low salt vegetable stock','irish','800ml low salt vegetable stock')
    assert low['calculation']['values_per_100g']['sodium_mg']==pytest.approx(109.6,abs=.1)
    assert engine.resolve('vegetable stock','irish','850ml vegetable stock') is plain


@pytest.mark.parametrize('label,supported',[
    ('Creme fraiche half fat',True),('Mayonnaise, light, 30% fat',True),('Coconut milk, light, canned',True),
    ('Creme fraiche',False),('Soy sauce, light and dark varieties',False),
])
def test_stated_fat_level_is_requested_and_supported_by_the_frozen_label(label,supported):
    from recipe_wrangler.matching.source_suitability import recovered_property_support
    query={'occurrence_recovery':{'recovered':[]},'facets':{'fat_level':'reduced'}}
    requested,support=recovered_property_support(query,{'label':label,'text_annotation':{'facets':{}}})
    assert requested=={'fat_level'}
    assert ('fat_level' in support) is supported


@pytest.mark.parametrize('name,region,expected',[
    ('wholemeal flour','irish','cofid:11-889'),
    ('Wholemeal Flour','eu','cofid:11-889'),
    ('light cream cheese','irish','fineli:11571'),
    ('wholemeal flour','slovenian',None),
    ('white flour','irish',None),
])
def test_preferred_record_applies_only_to_listed_names_in_irish_and_eu_views(name,region,expected):
    from recipe_wrangler.matching.reviewed_aliases import REVIEWED_SELECTIONS,preferred_name
    key=preferred_name(name,region)
    assert (REVIEWED_SELECTIONS[key]['eu_food_id'] if key else None)==expected


def test_stated_fat_level_on_an_or_alternative_without_recovery_does_not_crash():
    from recipe_wrangler.matching.source_suitability import choose_supported_occurrence_candidate
    query={'input_text':'plain yogurt','main_ingredient':'yogurt','facets':{'fat_level':'low'}}
    current={'label':'Yogurt, whole milk, plain','text_annotation':{'facets':{}}}
    assert choose_supported_occurrence_candidate(query,current,[],None) is None


@pytest.mark.parametrize('name,label,expected',[
    ('coconut milk','Coconut milk, light, canned',True),
    ('light cream cheese','Cream cheese, light, Philadelphia',True),
    ('cheddar cheese','Cheese, Cheddar type, half fat',True),
    ('evaporated milk','Milk, evaporated, light',True),
    ('cooking cream','Cream reduced fat (for cooking) 12%',True),
    ('tomatoes','Tomato, reduced fat',True),
    ('reduced-fat puff pastry','DANISH PASTRY, LOW-FAT MILK, 20% FAT',False),
    ('sour cream','Dips, sour-cream based, reduced fat',False),
    ('light condensed milk','Milk, evaporated, light',False),
])
def test_fat_variant_must_stay_the_same_food(name,label,expected):
    from recipe_wrangler.matching.source_suitability import fat_variant_keeps_food
    assert fat_variant_keeps_food(name,label) is expected


@pytest.mark.parametrize('name,line,grams',[
    ('tomatoes','½ x 400g / 14 oz. tin of tomatoes',200),
    ('lentils','2 x 400g cans no-added-salt lentils, rinsed, drained',800),
    ('lentils','2x 400g cans lentils',800),
    ('cherry tomatoes','½ 400g can cherry tomatoes',200),
    ('black olives','½ a 400 g tin of chopped black olives',200),
    ('cannellini beans','400g can no-added-salt cannellini beans, rinsed, drained',400),
    ('chicken breasts','4 skinless chicken breasts, 520g / 1lb 2½ oz.',520),
    ('sweet potato','1 sweet potato / 130g / 4.5 oz',130),
    ('sultanas','2ozs/50g sultanas',50),
])
def test_stated_weight_reads_pack_and_total_masses(name,line,grams):
    from recipe_wrangler.matching.source_evidence import stated_weight
    assert stated_weight(name,line)['weight_g']==pytest.approx(grams)


@pytest.mark.parametrize('name,line',[
    ('chicken breasts','4 chicken breasts (about 150g each)'),
    ('sweetcorn','1 x 325g / 11½ oz. can of sweetcorn - 260g drained'),
    ('tomatoes','4 small tomatoes (or 200g cherry tomatoes), roughly chopped'),
    ('tuna','1 can (180-200g) tuna'),
    ('lentils','1 x 400g can lentils and beans'),
    ('spaghetti','200g can spaghetti bolognese or 2 x 100g packs'),
])
def test_stated_weight_leaves_ambiguous_pack_lines_unknown(name,line):
    from recipe_wrangler.matching.source_evidence import stated_weight
    assert stated_weight(name,line) is None


def test_resolve_quantity_replaces_a_wrong_saved_weight_with_the_stated_pack_mass():
    from recipe_wrangler.matching.quantities import resolve_quantity
    line='400g can no-added-salt cannellini beans, rinsed, drained'
    assert resolve_quantity('cannellini beans',5.0,line)['weight_g']==400


def test_per_person_mass_is_multiplied_by_servings_once():
    from recipe_wrangler.matching.quantities import per_person_weight
    assert per_person_weight(60,'60 g basmati rice per person',4)==240
    assert per_person_weight(240,'60 g basmati rice per person',4)==240
    assert per_person_weight(60,'60 g basmati rice',4)==60
    assert per_person_weight(1038,'6 potatoes (1 medium per person)',6)==1038
    assert per_person_weight(600,'600g cooked prawns, peeled, de-veined (about 6 per person)',4)==600
    assert per_person_weight(600,'600g cooked prawns, about 6 per person',4)==600
    assert per_person_weight(600,'600g cooked prawns, six per person',4)==600


def test_calculate_scales_a_per_person_mass_by_servings():
    engine=object.__new__(MatchingEngine);engine.release_id='test'
    engine.resolve=Mock(return_value={'decision':{'confidence':'none'},'calculation':{
        'kind':'unresolved','values_per_100g':{},'assumptions':[],'blockers':['unknown']},
        'source_facts':None,'observed_facts':{'facets':{}},'leading_alternatives':[]})
    profile=engine.calculate(title='Curry',ingredient_names=['basmati rice'],weights=[60],
        original_texts=['60 g basmati rice per person'],source='eu',serves=4)
    assert profile['details'][0]['weight_g']==240
    assert profile['details'][0]['original_weight_g']==60
    assert profile['details'][0]['per_serving_mass_evidence']['serves']==4


def test_low_salt_stock_powder_is_not_priced_as_made_up_stock():
    from recipe_wrangler.matching.reviewed_aliases import REVIEWED_SELECTIONS,low_salt_name
    key=low_salt_name('chicken stock powder','1 tsp reduced-salt chicken stock powder')
    assert REVIEWED_SELECTIONS[key]['eu_food_id']=='nevo:1883'


def test_blank_records_are_filled_from_same_food_peers_and_traces():
    from recipe_wrangler.matching.reviewed_aliases import RECORD_FILLS
    assert RECORD_FILLS['cofid:13-876']['sugar_g']['value']==pytest.approx(2.8,abs=.3)
    assert any('trace_as_zero' in fill['source'] and fill['value']==0 for fills in RECORD_FILLS.values() for fill in fills.values())


@pytest.mark.parametrize('name,line,matched,expected',[
    ('basmati rice','3 cups steamed basmati rice','Rice, basmati, natural','cooked basmati rice'),
    ('brown rice','2 x 250g pouches microwavable brown rice','Rice brown raw','cooked brown rice'),
    ('rice','3 cups white rice, cooked','Rice, white, raw','cooked white rice'),
    ('quinoa','250 g Pre-Cooked Quinoa','QUINOA','cooked quinoa'),
    ('egg noodles','440g fresh egg noodles','Noodles, egg, raw','cooked egg noodles'),
    ('brown rice','100g brown rice','Rice brown raw',None),
    ('egg noodles','150g dried egg noodles, cooked','Egg noodles dried',None),
    ('fusilli pasta','8 ounces fusilli pasta (uncooked, 4 cups cooked)','Pasta, white, dried, raw',None),
    ('basmati rice','3 cups steamed basmati rice','Rice, Basmati, cooked',None),
    ('beef mince','500g lean beef mince','Beef, mince, raw','lean beef mince'),
    ('rump steak','500g lean rump steak, fat trimmed','Beef rump steak raw','lean beef steak'),
    ('beef','beef, lean, cubed','Beef, raw','lean beef'),
    ('lamb','400g lean lamb, cut in strips','Lamb, leg, raw',None),
    ('beef mince','500g beef mince','Beef, mince, raw',None),
    ('courgette zucchini noodles','250g packet fresh courgette zucchini noodles','Courgette or zucchini',None),
    ('udon noodles','400g shelf-fresh udon noodles','Udon noodles, fresh',None),
    ('lamb leg steaks','400g lean lamb leg steaks, diced','Lamb leg raw',None),
    ('pork steak','500g lean pork steak, cut in chunky pieces','Pork stewing steak raw','lean pork'),
    ('pork sausages','2 lean pork sausages','Sausages, pork, raw',None),
    ('beef meatballs','400g extra-lean beef meatballs','Meatballs, cow',None),
    ('pork schnitzel','350g lean pork schnitzel or steaks','Pork schnitzel breaded',None),
    ('whole skinless chicken','','Chicken, breast, without skin',None),
    ('lamb shanks','6 lamb shanks (about 900g meat)','Lamb, shoulder, raw',None),
    ('pork loin chop','1 pork loin chop, all fat removed','Pork loin chop raw','trimmed pork chops'),
    ('ground beef','1 pound lean ground beef (15% fat)','Beef, topside, raw',None),
    ('beef steaks','4 x 120g lean beef fillet steaks','Beef, fillet steak, raw, lean',None),
    ('lamb cutlets','8 lamb cutlets','Lamb, cutlet, raw','lamb cutlets with bone'),
    ('lamb cutlets','8 French-trimmed lamb cutlets','Lamb, cutlet, raw','trimmed lamb cutlets with bone'),
    ('lamb shanks','6 lamb shanks','Lamb, shoulder, raw','lamb shanks with bone'),
    ('lamb chops','4 lamb chops','Lamb chops raw','lamb chops with bone'),
    ('pork cutlets','4 x 250g pork cutlets (bone in)','Pork, cutlets, raw','pork chops with bone'),
    ('pork chops','4 top loin pork chops','Pork, neck chops, raw','pork chops with bone'),
    ('whole chicken','1 whole chicken','Chicken, whole, raw','whole chicken with bone'),
    ('chicken thighs','4 small bone-in chicken thighs','Chicken high leg, meat, raw','chicken pieces with bone'),
    ('chicken thighs','4 skinless chicken thigh fillets','Chicken high leg, meat, raw',None),
    ('whole chicken','800g boned whole chicken','Chicken, whole, raw',None),
])
def test_stated_state_picks_cooked_lean_or_bone_in_records(name,line,matched,expected):
    from recipe_wrangler.matching.reviewed_aliases import REVIEWED_SELECTIONS,stated_state_name
    assert stated_state_name(name,line,matched)==expected
    assert expected is None or expected in REVIEWED_SELECTIONS


def test_stated_state_leaves_exact_source_descriptions_alone(monkeypatch):
    monkeypatch.setattr('recipe_wrangler.matching.runtime.occurrence_key',lambda facts:'key')
    engine=object.__new__(MatchingEngine)
    exact={'decision':{'match':{'id':'x'},'matched_name':'Beef, mince, raw','reason':'exact_source_description'},'observed_facts':{},
        'calculation':{'kind':'automatic_estimate','values_per_100g':{}}}
    engine._match=Mock(return_value=exact)
    assert engine.resolve('beef mince','hungarian','500g lean beef mince') is exact


@pytest.mark.parametrize('name,line,matched,expected',[
    ('white fish fillets','600g firm white fish fillets','Glithead, fish breeding, fillets, raw','white fish'),
    ('fish fillets','400g fish fillets like snapper or hoki','European whitefish, raw','white fish'),
    ('smoked fish','200g smoked white fish','Whitefish gutted smoked',None),
    ('salmon fillets','4 x 100g skinless salmon fillets','Salmon fillet without skin','farmed salmon'),
    ('salmon','1 salmon fillet (100g)','Salmon, wild, raw','farmed salmon'),
    ('salmon','200g wild salmon','Salmon, wild, raw',None),
    ('salmon','200g smoked salmon','Smoked salmon',None),
    ('passata','300ml of passata','Tomato puree','passata'),
    ('tomato puree','2 tablespoons of tomato puree','Tomato puree',None),
    ('margarine','25g of low-fat margarine, melted','Baking fat and margarine (75-90% fat)','low fat spread'),
    ('margarine','25g of margarine','Baking fat and margarine (75-90% fat)',None),
    ('oranges','2 oranges','Oranges, raw','oranges'),
    ('orange juice','100ml orange juice','Oranges, raw',None),
])
def test_stated_state_picks_fish_passata_spread_and_orange_records(name,line,matched,expected):
    from recipe_wrangler.matching.reviewed_aliases import REVIEWED_SELECTIONS,stated_state_name
    assert stated_state_name(name,line,matched)==expected
    assert expected is None or expected in REVIEWED_SELECTIONS


@pytest.mark.parametrize('weight,line,grams',[
    (904,'8 lamb cutlets',400),(1356,'12 French-trimmed lamb cutlets',600),
    (500,'12 lamb cutlets (about 500g)',500),(514,'6-8 small lamb cutlets',514),
    (2040,'6 lamb shanks',1050),(600,'3 trimmed lamb shanks (about 600g meat)',600),
    (400,'400g can chickpeas, rinsed, drained',240),(800,'2 x 400g cans lentils, drained',480),
    (400,'1 tin of drained chickpeas (400g / 14oz.)',240),(240,'1 x 240g tin of kidney beans, drained weight',240),
    (400,'400g can chopped tomatoes',400),(400,'400g can chickpeas',400),(100,'100g canned lentils, drained',100),
    (400,'400g can sweetcorn, drained',400),(400,'400g can chickpeas or beans, drained',400),
])
def test_adjusted_weight_for_pieces_and_drained_legume_tins(weight,line,grams):
    from recipe_wrangler.matching.quantities import adjusted_weight
    assert adjusted_weight(weight,line)==pytest.approx(grams)


def test_calculate_reports_the_adjusted_weight():
    engine=object.__new__(MatchingEngine);engine.release_id='test'
    engine.resolve=Mock(return_value={'decision':{'confidence':'none'},'calculation':{
        'kind':'unresolved','values_per_100g':{},'assumptions':[],'blockers':['unknown']},
        'source_facts':None,'observed_facts':{'facets':{}},'leading_alternatives':[]})
    profile=engine.calculate(title='Soup',ingredient_names=['chickpeas'],weights=[5.0],
        original_texts=['400g can no-added-salt chickpeas, rinsed, drained'],source='eu',serves=4)
    assert profile['details'][0]['weight_g']==240



def test_reviewed_names_are_in_the_gated_fallback():
    from recipe_wrangler.matching.reviewed_aliases import REVIEWED_SELECTIONS,_NAMES
    assert all(n in REVIEWED_SELECTIONS for n in _NAMES)
    assert REVIEWED_SELECTIONS['prosciutto']['eu_food_id']=='ciqual:28812'
