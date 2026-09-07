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
    assert {
        "idempotency_key",
        "request_sha256",
        "owner_stage",
        "owner_subtask",
        "owner_revision",
        "attempt_id",
    }.issubset(columns)


def test_v8_database_upgrades_dirty_identity_to_owner_scoped_keys(tmp_path):
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="v8", project_type="modeling")
    connection = sqlite3.connect(store.path)
    try:
        connection.executescript(
            """
            ALTER TABLE dirty_flags RENAME TO dirty_flags_v9;
            CREATE TABLE dirty_flags (
                flag TEXT PRIMARY KEY,
                owner_stage INTEGER NOT NULL,
                cause_revision INTEGER NOT NULL,
                cause_artifact TEXT NOT NULL,
                baseline_fingerprint TEXT NOT NULL,
                current_fingerprint TEXT NOT NULL,
                classifier_contract_sha256 TEXT NOT NULL
            );
            INSERT INTO dirty_flags VALUES (
                'MODEL_DIRTY', 1, 1, 'problem/problem_brief.md',
                'aaaaaaaa', 'bbbbbbbb', 'cccccccc'
            );
            DROP TABLE dirty_flags_v9;
            ALTER TABLE dirty_flag_clear_receipts
                RENAME TO dirty_flag_clear_receipts_v9;
            CREATE TABLE dirty_flag_clear_receipts (
                revision INTEGER NOT NULL,
                flag TEXT NOT NULL,
                owner_stage INTEGER NOT NULL,
                cleared_fingerprint TEXT NOT NULL,
                classifier_contract_sha256 TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY(revision, flag)
            );
            DROP TABLE dirty_flag_clear_receipts_v9;
            UPDATE schema_info SET schema_version = 8 WHERE singleton = 1;
            UPDATE project_state SET schema_version = 8 WHERE singleton = 1;
            """
        )
        connection.commit()
    finally:
        connection.close()

    state = store.load()

    connection = sqlite3.connect(store.path)
    try:
        dirty_pk = [
            row[1]
            for row in sorted(
                connection.execute("PRAGMA table_info(dirty_flags)"),
                key=lambda row: row[5] if row[5] else 99,
            )
            if row[5]
        ]
        clear_pk = [
            row[1]
            for row in sorted(
                connection.execute(
                    "PRAGMA table_info(dirty_flag_clear_receipts)"
                ),
                key=lambda row: row[5] if row[5] else 99,
            )
            if row[5]
        ]
    finally:
        connection.close()
    assert state.schema_version == SCHEMA_VERSION
    assert dirty_pk == ["flag", "owner_stage"]
    assert clear_pk == ["revision", "flag", "owner_stage"]
    assert store.dirty_flags()[0]["owner_stage"] == 1


def test_v5_database_upgrades_to_step_scheduler_without_rewriting_events(tmp_path):
    store = SQLiteStateStore(tmp_path)
    created = store.initialize(project_id="v5", project_type="modeling")
    connection = sqlite3.connect(store.path)
    try:
        connection.executescript(
            """
            ALTER TABLE project_state RENAME TO project_state_v6;
            CREATE TABLE project_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                project_type TEXT NOT NULL,
                control_mode TEXT NOT NULL,
                runtime_generation TEXT NOT NULL,
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
            INSERT INTO project_state(
                singleton, schema_version, project_id, project_type, control_mode,
                runtime_generation, status, last_completed_step, active_step,
                attempt, revision, pending_action_json, runner_pid,
                runner_lease_id, heartbeat_at, storage_scope, created_at,
                updated_at, last_event_at
            )
            SELECT
                singleton, 5, project_id, project_type, control_mode,
                runtime_generation, status, 7, NULL, attempt, revision,
                pending_action_json, runner_pid, runner_lease_id, heartbeat_at,
                storage_scope, created_at, updated_at, last_event_at
            FROM project_state_v6;
            DROP TABLE project_state_v6;
            UPDATE schema_info SET schema_version = 5 WHERE singleton = 1;
            """
        )
        connection.commit()
    finally:
        connection.close()

    state = store.load()

    assert state.scheduler_generation == "step_v2"
    assert state.stage_catalog_version is None
    assert state.last_completed_step == 7
    assert state.last_completed_stage == 5
    assert [(event.revision, event.type) for event in store.events()] == [
        (created.revision, "PROJECT_CREATED")
    ]


