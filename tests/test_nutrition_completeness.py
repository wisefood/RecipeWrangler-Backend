import pytest
from recipe_wrangler.tools.nutrition_completeness import METRICS, native_values, contribution, completeness


@pytest.fixture(autouse=True)
def legacy_calculator_mode(monkeypatch):
    # These tests mock the legacy matcher, not a published runtime/database.
    monkeypatch.setenv('NUTRITION_MATCHER_MODE','legacy')
    monkeypatch.setenv('NUTRITION_TOTALS_MODE','legacy')


def test_missing_nutrients_are_not_zero_and_completeness_is_per_nutrient():
    known=native_values({'Protein (g)':10,'Sodium (mg)':0})
    result=completeness([contribution(known,100),contribution({'protein_g':4},50)],2)
    assert result['complete_totals']['protein_g']==12
    assert result['complete_totals']['sodium_mg'] is None
    assert result['supported_partial_totals']['sodium_mg']==0
    assert result['complete_totals_per_serving']['protein_g']==6
    assert result['nutrient_status']['protein_g']=='complete'
    assert result['nutrition_status']=='incomplete'


def test_wrong_units_remain_unknown_and_explicit_zero_is_valid():
    row={'nutrients':{'Energy':{'value':0,'unit':'kJ'},'Sodium, Na':{'value':4,'unit':'g'}}}
    values=native_values(row)
    assert values['energy_kcal']==0
    assert values['sodium_mg'] is None
    assert all(v==0 for v in contribution({},0).values())


@pytest.mark.parametrize('weight',[float('nan'),-1,float('inf')])
def test_invalid_quantity_cannot_produce_complete_totals(weight):
    with pytest.raises(ValueError):contribution({'protein_g':1},weight)


def test_calculator_exposes_incomplete_totals_without_changing_legacy_fields(monkeypatch):
    from recipe_wrangler.tools import nutritional_calculator as calc
    monkeypatch.setattr(calc,'best_nutrition_match',lambda *a,**k:{'match':None,'source_key':'eu','confidence':'none','reason':'no_match'})
    result=calc.nutritional_tool_vector.invoke({'title':'Unknown ingredient','ingredient_names':['unknown food'],
        'weights':[100],'source':'eu','serves':2})
    assert result['clean_totals']['energy_kcal']==0
    assert result['complete_totals']['energy_kcal'] is None
    assert result['supported_partial_totals']['energy_kcal']==0
    assert result['missing_contributions']['energy_kcal']==1
    assert result['nutrition_status']=='incomplete'


def test_profile_merge_preserves_completeness_metadata(monkeypatch):
    from types import SimpleNamespace
    from recipe_wrangler.tools import recipe_profiling_tool as rpt
    monkeypatch.setattr(rpt,'nutritional_tool_vector',SimpleNamespace(invoke=lambda payload:{
        'source_key':'eu','details':[],'nutrition_status':'incomplete',
        'quantity_input_hash':'shared-hash','quantity_version':'shared-v1',
        'missing_mass_share':{'energy_kcal':.035},
        'complete_totals':{'energy_kcal':None},'supported_partial_totals':{'energy_kcal':12}}))
    monkeypatch.setattr(rpt,'sustainability_tool_vector',SimpleNamespace(invoke=lambda payload:{'details':[]}))
    result=rpt.Recipe_Profiling_Tool({'title':'Test','region':'EU','ingredient_names':[],'weights':[]})
    assert result['nutrition_completeness']['complete_totals']['energy_kcal'] is None
    assert result['nutrition_completeness']['supported_partial_totals']['energy_kcal']==12
    assert result['nutrition_completeness']['missing_mass_share']['energy_kcal']==.035
    assert result['matching_metadata']['quantity_input_hash']=='shared-hash'


def test_nutrition_node_preserves_missing_mass_share(monkeypatch):
    from types import SimpleNamespace
    from recipe_wrangler.schemas import RecipeState
    from recipe_wrangler.tools import nutritional_calculator as calc
    monkeypatch.setattr(calc, 'nutritional_tool_vector', SimpleNamespace(invoke=lambda payload: {
        'source_key': 'eu', 'details': [], 'serves': 1,
        'nutrition_status': 'minor_gaps', 'missing_mass_share': {'energy_kcal': .01},
    }))
    state = calc.Nutrition_Node(RecipeState(title='Test', ingredient_names=['herbs'], weights=[1], serves=1))
    assert state.nutrition_completeness['missing_mass_share']['energy_kcal'] == .01


