import sqlite3
import threading

import pytest

from factory_core.domain import (
    SCHEMA_VERSION,
    RevisionConflict,
    RunnerLeaseLost,
    WorkflowStatus,
)
from factory_core.storage import SQLiteStateStore
from factory_core.projections import write_compatibility_projections


def test_initialize_writes_snapshot_and_first_event_atomically(tmp_path):
    store = SQLiteStateStore(tmp_path)

    state = store.initialize(project_id="demo", project_type="modeling")

    assert state.project_id == "demo"
    assert state.status is WorkflowStatus.READY
    assert state.last_completed_step == -1
    assert state.revision == 1
    events = store.events()
    assert [(event.revision, event.type) for event in events] == [(1, "PROJECT_CREATED")]


def test_transition_rejects_stale_revision_without_partial_event(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")
    updated = store.transition(
        expected_revision=state.revision,
        event_type="RUN_STARTED",
        changes={"status": WorkflowStatus.RUNNING, "active_step": 1},
    )

    with pytest.raises(RevisionConflict):
        store.transition(
            expected_revision=state.revision,
            event_type="PAUSED",
            changes={"status": WorkflowStatus.PAUSED},
        )

    assert store.load().revision == updated.revision
    assert [event.type for event in store.events()] == ["PROJECT_CREATED", "RUN_STARTED"]


def test_concurrent_transitions_allow_exactly_one_revision_commit(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")
    barrier = threading.Barrier(2)
    outcomes = []

    def transition(event_type):
        barrier.wait()
        try:
            updated = store.transition(
                expected_revision=state.revision,
                event_type=event_type,
                changes={"status": WorkflowStatus.PAUSED},
            )
            outcomes.append(("committed", updated.revision))
        except RevisionConflict:
            outcomes.append(("conflict", None))

    threads = [
        threading.Thread(target=transition, args=("PAUSED_A",)),
        threading.Thread(target=transition, args=("PAUSED_B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == [("committed", 2), ("conflict", None)]
    assert store.load().revision == 2
    assert len(store.events()) == 2


def test_transition_rejects_foreign_runner_lease_atomically(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")
    running = store.transition(
        expected_revision=state.revision,
        event_type="RUN_STARTED",
        changes={
            "status": WorkflowStatus.RUNNING,
            "runner_pid": 123,
            "runner_lease_id": "lease-a",
        },
    )

    with pytest.raises(RunnerLeaseLost):
        store.transition(
            expected_revision=running.revision,
            expected_runner_pid=123,
            expected_runner_lease_id="lease-b",
            event_type="STEP_SUCCEEDED",
            changes={"last_completed_step": 1},
        )

    assert store.load().revision == running.revision
    assert store.events()[-1].type == "RUN_STARTED"


def test_sensitive_event_payload_values_are_never_persisted(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")

    store.transition(
        expected_revision=state.revision,
        event_type="STEP_FAILED",
        changes={"status": WorkflowStatus.FAILED},
        payload={"api_token": "raw-secret", "nested": {"password": "also-secret"}},
    )

    connection = sqlite3.connect(store.path)
    try:
        raw = connection.execute(
            "SELECT payload_json FROM events ORDER BY revision DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert "raw-secret" not in raw
    assert "also-secret" not in raw
    assert "[REDACTED]" in raw


def test_sensitive_pending_action_values_are_redacted_from_snapshot(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="demo",
        project_type="modeling",
        pending_action={"type": "approval", "api_token": "raw-secret"},
    )

    assert state.pending_action == {"type": "approval", "api_token": "[REDACTED]"}
    assert "raw-secret" not in store.path.read_bytes().decode("utf-8", errors="ignore")


def test_database_is_stored_inside_project_factory_directory(tmp_path):
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling")

    assert store.path == tmp_path / ".factory" / "state.db"
    assert store.path.is_file()


def test_events_are_append_only_at_the_database_boundary(tmp_path):
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="demo", project_type="modeling")

    connection = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events WHERE revision = 1")
    finally:
        connection.close()


def test_compatibility_projection_preserves_checkpoint_mode(tmp_path):
    checkpoint = tmp_path / "checkpoint.md"
    checkpoint.write_text("- **Last completed step**: -1\n", encoding="utf-8")
    checkpoint.chmod(0o640)
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")

    write_compatibility_projections(tmp_path, state)

    assert checkpoint.stat().st_mode & 0o777 == 0o640


def test_completed_projection_preserves_imported_last_completed_step(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(
        project_id="historical",
        project_type="modeling",
        last_completed_step=2,
        status=WorkflowStatus.COMPLETED,
        imported=True,
    )

    write_compatibility_projections(tmp_path, state)

    assert (tmp_path / ".heartbeat").read_text(encoding="utf-8").startswith("2 ")


def test_v1_database_upgrades_in_place_without_rewriting_events(tmp_path):
    store = SQLiteStateStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(store.path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_info (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL
            );
            INSERT INTO schema_info VALUES (1, 1);
            CREATE TABLE project_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                project_type TEXT NOT NULL,
                control_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                last_completed_step INTEGER NOT NULL,
                active_step INTEGER,
                attempt INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                pending_action_json TEXT,
                runner_pid INTEGER,
                runner_lease_id TEXT,
                heartbeat_at INTEGER,
                storage_scope TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_event_at INTEGER NOT NULL
            );
            INSERT INTO project_state VALUES (
                1, 1, 'old', 'modeling', 'engine', 'ready', 4, NULL, 0, 1,
                NULL, NULL, NULL, NULL, 'ongoing', 10, 10, 10
            );
            CREATE TABLE events (
                revision INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                step INTEGER,
                attempt INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            INSERT INTO events VALUES (1, 'PROJECT_CREATED', 10, NULL, 0, '{}');
            """
        )
        connection.commit()
    finally:
        connection.close()

    state = store.load()

    assert state.schema_version == SCHEMA_VERSION
    assert state.runtime_generation == "legacy_adapter"
    assert state.last_completed_step == 4
    assert [event.type for event in store.events()] == ["PROJECT_CREATED"]


def test_v3_database_adds_independent_solver_job_revision(tmp_path):
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="v3", project_type="modeling")
    connection = sqlite3.connect(store.path)
    try:
        connection.executescript(
            """
            ALTER TABLE solver_jobs RENAME TO solver_jobs_v4;
            CREATE TABLE solver_jobs (
                job_id TEXT PRIMARY KEY,
                backend TEXT NOT NULL,
                runtime TEXT NOT NULL,
                script TEXT NOT NULL,
                workdir TEXT NOT NULL,
                argv_json TEXT NOT NULL,
                max_time_seconds INTEGER NOT NULL,
                external_id TEXT,
                status TEXT NOT NULL,
                requested_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER,
                result_refs_json TEXT NOT NULL,
                failure_json TEXT
            );
            DROP TABLE solver_jobs_v4;
            UPDATE schema_info SET schema_version = 3 WHERE singleton = 1;
            UPDATE project_state SET schema_version = 3 WHERE singleton = 1;
            """
        )
        connection.commit()
    finally:
        connection.close()

    state = store.load()

    connection = sqlite3.connect(store.path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(solver_jobs)")
        }
    finally:
        connection.close()
    assert state.schema_version == SCHEMA_VERSION
    assert "job_revision" in columns
