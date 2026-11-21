"""Real cache invalidation and worker ordering against controlled services."""
from unittest.mock import MagicMock, patch

import pytest

from recipe_wrangler.utils import recipe_cache


class Cache:
    def __init__(self):
        self.values = {'recipe:r':{}, 'recipe:r:detail:eu:v3':{}, 'recipe:r:card:v3':{}, 'recipe:other':{}}
        self.fail = False

    def scan_iter(self, **kwargs):
        assert kwargs['match'] == 'recipe:r:*'
        if self.fail:
            raise ConnectionError('controlled cache outage')
        return iter([key for key in self.values if key.startswith('recipe:r:')])

    def delete(self, *keys):
        if self.fail:
            raise ConnectionError('controlled cache outage')
        for key in keys:
            self.values.pop(key, None)


def test_strict_failure_retries_and_then_removes_base_and_all_variants(monkeypatch):
    cache = Cache()
    monkeypatch.setattr(recipe_cache, '_is_enabled', lambda:True)
    monkeypatch.setattr(recipe_cache, '_get_client', lambda:cache)
    cache.fail = True
    with pytest.raises(ConnectionError):
        recipe_cache.cache_delete('r', strict=True)
    assert len(cache.values) == 4
    cache.fail = False
    recipe_cache.cache_delete('r', strict=True)
    assert cache.values == {'recipe:other':{}}


def test_non_strict_callers_keep_existing_outage_behavior(monkeypatch):
    cache = Cache(); cache.fail = True
    monkeypatch.setattr(recipe_cache, '_is_enabled', lambda:True)
    monkeypatch.setattr(recipe_cache, '_get_client', lambda:cache)
    recipe_cache.cache_delete('r')
    assert len(cache.values) == 4


def test_disabled_cache_does_no_work_even_in_strict_mode(monkeypatch):
    monkeypatch.setattr(recipe_cache, '_is_enabled', lambda:False)
    monkeypatch.setattr(recipe_cache, '_get_client', lambda:pytest.fail('Cache must remain disabled'))
    recipe_cache.cache_delete('r', strict=True)


def test_worker_keeps_cache_failure_pending_and_finishes_retry(monkeypatch):
    from recipe_wrangler.matching import projection_worker as worker, claims, graph_sync
    from recipe_wrangler.catalog import projection
    from recipe_wrangler.utils import dietary_classification

    cache = Cache(); cache.fail = True
    monkeypatch.setattr(recipe_cache, '_is_enabled', lambda:True)
    monkeypatch.setattr(recipe_cache, '_get_client', lambda:cache)
    engine = MagicMock()
    lock = engine.connect.return_value.__enter__.return_value
    lock.execute.return_value.scalar.return_value = True
    lock.execute.return_value.mappings.return_value = [{'recipe_id':'r', 'generation':7}]
    writes = engine.begin.return_value.__enter__.return_value
    monkeypatch.setattr(worker, 'get_engine', lambda:engine)
    monkeypatch.setattr(worker.store, 'deployment', lambda:{'mode':'new', 'release_id':'released'})
    graph_writes = []
    monkeypatch.setattr(worker, 'run_query', lambda query, params: graph_writes.append(query) or [{'revision':'rev'}])
    monkeypatch.setattr(graph_sync, 'bridge', lambda *args, **kwargs:{'projected_runs':1})
    monkeypatch.setattr(claims, 'refresh_claims', lambda ids:None)
    monkeypatch.setattr(dietary_classification, 'refresh_recipe_suitability', lambda ids:None)
    projected = []
    monkeypatch.setattr(projection, 'project', lambda recipe_id:projected.append(recipe_id))
    assert worker.drain_once(['r']) == 1
    statements = [str(call.args[0]) for call in writes.execute.call_args_list]
    assert any('last_error=:error' in sql for sql in statements)
    assert not any('completed_generation=GREATEST' in sql for sql in statements)
    assert not any('SET r.matching_projection_release' in query for query in graph_writes)
    assert len(cache.values) == 4
    writes.reset_mock(); graph_writes.clear()
    cache.fail = False
    assert worker.drain_once(['r']) == 1
    assert cache.values == {'recipe:other':{}}
    assert projected == ['r', 'r']
    completion = next(call for call in writes.execute.call_args_list if 'completed_generation=GREATEST' in str(call.args[0]))
    assert completion.args[1]['generation'] == 7
    assert any('SET r.matching_projection_release' in query for query in graph_writes)
