"""Operator trust boundaries and serial cross-database crash recovery."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from scripts.neo4j import reconcile_allergen_journal as writer

spec=importlib.util.spec_from_file_location('operator_writer_fixture',Path(__file__).with_name('test_allergen_reconciliation_writer.py'))
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)


@contextmanager
def graph_factory(events,receipt,state='applied_queue_pending',fail_ack=False):
    events.append('graph_open')
    tx=fixtures.Tx(fixtures.stored(receipt,state))
    class Session:
        def execute_read(self,function):return function(tx)
        def execute_write(self,function):
            events.append('graph_ack')
            if fail_ack:raise ConnectionError('simulated disconnect before acknowledgement')
            return function(tx)
    @contextmanager
    def session(**kwargs):yield Session()
    try:yield SimpleNamespace(session=session)
    finally:events.append('graph_closed')


def postgres_factory(events):
    connection=Mock()
    @contextmanager
    def begin():
        events.append('pg_open')
        try:yield connection
        except Exception:
            events.append('pg_rollback');raise
        else:events.append('pg_committed')
        finally:events.append('pg_closed')
    return SimpleNamespace(begin=begin,dispose=lambda:None)


def test_relay_closes_each_connection_before_opening_the_next():
    from recipe_wrangler.matching import reconciliation_queue
    _,receipt,*_=fixtures.apply_scenario()
    events=[]
    with patch.object(writer,'_graph_driver',side_effect=lambda:graph_factory(events,receipt)), \
         patch.object(writer,'_postgres_engine',side_effect=lambda:postgres_factory(events)), \
         patch.object(reconciliation_queue,'enqueue_reconciliation',return_value={'r':7}) as enqueue:
        result=writer.relay_queue('operation',expected_journal_sha256=receipt['journal_sha256'])
    assert result['forward_generations']=={'r':7}
    assert events==['graph_open','graph_closed','pg_open','pg_committed','pg_closed','graph_open','graph_ack','graph_closed']
    assert enqueue.call_args.args[1:]==('operation:forward',['r'])


def test_relay_retry_after_ack_disconnect_uses_the_identical_idempotency_key():
    from recipe_wrangler.matching import reconciliation_queue
    _,receipt,*_=fixtures.apply_scenario()
    events=[];opens=0
    def graph():
        nonlocal opens
        opens+=1
        return graph_factory(events,receipt,fail_ack=opens==2)
    with patch.object(writer,'_graph_driver',side_effect=graph), \
         patch.object(writer,'_postgres_engine',side_effect=lambda:postgres_factory(events)), \
         patch.object(reconciliation_queue,'enqueue_reconciliation',return_value={'r':7}) as enqueue:
        with pytest.raises(ConnectionError):writer.relay_queue('operation')
        assert writer.relay_queue('operation')['forward_generations']=={'r':7}
    assert enqueue.call_count==2
    assert all(call.args[1:]==('operation:forward',['r']) for call in enqueue.call_args_list)


def test_failed_pg_handoff_leaves_graph_pending_without_acknowledgement():
    from recipe_wrangler.matching import reconciliation_queue
    _,receipt,*_=fixtures.apply_scenario()
    events=[]
    with patch.object(writer,'_graph_driver',side_effect=lambda:graph_factory(events,receipt)), \
         patch.object(writer,'_postgres_engine',side_effect=lambda:postgres_factory(events)), \
         patch.object(reconciliation_queue,'enqueue_reconciliation',side_effect=ConnectionError('disconnected')):
        with pytest.raises(ConnectionError):writer.relay_queue('operation')
    assert events==['graph_open','graph_closed','pg_open','pg_rollback','pg_closed']


def test_relay_rejects_wrong_reviewed_journal_before_pg_handoff():
    _,receipt,*_=fixtures.apply_scenario()
    with patch.object(writer,'_graph_driver',side_effect=lambda:graph_factory([],receipt)),patch.object(writer,'_postgres_engine') as pg:
        with pytest.raises(ValueError,match='reviewed operation'):
            writer.relay_queue('operation',expected_journal_sha256='different')
    pg.assert_not_called()


@pytest.mark.parametrize('extra',[[],['--controlled-window','--expected-journal-sha256','wrong']])
def test_cli_apply_refuses_unreviewed_hash_or_window_before_backend_access(tmp_path,monkeypatch,extra):
    journal,*_=fixtures.scenario()
    path=tmp_path/'journal.json';path.write_text(json.dumps(journal))
    monkeypatch.setattr('sys.argv',['repair','--mode','apply','--journal',str(path),'--out',str(tmp_path/'out'),*extra])
    with patch.object(writer,'_graph_driver') as graph,pytest.raises(SystemExit) as failure:
        writer.main()
    assert failure.value.code==2
    graph.assert_not_called()
    assert not (tmp_path/'out').exists()


def test_source_hash_guards_are_sensitive_to_types_and_array_order():
    from neo4j.time import Date
    first={'r':{'properties':{'source_order':['z','a'],'date':Date(2026,10,31)}}}
    ordered={'r':{'properties':{'source_order':['a','z'],'date':Date(2026,10,31)}}}
    string={'r':{'properties':{'source_order':['z','a'],'date':'2026-10-31'}}}
    assert writer.recipe_input_hashes(first)!=writer.recipe_input_hashes(ordered)
    assert writer.recipe_input_hashes(first)!=writer.recipe_input_hashes(string)
