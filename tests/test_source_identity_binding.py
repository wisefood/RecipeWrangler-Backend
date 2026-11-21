"""Explicit source identity must be exact, release-pinned and policy-checked."""
from unittest.mock import Mock
import pytest
from recipe_wrangler.matching import runtime


@pytest.fixture
def bound(monkeypatch):
    engine=object.__new__(runtime.MatchingEngine);engine.release_id='release'
    engine.annotator=Mock();engine.records={};engine.profiles={};engine.foodex=None
    engine.annotations={};engine.source_cache={}
    native={'id':'efsa_slovenia:code','source':'efsa_slovenia','country':'SI','food_name':'Veal, ribs',
        'nutrients':{'Energy, kcal':{'value':111.45,'unit':'kcal'},'Carbohydrate, by difference':{'value':0,'unit':'g'}}}
    candidate={'id':'efsa_slovenia:code','document':'Veal, ribs'}
    collection=runtime.COLLECTIONS['eu']
    engine.ids={(collection,candidate['id']):{'annotation':{},'candidate':candidate,'nutrient_row':native}}
    binding={'release_id':'release','collection':collection,'candidate_id':candidate['id'],
        'source_prefix':'efsa_slovenia','country':'SI','food_code':'code','expected_label':'Veal, ribs',
        'source_line':'1500g Veal, ribs',
        'nutrient_row_digest':runtime.store.digest(native)}
    facts={'input_text':'Veal, ribs','facets':{},'occurrence_recovery':{'conflicts':[],'recovered':[]}}
    monkeypatch.setattr(runtime,'recover_occurrence_facts',lambda *a:facts)
    source={'source_key':'eu','label':'Veal, ribs','values_per_100g':runtime.native_values(native),
            'blockers':[],'text_annotation':facts,'basis':{'evidence':'legacy_contract_assumption'},
            'native_id':candidate['id'],'native_annotation':None,'core_complete':False}
    policy=Mock();policy.source.return_value=source
    monkeypatch.setattr(runtime,'SourceNutrientPolicy',lambda *a:policy)
    return engine,binding,source


def test_exact_source_uses_whole_record_and_retains_true_zero(bound,monkeypatch):
    engine,binding,source=bound
    seen=[]
    def calculate(observed,candidate,facts,**kw):
        seen.append(facts)
        return {'kind':'retained_legacy_estimate','values_per_100g':facts['values_per_100g']}
    monkeypatch.setattr(runtime,'calculation_decision',calculate)
    result=engine.resolve_source_identity('Veal, ribs','slovenian','1500g Veal, ribs',binding)
    assert result['calculation']['values_per_100g']['carbs_g']==0
    assert seen==[source] and result['decision']['reason']=='explicit_source_identity'
    result['source_identity_evidence']['country']='changed'
    assert binding['country']=='SI'


@pytest.mark.parametrize('field,value',[
    ('release_id','other'),('candidate_id','missing'),('food_code','other'),('country','SK'),
    ('source_prefix','other'),('expected_label','Veal, breast'),('nutrient_row_digest','stale'),
    ('collection',runtime.COLLECTIONS['hungarian']),
    ('source_line','1500g Chicken'),
])
def test_source_identity_mismatch_cannot_fall_back_silently(bound,field,value):
    engine,binding,source=bound
    binding[field]=value
    with pytest.raises(ValueError):engine.resolve_source_identity('Veal, ribs','slovenian','1500g Veal, ribs',binding)


def test_correct_source_cannot_be_used_as_a_different_ingredient_or_country(bound):
    engine,binding,source=bound
    with pytest.raises(ValueError):engine.resolve_source_identity('Chicken','slovenian','1500g Chicken',binding)
    with pytest.raises(ValueError):engine.resolve_source_identity('Veal, ribs','irish','1500g Veal, ribs',binding)


def test_exact_source_still_obeys_source_blockers(bound):
    engine,binding,source=bound
    source['blockers']=['native_nutrient_linkage_mismatch:energy_kcal']
    result=engine.resolve_source_identity('Veal, ribs','slovenian','1500g Veal, ribs',binding)
    assert result['calculation']['kind']=='unresolved'
    assert result['calculation']['blockers']==source['blockers']


def test_source_annotation_cannot_override_pinned_native_values(bound):
    engine,binding,source=bound
    source['values_per_100g']['energy_kcal']=999
    with pytest.raises(ValueError,match='stale'):engine.resolve_source_identity('Veal, ribs','slovenian','1500g Veal, ribs',binding)


def test_binding_vector_is_checked_before_matching():
    engine=object.__new__(runtime.MatchingEngine)
    with pytest.raises(ValueError,match='lengths'):
        engine.calculate(title='Test',ingredient_names=['veal'],weights=[10],source='slovenian',source_bindings=[])


def test_binding_evidence_is_saved_as_matching_input_without_changing_quantity_hash(monkeypatch):
    engine=object.__new__(runtime.MatchingEngine);engine.release_id='release'
    binding={'food_code':'code','source_line':'1500g Veal, ribs'}
    values={key:1.0 for key in runtime.METRICS};values.update(energy_kcal=100,sugar_g=None)
    resolved={'decision':{'confidence':'strong','match':{'id':'record'}},
        'calculation':{'kind':'retained_legacy_estimate','values_per_100g':values,'assumptions':[],'blockers':[]},
        'source_facts':{'source_key':'eu','native_id':'record','label':'Veal, ribs'},
        'observed_facts':{'facets':{}},'leading_alternatives':[],'source_identity_evidence':binding}
    engine.resolve_source_identity=Mock(return_value=resolved);engine.resolve=Mock(return_value=resolved)
    save=Mock(return_value='run');monkeypatch.setattr(runtime.store,'save_run',save)
    kwargs={'title':'Test','ingredient_names':['Veal, ribs'],'weights':[5],
            'original_texts':['1500g Veal, ribs'],'source':'slovenian','serves':2}
    bound=engine.calculate(**kwargs,source_bindings=[binding],recipe_id='recipe')
    assert save.call_args.args[4]['source_bindings']==[binding]
    assert bound['details'][0]['source_identity_evidence']==binding
    engine.resolve_source_identity.assert_called_once()
    unbound=engine.calculate(**kwargs)
    assert bound['quantity_input_hash']==unbound['quantity_input_hash']
    assert 'source_identity_evidence' not in unbound['details'][0]
