"""Bounded normal-entry lifecycle checks; no model/provider/solver work."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from factory_core.adapters.infrastructure.process import _process_identity
from factory_core.domain import WorkflowStatus
from factory_core.service import FactoryService, WorkerLauncher
from factory_core.storage import SQLiteStateStore
from factory_core import persistent_launcher


def test_normal_service_entry_records_real_cli_initialization_failure(tmp_path):
    project = tmp_path / 'ongoing/demo'
    project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    # A runtime configuration that the actual CLI engine cannot initialize.
    store.initialize(project_id='demo', project_type='modeling', runtime_generation='unavailable-runtime')
    service = FactoryService(tmp_path)
    assert type(service.worker_launcher) is WorkerLauncher
    before = time.monotonic()
    with pytest.raises(RuntimeError, match='before initialization'):
        service.start('demo')
    assert time.monotonic() - before < 5
    state = store.load()
    assert state.status is WorkflowStatus.FAILED and state.runner_pid is None
    failure = store.events()[-1]
    assert failure.type == 'WORKER_START_FAILED'
    assert failure.payload['error_class'] == 'WORKER_INITIALIZATION_FAILED'
    assert failure.payload['process_tree_exited'] is True
    status = service.status('demo')
    assert status['workflow_error'] == 'WORKER_INITIALIZATION_FAILED'
    assert not list((project / '.factory').glob('worker_ready_*'))
    assert not any('CONTINUATION' in e.type or 'REPAIR_RETRY' in e.type for e in store.events())


def test_normal_launcher_times_out_child_that_never_acknowledges(tmp_path):
    code = tmp_path / 'controlled-code'
    (code / 'factory_core').mkdir(parents=True)
    (code / 'factory_core/__init__.py').touch()
    (code / 'factory_core/cli.py').write_text('import time; time.sleep(30)\n')
    project = tmp_path / 'ongoing/demo'; project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    store.initialize(project_id='demo', project_type='modeling')
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
    try:
        deadline = time.monotonic() + 3
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_file.exists()
        child = int(child_file.read_text())
        FactoryService._terminate_runner(process.pid)
        assert _process_identity(process.pid) is None
        assert _process_identity(child) is None
    finally:
        if process.poll() is None:
            FactoryService._terminate_runner(process.pid)
        process.wait(timeout=3)


def test_persistent_monitor_initialization_exception_is_durable(tmp_path, monkeypatch):
    root = tmp_path / 'control'; root.mkdir()
    (root / 'request.json').write_text('{ invalid json')
    descriptor = os.open(root / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        subprocess.run([sys.executable, '-m', 'factory_core.persistent_launcher', 'monitor',
            str(root), '--lock-fd', str(descriptor)], pass_fds=(descriptor,),
            check=True, timeout=5, cwd=Path(__file__).resolve().parents[1])
    finally:
        os.close(descriptor)
    value = persistent_launcher.status(root)
    assert value['status'] == 'FAILED' and value['ready'] is False
    assert value['error_class'] == 'JSONDecodeError'
    assert value['process_tree_exited'] is True


def test_persistent_pre_ready_process_exit_is_reported(tmp_path):
    root = tmp_path / 'control'
    result = persistent_launcher.start(root, tmp_path / 'missing-cwd',
        [sys.executable, '-c', 'pass'], key='missing-cwd', ready_timeout=2)
    assert result['status'] == 'EXITED'
    assert result['ready'] is False and result['exit_code'] == 127
    assert result['process_tree_exited'] is True


def test_missing_monitor_is_interrupted_and_never_verified(tmp_path):
    (tmp_path / 'status.json').write_text(json.dumps({'status': 'STARTING', 'ready': False,
        'parent_pid': os.getpid(), 'parent_identity': _process_identity(os.getpid())}))
    (tmp_path / 'monitor.json').write_text(json.dumps({'monitor_pid': 999999999,
        'monitor_identity': 'missing'}))
    value = persistent_launcher.status(tmp_path)
    assert value['status'] == 'INTERRUPTED'
    assert value['process_tree_exited'] is False


def test_cancel_failure_persists_unverified_interruption(tmp_path, monkeypatch):
    service = FactoryService(tmp_path)
    state, _ = service.create_project('demo', 'controlled fixture')
    def failed_stop(_pid):
        raise RuntimeError('controlled exit verification failure')
    monkeypatch.setattr(service, '_terminate_runner', failed_stop)
    with pytest.raises(RuntimeError, match='controlled exit verification failure'):
        service.kill('demo', expected_revision=state.revision)
    store = SQLiteStateStore(tmp_path / 'ongoing/demo')
    assert store.load().status is WorkflowStatus.INTERRUPTED
    assert store.events()[-1].payload['process_tree_exited'] is False
    assert service.status('demo')['workflow_error'] == 'RUNNER_EXIT_UNVERIFIED'
