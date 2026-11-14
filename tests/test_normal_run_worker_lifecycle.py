"""Bounded normal-entry lifecycle checks; no model/provider/solver work."""
import os
import subprocess
import sys
import time

import pytest

from factory_core.adapters.infrastructure.process import _process_identity
from factory_core.domain import WorkflowStatus
from factory_core.service import FactoryService, WorkerLauncher
from factory_core.storage import SQLiteStateStore


def test_normal_service_rejects_unavailable_runtime_before_launch(tmp_path):
    project = tmp_path / 'ongoing/demo'
    project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    before = store.initialize(project_id='demo', project_type='modeling', runtime_generation='unavailable-runtime', scheduler_generation='step_v2')
    events = store.events()
    with pytest.raises(RuntimeError, match='NATIVE_WORKFLOW_REQUIRED'):
        FactoryService(tmp_path).start('demo')
    assert store.load() == before
    assert store.events() == events
    assert not list((project / '.factory').glob('worker_ready_*'))


def test_normal_launcher_times_out_child_that_never_acknowledges(tmp_path):
    code = tmp_path / 'controlled-code'
    (code / 'factory_core').mkdir(parents=True)
    (code / 'factory_core/__init__.py').touch()
    (code / 'factory_core/cli.py').write_text('import time; time.sleep(30)\n')
    project = tmp_path / 'ongoing/demo'; project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    store.initialize(project_id='demo', project_type='modeling', scheduler_generation='stage_v1')
    service = FactoryService(tmp_path, worker_launcher=WorkerLauncher(tmp_path, code, ready_timeout=0.2))
    with pytest.raises(TimeoutError, match='initialization timed out'):
        service.start('demo')
    launch = next(e for e in store.events() if e.type == 'WORKER_LAUNCHED')
    assert _process_identity(launch.payload['worker_pid']) is None
    assert store.load().status is WorkflowStatus.FAILED


def test_service_run_acknowledges_only_after_engine_initialization(tmp_path):
    service = FactoryService(tmp_path)
    state, _ = service.create_project('demo', 'controlled fixture', start=False)
    ready = tmp_path / 'ready'
    # max_steps=0 executes no modeling step, but exercises real initialization.
    result = service.run('demo', max_steps=0, ready_file=ready)
    assert ready.with_suffix('.ack').read_text().strip() == str(os.getpid())
    assert result.status is WorkflowStatus.READY
    events = SQLiteStateStore(tmp_path / 'ongoing/demo').events()
    assert not any('CONTINUATION' in e.type or 'REPAIR_RETRY' in e.type for e in events)


def test_normal_cancel_verifies_leader_and_new_session_child(tmp_path):
    child_file = tmp_path / 'child.pid'
    command = [sys.executable, '-c',
        'import subprocess,sys,time,pathlib; '
        'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],start_new_session=True); '
        f'pathlib.Path({str(child_file)!r}).write_text(str(p.pid)); time.sleep(30)']
    process = subprocess.Popen(command, start_new_session=True)
    identity = _process_identity(process.pid)
    try:
        deadline = time.monotonic() + 3
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_file.exists()
        child = int(child_file.read_text())
        FactoryService._terminate_runner(process.pid, identity)
        assert _process_identity(process.pid) is None
        assert _process_identity(child) is None
    finally:
        if process.poll() is None:
            FactoryService._terminate_runner(process.pid, identity)
        process.wait(timeout=3)


def test_cancel_failure_persists_unverified_interruption(tmp_path, monkeypatch):
    service = FactoryService(tmp_path)
    state, _ = service.create_project('demo', 'controlled fixture')
    def failed_stop(_pid, _identity):
        raise RuntimeError('controlled exit verification failure')
    monkeypatch.setattr(service, '_terminate_runner', failed_stop)
    # A live, verified runner whose termination then cannot be confirmed.
    monkeypatch.setattr(service, '_runner_stop_identity', lambda _snapshot: 'controlled-start')
    with pytest.raises(RuntimeError, match='controlled exit verification failure'):
        service.kill('demo', expected_revision=state.revision)
    store = SQLiteStateStore(tmp_path / 'ongoing/demo')
    assert store.load().status is WorkflowStatus.INTERRUPTED
    assert store.events()[-1].payload['process_tree_exited'] is False
    assert service.status('demo')['workflow_error'] == 'RUNNER_EXIT_UNVERIFIED'
