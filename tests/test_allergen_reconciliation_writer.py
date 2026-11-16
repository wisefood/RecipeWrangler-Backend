"""Guarded writer/rollback contracts; live graph mutation is deliberately separate."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import importlib.util
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from neo4j.time import DateTime, Date, Time, Duration
from neo4j.spatial import WGS84Point, CartesianPoint

from scripts.neo4j import reconcile_allergen_journal as writer
from scripts.neo4j.preview_allergen_reconciliation import canonical, digest, encoded
from scripts.neo4j.rehearse_allergen_reconciliation import rehearse_journal

_spec = importlib.util.spec_from_file_location('writer_fixture', Path(__file__).with_name('test_allergen_reconciliation_rehearsal.py'))
_fixture = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_fixture)


class Record(dict):
    def data(self):
        return dict(self)


class Tx:
    def __init__(self, stored=None, declarations=()):
        self.calls=[];self.stored=stored;self.declarations={row['element_id']:row for row in declarations}
    def run(self, query, **params):
        self.calls.append((query,params))
        if 'RETURN properties(n) AS receipt' in query:
            return [Record(receipt=self.stored)] if self.stored else []
        if 'MATCH (d:AllergenDeclaration) WHERE elementId(d)' in query:
            row = self.declarations[params['id']]
            return [Record({key:value for key,value in row.items() if key not in {'edge_id','edge_properties'}})]
        if 'MATCH (d:AllergenDeclaration {declaration_id:$id})' in query:
            return [Record(count=0)]
        return [Record(count=1)]


def stored(receipt, state='applied_queue_pending'):
    raw=writer.encode_receipt(receipt)
    return {'payload_json':raw,'payload_sha256':hashlib.sha256(raw.encode()).hexdigest(),'state':state}


def scenario(supported=False):
    journal,ingredients,recipes,classes=_fixture.fixture(supported=supported)
    ingredients=ingredients[:1]
    stamp=DateTime(2026,8,6,10,9,28,nanosecond=843000000,tzinfo=ZoneInfo('UTC'))
    ingredients[0]['links'][0]['properties']={'linked_at':stamp,'source_order':['z','a']}
    journal['cases'][0]['before_sha256']=digest(ingredients[0])
    journal['cases'][0]['link_operation']['before']=deepcopy(ingredients[0]['links'][0])
    journal=json.loads(encoded(journal))
    current={'ingredients':canonical(ingredients),'recipes':recipes,'classes':classes}
    expected=rehearse_journal(journal,**json.loads(encoded(current)))
    after=deepcopy(current);after['ingredients']=deepcopy(expected['after'])
    for row in after['ingredients']:
        for link in row['links']:
            if link['element_id'].startswith('rehearsal:'):
                link['element_id']='allocated_link';link['properties']['linked_at']=stamp
        for declaration in row['declarations']:
            if declaration['element_id'].startswith('rehearsal:'):
                declaration['element_id']='allocated_declaration';declaration['edge_id']='allocated_attachment'
                for key in ['created_at','updated_at']:declaration['properties'][key]=stamp
                declaration['concerns'][0]['element_id']='allocated_concern'
    after=canonical(after)
    return journal,current,after,expected


def apply_scenario(supported=False):
    journal,current,after,expected=scenario(supported)
    tx=Tx()
    inputs={'r':{'properties':{'title':'source title'},'ingredients':[]}}
    with patch.object(writer,'_lock_selected'),patch.object(writer,'capture_current',side_effect=[current,after]),patch.object(writer,'recipe_inputs',return_value=inputs):
        receipt=writer.apply_transaction(tx,journal,operation_id='operation',profile_guard={'version':'owner1'},
                                         expected_classes=current['classes'],controlled_window=True)
    return tx,receipt,journal,current,after,expected


@pytest.mark.parametrize('supported',[False,True])
def test_apply_records_real_ids_timestamps_and_durable_complete_sibling_set(supported):
    tx,receipt,_,current,after,_=apply_scenario(supported)
    assert receipt['before_ingredients']==current['ingredients']
    assert receipt['after_ingredients']==after['ingredients']
    assert receipt['recipe_ids']==['r']
    assert receipt['memberships']=={'i':['r']}
    assert writer.decode_receipt(stored(receipt))==receipt
    assert tx.calls[-1][1]['state']=='applied_queue_pending'
    assert not any('DELETE d' in query or 'DETACH' in query for query,_ in tx.calls)


def test_apply_requires_controlled_window_before_any_graph_call():
    tx=Tx();journal,current,_,_=scenario()
    with pytest.raises(ValueError,match='controlled'):
        writer.apply_transaction(tx,journal,operation_id='operation',profile_guard={},expected_classes=current['classes'])
    assert not tx.calls


@pytest.mark.parametrize('drift',['ingredient','membership','ontology'])
def test_apply_rejects_precondition_drift_before_mutating_evidence(drift):
    journal,current,_,_=scenario();expected_classes=deepcopy(current['classes'])
    if drift=='ingredient':current['ingredients'][0]['properties']['name']='user edit'
    elif drift=='membership':current['recipes'].append({**deepcopy(current['recipes'][0]),'element_id':'new_sibling'})
    else:current['classes'][0]['properties']['label']='new label'
    tx=Tx()
    with patch.object(writer,'_lock_selected'),patch.object(writer,'capture_current',return_value=current),pytest.raises(ValueError,match='preconditions'):
        writer.apply_transaction(tx,journal,operation_id='operation',profile_guard={},expected_classes=expected_classes,controlled_window=True)
    assert len(tx.calls)==1


@pytest.mark.parametrize('drift',['unrelated_property','array_order','new_link_timestamp','recipe'])
def test_expected_after_validation_rejects_unreviewed_changes(drift):
    journal,current,after,expected=scenario(supported=True)
    if drift=='unrelated_property':after['ingredients'][0]['properties']['name']='user edit'
    elif drift=='array_order':after['ingredients'][0]['edges'][0]['properties']['sources'].reverse()
    elif drift=='new_link_timestamp':after['ingredients'][0]['links'][0]['properties']['linked_at']='stringified timestamp'
    else:after['recipes'][0]['properties']['declared_allergens']=[]
    with pytest.raises(ValueError):writer.verify_after(journal,current,after,expected)


@pytest.mark.parametrize('supported',[False,True])
def test_rollback_restores_typed_values_with_new_internal_relationship_ids(supported):
    _,receipt,journal,current,after,_=apply_scenario(supported)
    restored=deepcopy(current)
    for row in restored['ingredients']:
        for key in ['links','edges']:
            for edge in row[key]:edge['element_id']='restored:'+edge['element_id']
        for declaration in row['declarations']:declaration['edge_id']='restored:'+declaration['edge_id']
    tx=Tx(stored(receipt),declarations=current['ingredients'][0]['declarations'])
    with patch.object(writer,'_lock_selected'),patch.object(writer,'capture_current',side_effect=[after,restored]),patch.object(writer,'recipe_inputs',return_value={'r':{'properties':{'title':'source title'},'ingredients':[]}}):
        result=writer.rollback_transaction(tx,'operation',profile_guard={'version':'owner1'},expected_classes=current['classes'],controlled_window=True)
    assert result['restored_ingredients']==restored['ingredients']
    assert tx.calls[-1][1]['state']=='rolled_back_queue_pending'
    original_link=current['ingredients'][0]['links'][0]['properties']
    assert any(params.get('properties')==original_link for _,params in tx.calls)
    assert not any('DELETE d' in query or 'DETACH' in query for query,_ in tx.calls)


@pytest.mark.parametrize('drift',['ingredient','recipe','membership','profile','detached_declaration'])
def test_rollback_refuses_intervening_edits(drift):
    _,receipt,_,current,after,_=apply_scenario(supported=True)
    tx=Tx(stored(receipt),declarations=deepcopy(current['ingredients'][0]['declarations']))
    inputs={'r':{'properties':{'title':'source title'},'ingredients':[]}};profile={'version':'owner1'}
    if drift=='ingredient':after['ingredients'][0]['properties']['name']='edit'
    elif drift=='recipe':inputs['r']['properties']['title']='edit'
    elif drift=='membership':after['recipes'].append({**deepcopy(after['recipes'][0]),'element_id':'new_sibling'})
    elif drift=='profile':profile['version']='owner2'
    else:next(iter(tx.declarations.values()))['properties']['sources']=['manual']
    with patch.object(writer,'_lock_selected'),patch.object(writer,'capture_current',return_value=after),patch.object(writer,'recipe_inputs',return_value=inputs),pytest.raises(ValueError):
        writer.rollback_transaction(tx,'operation',profile_guard=profile,expected_classes=current['classes'],controlled_window=True)
    assert not any('DELETE ' in query or 'CREATE ' in query for query,_ in tx.calls)


def test_temporal_spatial_codec_keeps_types_zone_precision_and_property_array_order():
    value={'date':Date(2026,10,31),'time':Time(12,1,2,nanosecond=123456789),
           'stamp':DateTime(2026,10,31,12,1,2,nanosecond=123456789,tzinfo=ZoneInfo('Europe/Athens')),
           'duration':Duration(months=1,days=2,seconds=3,nanoseconds=4),
           'point':WGS84Point((23.7,37.9)), 'cartesian':CartesianPoint((3,1,2)),
           'properties':{'source_order':['z','a'],'looks_temporal':'2026-10-31'}}
    decoded=writer.typed_unpack(json.loads(json.dumps(writer.typed_pack(value))))
    assert writer.typed_pack(decoded)==writer.typed_pack(value)
    assert decoded['stamp'].tzinfo.zone=='Europe/Athens'
    assert decoded['stamp'].nanosecond==123456789
    assert decoded['properties']['source_order']==['z','a']
    assert isinstance(decoded['properties']['looks_temporal'],str)
    attachment={'edge_properties':{'source_order':['z','a']}}
    assert canonical(attachment)==attachment


def test_receipt_corruption_and_oversize_abort():
    value=stored({'source':'preserved'})
    value['payload_json']='{}'
    with pytest.raises(ValueError,match='fingerprint'):writer.decode_receipt(value)
    with pytest.raises(ValueError,match='size budget'):writer.encode_receipt({'large':'x'*writer.MAX_RECEIPT_BYTES})


def test_relay_acknowledgement_rejects_obsolete_phase_and_missing_siblings():
    _,receipt,*_=apply_scenario()
    for state,generations in [('rolled_back_queue_pending',{'r':1}),('applied_queue_pending',{})]:
        with pytest.raises(ValueError):writer.acknowledge_queue(Tx(stored(receipt,state)),'operation','forward',generations)
    tx=Tx(stored(receipt))
    result=writer.acknowledge_queue(tx,'operation','forward',{'r':7})
    assert result['forward_generations']=={'r':7}
    assert tx.calls[-1][1]['state']=='applied_queued'
