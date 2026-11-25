"""Scoped repair preparation and verified canonical projection, never live writes."""

from copy import deepcopy
from unittest.mock import MagicMock,Mock

import pytest

from test_original_ingredient_context import case
from test_adaptation_preview import inputs,model
from test_adaptation_instruction_editor import setup
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement_pipeline import flow,active
from test_adaptation_objective_approval import context
from test_adaptation_enhancement import breakdown
from recipe_wrangler.matching.source_context import recipe_revision
from recipe_wrangler.services.adaptation import enhancement_pipeline as pipeline
from scripts.composition import activate_original_ingredient_context as workflow


@pytest.fixture
def prepared(case,monkeypatch):
    profile,inputs=case
    profile.update(catalogue_release='release',source_context_rematch_positions=[])
    for detail,line in zip(profile['details'],inputs['originals'],strict=True):
        detail.update(original_ingredient_text=line,quantity_input_hash=inputs['quantity_input_hash'],
            source_context_status='matched_with_original_text')
    profiles=[];saved={}
    for source in sorted(workflow.REGIONS):
        run='old-'+source
        profiles.append({'recipe_id':'r','nutrition_source':source,'baseline_run_id':run,
            'status':'rematched_for_review','missing_positions':[],'recalculated_profile':deepcopy(profile)})
        saved[source]={'run_id':run,'release_id':'release','profile':deepcopy(profile)}
    recipe={'recipe_id':'r','title':'Curry','serves':6,'original_ingredients':['fragment','fragment2']}
    monkeypatch.setattr(workflow.store,'active_profile',lambda recipe_id,source,**kwargs:deepcopy(saved[source]))
    monkeypatch.setattr(workflow,'fetch_recipe_info_by_id',lambda recipe_id:deepcopy(recipe))
    return {'profiles':profiles},saved,recipe


def test_preparation_requires_consistent_four_region_vectors_and_pins_graph_revision(prepared):
    report,saved,recipe=prepared
    before=deepcopy(report)
    entries=workflow.prepare([report])
    assert len(entries)==4 and report==before
    assert all(item['profile']['source_recipe_revision']==recipe_revision(recipe) for item in entries)


@pytest.mark.parametrize('issue',['incomplete_regions','duplicate','missing_text','stale','mass','serves','hash','pending'])
def test_invalid_repairs_never_reach_activation(prepared,issue):
    report,saved,recipe=prepared
    candidate=report['profiles'][0]['recalculated_profile']
    if issue=='incomplete_regions':report['profiles'].pop()
    elif issue=='duplicate':report['profiles'].append(deepcopy(report['profiles'][0]))
    elif issue=='missing_text':candidate['details'][0]['original_ingredient_text']=''
    elif issue=='stale':report['profiles'][0]['baseline_run_id']='other'
    elif issue=='mass':candidate['details'][0]['weight_g']=3
    elif issue=='serves':candidate['serves']=3
    elif issue=='hash':candidate['quantity_input_hash']='other'
    else:candidate['details'][0]['source_context_status']='requires_rematching'
    with pytest.raises(ValueError):workflow.prepare([report])


def test_activation_refuses_stale_predecessor_before_writes(prepared,monkeypatch,tmp_path):
    entries=workflow.prepare([prepared[0]])
    engine=MagicMock();connection=engine.begin.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.one.return_value={'mode':'new','release_id':'release'}
    connection.execute.return_value.scalar.return_value='changed-run'
    monkeypatch.setattr(workflow,'get_engine',lambda:engine)
    insert=Mock();monkeypatch.setattr(workflow.store,'insert_run',insert)
    with pytest.raises(ValueError,match='predecessor changed'):workflow.activate(entries,tmp_path/'backup.json')
    insert.assert_not_called()
    assert not (tmp_path/'backup.json').exists()


def test_rollback_will_not_overwrite_newer_profile(monkeypatch,tmp_path):
    import json
    backup=tmp_path/'backup.json'
    backup.write_text(json.dumps({'entries':[{'recipe_id':'r','nutrition_source':'eu','new_run_id':'repair'}]}))
    engine=MagicMock();connection=engine.begin.return_value.__enter__.return_value
    connection.execute.return_value.scalar.return_value='newer-profile'
    monkeypatch.setattr(workflow,'get_engine',lambda:engine)
    with pytest.raises(ValueError,match='advanced'):workflow.rollback(backup)
    assert not any(str(call.args[0]).startswith('UPDATE') for call in connection.execute.call_args_list)


@pytest.fixture
def bound(flow,active,monkeypatch):
    recipe=active[0];snapshot=flow[0][0]
    recipe['original_ingredients']=['split source fragment','another fragment']
    revision=recipe_revision(recipe)
    active[2].side_effect=lambda *args,**kwargs:{'run_id':snapshot.run_id,
        'profile':{'source_recipe_revision':revision,'nutri_score_breakdown':breakdown(energy=7)}}
    rows=[{'position':item.position,'ingredient':item.name,'original_text':item.original_text,
        'weight_g':item.quantity.weight_g,'quantity_input_hash':snapshot.quantity_input_hash,
        'source_recipe_revision':revision} for item in snapshot.ingredients]
    query=Mock(side_effect=lambda *args:deepcopy(rows))
    monkeypatch.setattr(pipeline,'projected_occurrences',query)
    return rows,query


def test_canonical_projection_binds_without_positional_raw_fragment_equality(flow,active,bound):
    result=pipeline.enhance_active_recipe('recipe-1','irish',score_context=context(flow[0][0]))
    assert result['status']=='draft_requires_review'
    assert bound[1].call_count>=3


@pytest.mark.parametrize('field',['position','ingredient','original_text','weight_g','quantity_input_hash','source_recipe_revision','missing','duplicate'])
def test_projection_mismatch_does_not_relax_evidence_checks(flow,active,bound,field):
    rows=bound[0]
    if field=='missing':rows.clear()
    elif field=='duplicate':rows.append(deepcopy(rows[0]))
    else:rows[0][field]='changed'
    result=pipeline.enhance_active_recipe('recipe-1','irish',score_context=context(flow[0][0]))
    assert result['status']=='needs_evidence'
    assert result['reasons']==['canonical_occurrence_projection_not_synchronized']
    assert not flow[1][0]


def test_scoped_worker_does_not_reconcile_unrelated_recipes(monkeypatch):
    from recipe_wrangler.matching import projection_worker as worker
    engine=MagicMock();lock=engine.connect.return_value.__enter__.return_value
    lock.execute.return_value.scalar.return_value=True
    lock.execute.return_value.mappings.return_value=[]
    monkeypatch.setattr(worker,'get_engine',lambda:engine)
    monkeypatch.setattr(worker.store,'deployment',lambda:{'mode':'new','release_id':'release'})
    reconcile=Mock();monkeypatch.setattr(worker,'reconcile_new_recipes',reconcile)
    assert worker.drain_once(['r'])==0
    reconcile.assert_not_called()
    query=next(call for call in lock.execute.call_args_list if 'FROM matching_projection_jobs' in str(call.args[0]))
    assert 'recipe_id=ANY(:ids)' in str(query.args[0]) and query.args[1]=={'ids':['r']}
