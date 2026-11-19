"""A source's exact released food description must survive generic parsed names."""
from recipe_wrangler.matching.occurrence_facts import source_description_name
import pytest


@pytest.mark.parametrize('name,original,label', [
    ('Cheese', '100 g Cheese, Quark', 'Cheese, Quark'),
    ('Pasta', '150g Pasta, white, twists, fusilli, dried, boiled in salted water',
     'Pasta, white, twists, fusilli, dried, boiled in salted water'),
    ('Chilli', '200 g Chilli, bean and lentil, homemade', 'Chilli, bean and lentil, homemade'),
    ('bananas', '80 g Banana bread, homemade', 'Banana bread, homemade'),
    ('spaghetti', '200 g Spaghetti bolognese, homemade', 'Spaghetti bolognese, homemade'),
])
def test_full_source_identity_is_restored_only_from_a_released_literal_label(name,original,label):
    assert source_description_name(name,original,{label.casefold()}) == label
    assert source_description_name(name,original,set()) == name


@pytest.mark.parametrize('name,original', [
    ('Cheese', '100 g Chicken, roasted'),
    ('Cheese', '100 g Cheese, Quark or cheddar'),
    ('low-fat cheese', '100 g Cheese, Quark'),
    ('Pasta, raw', '150 g Pasta, boiled'),
    ('Cheese', '100 g Cheese, Quark, for the sauce'),
    ('Cheese', 'Cheese with Quark'),
    ('bananas', '80 g Bread, banana'),
    ('black pepper', '1 g Pepper, black'),
])
def test_unreleased_or_different_source_context_cannot_override_the_input(name,original):
    labels={'cheese, quark','pasta, boiled','chicken, roasted','bread, banana','pepper, black'}
    assert source_description_name(name,original,labels) == name


def test_alternative_is_not_recovered_even_if_a_catalogue_label_uses_that_phrase():
    assert source_description_name('Cheese','100 g Cheese, Quark or cheddar',
                                   {'cheese, quark or cheddar'}) == 'Cheese'


def test_literal_source_selection_preserves_region_and_never_breaks_duplicate_ties():
    from recipe_wrangler.matching.runtime import MatchingEngine
    from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS
    engine=object.__new__(MatchingEngine)
    eu={'annotation':{},'candidate_id':'source-eu'};local={'annotation':{},'candidate_id':'source-local'}
    engine.source_description_rows={(COLLECTIONS['eu'],'cheese, quark'):[eu]}
    assert engine._exact_source_selection('Cheese, Quark','hungarian') == ('eu',eu)
    engine.source_description_rows[COLLECTIONS['hungarian'],'cheese, quark']=[local]
    assert engine._exact_source_selection('Cheese, Quark','hungarian') == ('hungarian',local)
    engine.source_description_rows[COLLECTIONS['hungarian'],'cheese, quark'].append({'annotation':{},'candidate_id':'duplicate'})
    assert engine._exact_source_selection('Cheese, Quark','hungarian') == ('hungarian',None)


def test_expanded_copies_do_not_displace_a_unique_native_literal_description():
    from recipe_wrangler.matching.runtime import MatchingEngine
    from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS
    engine=object.__new__(MatchingEngine)
    native={'annotation':{},'candidate_id':'cofid:12-174'}
    engine.source_description_rows={(COLLECTIONS['eu'],'cheese, quark'):
        [{'annotation':{},'candidate_id':'catalogue:copy'},native]}
    assert engine._exact_source_selection('Cheese, Quark','eu') == ('eu',native)
    engine.source_description_rows[COLLECTIONS['eu'],'cheese, quark']=[
        {'annotation':{},'candidate_id':'catalogue:one'},{'annotation':{},'candidate_id':'catalogue:two'}]
    assert engine._exact_source_selection('Cheese, Quark','eu') == ('eu',None)


