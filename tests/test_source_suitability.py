import pytest
from recipe_wrangler.matching.source_suitability import replacement_reason, unsupported_specialist_part


@pytest.mark.parametrize('query,candidate,blocked',[
    ('low fat Greek yogurt','Yogurt, low fat, toffee',True),
    ('low fat Greek yoghurt','Yogurt, low fat, hazelnut',True),
    ('Greek yogurt','Yogurt, low fat, fruit',True),
    ('Greek yogurt','Yogurt, low fat, plain',False),
    ('low fat toffee yogurt','Yogurt, low fat, toffee',False),
    ('hazelnut yogurt','Yogurt, low fat, hazelnut',False),
    ('vanilla extract','Vanilla extract',False),
])
def test_yogurt_flavour_is_material_even_when_the_fat_level_matches(query,candidate,blocked):
    from recipe_wrangler.matching.source_suitability import material_scope_conflicts
    conflicts=material_scope_conflicts({'input_text':query,'facets':{}},{'input_text':candidate,'facets':{}})
    assert ('product.unsupported_yogurt_flavour' in conflicts) is blocked


@pytest.mark.parametrize('query,candidate,rule',[
    ('egg substitute','Coffee substitute','identity.egg_substitute_target_conflict'),
    ('liquid','Iron liquid','identity.unspecified_liquid'),
    ('beef bones','Beef, bone marrow, raw','basis.bones_require_edible_yield'),
    ('kamut berries','Physalis berries raw','identity.grain_berries_not_fruit'),
    ('italian prune plums','Plums, lyophilized','product.unsupported_produce_drying'),
])
def test_confirmed_outlier_identity_and_basis_conflicts(query,candidate,rule):
    from recipe_wrangler.matching.source_suitability import material_scope_conflicts
    annotation=lambda text:{'input_text':text,'facets':{}}
    assert rule in material_scope_conflicts(annotation(query),annotation(candidate))


@pytest.mark.parametrize('query,candidate',[
    ('egg substitute','Egg substitute, liquid'),('kamut berries','Khorasan wheat, raw'),
    ('dried plums','Plums, lyophilized'),('chicken breast','Chicken breast, raw')])
def test_outlier_guards_preserve_supported_identity_and_state(query,candidate):
    from recipe_wrangler.matching.source_suitability import material_scope_conflicts
    annotation=lambda text:{'input_text':text,'facets':{}}
    assert material_scope_conflicts(annotation(query),annotation(candidate))==[]


@pytest.mark.parametrize('query,candidate',[
    ('dates','Dates, dried'),('raisins','Raisins, dried'),('onion powder','Onion, dried')])
def test_drying_guard_preserves_conventional_dried_foods_and_explicit_powders(query,candidate):
    from recipe_wrangler.matching.source_suitability import material_scope_conflicts
    assert material_scope_conflicts({'input_text':query,'facets':{}},{'input_text':candidate,'facets':{}})==[]


@pytest.mark.parametrize('query,candidate,blocked',[
    ('milk','Milk reduced fat dried',True),
    ('low-fat milk','Milk lowfat fat dried',True),
    ('milk powder','Milk reduced fat dried',False),
    ('reconstituted milk','Milk reduced fat dried',False),
    ('milk','Milk, semi-skimmed, pasteurised',False),
])
def test_milk_fat_level_does_not_override_liquid_powder_form(query,candidate,blocked):
    from recipe_wrangler.matching.source_suitability import material_scope_conflicts
    annotation=lambda label:{'input_text':label,'facets':{}}
    assert ('product.unsupported_milk_powder' in material_scope_conflicts(annotation(query),annotation(candidate))) is blocked


def facts(label, state=None, known=('energy_kcal','fat_g')):
    return {'label':label,'text_annotation':{'main_ingredient':label.split(',')[0].casefold(),'facets':{'preparation_state':state}},'known_nutrients':list(known)}


@pytest.mark.parametrize('query,label,blocked',[
    ('pork','Pork, head',True),('pork liver','Pork liver',False),
    ('lamb','Lamb kidney',True),('chicken','Chicken meat raw',False),
    ('lettuce','Lettuce head',False)])
