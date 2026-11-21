"""A schema-ready index is insufficient when the running worker is still old."""
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
import pytest
from recipe_wrangler.utils.consumer_suitability import composition_policy_versions
from scripts.neo4j import reconcile_allergen_journal as writer


def prerequisites(*, drift=False, health=None):
    settings=SimpleNamespace(catalog_recipes_alias='recipes',elastic_index='search_recipes')
    properties={'allergen_uncertainty':{'type':'boolean'},'allergen_assessment':{'type':'object','enabled':False},
                'allergen_evidence':{'type':'nested'},'consumer_suitability':{'type':'nested'}}
    client=Mock()
    client._request.side_effect=lambda method,path,**kwargs:{'other' if drift and path.startswith('search_recipes') else 'reviewed':{'mappings':{'properties':properties}}}
    engine=MagicMock();connection=engine.connect.return_value.__enter__.return_value
    pk=Mock();pk.scalar.return_value=True
    connection.execute.side_effect=[Mock(),Mock(),[('operation_id','text'),('recipe_ids','jsonb'),('generations','jsonb')],pk]
    graph=MagicMock();graph.session.return_value.__enter__.return_value.execute_read.return_value=[{'type':'UNIQUENESS'}]
    from recipe_wrangler.matching.projection_worker import PROJECTION_PIPELINE_VERSION
    response=Mock()
    response.json.return_value=health or {'mode':'new','runtime_mode':'new',
        'composition_policy_versions':composition_policy_versions(),'projection_pipeline_version':PROJECTION_PIPELINE_VERSION}
    session=Mock();session.get.return_value=response
    with patch('recipe_wrangler.api.config.get_settings',return_value=settings), \
         patch('recipe_wrangler.catalog.elastic.get_catalog_client',return_value=client), \
         patch.object(writer,'_postgres_engine',return_value=engine), \
         patch.object(writer,'_graph_driver',return_value=graph), \
         patch('recipe_wrangler.utils.http_pool.get_http_session',return_value=session):
        result=writer.check_prerequisites('reviewed',api_base='http://offline')
    assert all(call.args[0]=='GET' for call in client._request.call_args_list)
    session.get.assert_called_once_with('http://offline/health/matching',timeout=(5,60))
    return result


def test_readiness_requires_same_reviewed_alias_target_and_runtime_versions():
    result=prerequisites()
    assert result['ready'] is True and result['api_policy_ready'] is True
    assert prerequisites(drift=True)['ready'] is False


@pytest.mark.parametrize('health',[
    {'mode':'new','runtime_mode':'new'},
    {'mode':'legacy','runtime_mode':'legacy'},
    {'mode':'new','runtime_mode':'new','composition_policy_versions':{'vegan':'vegan-vegetarian-v1'},
     'projection_pipeline_version':'old'},
])
def test_undeployed_or_incompatible_runtime_never_passes_schema_only_readiness(health):
    result=prerequisites(health=health)
    assert result['mapping_ready'] and result['queue_receipt_schema_ready'] and result['graph_receipt_constraint_ready']
    assert result['api_policy_ready'] is False and result['ready'] is False


def test_health_exposes_policies_and_pipeline_with_one_bounded_read_connection(monkeypatch):
    from recipe_wrangler.api.routers.health import matching_health
    from recipe_wrangler.utils import nutrition_postgres
    monkeypatch.setenv('NUTRITION_MATCHER_MODE','new')
    monkeypatch.setenv('MATCHING_PROJECTION_WORKER_ENABLED','0')
    engine=MagicMock();connection=engine.connect.return_value.__enter__.return_value
    deployment=Mock();deployment.mappings.return_value.one.return_value={'mode':'new','release_id':'release'}
    pending=Mock();pending.scalar.return_value=3
    connection.execute.side_effect=[Mock(),Mock(),deployment,pending]
    with patch.object(nutrition_postgres,'get_engine',return_value=engine):
        result=matching_health()
    assert result['composition_policy_versions']==composition_policy_versions()
    assert result['projection_pipeline_version']=='allergen-reconciliation-v1'
    assert result['pending_projections']==3
    assert result['background_projection_enabled'] is False
    assert engine.connect.call_count==1
    assert any("statement_timeout='60s'" in str(call.args[0]) for call in connection.execute.call_args_list)
