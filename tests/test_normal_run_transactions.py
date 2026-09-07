"""State transitions keep their checkpoint, dirty evidence and event atomic."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from factory_core import dirty_rebase
from factory_core.current_dirty import classifier_contract_sha256
from factory_core.dirty import capture_artifact_manifest, manifest_fingerprint
from factory_core.domain import RevisionConflict, WorkflowStatus
from factory_core.prompt_receipts import ensure_prompt_receipt_schema
from factory_core.storage import SQLiteStateStore


TABLES = (
    "project_state", "stage_cursor_inputs", "stage_checkpoints",
    "stage_checkpoint_history", "dirty_flags", "dirty_causes",
    "dirty_classifier_rebases", "dirty_flag_clear_receipts", "events",
)


def snapshot(store):
    with sqlite3.connect(store.path) as connection:
        connection.execute("BEGIN")
        return {table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in TABLES}


def pending_completion(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="atomic", project_type="modeling")
    state = store.transition(
        expected_revision=state.revision, event_type="ARTIFACT_CHANGED",
        dirty_changes=[dict(
            flag="MODEL_DIRTY", owner_stage=8,
            cause_artifact="method_fit_suggestions.json",
            baseline_fingerprint="a" * 64, current_fingerprint="b" * 64,
            classifier_contract_sha256="prior-classifier",
        )],
    )
    output = manifest_fingerprint(capture_artifact_manifest(tmp_path))
    receipt = dict(schema_version="factory-stage-checkpoint-v1", status="PASS",
                   stage=1, output_fingerprint=output,
                   classifier_contract_sha256=classifier_contract_sha256())
    completion = dict(
        expected_revision=state.revision, event_type="STAGE_SUBTASK_SUCCEEDED",
        changes=dict(status=WorkflowStatus.READY, last_completed_step=0),
        subtask_baseline=dict(stage_id=1, subtask="problem_setup", source_step_id=0,
                              input_fingerprint=output, manifest={}),
        stage_checkpoint=dict(stage_id=1, subtask="problem_setup", source_step_id=0,
                              completed_step_id=0, input_fingerprint=output,
                              output_fingerprint=output, receipt=receipt),
        clear_dirty_stage=dict(owner_stage=1, cleared_fingerprint=output,
                               classifier_contract_sha256=classifier_contract_sha256(),
                               success_receipt=receipt),
    )
    return store, completion


@pytest.mark.parametrize("boundary", ["after_rebase", "before_event"])
def test_completion_rolls_back_every_business_record(tmp_path, monkeypatch, boundary):
    store, completion = pending_completion(tmp_path)
    before = snapshot(store)
    target, name = ((dirty_rebase, "rebase_dirty_classifier_state")
                    if boundary == "after_rebase" else (store, "_versioned_event_payload"))
    original = getattr(target, name)

    def fail(connection, *args, **kwargs):
        original(connection, *args, **kwargs)
        assert connection.in_transaction
        assert snapshot(store) == before
        raise RuntimeError("controlled persistence failure")

    monkeypatch.setattr(target, name, fail)
    with pytest.raises(RuntimeError, match="controlled persistence failure"):
        store.transition(**completion)
    assert snapshot(store) == before


def test_concurrent_completion_is_one_revision_with_no_partial_read(tmp_path, monkeypatch):
    store, completion = pending_completion(tmp_path)
    before = snapshot(store)
    reached, release, contender_started = threading.Event(), threading.Event(), threading.Event()
    original = store._versioned_event_payload

    def before_event(connection, **kwargs):
        assert connection.in_transaction
        reached.set()
        assert release.wait(5)
        return original(connection, **kwargs)

    monkeypatch.setattr(store, "_versioned_event_payload", before_event)
    other = SQLiteStateStore(tmp_path)

    def contend():
        contender_started.set()
        return other.transition(**completion)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.transition, **completion)
        try:
            assert reached.wait(5)
            second = pool.submit(contend)
            assert contender_started.wait(5)
            assert snapshot(store) == before
        finally:
            release.set()
        completed = first.result(timeout=5)
        with pytest.raises(RevisionConflict):
            second.result(timeout=5)
    assert completed.revision == completion["expected_revision"] + 1
    assert store.dirty_flags() == []
    assert store.stage_cursor_input()["selected_revision"] == completed.revision
    assert store.stage_checkpoints()[-1]["completed_revision"] == completed.revision
    assert store.dirty_clear_receipts()[-1]["revision"] == completed.revision
    assert store.events()[-1].revision == completed.revision
    after = snapshot(store)
    assert len(after["dirty_classifier_rebases"]) == len(before["dirty_classifier_rebases"]) + 1
    assert len(after["events"]) == len(before["events"]) + 1


@pytest.mark.parametrize("ensure", [dirty_rebase.ensure_dirty_rebase_schema,
                                    ensure_prompt_receipt_schema])
def test_additive_schema_statements_preserve_transaction(ensure):
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN")
        connection.execute("CREATE TABLE sentinel (value INTEGER)")
        connection.execute("INSERT INTO sentinel VALUES (1)")
        ensure(connection)
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()
