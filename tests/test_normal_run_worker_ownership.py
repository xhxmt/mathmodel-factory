"""Persisted cancellation ownership: in-memory decisions and owned children only."""
import subprocess
import sys

import pytest

from factory_core.adapters.infrastructure import process as processes
from factory_core.domain import WorkflowStatus
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore


@pytest.mark.parametrize('operation', ['pause', 'kill'])
@pytest.mark.parametrize('fault', ['identity_missing', 'identity_mismatch', 'lease_mismatch', 'no_owner_event'])
def test_unverified_owner_never_signals_or_discovers_processes(tmp_path, monkeypatch, operation, fault):
    service = FactoryService(tmp_path)
    state, _ = service.create_project('demo', 'A static ownership fixture.', start=False)
    store = SQLiteStateStore(tmp_path / 'ongoing/demo')
    payload = dict(worker_pid=123456789, lease_id='lease-a', worker_identity='recorded-start')
    if fault == 'identity_missing':
        payload.pop('worker_identity')
    if fault == 'lease_mismatch':
        payload['lease_id'] = 'older-lease'
    state = store.transition(expected_revision=state.revision,
        event_type='RUN_STARTED' if fault != 'no_owner_event' else 'CONTROLLED_STATE',
        changes=dict(status=WorkflowStatus.RUNNING, runner_pid=123456789, runner_lease_id='lease-a'),
        payload=payload)
    calls = []
    monkeypatch.setattr(processes, '_process_identity', lambda pid: 'different-start')
    monkeypatch.setattr(processes, '_descendants', lambda pid: calls.append(('descendants', pid)))
    monkeypatch.setattr('factory_core.service.os.kill', lambda *args: calls.append(('signal', args)))
    with pytest.raises(RuntimeError, match='runner'):
        getattr(service, operation)('demo', expected_revision=state.revision)
    assert calls == []
    assert store.load().status is WorkflowStatus.INTERRUPTED
    assert store.events()[-1].type == 'RUNNER_EXIT_UNVERIFIED'
    assert store.events()[-1].payload['process_tree_exited'] is False


@pytest.mark.parametrize('operation', ['pause', 'kill'])
def test_persisted_owner_allows_cancellation_of_this_tests_child(tmp_path, operation):
    service = FactoryService(tmp_path)
    state, _ = service.create_project('demo', 'An owned idle worker.', start=False)
    store = SQLiteStateStore(tmp_path / 'ongoing/demo')
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    identity = processes._process_identity(child.pid)
    try:
        state = store.transition(expected_revision=state.revision, event_type='WORKER_LAUNCHED',
            changes=dict(status=WorkflowStatus.RUNNING, runner_pid=child.pid, runner_lease_id=f'launch:{child.pid}'),
            payload=dict(worker_pid=child.pid, worker_identity=identity, lease_id=f'launch:{child.pid}'))
        result = getattr(service, operation)('demo', expected_revision=state.revision)
        assert result.status is (WorkflowStatus.PAUSED if operation == 'pause' else WorkflowStatus.KILLED)
        assert processes._process_identity(child.pid) is None
        child.wait(timeout=3)
        assert not any(e.type == 'RUNNER_EXIT_UNVERIFIED' for e in store.events())
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


def test_foreground_engine_persists_current_identity_with_its_lease(tmp_path):
    import os
    service = FactoryService(tmp_path)
    service.create_project('demo', 'No step execution.', start=False)
    service.run('demo', max_steps=0)
    store = SQLiteStateStore(tmp_path / 'ongoing/demo')
    event = next(e for e in store.events() if e.type == 'RUN_STARTED')
    assert event.payload['worker_pid'] == os.getpid()
    assert event.payload['worker_identity'] == processes._process_identity(os.getpid())
    assert event.payload['lease_id']