@pytest.mark.parametrize('parsed_name,label',[('Cheese','Cheese, Quark'),('CELERY, Bulb','CELERY, Bulb')])
def test_literal_identity_never_bypasses_a_blocked_source_profile(monkeypatch,parsed_name,label):
    from unittest.mock import Mock
    from recipe_wrangler.matching import runtime
    from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS
    engine=object.__new__(runtime.MatchingEngine)
    engine.source_descriptions={label.casefold()}
    row={'annotation':{},'candidate_id':'cofid:12-174','candidate':{'id':'cofid:12-174','document':label}}
    engine.source_description_rows={(COLLECTIONS['eu'],label.casefold()):[row]}
    engine.annotator=Mock();engine.foodex=None
    engine.records={};engine.profiles={};engine.annotations={};engine.source_cache={}
    facts={'input_text':label,'facets':{},
           'occurrence_recovery':{'conflicts':[],'recovered':[]}}
    monkeypatch.setattr(runtime,'recover_occurrence_facts',lambda *args:facts)
    policy=Mock();policy.source.return_value={'label':label,'source_key':'eu',
                                            'blockers':['native_nutrient_linkage_mismatch:energy_kcal']}
    monkeypatch.setattr(runtime,'SourceNutrientPolicy',lambda *args:policy)
    result=engine.resolve(parsed_name,'eu','100 g '+label)
    assert result['calculation']['kind']=='unresolved'
    assert result['calculation']['values_per_100g']['energy_kcal'] is None
    assert result['calculation']['blockers']==['native_nutrient_linkage_mismatch:energy_kcal']


@pytest.mark.parametrize('name,original,expected',[
    ('CELERY, Bulb','40g CELERY, Bulb',True),
    ('CELERY, Bulb','40g Celery, raw',False),
    ('CELERY, Bulb','',False),
    ('Celery','40g CELERY, Bulb',False),
    ('Cheese, Quark or cheddar','100g Cheese, Quark or cheddar',False),
    ('PARSLEY, Root','30g PARSLEY, Root',True),
    ('PARSLEY, leaves','30g PARSLEY, leaves',True),
    ('Pepper, black','10g Pepper, black',False),
])
def test_already_qualified_literal_requires_matching_original_and_released_label(name,original,expected):
    from recipe_wrangler.matching.runtime import MatchingEngine
    engine=object.__new__(MatchingEngine)
    engine.source_descriptions={'celery, bulb','cheese, quark or cheddar','parsley, root','parsley, leaves','pepper, black'}
    assert engine._stated_literal(name,original) is expected


@pytest.mark.parametrize('legacy',[False,True])
def test_literal_scope_filters_retrieval_and_aliases_without_changing_scores(legacy):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from recipe_wrangler.matching.runtime import Catalogue
    exact={'id':'feta','document':'Cheese, Feta','distance':0.13}
    generic={'id':'average','document':'Cheese (average)','distance':0.01}
    resource=SimpleNamespace(search=Mock(return_value=[generic,exact]),
        ids={('eu','feta'):{'annotation':{},'candidate':exact},('eu','average'):{'annotation':{},'candidate':generic}})
    catalogue=Catalogue(resource,legacy=legacy)
    assert catalogue.query('eu','Cheese')==[generic,exact]
    catalogue.literal_scope=('eu','cheese, feta')
    assert catalogue.query('eu','Cheese, Feta')==[exact]
    assert catalogue.by_id('eu','average') is None
    assert catalogue.by_id('eu','feta')==exact
    resource.search.reset_mock()
    assert catalogue.query('other','Cheese, Feta')==[]
    resource.search.assert_not_called()
    result=catalogue.query('eu','Cheese, Feta')
    result[0]['distance']=1
    assert exact['distance']==0.13


def test_literal_scope_uses_the_released_metadata_label():
    from types import SimpleNamespace
    from recipe_wrangler.matching.runtime import Catalogue
    resource=SimpleNamespace()
    catalogue=Catalogue(resource)
    catalogue.literal_scope=('eu','cheese, feta')
    assert catalogue._in_scope('eu',{'document':'expanded text',
                                  'metadata':{'food_name':' Cheese,   FETA '}})
    assert not catalogue._in_scope('hungarian',{'document':'Cheese, Feta'})
    assert not catalogue._in_scope('eu',{'document':'Cheese, Feta',
                                      'metadata':{'food_name':'Cheese (average)'}})