@pytest.mark.parametrize('mode,expected',[('legacy',0),('shadow',0),('strict',None)])
def test_totals_mode_is_reversible_and_keeps_legacy_values(monkeypatch,mode,expected):
    from recipe_wrangler.tools import nutritional_calculator as calc
    monkeypatch.setenv('NUTRITION_TOTALS_MODE',mode)
    monkeypatch.setattr(calc,'best_nutrition_match',lambda *a,**k:{'match':None,'source_key':'eu','confidence':'none'})
    result=calc.nutritional_tool_vector.invoke({'title':'Test','ingredient_names':['unknown'],
        'weights':[100],'source':'eu','serves':2})
    assert result['clean_totals']['energy_kcal']==expected
    assert result['total_energy_kcal_per_serving_eu']==expected
    assert result['totals_mode']==mode
    if mode!='legacy':assert result['legacy_totals']['energy_kcal']==0


def test_invalid_mode_does_not_silently_fall_back():
    from recipe_wrangler.tools.nutrition_completeness import apply_totals_mode
    with pytest.raises(ValueError,match='NUTRITION_TOTALS_MODE'):apply_totals_mode({},'eu','typo')


def test_incomplete_strict_profile_is_not_scored_or_cast_to_float():
    from recipe_wrangler.tools import recipe_profiling_tool as rpt
    totals={'clean_totals':dict.fromkeys(rpt._CLEAN_TOTAL_KEYS)}
    assert rpt._extract_clean_totals(totals,'_eu') is None
    assert rpt._build_total_nutrients_for_score(totals,'_eu',2) is None


def test_persistence_keeps_known_nutrients_when_another_is_missing():
    from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals,_CLEAN_TOTAL_KEYS
    totals={'clean_totals':dict.fromkeys(_CLEAN_TOTAL_KEYS)}
    totals['clean_totals']['energy_kcal']=120
    assert _extract_clean_totals(totals,'_eu') is None
    stored=_extract_clean_totals(totals,'_eu',allow_missing=True)
    assert stored['energy_kcal']==120
    assert stored['fibre_g'] is None


def test_gap_at_or_below_tolerance_counts_as_zero_and_reports_its_share():
    known=dict.fromkeys(METRICS,1)
    result=completeness([contribution(known,980),contribution({**known,'protein_g':None},20)],2)
    assert result['complete_totals']['protein_g']==pytest.approx(9.8)
    assert result['nutrient_status']['protein_g']=='minor_gaps'
    assert result['nutrient_status']['energy_kcal']=='complete'
    assert result['missing_mass_share']['protein_g']==pytest.approx(.02)
    assert result['missing_contributions']['protein_g']==1
    assert result['nutrition_status']=='minor_gaps'


def test_gap_above_tolerance_stays_unknown():
    result=completeness([contribution({'protein_g':1},970),contribution({},30)],2)
    assert result['complete_totals']['protein_g'] is None
    assert result['supported_partial_totals']['protein_g']==pytest.approx(9.7)
    assert result['nutrient_status']['protein_g']=='incomplete'
    assert result['nutrition_status']=='incomplete'


def test_small_unmatched_chilli_is_disclosed_without_blocking_totals():
    result=completeness([contribution(dict.fromkeys(METRICS,1),1000),
        contribution({},0.2,ingredient='dried chilli flakes')],2)
    assert result['nutrition_status']=='minor_gaps'
    assert result['complete_totals']['sodium_mg']==10
    assert result['missing_contributions']['sodium_mg']==1
    assert result['missing_mass_share']['sodium_mg']>0


def test_small_unmatched_salt_cannot_be_hidden_by_mass_tolerance():
    result=completeness([contribution(dict.fromkeys(METRICS,1),1000),
        contribution({},1,ingredient='salt and pepper')],2)
    assert result['complete_totals']['sodium_mg'] is None
    assert result['nutrient_status']['sodium_mg']=='incomplete'
    assert result['nutrient_status']['energy_kcal']=='minor_gaps'

