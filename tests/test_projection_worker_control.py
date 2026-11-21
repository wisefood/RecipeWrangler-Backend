"""A controlled API can serve reads without draining the projection queue."""
from unittest.mock import Mock

import pytest

from recipe_wrangler.matching import projection_worker as worker


def test_disabled_start_never_launches_background_work(monkeypatch):
    monkeypatch.setenv('MATCHING_PROJECTION_WORKER_ENABLED', '0')
    thread = Mock()
    monkeypatch.setattr(worker.threading, 'Thread', thread)
    monkeypatch.setattr(worker, '_thread', None)
    worker.start()
    thread.assert_not_called()
    assert worker.background_enabled() is False


def test_default_still_starts_worker(monkeypatch):
    monkeypatch.delenv('MATCHING_PROJECTION_WORKER_ENABLED', raising=False)
    thread = Mock()
    monkeypatch.setattr(worker.threading, 'Thread', thread)
    monkeypatch.setattr(worker, '_thread', None)
    worker.start()
    thread.return_value.start.assert_called_once()
    assert worker.background_enabled() is True


@pytest.mark.parametrize('value', ['', 'false', 'yes', '2'])
def test_invalid_setting_refuses_start(monkeypatch, value):
    monkeypatch.setenv('MATCHING_PROJECTION_WORKER_ENABLED', value)
    thread = Mock()
    monkeypatch.setattr(worker.threading, 'Thread', thread)
    with pytest.raises(ValueError, match='must be 0 or 1'):
        worker.start()
    thread.assert_not_called()