def test_duplicate_literal_keeps_full_identity_and_scopes_both_matchers(monkeypatch):
    from unittest.mock import Mock
    from recipe_wrangler.matching import runtime
    from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS
    engine=object.__new__(runtime.MatchingEngine)
    engine.source_descriptions={'cheese, feta'}
    engine.source_description_rows={(COLLECTIONS['eu'],'cheese, feta'):
                                   [{'annotation':{},'candidate_id':'one'},{'annotation':{},'candidate_id':'two'}]}
    engine.annotator=Mock();engine.foodex=None;engine.ids={}
    engine.records={};engine.profiles={};engine.annotations={};engine.source_cache={}
    engine.aliases=[];engine.alias_index={}
    facts={'input_text':'Cheese, Feta','facets':{},
           'occurrence_recovery':{'conflicts':[],'recovered':[]}}
    monkeypatch.setattr(runtime,'recover_occurrence_facts',lambda *args:facts)
    monkeypatch.setattr(runtime,'SourceNutrientPolicy',lambda *args:Mock())
    gate=Mock();gate.audit=[];gate.semantic_input.return_value=facts
    monkeypatch.setattr(runtime,'CompatibilityGatedCatalogue',lambda *args,**kwargs:gate)
    seen=[]
    def match(name,region,**kwargs):
        backend=runtime.nm._BACKEND.get()[0]
        if backend is not gate:
            seen.append((name,backend.literal_scope,backend.legacy))
        return {'match':None,'source_key':'eu','confidence':'none'}
    monkeypatch.setattr(runtime.nm,'best_nutrition_match',match)
    monkeypatch.setattr(runtime,'choose_supported_occurrence_candidate',lambda *args:None)
    monkeypatch.setattr(runtime,'calculation_decision',lambda *args,**kwargs:{'kind':'unresolved'})
    result=engine.resolve('Cheese','eu','40 g Cheese, Feta')
    assert seen==[('Cheese, Feta',(COLLECTIONS['eu'],'cheese, feta'),True),
                  ('Cheese, Feta',(COLLECTIONS['eu'],'cheese, feta'),False)]
    assert result['retrieval_name']=='Cheese, Feta'
    assert result['source_description_recovered'] is True
    assert result['source_description_ambiguous'] is True


def test_literal_name_selects_its_record_only_when_energy_is_present():
    from recipe_wrangler.matching.runtime import MatchingEngine
    from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS
    engine=object.__new__(MatchingEngine)
    engine.source_descriptions={'dill, fresh','pepper, black'}
    engine.source_description_rows={
        (COLLECTIONS['hungarian'],'dill, fresh'):[{'annotation':{},'candidate_id':'cofid:dill','candidate':{'id':'dill'}}],
        (COLLECTIONS['hungarian'],'pepper, black'):[{'annotation':{},'candidate_id':'cofid:pepper','candidate':{'id':'pepper'}}]}
    class Policy:
        def source(self,collection,candidate):
            return {'label':candidate['id'],'values_per_100g':{'energy_kcal':25.0 if candidate['id']=='dill' else None}}
    selected=engine._literal_name_selection('Dill,  Fresh','3 g Dill, fresh','hungarian',Policy())
    assert selected[0]=='hungarian' and selected[1]=={'id':'dill'}
    assert engine._literal_name_selection('Pepper, black','1 g Pepper, black','hungarian',Policy()) is None
    assert engine._literal_name_selection('Dill','3 g Dill','hungarian',Policy()) is None
    assert engine._literal_name_selection('Dill, fresh','a handful of dill, fresh, chopped','hungarian',Policy()) is None
    assert engine._literal_name_selection('Dill, fresh','','hungarian',Policy()) is None


