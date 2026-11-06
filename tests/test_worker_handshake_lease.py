"""The worker launch handshake must not break a concurrent state snapshot.

``worker_ready_*`` and its ``.ack`` live in ``.factory/`` beside ``state.db``.
``isolated_state_snapshot_ro`` fails closed when that directory changes while
it copies the database, so a handshake file created or removed outside the
commit lease made the worker's own first store access fail with
``NATIVE_WORKFLOW_REQUIRED`` (PR #45 CI, run 42163370537).
"""
import os
from pathlib import Path
import subprocess
import threading

from factory_core import projections, state_lease
from factory_core.native_boundary import require_native_project
from factory_core.service import FactoryService, wait_for_worker_ready
from factory_core.storage import SQLiteStateStore

SOURCE = Path(__file__).resolve().parents[1]


def _project(tmp_path):
    service = FactoryService(tmp_path)
    state, _ = service.create_project('demo', 'handshake fixture', start=False)
    return service, state, tmp_path / 'ongoing/demo'


def _holds_project_lease(project: Path) -> bool:
    metadata = project.resolve().lstat()
    return (metadata.st_dev, metadata.st_ino) in state_lease._leases_for_current_process()


def test_consuming_the_launch_permit_waits_for_a_running_snapshot(tmp_path, monkeypatch):
    _, _, project = _project(tmp_path)
    ready = project / '.factory/worker_ready_concurrent'
    ready.write_text('1\n')
    original = state_lease._read_snapshot_component
    consumer = []

    def snapshot_with_concurrent_consumer(*args, **kwargs):
        if not consumer:
            thread = threading.Thread(target=wait_for_worker_ready, args=(ready, project))
            consumer.append(thread)
            thread.start()
            # Unleased, the permit is gone (and .factory changed) well before this.
            thread.join(timeout=0.5)
        return original(*args, **kwargs)

    monkeypatch.setattr(state_lease, '_read_snapshot_component', snapshot_with_concurrent_consumer)
    require_native_project(project)  # raised "directory changed during snapshot"
    consumer[0].join(timeout=5)
    assert not consumer[0].is_alive()
    assert not ready.exists()


def test_worker_publishes_its_acknowledgment_under_the_lease(tmp_path, monkeypatch):
    service, _, project = _project(tmp_path)
    ready = project / '.factory/worker_ready_ack'
    original = projections._atomic_text
    seen = []

    def record(path, text):
        if path == ready.with_suffix('.ack'):
            seen.append(_holds_project_lease(project))
        return original(path, text)

    monkeypatch.setattr(projections, '_atomic_text', record)
    service.run('demo', max_steps=0, ready_file=ready)
    assert seen == [True]


def test_launcher_changes_handshake_files_only_under_the_lease(tmp_path, monkeypatch):
    service, state, project = _project(tmp_path)
    service.pause('demo', expected_revision=state.revision)
    # A restricted child environment: no provider credentials or config.
    monkeypatch.setattr(os, 'environ', {
        'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'FACTORY': str(tmp_path),
        'PYTHONPATH': str(SOURCE), 'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(tmp_path)})
    seen, children = [], []
    original_write, original_unlink = Path.write_text, Path.unlink
    original_spawn = subprocess.Popen

    def capture_child(*args, **kwargs):
        children.append(original_spawn(*args, **kwargs))
        return children[-1]

    def record_write(path, *args, **kwargs):
        if path.name.startswith('worker_ready_'):
            seen.append(('write', path.suffix, _holds_project_lease(project)))
            # Pause before the permit, so the real child initializes and stops
            # without running any step.
            engine = service.engine(project)
            engine.pause(expected_revision=engine.get_state().revision)
        return original_write(path, *args, **kwargs)

    def record_unlink(path, *args, **kwargs):
        if path.name.startswith('worker_ready_'):
            seen.append(('unlink', path.suffix, _holds_project_lease(project)))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', capture_child)
    monkeypatch.setattr(Path, 'write_text', record_write)
    monkeypatch.setattr(Path, 'unlink', record_unlink)
    try:
        service.start('demo')
        assert children[0].wait(timeout=10) == 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
    assert ('write', '', True) in seen
    assert ('unlink', '.ack', True) in seen
    assert all(held for *_, held in seen), seen
    assert not list((project / '.factory').glob('worker_ready_*'))
    assert not any(e.type in {'WORKER_START_FAILED', 'STEP_STARTED'}
                   for e in SQLiteStateStore(project).events())