def test_v7_migration_never_treats_string_false_as_approval(tmp_path):
    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="v7", project_type="modeling")
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "INSERT INTO workflow_decisions(gate, decided_at, decision_json) "
            "VALUES ('content_freeze', 10, ?)",
            ('{"gate":"content_freeze","kind":"approval","approved":"false"}',),
        )
        connection.execute(
            "UPDATE schema_info SET schema_version = 7 WHERE singleton = 1"
        )
        connection.execute(
            "UPDATE project_state SET schema_version = 7 WHERE singleton = 1"
        )
        connection.commit()
    finally:
        connection.close()

    store.load()
    decision = store.decision_history("content_freeze")[0]

    assert decision.get("approved") is not True
    assert decision["outcome"] != "approved"
    assert decision["receipt_verification"]["valid"] is False
    assert store.decision("content_freeze") is None



def _dirty_change(flag, owner, artifact, classifier):
    return {
        "flag": flag,
        "owner_stage": owner,
        "cause_artifact": artifact,
        "baseline_fingerprint": "a" * 64,
        "current_fingerprint": "b" * 64,
        "classifier_contract_sha256": classifier,
    }


def test_future_classifier_change_rebases_active_obligation_and_emits_receipt(tmp_path):
    from factory_core.current_dirty import classifier_contract_sha256

    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="rebase", project_type="modeling")
    store.transition(
        expected_revision=state.revision,
        event_type="OLD_CLASSIFIER_DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            _dirty_change(
                "MODEL_DIRTY", 1, "problem/problem_brief.md", "old-classifier"
            )
        ],
    )

    rebased = store.rebase_dirty_classifier(
        expected_revision=store.load().revision
    )
    flags = store.dirty_flags()
    receipts = store.dirty_classifier_rebase_receipts()

    assert rebased.revision > state.revision
    assert store.events()[-1].type == "DIRTY_CLASSIFIER_REBASED"
    assert flags[0]["classifier_contract_sha256"] == classifier_contract_sha256()
    assert receipts
    assert receipts[-1]["receipt"]["schema_version"] == (
        "factory-dirty-classifier-rebase-v1"
    )
    assert receipts[-1]["receipt"]["obligations"][0]["old_classifier_sha256"] == (
        "old-classifier"
    )


def test_classifier_rebase_moves_obligation_to_current_artifact_owner(tmp_path):
    from factory_core.current_dirty import classifier_contract_sha256

    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="owner-move", project_type="modeling")
    store.transition(
        expected_revision=state.revision,
        event_type="OLD_ASSUMPTION_OWNER_FOR_TEST",
        changes={},
        dirty_changes=[
            _dirty_change(
                "MODEL_DIRTY", 3, "assumption_ledger.md", "old-classifier"
            )
        ],
    )

    first = store.rebase_dirty_classifier(expected_revision=store.load().revision)

    flags = store.dirty_flags()
    receipt = store.dirty_classifier_rebase_receipts()[-1]["receipt"]
    assert [(row["flag"], row["owner_stage"]) for row in flags] == [
        ("MATH_DIRTY", 8)
    ]
    assert flags[0]["classifier_contract_sha256"] == classifier_contract_sha256()
    assert receipt["obligations"][0]["previous_flag"] == "MODEL_DIRTY"
    assert receipt["obligations"][0]["previous_owner_stage"] == 3
    assert receipt["obligations"][0]["ownership_migrated"] is True
    second = store.rebase_dirty_classifier(expected_revision=first.revision)
    assert second.revision == first.revision
    assert len(store.dirty_classifier_rebase_receipts()) == 1


def test_classifier_rebase_routes_solver_receipt_to_durable_job_owner(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="receipt-owner-move", project_type="modeling")
    job_id = "local_python_sensitivity"
    state = store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": job_id,
            "owner_stage": 5,
            "owner_subtask": "sensitivity",
            "backend": "local",
            "runtime": "python",
            "script": "models/m2/05_sensitivity.py",
            "workdir": "models/m2",
            "argv": [],
            "max_time_seconds": 60,
            "status": "completed",
            "result_refs": {},
        },
    )
    receipt_path = f".factory/solver_receipts/{job_id}.completed.json"
    state = store.transition(
        expected_revision=state.revision,
        event_type="STATIC_RECEIPT_OWNER_FOR_TEST",
        changes={},
        dirty_changes=[
            _dirty_change("RESULT_DIRTY", 4, receipt_path, "old-classifier")
        ],
    )

    store.rebase_dirty_classifier(expected_revision=state.revision)

    flags = store.dirty_flags()
    receipt = store.dirty_classifier_rebase_receipts()[-1]["receipt"]
    assert [(row["flag"], row["owner_stage"]) for row in flags] == [
        ("RESULT_DIRTY", 5)
    ]
    obligation = receipt["obligations"][0]
    assert obligation["cause_artifact"] == receipt_path
    assert obligation["previous_owner_stage"] == 4
    assert obligation["owner_stage"] == 5
    assert obligation["ownership_migrated"] is True