def test_literal_record_cannot_lose_a_nutrient_the_normal_match_supplies():
    from recipe_wrangler.matching.runtime import missing_metrics
    full={'values_per_100g':{'energy_kcal':37,'protein_g':1,'carbs_g':8,'fat_g':0.1,'sugar_g':5,
                             'saturated_fat_g':0.04,'sodium_mg':4,'fibre_g':1.7}}
    no_satfat={'values_per_100g':{**full['values_per_100g'],'saturated_fat_g':None}}
    assert not missing_metrics(no_satfat)<=missing_metrics(full)
    assert missing_metrics(full)<=missing_metrics(no_satfat)
    assert missing_metrics(None)=={'energy_kcal','protein_g','carbs_g','fat_g','sugar_g','saturated_fat_g','sodium_mg','fibre_g'}


def test_slovenian_2025_record_outranks_the_older_slovenian_plant_collection():
    from recipe_wrangler.matching.runtime import MatchingEngine
    from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS
    engine=object.__new__(MatchingEngine)
    plant={'annotation':{},'candidate_id':'slovenian_plant:146'};si2025={'annotation':{},'candidate_id':'efsa_slovenia:3cbd'}
    engine.source_description_rows={(COLLECTIONS['slovenian'],'garlic'):[plant],
        (COLLECTIONS['eu'],'garlic'):[{'annotation':{},'candidate_id':'catalogue:copy'},si2025,{'annotation':{},'candidate_id':'fineli:393'}]}
    assert engine._exact_source_selection('GARLIC','slovenian')==('eu',si2025)
    assert engine._exact_source_selection('GARLIC','hungarian')==('eu',None)
    del engine.source_description_rows[COLLECTIONS['eu'],'garlic']
    assert engine._exact_source_selection('GARLIC','slovenian')==('slovenian',plant)


def test_truncated_workbook_label_restores_a_unique_released_label_it_starts():
    labels={'soup, fish and/or crustacean, dehydrated and reconstituted'}
    assert source_description_name('Soup','200g Soup, fish and/or crustacean, dehydrated',labels)==\
        'Soup, fish and/or crustacean, dehydrated and reconstituted'
    two=labels|{'soup, fish and/or crustacean, dehydrated and canned'}
    assert source_description_name('Soup','200g Soup, fish and/or crustacean, dehydrated',two)=='Soup'
    assert source_description_name('Cheese','100 g Cheese, Quark or cheddar',{'cheese, quark or cheddar'})=='Cheese'


@pytest.mark.parametrize('facets,original,expected',[
    ({'preservation_methods':['canned'],'preparation_state':None,'drainage_status':'drained'},'400g can chickpeas, drained','canned chickpeas'),
    ({'preservation_methods':[],'preparation_state':None,'drainage_status':'drained'},'400g chickpeas, drained','canned chickpeas'),
    ({'preservation_methods':['dried'],'preparation_state':None,'drainage_status':'rinsed'},'1 cup dried chickpeas, rinsed','dried chickpeas'),
    ({'preservation_methods':[],'preparation_state':'cooked','drainage_status':None},'2 cups cooked chickpeas','cooked chickpeas'),
    ({'preservation_methods':[],'preparation_state':None,'drainage_status':None},'1 cup chickpeas',None),
])
def test_stated_line_state_resolves_pulse_preparation_abstention(monkeypatch,facets,original,expected):
    from unittest.mock import Mock
    from recipe_wrangler.matching import runtime
    engine=object.__new__(runtime.MatchingEngine)
    engine.source_descriptions=set();engine.source_description_rows={}
    engine.annotator=Mock();engine.foodex=None;engine.ids={}
    engine.records={};engine.profiles={};engine.annotations={};engine.source_cache={}
    engine.aliases=[];engine.alias_index={}
    monkeypatch.setattr(runtime,'recover_occurrence_facts',lambda name,*args:{'input_text':name,'facets':dict(facets),
        'main_ingredient':'chickpea','occurrence_recovery':{'conflicts':[],'recovered':[]}})
    monkeypatch.setattr(runtime,'SourceNutrientPolicy',lambda *args:Mock())
    gate=Mock();gate.audit=[]
    monkeypatch.setattr(runtime,'CompatibilityGatedCatalogue',lambda *args,**kwargs:gate)
    def match(name,region,**kwargs):
        if name.split()[0] in {'canned','dried','cooked'}:return {'match':None,'source_key':'eu','confidence':'none','reason':'resolved:'+name}
        return {'match':None,'source_key':'eu','confidence':'none','reason':'ambiguous_preparation_state'}
    monkeypatch.setattr(runtime.nm,'best_nutrition_match',match)
    monkeypatch.setattr(runtime,'choose_supported_occurrence_candidate',lambda *args:None)
    monkeypatch.setattr(runtime,'calculation_decision',lambda *args,**kwargs:{'kind':'unresolved'})
    result=engine.resolve('chickpeas','eu',original)
    assert result['decision']['reason']==('resolved:'+expected if expected else 'ambiguous_preparation_state')


