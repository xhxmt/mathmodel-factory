"""Independent normal CLI acceptance. Only paused, task-owned projects run.

These tests intentionally keep expected successful behavior; failures are audit
findings, not rewritten into tests that expect a candidate defect.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

from factory_core.projections import AUDIT_FIELDS
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore
from factory_core.domain import WorkflowStatus
from web.backend import project_actions
import pytest

SOURCE = Path(__file__).resolve().parents[1]


def paused_project(tmp_path):
    service = FactoryService(tmp_path)
    state, _ = service.create_project('audit_demo', 'A paused local control fixture.', start=False)
    service.pause('audit_demo', expected_revision=state.revision)
    project = tmp_path / 'ongoing/audit_demo'
    # Explicitly restrict the child environment; no provider credentials/config.
    environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                   'FACTORY': str(tmp_path), 'PYTHONPATH': str(SOURCE),
                   'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(tmp_path)}
    return service, project, environment


def test_cli_compat_paused_native_project_returns_normally(tmp_path):
    _, project, env = paused_project(tmp_path)
    result = subprocess.run([sys.executable, '-B', '-m', 'factory_core.cli',
                             'compat', str(project)], cwd=SOURCE, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['state'] == 'paused'


def test_real_cli_worker_acknowledges_successful_initialization(tmp_path):
    _, project, env = paused_project(tmp_path)
    ready = project / '.factory/worker_ready_independent'
    # A user pause may arrive before a newly launched worker initializes.
    # The real engine initializes and returns the paused state without a step.
    child = subprocess.Popen([sys.executable, '-B', '-m', 'factory_core.cli',
                              'worker', str(project), '--ready-file', str(ready)],
                             cwd=SOURCE, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    ready.write_text(str(child.pid) + '\n')
    try:
        stdout, stderr = child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
    assert child.returncode == 0, stderr
    assert json.loads(stdout)['state'] == 'paused'
    assert not ready.exists(), 'worker consumed the parent launch permit'
    assert not any(e.type == 'STEP_STARTED' for e in SQLiteStateStore(project).events())
    ack = ready.with_suffix('.ack')
    assert ack.is_file(), 'real CLI worker initialized but did not publish the required acknowledgment'
    assert ack.read_text().strip() == str(child.pid)


@pytest.mark.parametrize('entry', ['service_start', 'web_resume'])
def test_real_normal_entry_completes_initialization_when_pause_arrives(tmp_path, monkeypatch, entry):
    service, project, env = paused_project(tmp_path)
    monkeypatch.setattr(os, 'environ', env)
    children = []
    original_spawn = subprocess.Popen
    original_write = Path.write_text

    def capture_child(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        children.append(process)
        return process

    def pause_before_launch_permit(path, text, *args, **kwargs):
        if path.name.startswith('worker_ready_') and path.suffix != '.ack':
            # An ordinary pause at the parent/child handoff, before any step.
            # The child, CLI and engine remain the real implementations.
            engine = service.engine(project)
            engine.pause(expected_revision=engine.get_state().revision)
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', capture_child)
    monkeypatch.setattr(Path, 'write_text', pause_before_launch_permit)
    try:
        if entry == 'service_start':
            handle = service.start(project)
            assert handle.pid == children[0].pid
        else:
            result = project_actions.run_action(tmp_path, 'resume', project.name)
            assert result.ok, result.stderr
            assert json.loads(result.stdout)['worker_pid'] == children[0].pid
        assert children[0].wait(timeout=5) == 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
    store = SQLiteStateStore(project)
    assert store.load().status is WorkflowStatus.PAUSED
    assert not any(e.type in {'WORKER_START_FAILED', 'STEP_STARTED'} for e in store.events())
    launch = next(e for e in store.events() if e.type == 'WORKER_LAUNCHED')
    assert launch.payload['worker_identity']
    assert launch.payload['lease_id'] == f'launch:{children[0].pid}'
    assert not list((project / '.factory').glob('worker_ready_*'))