def test_lost_multi_owner_dirty_obligations_reconstruct_from_causes(tmp_path):
    from factory_core.current_dirty import classifier_contract_sha256
    from factory_core.workflow_events import canonical_hash

    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="multi-owner", project_type="modeling")
    connection = sqlite3.connect(store.path)
    try:
        for revision, owner, artifact in (
            (2, 1, "problem/problem_brief.md"),
            (3, 2, "chosen_method.md"),
        ):
            cause_id = canonical_hash(
                {"revision": revision, "flag": "MODEL_DIRTY", "owner": owner}
            )[:32]
            connection.execute(
                """
                INSERT INTO dirty_causes(
                    cause_id, flag, owner_stage, cause_revision,
                    cause_artifact, baseline_fingerprint,
                    current_fingerprint, classifier_contract_sha256
                ) VALUES (?, 'MODEL_DIRTY', ?, ?, ?, ?, ?, ?)
                """,
                (
                    cause_id,
                    owner,
                    revision,
                    artifact,
                    "a" * 64,
                    "b" * 64,
                    "old-classifier",
                ),
            )
        connection.execute(
            """
            INSERT INTO dirty_flags(
                flag, owner_stage, cause_revision, cause_artifact,
                baseline_fingerprint, current_fingerprint,
                classifier_contract_sha256
            ) VALUES ('MODEL_DIRTY', 2, 3, 'chosen_method.md', ?, ?, ?)
            """,
            ("a" * 64, "b" * 64, "old-classifier"),
        )
        connection.commit()
    finally:
        connection.close()

    store.rebase_dirty_classifier(expected_revision=store.load().revision)
    flags = store.dirty_flags()

    assert {(row["flag"], row["owner_stage"]) for row in flags} == {
        ("MODEL_DIRTY", 1),
        ("MODEL_DIRTY", 2),
    }
    assert all(
        row["classifier_contract_sha256"] == classifier_contract_sha256()
        for row in flags
    )