def test_stated_can_outranks_dried_read_from_patted_dry(monkeypatch):
    from unittest.mock import Mock
    from recipe_wrangler.matching import runtime
    engine=object.__new__(runtime.MatchingEngine)
    engine.source_descriptions=set();engine.source_description_rows={}
    engine.annotator=Mock();engine.foodex=None;engine.ids={}
    engine.records={};engine.profiles={};engine.annotations={};engine.source_cache={}
    engine.aliases=[];engine.alias_index={}
    facets={'preservation_methods':['dried'],'preparation_state':None,'drainage_status':'drained_and_rinsed'}
    monkeypatch.setattr(runtime,'recover_occurrence_facts',lambda name,*args:{'input_text':name,'facets':dict(facets),
        'main_ingredient':'chickpea','occurrence_recovery':{'conflicts':[],'recovered':[]}})
    monkeypatch.setattr(runtime,'SourceNutrientPolicy',lambda *args:Mock())
    gate=Mock();gate.audit=[]
    monkeypatch.setattr(runtime,'CompatibilityGatedCatalogue',lambda *args,**kwargs:gate)
    def match(name,region,**kwargs):
        if name.split()[0] in {'canned','dried'}:return {'match':None,'source_key':'eu','confidence':'none','reason':'resolved:'+name}
        return {'match':None,'source_key':'eu','confidence':'none','reason':'ambiguous_preparation_state'}
    monkeypatch.setattr(runtime.nm,'best_nutrition_match',match)
    monkeypatch.setattr(runtime,'choose_supported_occurrence_candidate',lambda *args:None)
    monkeypatch.setattr(runtime,'calculation_decision',lambda *args,**kwargs:{'kind':'unresolved'})
    result=engine.resolve('chickpeas','eu','400g can chickpeas, rinsed, drained, patted dry')
    assert result['decision']['reason']=='resolved:canned chickpeas'


def test_comma_free_source_label_is_restored_after_a_shortened_head():
    assert source_description_name('cream', '90g Cream sour', {'cream sour'}) == 'Cream sour'
    assert source_description_name('cream', '90g Cream sour', set()) == 'cream'
    assert source_description_name('cream', '90g Cream sour or yoghurt', {'cream sour or yoghurt'}) == 'cream'
    assert source_description_name('flour', '90g Cream sour', {'cream sour'}) == 'flour'


def test_reviewed_fills_complete_records_missing_a_score_nutrient():
    from recipe_wrangler.matching.reviewed_aliases import RECORD_FILLS, REVIEWED_SELECTIONS
    anses = 'catalogue:["Anses. 2025. CIQUAL French food composition table","EFSA EU FCDB 2026","20195"]'
    assert RECORD_FILLS[anses]['sugar_g']['value'] == 2.5
    assert 'whole berry cranberry sauce' in REVIEWED_SELECTIONS


def test_low_sodium_salt_text_selects_the_losalt_record_whatever_the_parser_name_is():
    from recipe_wrangler.matching.reviewed_aliases import low_salt_name, REVIEWED_SELECTIONS
    assert low_salt_name('salt', '15g / ½ oz. of low-sodium salt (LoSalt)') == 'low sodium salt'
    assert low_salt_name('salt', '1 tsp salt') is None
    assert 'low sodium salt' in REVIEWED_SELECTIONS
