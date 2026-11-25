from copy import deepcopy
import pytest
from recipe_wrangler.matching import completeness_repair as repair


def facts(label='Rice, boiled', **values):
    nutrients=dict.fromkeys(repair.METRICS,0.)
    nutrients.update(values)
    return {'label':label,'values_per_100g':nutrients,'text_annotation':{
        'main_ingredient':'rice','facets':{'base_identity':'rice','preparation_state':'cooked'},'qualifiers':{}}}


def row(source):
    return {'annotation':source,'candidate':{'id':'better'},'collection':'eu','candidate_id':'better'}


@pytest.mark.parametrize('label',['Sea salt','Salt, sea','Salt, table','Salt, white (sea, igneous or rock)'])
def test_salt_without_sodium_is_ineligible_even_if_ontology_head_is_missing(label):
    source=facts(label,sodium_mg=None);source['text_annotation']={}
    assert not repair.eligible_record(source)
    source['values_per_100g']['sodium_mg']=37600
    assert repair.eligible_record(source)


def test_salt_rule_does_not_exclude_unrelated_food_with_salt_in_label():
    assert repair.eligible_record(facts('Chocolate with sea salt',sodium_mg=None))


def test_more_complete_record_preserves_state_and_never_trades_missing_nutrients(monkeypatch):
    monkeypatch.setattr(repair,'calculation_decision',lambda *a,**k:{'kind':'automatic_estimate'})
    old=facts(sodium_mg=None)
    chosen=repair.choose_complete_record(old['text_annotation'],old,[row(facts())])
    assert chosen is not None
    raw=facts();raw['text_annotation']['facets']['preparation_state']='raw'
    assert repair.choose_complete_record(old['text_annotation'],old,[row(raw)]) is None
    assert repair.choose_complete_record(old['text_annotation'],old,[row(facts(fibre_g=None))]) is None


def test_semantic_rejection_is_not_overridden_by_coverage(monkeypatch):
    monkeypatch.setattr(repair,'calculation_decision',lambda *a,**k:{'kind':'unresolved'})
    old=facts(sodium_mg=None)
    assert repair.choose_complete_record(old['text_annotation'],old,[row(facts())]) is None


def test_sea_salt_word_order_is_supported_without_generic_salt_substitution(monkeypatch):
    monkeypatch.setattr(repair,'calculation_decision',lambda *a,**k:{'kind':'automatic_estimate'})
    old=facts('Sea salt',sodium_mg=None);old['text_annotation']={}
    salt=facts('Salt, sea',sodium_mg=37600);salt['text_annotation']={}
    assert repair.choose_complete_record({},old,[row(salt)]) is not None
    salt['label']='Salt, low sodium'
    assert repair.choose_complete_record({},old,[row(salt)]) is None


@pytest.mark.parametrize('old_label,new_label',[
    ('Pepper, black','Sweet pepper, green, yellow or red, raw'),
    ('Cheese, Cheddar type, half fat','Plant-based cheese, without soybean, prepacked'),
    ('Basmati rice, raw','Rice, white, raw'),
    ('Pasta, white, dried, raw','Pasta, egg, white, dried, raw'),
])
def test_shared_ontology_head_cannot_change_food_subtype(monkeypatch,old_label,new_label):
    monkeypatch.setattr(repair,'calculation_decision',lambda *a,**k:{'kind':'automatic_estimate'})
    old=facts(old_label,sodium_mg=None)
    assert repair.choose_complete_record(old['text_annotation'],old,[row(facts(new_label))]) is None


def test_rejected_first_record_does_not_hide_next_same_food_record(monkeypatch):
    monkeypatch.setattr(repair,'calculation_decision',lambda *a,**k:{'kind':'automatic_estimate'})
    old=facts('Courgette, raw',sodium_mg=None)
    old['text_annotation']['facets'].update(base_identity='zucchini',preparation_state='raw')
    unsafe=facts('Courgette, roasted in oil')
    unsafe['text_annotation']['facets'].update(base_identity='zucchini',preparation_state='cooked')
    valid=deepcopy(old);valid['values_per_100g']['sodium_mg']=3.
    bad_row={**row(unsafe),'candidate_id':'first'}
    good_row={**row(valid),'candidate_id':'second'}
    chosen=repair.choose_complete_record(old['text_annotation'],old,[bad_row,good_row])
    assert chosen is not None and chosen[0]['candidate_id']=='second'