def test_specialist_parts_require_input_evidence(query,label,blocked):
    assert unsupported_specialist_part(query,label) is blocked


def test_short_label_does_not_replace_supported_state():
    assert replacement_reason('chicken',facts('Chicken, meat, raw','raw'),facts('CHICKEN')) == 'lost_source_preparation_evidence'


def test_more_complete_unrelated_food_does_not_win_completeness_tiebreak():
    assert replacement_reason('yoghurt',facts('Yoghurt full fat'),facts('Yoghurt strawberry',known=('fat_g',))) is None


def test_equal_description_prefers_available_nutrients_without_imputation():
    assert replacement_reason('courgette',facts('Courgette, raw','raw'),facts('Courgette, raw','raw',('fat_g',))) == 'less_complete_equivalent_record'


def test_raw_default_does_not_discard_strictly_fuller_low_risk_profile():
    assert replacement_reason('Brussels sprouts',facts('Brussels sprouts, raw','raw',('fat_g',)),facts('Brussels sprouts')) is None


@pytest.mark.parametrize('query',['cod','chicken','milk','rice','chickpeas'])
def test_completeness_cannot_override_material_preparation_uncertainty(query):
    assert replacement_reason(query,facts(query+', raw','raw',('fat_g',)),facts(query))=='lost_source_preparation_evidence'


def test_explicit_raw_input_retains_preparation_evidence():
    assert replacement_reason('raw spinach',facts('Spinach, raw','raw',('fat_g',)),facts('Spinach'))=='lost_source_preparation_evidence'


def test_raw_occurrence_property_is_protected_without_changing_query_text():
    assert replacement_reason('spinach', facts('Spinach, raw', 'raw', ('fat_g',)), facts('Spinach'),
        observed_facets={'preparation_state': 'raw'}) == 'lost_source_preparation_evidence'


def test_unknown_identity_cannot_use_fuller_profile_exception():
    old=facts('Unidentified, raw','raw',('fat_g',));new=facts('Unidentified')
    old['text_annotation']['main_ingredient']=None
    new['text_annotation']['main_ingredient']=None
    assert replacement_reason('unidentified',old,new)=='lost_source_preparation_evidence'


def test_fuller_profile_with_preservation_is_not_equivalent_to_raw_default():
    old=facts('Spinach, raw','raw',('fat_g',));new=facts('Spinach')
    new['text_annotation']['facets']['preservation_methods']=['dried']
    assert replacement_reason('spinach',old,new)=='lost_source_preparation_evidence'


def test_shared_base_identity_does_not_establish_full_description_equivalence():
    old=facts('Spinach, young leaves for salad, raw','raw',('fat_g',))
    new=facts('Spinach, baby leaf')
    old['text_annotation']['main_ingredient']=new['text_annotation']['main_ingredient']='spinach'
    assert replacement_reason('spinach',old,new)=='lost_source_preparation_evidence'


def test_explicit_dried_source_wording_covers_facet_extraction_omission():
    from recipe_wrangler.matching.source_suitability import material_scope_conflicts
    query={'input_text':'thyme','facets':{'preservation_methods':['dried']}}
    source={'input_text':'Thyme, dried','facets':{'preservation_methods':[],'preparation_state':'raw'}}
    assert material_scope_conflicts(query,source)==[]
    source['input_text']='Thyme, raw'
    assert 'product.dried_raw_conflict' in material_scope_conflicts(query,source)


@pytest.mark.parametrize('name',['bulgur wheat','pasta','rice'])
def test_raw_dry_staples_are_not_mistaken_for_fresh_produce(name):
    from recipe_wrangler.matching.source_suitability import material_scope_conflicts
    query={'input_text':name,'facets':{'preservation_methods':['dried']}}
    source={'input_text':name+', raw','facets':{'preservation_methods':[],'preparation_state':'raw'}}
    assert material_scope_conflicts(query,source)==[]
