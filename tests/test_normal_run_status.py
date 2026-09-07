"""Normal status crosses SQLite, projection publication and the public Web schema."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from factory_core.domain import WorkflowStatus
from factory_core.projections import (
    AUDIT_FIELDS, authoritative_status, read_compatibility_projection,
    write_compatibility_projections,
)
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore
from web.backend.diagnostics_service import build_project_diagnostics
from web.backend.project_api import _runtime_to_project_status
from web.backend.state_store import read_runtime_status


def test_failure_and_normal_recovery_match_all_status_surfaces(tmp_path):
    project = tmp_path / 'ongoing/demo'
    project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    initial = store.initialize(project_id='demo', project_type='modeling')
    failed = store.transition(expected_revision=initial.revision, event_type='STEP_FAILED',
        changes={'status': WorkflowStatus.FAILED, 'active_step': 13},
        payload={'error_class': 'TRANSIENT_JUDGE_PROVENANCE'})
    (project / 'judge_outputs').mkdir()
    (project / 'judge_outputs/aggregate.json').write_text('{"verdict":"PASS","overall_score":99,"score_available":true}')
    for state in (failed, None):
        if state is None:
            current = store.load()
            store.transition(expected_revision=current.revision, event_type='WORKFLOW_RESUMED',
                changes={'status': WorkflowStatus.READY, 'active_step': None})
        cli = FactoryService(tmp_path).status('demo')
        web = _runtime_to_project_status(read_runtime_status(project, 'demo'), project).model_dump()
        diag = build_project_diagnostics(project, 'demo', is_running=False,
            consultation_pending=False, consultation_gate=None)['status']
        files = read_compatibility_projection(project)
        for key in AUDIT_FIELDS:
            assert cli[key] == web[key] == diag[key] == files[key], key
        assert cli['workflow_error'] == ('TRANSIENT_JUDGE_PROVENANCE' if state else None)
        assert cli['evidence_validity'] == 'INVALID'
        assert cli['evidence_errors']
        assert cli['diagnostic_score'] is None and cli['official_score'] is None
        assert cli['scientific_verdict'] == 'UNAVAILABLE'


def test_snapshot_remains_one_revision_during_concurrent_write(tmp_path, monkeypatch):
    store = SQLiteStateStore(tmp_path)
    old = store.initialize(project_id='demo', project_type='modeling')
    reached, release = threading.Event(), threading.Event()
    original = store._state_from_row
    def paused(row):
        value = original(row)
        reached.set()
        assert release.wait(5)
        return value
    monkeypatch.setattr(store, '_state_from_row', paused)
    with ThreadPoolExecutor() as pool:
        reading = pool.submit(store.status_snapshot)
        assert reached.wait(5)
        other = SQLiteStateStore(tmp_path)
        new = other.transition(expected_revision=old.revision, event_type='STEP_FAILED',
            changes={'status': WorkflowStatus.FAILED}, payload={'error_class': 'CONTROLLED'})
        release.set()
        snapshot = reading.result()
    assert snapshot['state'].revision == old.revision
    assert snapshot['events'][-1].revision == old.revision
    assert snapshot['aggregate_valid']
    assert other.status_snapshot()['state'].revision == new.revision


def test_concurrent_publish_reloads_after_lock_and_rejects_partial_files(tmp_path, monkeypatch):
    from factory_core import projections
    store = SQLiteStateStore(tmp_path)
    old = store.initialize(project_id='demo', project_type='modeling')
    (tmp_path / 'checkpoint.md').write_text('Last completed step: -1\n')
    write_compatibility_projections(tmp_path, old)
    first = store.transition(expected_revision=old.revision, event_type='STEP_STARTED',
        changes={'status': WorkflowStatus.RUNNING, 'last_completed_step': 1})
    reached, release = threading.Event(), threading.Event()
    original = projections._heartbeat
    def paused(project, state):
        if state.revision == first.revision:
            reached.set()
            assert release.wait(5)
        original(project, state)
    monkeypatch.setattr(projections, '_heartbeat', paused)
    with ThreadPoolExecutor() as pool:
        writer = pool.submit(write_compatibility_projections, tmp_path, first)
        assert reached.wait(5)
        assert read_compatibility_projection(tmp_path) is None
        new = store.transition(expected_revision=first.revision, event_type='STEP_FAILED',
            changes={'status': WorkflowStatus.FAILED, 'last_completed_step': 2})
        stale_writer = pool.submit(write_compatibility_projections, tmp_path, old)
        release.set()
        writer.result()
        stale_writer.result()
    snapshot = read_compatibility_projection(tmp_path)
    assert snapshot['revision'] == new.revision
    assert snapshot['state'] == 'failed'
    assert 'Last completed step: 2' in (tmp_path / 'checkpoint.md').read_text()


def test_precheck_has_its_own_bound_verdict_and_never_a_score(tmp_path, monkeypatch):
    from tests.test_normal_run_api_batch import native_api
    from factory_core.steps.specialized import JudgeStep
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id=tmp_path.name, project_type='modeling')
    run, _, _ = native_api(tmp_path, monkeypatch)
    result = run('math', 13)
    assert result.returncode == 0
    JudgeStep._write_precheck(tmp_path, 'PRECHECK_PASS', 'PASS', result.metadata)
    status = authoritative_status(tmp_path)
    assert status['evidence_validity'] == 'VALID', status
    assert status['scientific_verdict'] == 'PRECHECK_PASS'
    assert status['review_mode'] == 'math_only'
    assert not status['score_available'] and not status['delivery_allowed']
    store.transition(expected_revision=state.revision, event_type='STEP_STARTED', event_step=13,
                     changes={'status': WorkflowStatus.RUNNING, 'active_step': 13})
    assert authoritative_status(tmp_path)['scientific_verdict'] == 'UNAVAILABLE'
    # A stale result cannot be used for a new attempt, even with unchanged files.
    p = tmp_path / 'judge_outputs/precheck.json'
    value = json.loads(p.read_text()); value['verdict'] = 'PASS'; p.write_text(json.dumps(value))
    assert authoritative_status(tmp_path)['evidence_validity'] == 'INVALID'
