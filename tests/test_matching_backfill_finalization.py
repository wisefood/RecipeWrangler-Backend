from unittest.mock import Mock,MagicMock
import pytest

from scripts.composition import finish_matching_backfill as workflow
from scripts.composition import bridge_matching_graph,audit_matching_rollout,report_live_matching_reference


@pytest.mark.parametrize('missing_graph,quantity_failures,graph_quantities,should_finish',[
    (0,0,0,True),(1,0,0,False),(0,1,0,False),(0,0,1,False)])
def test_finalization_checks_links_before_claiming_completion(monkeypatch,tmp_path,missing_graph,quantity_failures,graph_quantities,should_finish):
    monkeypatch.chdir(tmp_path)
    engine=MagicMock();connection=engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar.return_value=0
    monkeypatch.setattr(workflow,'get_engine',lambda:engine)
    bridge=Mock();monkeypatch.setattr(bridge_matching_graph,'bridge',bridge)
    result={'backfill_pending':0,'graph_runs_pending':missing_graph,'legacy_total_mismatches':0,
        'selected_occurrences_without_variant':0,'graph_concepts_missing_registry':[],
        'quantity_invariant_failures':quantity_failures,'graph_quantity_mismatches':graph_quantities}
    monkeypatch.setattr(audit_matching_rollout,'audit',lambda release:result)
    report=Mock(return_value=0);monkeypatch.setattr(report_live_matching_reference,'report',report)
    if should_finish:
        workflow.finish('release');report.assert_called_once_with('release')
    else:
        with pytest.raises(RuntimeError,match='graph_runs_pending|quantity_invariant_failures|graph_quantity_mismatches'):workflow.finish('release')
        report.assert_not_called()
    bridge.assert_called_once_with('release',apply=True)
    assert not any('matching_active_profiles' in str(c.args[0]) for c in connection.execute.call_args_list)
