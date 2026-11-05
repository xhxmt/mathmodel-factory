"""Persisted cancellation ownership: in-memory decisions and owned children only."""
import subprocess
import sys

import pytest

from factory_core.adapters.infrastructure import process as processes
from factory_core.domain import InvalidTransition, WorkflowStatus
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore


@pytest.mark.parametrize('operation', ['pause', 'kill'])
@pytest.mark.parametrize('fault', ['identity_missing', 'identity_mismatch', 'lease_mismatch', 'no_owner_event'])
def test_unverified_owner_never_signals_or_discovers_processes(tmp_path, monkeypatch, operation, fault):
    """A live pid without a matching launch identity is never signalled.

    identity_mismatch: the recorded owner exited and its pid was reused, so
    the project is stopped without signalling anyone.  Otherwise the live
    process cannot be told apart from the runner: the operation is refused
    and nothing is committed, rather than recording a stop the process tree
    contradicts.
    """
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
    if fault == 'identity_mismatch':
        result = getattr(service, operation)('demo', expected_revision=state.revision)
        assert result.status is (WorkflowStatus.PAUSED if operation == 'pause' else WorkflowStatus.KILLED)
        assert calls == []
        assert not any(e.type == 'RUNNER_EXIT_UNVERIFIED' for e in store.events())
        return
    events_before = len(store.events())
    with pytest.raises(InvalidTransition, match='STOP_IDENTITY_UNAVAILABLE'):
        getattr(service, operation)('demo', expected_revision=state.revision)
    assert calls == []
    assert store.load().status is WorkflowStatus.RUNNING
    assert len(store.events()) == events_before


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


@pytest.mark.parametrize('operation', ['pause', 'kill'])
def test_a_broken_event_chain_does_not_prevent_stopping_the_runner(tmp_path, operation):
    """run2: from rev 437 the chain no longer replayed, and pause stopped nothing.

    The identity token is checked against the live process, which does not
    depend on the store; the runner is signalled and verified gone.
    """
    import sqlite3

    service = FactoryService(tmp_path)
    state, _ = service.create_project('demo', 'An owned worker in a tampered store.', start=False)
    store = SQLiteStateStore(tmp_path / 'ongoing/demo')
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    identity = processes._process_identity(child.pid)
    try:
        state = store.transition(expected_revision=state.revision, event_type='WORKER_LAUNCHED',
            changes=dict(status=WorkflowStatus.RUNNING, runner_pid=child.pid, runner_lease_id=f'launch:{child.pid}'),
            payload=dict(worker_pid=child.pid, worker_identity=identity, lease_id=f'launch:{child.pid}'))
        connection = sqlite3.connect(tmp_path / 'ongoing/demo/.factory/state.db')
        connection.execute('UPDATE project_state SET attempt = attempt + 5')  # an un-evented edit
        connection.commit()
        connection.close()
        assert store.status_snapshot()['aggregate_valid'] is False

        getattr(service, operation)('demo', expected_revision=state.revision)

        assert processes._process_identity(child.pid) is None
        child.wait(timeout=3)
        assert not any(e.type == 'RUNNER_EXIT_UNVERIFIED' for e in store.events())
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)