def test_cleared_historical_cause_is_not_reconstructed_by_rebase(tmp_path):
    from factory_core.workflow_events import canonical_hash

    store = SQLiteStateStore(tmp_path)
    store.initialize(project_id="cleared", project_type="modeling")
    connection = sqlite3.connect(store.path)
    try:
        cause_id = canonical_hash({"cause": "cleared-owner"})[:32]
        connection.execute(
            """
            INSERT INTO dirty_causes(
                cause_id, flag, owner_stage, cause_revision,
                cause_artifact, baseline_fingerprint,
                current_fingerprint, classifier_contract_sha256
            ) VALUES (?, 'RESULT_DIRTY', 4, 2, 'results/canonical_results.json',
                      ?, ?, 'old-classifier')
            """,
            (cause_id, "a" * 64, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO dirty_flag_clear_receipts(
                revision, flag, owner_stage, cleared_fingerprint,
                classifier_contract_sha256, receipt_json
            ) VALUES (3, 'RESULT_DIRTY', 4, ?, 'old-classifier', '{}')
            """,
            ("c" * 64,),
        )
        connection.commit()
    finally:
        connection.close()

    assert store.dirty_flags() == []


def test_dirty_classifier_rebase_receipt_is_append_only(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="append-only-rebase", project_type="modeling")
    store.transition(
        expected_revision=state.revision,
        event_type="OLD_DIRTY",
        changes={},
        dirty_changes=[
            _dirty_change("MATH_DIRTY", 8, "paper/paper.tex", "old-classifier")
        ],
    )
    store.rebase_dirty_classifier(expected_revision=store.load().revision)
    assert store.dirty_flags()
    connection = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM dirty_classifier_rebases")
    finally:
        connection.close()


def test_rebased_v8_dirty_obligation_can_clear_with_current_owner_checkpoint(tmp_path):
    from factory_core.current_dirty import (
        capture_artifact_manifest,
        classifier_contract_sha256,
        manifest_fingerprint,
    )

    store = SQLiteStateStore(tmp_path)
    created = store.initialize(project_id="v8-clear", project_type="modeling")
    dirty = store.transition(
        expected_revision=created.revision,
        event_type="OLD_V8_DIRTY",
        changes={},
        dirty_changes=[
            _dirty_change(
                "MODEL_DIRTY", 1, "problem/problem_brief.md", "old-classifier"
            )
        ],
    )
    flags = store.dirty_flags()
    assert flags[0]["classifier_contract_sha256"] == "old-classifier"
    output = manifest_fingerprint(capture_artifact_manifest(tmp_path))
    success = {
        "schema_version": "factory-stage-checkpoint-v1",
        "status": "PASS",
        "stage": 1,
        "output_fingerprint": output,
        "classifier_contract_sha256": classifier_contract_sha256(),
    }
    store.transition(
        expected_revision=dirty.revision,
        event_type="REBASED_OWNER_SUCCEEDED",
        changes={},
        stage_checkpoint={
            "stage_id": 1,
            "subtask": "research_viability",
            "source_step_id": 1,
            "completed_step_id": 1,
            "input_fingerprint": output,
            "output_fingerprint": output,
            "receipt": success,
        },
        clear_dirty_stage={
            "owner_stage": 1,
            "cleared_fingerprint": output,
            "classifier_contract_sha256": classifier_contract_sha256(),
            "success_receipt": success,
        },
    )
    assert store.dirty_flags() == []



def test_prompt_attempt_input_is_bound_before_execution_with_revision_cas(tmp_path):
    from factory_core.effective_prompt import build_effective_prompt_receipt

    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="prompt", project_type="modeling")
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    template = prompt_dir / "step4.txt"
    template.write_text("do work\n", encoding="utf-8")
    receipt = build_effective_prompt_receipt(
        project_dir=tmp_path,
        factory_root=tmp_path,
        project_id="prompt",
        source_step_id=4,
        stage_id=3,
        subtask="model_construction",
        attempt=1,
        selected_revision=state.revision,
        prompt_template=template,
        prompt="effective prompt",
        researcher_note="",
    )

    bound, stored = store.bind_prompt_attempt_input(
        expected_revision=state.revision, receipt=receipt
    )

    assert bound.revision == state.revision + 1
    assert store.events()[-1].type == "PROMPT_INPUT_BOUND"
    assert stored["bound_revision"] == bound.revision
    assert store.prompt_attempt_input(receipt["attempt_key"])["receipt_id"] == (
        receipt["receipt_id"]
    )
    with pytest.raises(RevisionConflict):
        store.bind_prompt_attempt_input(
            expected_revision=state.revision, receipt=receipt
        )


def test_prompt_attempt_identity_allows_attempt_one_after_semantic_reopen(tmp_path):
    from factory_core.effective_prompt import build_effective_prompt_receipt

    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="prompt-reopen", project_type="modeling")
    template = tmp_path / "template.txt"
    template.write_text("prompt\n", encoding="utf-8")

    first = build_effective_prompt_receipt(
        project_dir=tmp_path,
        factory_root=tmp_path,
        project_id="prompt-reopen",
        source_step_id=4,
        stage_id=3,
        subtask="model_construction",
        attempt=1,
        selected_revision=state.revision,
        prompt_template=template,
        prompt="first effective prompt",
        researcher_note="",
    )
    bound, _ = store.bind_prompt_attempt_input(
        expected_revision=state.revision, receipt=first
    )
    reopened = store.transition(
        expected_revision=bound.revision,
        event_type="SEMANTIC_REOPEN_FOR_PROMPT_TEST",
        changes={},
    )
    second = build_effective_prompt_receipt(
        project_dir=tmp_path,
        factory_root=tmp_path,
        project_id="prompt-reopen",
        source_step_id=4,
        stage_id=3,
        subtask="model_construction",
        attempt=1,
        selected_revision=reopened.revision,
        prompt_template=template,
        prompt="second effective prompt",
        researcher_note="",
    )
    store.bind_prompt_attempt_input(
        expected_revision=reopened.revision, receipt=second
    )

    assert first["attempt_key"] != second["attempt_key"]
    assert len(store.prompt_attempt_inputs()) == 2
    latest = store.latest_prompt_attempt_input(
        stage_id=3,
        subtask="model_construction",
        source_step_id=4,
        attempt=1,
    )
    assert latest is not None
    assert latest["receipt_id"] == second["receipt_id"]


def test_prompt_attempt_input_receipt_is_append_only(tmp_path):
    from factory_core.effective_prompt import build_effective_prompt_receipt

    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="prompt-append", project_type="modeling")
    template = tmp_path / "template.txt"
    template.write_text("prompt\n", encoding="utf-8")
    receipt = build_effective_prompt_receipt(
        project_dir=tmp_path,
        factory_root=tmp_path,
        project_id="prompt-append",
        source_step_id=1,
        stage_id=1,
        subtask="research_viability",
        attempt=1,
        selected_revision=state.revision,
        prompt_template=template,
        prompt="effective",
        researcher_note="note",
    )
    store.bind_prompt_attempt_input(
        expected_revision=state.revision, receipt=receipt
    )

    connection = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM prompt_attempt_inputs")
    finally:
        connection.close()
