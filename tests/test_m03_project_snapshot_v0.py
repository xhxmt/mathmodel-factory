from __future__ import annotations

from dataclasses import fields, replace
import builtins
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

import factory_core.project_snapshot_v0 as snapshot_module
from factory_core.project_snapshot_v0 import (
    EVENT_HEAD_FACT_TYPE,
    EventAttemptBindingModeV1,
    EventStepBindingModeV1,
    ProjectSnapshotV0,
    SOLVER_RECEIPT_FACT_TYPE,
    SnapshotAvailabilityV0,
    SnapshotBuildResult,
    SnapshotCompletenessV0,
    SnapshotErrorCodeV0,
    SnapshotSectionIdV0,
    SnapshotV0ValidationError,
    build_project_snapshot_v0,
    project_snapshot_v0_semantic_bytes,
    project_snapshot_v0_semantic_sha256,
    snapshot_build_result_analysis_bytes,
    snapshot_build_result_semantic_bytes,
    compile_snapshot_policy_v0,
    validate_project_snapshot_v0,
    validate_snapshot_policy_v0,
    validate_snapshot_build_result,
)
from factory_core.domain import WorkflowStatus
from factory_core.human_decisions import build_decision_request
from factory_core.stages import (
    STAGE_CATALOG_VERSION,
    STAGE_CONTRACTS,
    STAGE_SCHEDULER_GENERATION,
)
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import canonical_hash


def _store(tmp_path: Path, project_id: str = "fixture:m03") -> SQLiteStateStore:
    project = tmp_path / "project"
    store = SQLiteStateStore(project, clock=lambda: 1)
    store.initialize(project_id=project_id, project_type="test")
    return store


def _create_submitting_solver_job(
    store: SQLiteStateStore, *, job_id: str
) -> None:
    state = store.load()
    store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": job_id,
            "backend": "local",
            "runtime": "python",
            "script": "solve.py",
            "workdir": ".",
            "argv": [],
            "max_time_seconds": 30,
            "status": "submitting",
            "result_refs": {},
        },
    )


def _permit_fixture_only_event_corruption(connection: sqlite3.Connection) -> None:
    """Disable the append-only guard only inside a disposable fault fixture."""

    connection.execute("DROP TRIGGER events_append_only_update")


def _record_solver_receipt(
    store: SQLiteStateStore,
    job_id: str,
    stage: str,
    *,
    request_sha256: str = "c" * 64,
    receipt_sha256: str = "a" * 64,
    content_sha256: str = "b" * 64,
) -> None:
    store.record_solver_receipt(
        job_id,
        stage=stage,
        receipt_path=f".factory/solver_receipts/{job_id}.{stage}.json",
        receipt_sha256=receipt_sha256,
        content_sha256=content_sha256,
        request_sha256=request_sha256,
    )


def _mutate_solver_event_payload(
    store: SQLiteStateStore,
    event_type: str,
    mutate,
) -> None:
    connection = sqlite3.connect(store.path)
    _permit_fixture_only_event_corruption(connection)
    row = connection.execute(
        "SELECT revision,payload_json FROM events WHERE type=? ORDER BY revision DESC LIMIT 1",
        (event_type,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row[1])
    mutate(payload)
    connection.execute(
        "UPDATE events SET payload_json=? WHERE revision=?",
        (json.dumps(payload, sort_keys=True), row[0]),
    )
    connection.commit()
    connection.close()


def _mutate_event_row(
    store: SQLiteStateStore,
    event_type: str,
    *,
    row_changes: dict[str, object],
) -> None:
    connection = sqlite3.connect(store.path)
    _permit_fixture_only_event_corruption(connection)
    assignments = ", ".join(f"{name}=?" for name in row_changes)
    connection.execute(
        f"UPDATE events SET {assignments} WHERE revision=(SELECT MAX(revision) FROM events WHERE type=?)",
        (*row_changes.values(), event_type),
    )
    connection.commit()
    connection.close()


def _files(path: Path) -> dict[str, tuple[int, str]]:
    result = {}
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            data = candidate.read_bytes()
            result[candidate.name] = (len(data), hashlib.sha256(data).hexdigest())
    return result


def _tree(root: Path) -> dict[str, tuple[str, int | None, str | None]]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative] = ("directory", None, None)
        elif path.is_file():
            data = path.read_bytes()
            result[relative] = ("file", len(data), hashlib.sha256(data).hexdigest())
        else:
            result[relative] = ("other", None, None)
    return result


def _logical_digest(path: Path) -> tuple[int, int, str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        schema = int(
            connection.execute(
                "SELECT schema_version FROM schema_info WHERE singleton=1"
            ).fetchone()[0]
        )
        revision = int(
            connection.execute(
                "SELECT revision FROM project_state WHERE singleton=1"
            ).fetchone()[0]
        )
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        rows = []
        for table in tables:
            values = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()]
            rows.append((table, values))
        digest = hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        connection.execute("ROLLBACK")
        return schema, revision, digest
    finally:
        connection.close()


def _change_kind(before, after) -> str:
    if before is None and after is not None:
        return "CREATED"
    if before is not None and after is None:
        return "DELETED"
    assert before is not None and after is not None
    if before == after:
        return "UNCHANGED"
    if after[0] < before[0]:
        return "TRUNCATED"
    return "MODIFIED"


def test_missing_database_is_legacy_unavailable_not_empty_snapshot(tmp_path: Path) -> None:
    result = build_project_snapshot_v0(str((tmp_path / "missing.db").absolute()))
    assert result.availability is SnapshotAvailabilityV0.UNAVAILABLE_LEGACY_UNBOUND
    assert result.error_code is SnapshotErrorCodeV0.DB_NOT_FOUND_LEGACY_FILESYSTEM_ONLY
    assert result.snapshot is None


def test_database_path_rejects_symlink_and_nonregular(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    real.write_bytes(b"not sqlite")
    link = tmp_path / "link.db"
    link.symlink_to(real)
    assert build_project_snapshot_v0(str(link)).error_code is SnapshotErrorCodeV0.DB_PATH_SYMLINK
    assert build_project_snapshot_v0(str(tmp_path)).error_code is SnapshotErrorCodeV0.DB_PATH_NOT_REGULAR
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    (real_parent / "nested.db").write_bytes(b"not sqlite")
    assert build_project_snapshot_v0(str(parent_link / "nested.db")).error_code is (
        SnapshotErrorCodeV0.DB_PATH_SYMLINK
    )


def test_current_legacy_database_builds_partial_single_coordinate_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = build_project_snapshot_v0(str(store.path), expected_project_id="fixture:m03")
    assert result.availability is SnapshotAvailabilityV0.AVAILABLE
    assert result.snapshot is not None
    snapshot = result.snapshot
    assert snapshot.completeness is SnapshotCompletenessV0.PARTIAL
    assert snapshot.coordinate.project_generation is None
    assert snapshot.coordinate.run_generation is None
    assert snapshot.coordinate.recorded_contract_pin_set_sha256 is None
    assert all(section.coordinate == snapshot.coordinate for section in snapshot.sections)
    by_id = {section.section_id: section for section in snapshot.sections}
    assert by_id[SnapshotSectionIdV0.GENERATION_BINDING].availability is (
        SnapshotAvailabilityV0.UNAVAILABLE_LEGACY_UNBOUND
    )
    assert by_id[SnapshotSectionIdV0.CONTRACT_PINS].facts == ()
    assert by_id[SnapshotSectionIdV0.DELIVERY_AUTHORIZATION].facts == ()
    statements = "\n".join(result.analysis_sql_trace).upper()
    assert "BEGIN" in statements and "ROLLBACK" in statements
    for statement in result.analysis_sql_trace:
        normalized = statement.strip().upper()
        assert not normalized.startswith(
            ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "ATTACH", "VACUUM")
        )
        assert "JOURNAL_MODE" not in normalized
    assert result.authoritative is False
    assert result.performed_workflow_side_effects == ()
    assert result.application_initiated_write_operations == ()


def test_read_only_wal_snapshot_allows_audited_sqlite_auxiliary_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    journal = sqlite3.connect(store.path)
    assert journal.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    logical_before = _logical_digest(store.path)
    journal.close()
    wal = Path(str(store.path) + "-wal")
    shm = Path(str(store.path) + "-shm")
    assert not wal.exists() and not shm.exists()
    assert not wal.exists() and not shm.exists()
    main_before = _files(store.path)[store.path.name]
    tree_before = _tree(store.project_dir)

    def deny(*_args, **_kwargs):
        raise AssertionError("Snapshot application code attempted a filesystem write")

    original_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(marker in mode for marker in ("w", "a", "x", "+")):
            deny()
        return original_open(file, mode, *args, **kwargs)

    original_os_open = os.open

    def guarded_os_open(file, flags, *args, **kwargs):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if flags & write_flags:
            deny()
        return original_os_open(file, flags, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "open", guarded_open)
        scoped.setattr(os, "open", guarded_os_open)
        scoped.setattr(os, "unlink", deny)
        scoped.setattr(os, "rename", deny)
        scoped.setattr(os, "truncate", deny)
        scoped.setattr(Path, "touch", deny)
        scoped.setattr(Path, "write_bytes", deny)
        scoped.setattr(Path, "write_text", deny)
        scoped.setattr(Path, "unlink", deny)
        scoped.setattr(Path, "rename", deny)
        scoped.setattr(Path, "mkdir", deny)
        first = build_project_snapshot_v0(str(store.path))
        second = build_project_snapshot_v0(str(store.path))

    assert first.availability is SnapshotAvailabilityV0.AVAILABLE
    assert second.availability is SnapshotAvailabilityV0.AVAILABLE
    assert first.snapshot is not None and second.snapshot is not None
    assert first.snapshot.completeness is SnapshotCompletenessV0.PARTIAL
    assert project_snapshot_v0_semantic_bytes(first.snapshot) == (
        project_snapshot_v0_semantic_bytes(second.snapshot)
    )
    assert project_snapshot_v0_semantic_sha256(first.snapshot) == (
        project_snapshot_v0_semantic_sha256(second.snapshot)
    )
    assert first.snapshot.coordinate.project_revision == second.snapshot.coordinate.project_revision
    main_after = _files(store.path)[store.path.name]
    tree_after = _tree(store.project_dir)
    assert main_before == main_after
    assert logical_before == _logical_digest(store.path)
    changed_paths = {
        path
        for path in set(tree_before) | set(tree_after)
        if tree_before.get(path) != tree_after.get(path)
    }
    allowed = {".factory/state.db-wal", ".factory/state.db-shm"}
    assert changed_paths <= allowed
    observations = {
        relative: {
            "before": tree_before.get(relative),
            "after": tree_after.get(relative),
            "change_kind": _change_kind(
                (
                    tree_before[relative][1],
                    tree_before[relative][2],
                )
                if relative in tree_before
                else None,
                (
                    tree_after[relative][1],
                    tree_after[relative][2],
                )
                if relative in tree_after
                else None,
            ),
        }
        for relative in sorted(allowed)
    }
    observation_context = {
        "sqlite_runtime_version": sqlite3.sqlite_version,
        "vfs_name": None,
        "sidecars": observations,
    }
    assert observation_context["sqlite_runtime_version"]
    assert observation_context["vfs_name"] is None
    assert {item["change_kind"] for item in observations.values()} <= {
        "UNCHANGED",
        "CREATED",
        "MODIFIED",
        "TRUNCATED",
        "DELETED",
    }
    normalized_trace = tuple(
        statement.strip().upper() for statement in first.analysis_sql_trace
    )
    assert "BEGIN" in normalized_trace and "ROLLBACK" in normalized_trace
    assert all(
        statement.startswith(("PRAGMA QUERY_ONLY=ON", "BEGIN", "SELECT", "ROLLBACK"))
        for statement in normalized_trace
    )


def test_read_only_wal_snapshot_returns_typed_error_when_auxiliary_files_cannot_be_established(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    assert not Path(str(store.path) + "-wal").exists()
    assert not Path(str(store.path) + "-shm").exists()
    directory = store.path.parent
    original_mode = directory.stat().st_mode & 0o777
    directory.chmod(0o500)
    try:
        result = build_project_snapshot_v0(str(store.path))
    finally:
        directory.chmod(original_mode)
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SQLITE_WAL_AUXILIARY_UNAVAILABLE
    assert result.snapshot is None


def test_semantic_serializer_excludes_only_sql_trace_analysis(tmp_path: Path) -> None:
    result = build_project_snapshot_v0(str(_store(tmp_path).path))
    changed = replace(result, analysis_sql_trace=result.analysis_sql_trace + ("analysis-only",))
    assert snapshot_build_result_semantic_bytes(result) == snapshot_build_result_semantic_bytes(changed)
    assert snapshot_build_result_analysis_bytes(result) != snapshot_build_result_analysis_bytes(changed)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("schema", SnapshotErrorCodeV0.SCHEMA_INVALID),
        ("project_json", SnapshotErrorCodeV0.REQUIRED_JSON_INVALID),
        ("event_gap", SnapshotErrorCodeV0.EVENT_CHAIN_INVALID),
        ("future_config", SnapshotErrorCodeV0.FUTURE_ROW_REVISION),
        ("bad_hash", SnapshotErrorCodeV0.REQUIRED_HASH_INVALID),
    ],
)
def test_required_schema_json_hash_revision_and_chain_faults_fail_top_level(
    tmp_path: Path, mutation: str, expected: SnapshotErrorCodeV0
) -> None:
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    if mutation == "schema":
        connection.execute("UPDATE schema_info SET schema_version=999")
    elif mutation == "project_json":
        connection.execute("UPDATE project_state SET pending_action_json='{' WHERE singleton=1")
    elif mutation == "event_gap":
        connection.execute("UPDATE project_state SET revision=2 WHERE singleton=1")
    elif mutation == "future_config":
        connection.execute(
            "INSERT INTO project_config(singleton,solver_mode,solver_threshold_seconds,solver_runtimes_json,updated_revision) VALUES (1,'local',1,'[]',99)"
        )
    elif mutation == "bad_hash":
        connection.execute(
            "INSERT INTO dirty_flags VALUES ('MATH_DIRTY',8,1,'x','a','b','NOT-A-HASH')"
        )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is expected
    assert result.snapshot is None


def test_foreign_project_fails_closed(tmp_path: Path) -> None:
    result = build_project_snapshot_v0(
        str(_store(tmp_path, "project:a").path), expected_project_id="project:b"
    )
    assert result.error_code is SnapshotErrorCodeV0.PROJECT_ID_MISMATCH


def test_projector_corruption_is_typed_partial_section_not_fake_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO projector_snapshots(projector_name,projector_version,through_revision,state_hash,snapshot_json,created_at) VALUES ('x',1,1,?, '{',1)",
        ("a" * 64,),
    )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.snapshot is not None
    section = next(
        item
        for item in result.snapshot.sections
        if item.section_id is SnapshotSectionIdV0.PROJECTOR_STATE
    )
    assert section.availability is SnapshotAvailabilityV0.ERROR
    assert section.facts == ()
    assert section.error_code is SnapshotErrorCodeV0.REQUIRED_JSON_INVALID


def test_snapshot_policy_is_rebuilt_from_source_and_rejects_supplied_drift() -> None:
    policy = compile_snapshot_policy_v0()
    assert validate_snapshot_policy_v0(policy) is policy
    assert policy.solver_lifecycle_events == (
        ("SOLVER_JOB_SUBMITTED", "submitted"),
        ("SOLVER_JOB_SUBMITTING", "submitting"),
        ("SOLVER_JOB_QUEUED", "queued"),
        ("SOLVER_JOB_RUNNING", "running"),
        ("SOLVER_JOB_CANCELLING", "cancelling"),
        ("SOLVER_JOB_COMPLETED", "completed"),
        ("SOLVER_JOB_FAILED", "failed"),
        ("SOLVER_JOB_TIMEOUT", "timeout"),
        ("SOLVER_JOB_CANCELLED", "cancelled"),
    )
    assert policy.solver_receipt_events == (
        ("SOLVER_JOB_RECEIPT_SUBMITTED", "submitted"),
        ("SOLVER_JOB_RECEIPT_COMPLETED", "completed"),
    )
    assert policy.solver_statuses == (
        "submitted",
        "submitting",
        "queued",
        "running",
        "cancelling",
        "completed",
        "failed",
        "timeout",
        "cancelled",
    )
    for field in fields(type(policy)):
        original = getattr(policy, field.name)
        if type(original) is str:
            forged_value = original + ":forged"
        elif type(original) is int:
            forged_value = original + 1
        else:
            forged_value = original + (original[0],)
        with pytest.raises(SnapshotV0ValidationError):
            validate_snapshot_policy_v0(replace(policy, **{field.name: forged_value}))


def test_event_row_policy_is_an_exact_closed_set_and_accepts_every_authorized_family() -> None:
    policy = compile_snapshot_policy_v0()
    expected_types = {
        "ACTION_PROJECTION_FAILED",
        "ACTION_RESOLVED",
        "AWAITING_ACTION",
        "CONTEST_DEADLINE_EXHAUSTED",
        "DECISION_REQUEST_BUILD_FAILED",
        "DIRTY_CLASSIFIER_REBASED",
        "ENGINE_DEACTIVATED",
        "FINALIZATION_ABORTED_SNAPSHOT_CHANGED",
        "FINAL_SNAPSHOT_CREATED",
        "HUMAN_DECISION_RECORDED",
        "HUMAN_DECISION_REQUEST_SUPERSEDED",
        "KILLED",
        "PAUSED",
        "PROJECT_ARCHIVED",
        "PROJECT_ARCHIVE_REQUESTED",
        "PROJECT_COMPLETED",
        "PROJECT_CREATED",
        "PROJECT_IMPORTED",
        "PROMPT_INPUT_BOUND",
        "RECOVERY_DECIDED",
        "RESUMED",
        "RETRY_SCHEDULED",
        "RUNNER_INTERRUPTED",
        "RUN_STARTED",
        "RUN_STOPPED",
        "SOLVER_JOB_CANCELLED",
        "SOLVER_JOB_CANCELLING",
        "SOLVER_JOB_COMPLETED",
        "SOLVER_JOB_FAILED",
        "SOLVER_JOB_QUEUED",
        "SOLVER_JOB_RECEIPT_COMPLETED",
        "SOLVER_JOB_RECEIPT_SUBMITTED",
        "SOLVER_JOB_RUNNING",
        "SOLVER_JOB_SUBMITTED",
        "SOLVER_JOB_SUBMITTING",
        "SOLVER_JOB_TIMEOUT",
        "SOLVER_POLICY_CONFIGURED",
        "STAGE_CHECKPOINT_INVALIDATED",
        "STAGE_SCHEDULER_ACTIVATED",
        "STAGE_SCHEDULER_ROLLED_BACK",
        "STAGE_SEMANTIC_REOPENED",
        "STAGE_SUBTASK_SELECTED",
        "STEP_FAILED",
        "STEP_PREPARE_AWAITING_ACTION",
        "STEP_REOPENED",
        "STEP_STARTED",
        "STEP_SUCCEEDED",
        "WORKER_LAUNCHED",
        "WORK_REOPENED",
    }
    assert {item.event_type for item in policy.event_row_policies} == expected_types
    assert len(policy.event_row_policies) == len(
        {(item.event_type, item.payload_family) for item in policy.event_row_policies}
    )

    subject = {"source_step_id": 3, "attempt": 2}
    result = {"source_step_id": 4, "attempt": 3}
    for index, item in enumerate(policy.event_row_policies, start=1):
        payload = {name: None for name in item.required_payload_fields}
        if item.step_binding_mode is EventStepBindingModeV1.STEP_NONE:
            step = None
        elif item.step_binding_mode is EventStepBindingModeV1.STEP_SOURCE_CATALOG:
            step = item.source_catalog_steps[0]
        elif item.step_binding_mode is EventStepBindingModeV1.STEP_PAYLOAD_SOURCE:
            assert item.payload_source_step_field is not None
            step = 5
            payload[item.payload_source_step_field] = step
        elif item.step_binding_mode is EventStepBindingModeV1.STEP_PAYLOAD_STAGE_SUBTASK:
            step = 0
            payload.update(stage=1, subtask="problem_setup")
        elif item.step_binding_mode is EventStepBindingModeV1.STEP_SUBJECT_SOURCE:
            step = subject["source_step_id"]
        else:
            assert item.step_binding_mode is EventStepBindingModeV1.STEP_RESULT_SOURCE
            step = result["source_step_id"]
        if item.attempt_binding_mode is EventAttemptBindingModeV1.ATTEMPT_ZERO:
            attempt = 0
        elif item.attempt_binding_mode is EventAttemptBindingModeV1.ATTEMPT_SUBJECT:
            attempt = subject["attempt"]
        else:
            assert item.attempt_binding_mode is EventAttemptBindingModeV1.ATTEMPT_RESULT
            attempt = result["attempt"]
        if item.payload_attempt_field is not None:
            payload[item.payload_attempt_field] = attempt
        assert snapshot_module._select_event_row_policy_v1(
            item.event_type, payload, revision=index
        ) == item
        snapshot_module._validate_event_row_binding_v1(
            item,
            revision=index,
            step=step,
            attempt=attempt,
            payload=payload,
            subject_state=subject,
            result_state=result,
        )


@pytest.mark.parametrize("event_step", [999, 1])
def test_public_transition_rejects_out_of_catalog_and_wrong_in_range_step(
    tmp_path: Path, event_step: int
) -> None:
    store = _store(tmp_path)
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="STEP_STARTED",
        changes={"active_step": 0, "attempt": 1},
        payload={"step_name": "fixture"},
        event_step=event_step,
    )
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert result.snapshot is None


def test_project_created_and_result_attempt_row_mismatches_fail_closed(
    tmp_path: Path,
) -> None:
    created = _store(tmp_path / "created")
    _mutate_event_row(created, "PROJECT_CREATED", row_changes={"attempt": 999})
    created_result = build_project_snapshot_v0(str(created.path))
    assert created_result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID

    result_bound = _store(tmp_path / "result")
    state = result_bound.load()
    result_bound.transition(
        expected_revision=state.revision,
        event_type="STEP_STARTED",
        changes={"active_step": 0, "attempt": 1},
        payload={"step_name": "fixture"},
    )
    _mutate_event_row(result_bound, "STEP_STARTED", row_changes={"attempt": 0})
    result = build_project_snapshot_v0(str(result_bound.path))
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID


def test_subject_attempt_binding_rejects_a_row_that_only_matches_result_shape() -> None:
    policy = next(
        item
        for item in compile_snapshot_policy_v0().event_row_policies
        if item.event_type == "HUMAN_DECISION_RECORDED"
    )
    payload = {name: None for name in policy.required_payload_fields}
    with pytest.raises(snapshot_module._SnapshotBuildFailure) as caught:
        snapshot_module._validate_event_row_binding_v1(
            policy,
            revision=2,
            step=4,
            attempt=3,
            payload=payload,
            subject_state={"source_step_id": 4, "attempt": 2},
            result_state={"source_step_id": 4, "attempt": 3},
        )
    assert caught.value.code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID


@pytest.mark.parametrize(
    "event_type",
    ["FORGED_EVENT", "GATE_BLOCKED", "SOLVER_JOB_FUTURE"],
)
def test_unknown_canonical_alias_and_dynamic_event_families_fail_closed(
    tmp_path: Path, event_type: str
) -> None:
    store = _store(tmp_path)
    state = store.load()
    store.transition(expected_revision=state.revision, event_type=event_type, changes={})
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert result.snapshot is None


@pytest.mark.parametrize("value", ["logs/line\nbreak", "logs/tab\tbreak", "logs/del\x7fbreak"])
def test_project_relative_immutable_paths_reject_c0_and_del(
    value: str,
) -> None:
    with pytest.raises(snapshot_module._SnapshotBuildFailure) as caught:
        snapshot_module._project_relative_posix_path(
            value,
            context="fixture",
            code=SnapshotErrorCodeV0.IMMUTABLE_REF_CONTRACT_INVALID,
        )
    assert caught.value.code is SnapshotErrorCodeV0.IMMUTABLE_REF_CONTRACT_INVALID


def test_source_authorized_storage_rows_remain_available(tmp_path: Path) -> None:
    project = tmp_path / "source-authorized"
    store = SQLiteStateStore(project, clock=lambda: 10)
    state = store.initialize(
        project_id="fixture:m03:authorized",
        project_type="test",
        last_completed_step=-1,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    state = store.transition(
        expected_revision=state.revision,
        event_type="STAGE_SUBTASK_SELECTED",
        changes={
            "active_step": 0,
            "active_stage": 1,
            "active_subtask": "problem_setup",
            "source_step_id": 0,
            "attempt": 1,
        },
        payload={
            "stage": 1,
            "stage_name": "Problem Setup",
            "subtask": "problem_setup",
            "source_step": 0,
            "checkpoint_step": 0,
            "stage_catalog_version": STAGE_CATALOG_VERSION,
        },
        subtask_baseline={
            "stage_id": 1,
            "subtask": "problem_setup",
            "source_step_id": 0,
            "input_fingerprint": "a" * 64,
            "manifest": {},
        },
    )
    checkpoint_receipt = {
        "schema_version": "factory-stage-checkpoint-v1",
        "status": "PASS",
        "stage": 1,
        "subtask": "problem_setup",
        "source_step_id": 0,
        "completed_step_id": 0,
        "input_fingerprint": "a" * 64,
        "output_fingerprint": "b" * 64,
    }
    state = store.transition(
        expected_revision=state.revision,
        event_type="STEP_SUCCEEDED",
        changes={
            "last_completed_step": 0,
            "last_completed_stage": 0,
            "active_step": 1,
            "active_stage": 1,
            "active_subtask": "research_and_viability",
            "source_step_id": 1,
            "attempt": 0,
        },
        payload={
            "stage": 1,
            "subtask": "problem_setup",
            "source_step": 0,
            "evidence": [],
        },
        subtask_baseline={
            "stage_id": 1,
            "subtask": "research_and_viability",
            "source_step_id": 1,
            "input_fingerprint": "b" * 64,
            "manifest": {},
        },
        stage_checkpoint={
            "stage_id": 1,
            "subtask": "problem_setup",
            "source_step_id": 0,
            "completed_step_id": 0,
            "input_fingerprint": "a" * 64,
            "output_fingerprint": "b" * 64,
            "receipt": checkpoint_receipt,
        },
        event_step=0,
    )
    action = {"type": "step3_selection", "gate": "step3"}
    request = build_decision_request(
        project_id=state.project_id,
        project_dir=project,
        requested_revision=state.revision + 1,
        generation=1,
        action=action,
        reason="source-authorized fixture",
    ).to_dict()
    pending = {**action, "metadata": {"human_decision": request}}
    state = store.transition(
        expected_revision=state.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": pending,
        },
        payload={"action": request, "pending_action": pending},
    )
    owner_revision = state.revision
    state = store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": "job:authorized",
            "owner_stage": 1,
            "owner_subtask": "research_and_viability",
            "owner_revision": owner_revision,
            "attempt_id": "attempt:1",
            "backend": "local",
            "runtime": "python",
            "script": "solve.py",
            "workdir": ".",
            "argv": [],
            "max_time_seconds": 30,
            "status": "submitting",
            "result_refs": {},
        },
    )
    state = store.transition(
        expected_revision=state.revision,
        event_type="STEP_FAILED",
        changes={},
        payload={"error_class": "FIXTURE_DIRTY_FACT"},
        dirty_changes=[
            {
                "flag": "MATH_DIRTY",
                "owner_stage": 8,
                "cause_artifact": "paper/paper.tex",
                "baseline_fingerprint": "c" * 64,
                "current_fingerprint": "d" * 64,
                "classifier_contract_sha256": "e" * 64,
            }
        ],
    )
    state = store.rebase_dirty_classifier(expected_revision=state.revision)
    projector = {"pending": []}
    store.save_projector_snapshot(
        "action-center",
        projector_version=1,
        through_revision=state.revision,
        state_hash=canonical_hash(projector),
        snapshot=projector,
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.AVAILABLE
    assert result.snapshot is not None
    by_id = {section.section_id: section for section in result.snapshot.sections}
    for section_id in (
        SnapshotSectionIdV0.PROJECTOR_STATE,
        SnapshotSectionIdV0.STAGE_CURSOR_CHECKPOINTS,
        SnapshotSectionIdV0.DIRTY_FACTS,
        SnapshotSectionIdV0.PENDING_HUMAN,
        SnapshotSectionIdV0.INVOCATIONS_SOLVER,
        SnapshotSectionIdV0.IMMUTABLE_REFS,
    ):
        assert by_id[section_id].availability is SnapshotAvailabilityV0.AVAILABLE


def test_forged_stage_checkpoint_domain_values_fail_top_level(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO stage_checkpoints(stage_id,subtask,source_step_id,completed_step_id,input_fingerprint,output_fingerprint,completed_revision,receipt_json) VALUES (999,'evil',999,999,?,?,1,?)",
        ("a" * 64, "b" * 64, json.dumps({"schema": "x"})),
    )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.STAGE_CHECKPOINT_CONTRACT_INVALID
    assert result.snapshot is None


def test_stage_source_and_completed_step_must_match_source_catalog(tmp_path: Path) -> None:
    store = _store(tmp_path)
    subtask = STAGE_CONTRACTS[0].subtasks[0]
    receipt = {
        "schema_version": "factory-stage-checkpoint-v1",
        "status": "PASS",
        "stage": 1,
        "subtask": subtask.key,
        "source_step_id": subtask.source_step_id + 1,
        "completed_step_id": subtask.checkpoint_step_id,
        "input_fingerprint": "a" * 64,
        "output_fingerprint": "b" * 64,
    }
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO stage_checkpoints(stage_id,subtask,source_step_id,completed_step_id,input_fingerprint,output_fingerprint,completed_revision,receipt_json) VALUES (?,?,?,?,?,?,1,?)",
        (
            1,
            subtask.key,
            subtask.source_step_id + 1,
            subtask.checkpoint_step_id,
            "a" * 64,
            "b" * 64,
            json.dumps(receipt),
        ),
    )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.STAGE_CHECKPOINT_CONTRACT_INVALID


def test_compatibility_checkpoint_receipt_must_match_source_catalog(tmp_path: Path) -> None:
    project = tmp_path / "compatibility-checkpoint"
    store = SQLiteStateStore(project, clock=lambda: 1)
    store.initialize(
        project_id="fixture:m03:compatibility",
        project_type="test",
        last_completed_step=-1,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    connection = sqlite3.connect(store.path)
    receipt = {
        "schema_version": "factory-stage-checkpoint-v1",
        "source": "compatibility_cursor_seed",
        "stage_id": 6,
        "subtask": "reviewer_entry_gate",
        "source_step_id": 999,
        "completed_step_id": None,
    }
    connection.execute(
        "INSERT INTO stage_checkpoints(stage_id,subtask,source_step_id,completed_step_id,input_fingerprint,output_fingerprint,completed_revision,receipt_json) VALUES (6,'reviewer_entry_gate',8,NULL,'MIGRATION_SEED','MIGRATION_SEED',1,?)",
        (json.dumps(receipt, sort_keys=True),),
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.STAGE_CHECKPOINT_CONTRACT_INVALID


def _decision_request(
    request_id: str,
    *,
    generation: int,
    subject: str,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "gate": "step3",
        "generation": generation,
        "kind": "selection",
        "type": "step3_selection",
        "requested_revision": 1,
        "subject_fingerprint": subject,
        "options_fingerprint": "c" * 64,
    }


@pytest.mark.parametrize("mismatch", ["request_id", "subject_fingerprint"])
def test_pending_action_must_bind_the_unique_open_request(
    tmp_path: Path, mismatch: str
) -> None:
    store = _store(tmp_path)
    stored = _decision_request("request:B", generation=2, subject="b" * 64)
    pending = dict(stored)
    pending[mismatch] = "request:A" if mismatch == "request_id" else "d" * 64
    pending_action = {
        "gate": "step3",
        "type": "step3_selection",
        "metadata": {"human_decision": pending},
    }
    connection = sqlite3.connect(store.path)
    connection.execute(
        "UPDATE project_state SET status='awaiting_selection', pending_action_json=? WHERE singleton=1",
        (json.dumps(pending_action),),
    )
    connection.execute(
        "INSERT INTO workflow_decision_requests(request_id,gate_type,generation,kind,action_type,requested_revision,subject_fingerprint,options_fingerprint,status,created_at,request_json) VALUES (?,?,?,?,?,?,?,?, 'open',1,?)",
        (
            stored["request_id"],
            stored["gate"],
            stored["generation"],
            stored["kind"],
            stored["type"],
            stored["requested_revision"],
            stored["subject_fingerprint"],
            stored["options_fingerprint"],
            json.dumps(stored, sort_keys=True),
        ),
    )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.PENDING_REQUEST_CONTRACT_INVALID


@pytest.mark.parametrize(
    ("flag", "owner_stage"),
    [("UNKNOWN_DIRTY", 8), ("MATH_DIRTY", 999)],
)
def test_dirty_flag_and_owner_stage_must_be_source_authorized(
    tmp_path: Path, flag: str, owner_stage: int
) -> None:
    store = _store(tmp_path)
    values = (flag, owner_stage, 1, "paper/model.tex", "a" * 64, "b" * 64, "c" * 64)
    cause_id = canonical_hash(
        {
            "revision": 1,
            "flag": flag,
            "owner_stage": owner_stage,
            "artifact": "paper/model.tex",
            "baseline": "a" * 64,
            "current": "b" * 64,
        }
    )[:32]
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO dirty_causes(cause_id,flag,owner_stage,cause_revision,cause_artifact,baseline_fingerprint,current_fingerprint,classifier_contract_sha256) VALUES (?,?,?,?,?,?,?,?)",
        (cause_id,) + values,
    )
    connection.execute(
        "INSERT INTO dirty_flags(flag,owner_stage,cause_revision,cause_artifact,baseline_fingerprint,current_fingerprint,classifier_contract_sha256) VALUES (?,?,?,?,?,?,?)",
        values,
    )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.DIRTY_FACT_CONTRACT_INVALID


def test_dirty_cause_identity_and_clear_receipt_relation_are_source_bound(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = store.load()
    dirty = {
        "flag": "MATH_DIRTY",
        "owner_stage": 8,
        "cause_artifact": "paper/model.tex",
        "baseline_fingerprint": "a" * 64,
        "current_fingerprint": "b" * 64,
        "classifier_contract_sha256": "c" * 64,
    }
    state = store.transition(
        expected_revision=state.revision,
        event_type="STEP_FAILED",
        changes={},
        payload={"error_class": "FIXTURE_DIRTY_FACT"},
        dirty_changes=[dirty],
    )
    state = store.transition(
        expected_revision=state.revision,
        event_type="RUNNER_INTERRUPTED",
        changes={},
        payload={"reason": "fixture clear coordinate"},
    )
    receipt = {
        "schema_version": "factory-dirty-clear-receipt-v1",
        "flag": "MATH_DIRTY",
        "owner_stage": 8,
        "cause_revision": state.revision - 1,
        "cause_artifact": "paper/forged.tex",
        "cleared_fingerprint": "d" * 64,
        "classifier_contract_sha256": "c" * 64,
        "success_receipt": {
            "schema_version": "factory-stage-checkpoint-v1",
            "status": "PASS",
            "stage": 8,
            "output_fingerprint": "d" * 64,
        },
    }
    connection = sqlite3.connect(store.path)
    connection.execute("DELETE FROM dirty_flags")
    connection.execute(
        "INSERT INTO dirty_flag_clear_receipts(revision,flag,owner_stage,cleared_fingerprint,classifier_contract_sha256,receipt_json) VALUES (?,?,?,?,?,?)",
        (
            state.revision,
            "MATH_DIRTY",
            8,
            "d" * 64,
            "c" * 64,
            json.dumps(receipt, sort_keys=True),
        ),
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.DIRTY_FACT_CONTRACT_INVALID


def test_dirty_rebase_receipt_contradiction_fails_top_level(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old_hashes = ["a" * 64]
    receipt = {
        "schema_version": "factory-dirty-classifier-rebase-v1",
        "rebase_id": "different",
        "source_schema_version": 9,
        "target_schema_version": 9,
        "old_classifier_sha256": old_hashes,
        "new_classifier_sha256": "b" * 64,
        "obligations": [],
        "retired_obligations": [],
    }
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO dirty_classifier_rebases(rebase_id,source_schema_version,target_schema_version,old_classifier_sha256,new_classifier_sha256,obligation_count,created_at,receipt_json) VALUES ('rebase:1',9,9,?,?,0,1,?)",
        (json.dumps(old_hashes), "b" * 64, json.dumps(receipt)),
    )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.DIRTY_FACT_CONTRACT_INVALID


def test_unsupported_projector_version_is_typed_optional_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = {"project_id": "fixture:m03"}
    state_hash = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO projector_snapshots(projector_name,projector_version,through_revision,state_hash,snapshot_json,created_at) VALUES ('workflow',999,0,?,?,1)",
        (state_hash, json.dumps(snapshot)),
    )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.AVAILABLE
    assert result.snapshot is not None
    section = next(
        item for item in result.snapshot.sections if item.section_id is SnapshotSectionIdV0.PROJECTOR_STATE
    )
    assert section.availability is SnapshotAvailabilityV0.ERROR
    assert section.error_code is SnapshotErrorCodeV0.PROJECTOR_CONTRACT_INVALID
    assert result.snapshot.completeness is SnapshotCompletenessV0.PARTIAL


def test_solver_job_generation_must_match_recorded_event_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO solver_jobs(job_id,job_revision,backend,runtime,script,workdir,argv_json,max_time_seconds,status,requested_at,result_refs_json) VALUES ('job:1',2,'local','python','x.py','.', '[]',60,'running',1,'{}')"
    )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


@pytest.mark.parametrize("status", ["submitting", "queued", "cancelling"])
def test_source_authorized_solver_lifecycle_updates_remain_available(
    tmp_path: Path, status: str
) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id=f"job:{status}")
    store.update_solver_job(
        f"job:{status}",
        expected_job_revision=1,
        status=status,
        external_id="external:1" if status == "submitting" else None,
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.AVAILABLE
    assert result.snapshot is not None
    section = next(
        item
        for item in result.snapshot.sections
        if item.section_id is SnapshotSectionIdV0.INVOCATIONS_SOLVER
    )
    assert section.availability is SnapshotAvailabilityV0.AVAILABLE
    bound = next(item for item in section.facts if item.fact_type == "bound_solver_job")
    assert bound.entity_id == f"job:{status}"
    assert bound.entity_generation == 2


def test_source_authorized_solver_receipts_are_available_without_advancing_job_generation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job_id = "job_receipt"
    _create_submitting_solver_job(store, job_id=job_id)
    _record_solver_receipt(store, job_id, "submitted")
    assert store.solver_job(job_id)["job_revision"] == 1

    submitted = build_project_snapshot_v0(str(store.path))
    assert submitted.availability is SnapshotAvailabilityV0.AVAILABLE
    assert submitted.snapshot is not None
    invocation = next(
        section
        for section in submitted.snapshot.sections
        if section.section_id is SnapshotSectionIdV0.INVOCATIONS_SOLVER
    )
    assert invocation.availability is SnapshotAvailabilityV0.AVAILABLE
    assert any(
        fact.fact_type == SOLVER_RECEIPT_FACT_TYPE and fact.fact_key == f"{job_id}:submitted"
        for fact in invocation.facts
    )

    store.update_solver_job(job_id, expected_job_revision=1, status="completed")
    _record_solver_receipt(store, job_id, "completed")
    assert store.solver_job(job_id)["job_revision"] == 2

    completed = build_project_snapshot_v0(str(store.path))
    assert completed.availability is SnapshotAvailabilityV0.AVAILABLE
    assert completed.snapshot is not None
    invocation = next(
        section
        for section in completed.snapshot.sections
        if section.section_id is SnapshotSectionIdV0.INVOCATIONS_SOLVER
    )
    immutable = next(
        section
        for section in completed.snapshot.sections
        if section.section_id is SnapshotSectionIdV0.IMMUTABLE_REFS
    )
    expected_keys = {f"{job_id}:submitted", f"{job_id}:completed"}
    assert {
        fact.fact_key for fact in invocation.facts if fact.fact_type == SOLVER_RECEIPT_FACT_TYPE
    } == expected_keys
    assert {
        fact.fact_key for fact in immutable.facts if fact.fact_type == SOLVER_RECEIPT_FACT_TYPE
    } == expected_keys


@pytest.mark.parametrize(
    ("event_type", "field", "value"),
    [
        ("SOLVER_JOB_SUBMITTED", "backend", "forged-backend"),
        ("SOLVER_JOB_SUBMITTED", "runtime", "forged-runtime"),
        ("SOLVER_JOB_SUBMITTED", "max_time_seconds", 31),
        ("SOLVER_JOB_SUBMITTED", "idempotency_key", "forged-key"),
        ("SOLVER_JOB_SUBMITTED", "owner_stage", 2),
        ("SOLVER_JOB_SUBMITTED", "owner_subtask", "statement_analysis"),
        ("SOLVER_JOB_SUBMITTED", "owner_revision", 2),
        ("SOLVER_JOB_SUBMITTED", "attempt_id", "forged-attempt"),
    ],
)
def test_solver_submission_event_must_bind_all_recorded_source_fields(
    tmp_path: Path, event_type: str, field: str, value: object
) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_source_binding")
    _mutate_solver_event_payload(
        store, event_type, lambda payload: payload.__setitem__(field, value)
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_row_backend_must_match_submission_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_backend")
    connection = sqlite3.connect(store.path)
    connection.execute(
        "UPDATE solver_jobs SET backend='forged-backend' WHERE job_id='job_backend'"
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_receipt_event_type_must_match_stage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_stage")
    _record_solver_receipt(store, "job_stage", "submitted")
    _mutate_solver_event_payload(
        store,
        "SOLVER_JOB_RECEIPT_SUBMITTED",
        lambda payload: payload.__setitem__("stage", "completed"),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_receipt_event_payload_schema_is_exact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_receipt_schema")
    _record_solver_receipt(store, "job_receipt_schema", "submitted")
    _mutate_solver_event_payload(
        store,
        "SOLVER_JOB_RECEIPT_SUBMITTED",
        lambda payload: payload.__setitem__("untrusted_extra", "forged"),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_receipt_event_must_reference_one_recorded_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_receipt_known")
    _record_solver_receipt(store, "job_receipt_known", "submitted")
    _mutate_solver_event_payload(
        store,
        "SOLVER_JOB_RECEIPT_SUBMITTED",
        lambda payload: payload.__setitem__("job_id", "job_receipt_unknown"),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


@pytest.mark.parametrize("field", ["receipt_sha256", "content_sha256", "request_sha256"])
def test_solver_receipt_hashes_must_be_canonical(
    tmp_path: Path, field: str
) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_hash")
    _record_solver_receipt(store, "job_hash", "submitted")
    _mutate_solver_event_payload(
        store,
        "SOLVER_JOB_RECEIPT_SUBMITTED",
        lambda payload: payload.__setitem__(field, "NOT-A-SHA"),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_completed_receipt_request_must_match_submitted_receipt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_request")
    _record_solver_receipt(store, "job_request", "submitted")
    store.update_solver_job("job_request", expected_job_revision=1, status="completed")
    _record_solver_receipt(
        store, "job_request", "completed", request_sha256="d" * 64
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_receipt_event_order_is_source_authorized(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_order")
    _record_solver_receipt(store, "job_order", "submitted")
    store.update_solver_job("job_order", expected_job_revision=1, status="completed")
    _record_solver_receipt(store, "job_order", "completed")
    connection = sqlite3.connect(store.path)
    _permit_fixture_only_event_corruption(connection)
    rows = connection.execute(
        "SELECT revision,type,payload_json FROM events WHERE type LIKE 'SOLVER_JOB_RECEIPT_%' ORDER BY revision"
    ).fetchall()
    assert len(rows) == 2
    connection.execute(
        "UPDATE events SET type=?, payload_json=? WHERE revision=?",
        (rows[1][1], rows[1][2], rows[0][0]),
    )
    connection.execute(
        "UPDATE events SET type=?, payload_json=? WHERE revision=?",
        (rows[0][1], rows[0][2], rows[1][0]),
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_receipt_conflicting_duplicate_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_duplicate")
    _record_solver_receipt(store, "job_duplicate", "submitted")
    store.update_solver_job("job_duplicate", expected_job_revision=1, status="completed")
    _record_solver_receipt(store, "job_duplicate", "completed")
    connection = sqlite3.connect(store.path)
    _permit_fixture_only_event_corruption(connection)
    submitted = connection.execute(
        "SELECT payload_json FROM events WHERE type='SOLVER_JOB_RECEIPT_SUBMITTED'"
    ).fetchone()
    completed = connection.execute(
        "SELECT revision FROM events WHERE type='SOLVER_JOB_RECEIPT_COMPLETED'"
    ).fetchone()
    payload = json.loads(submitted[0])
    payload["receipt_sha256"] = "d" * 64
    connection.execute(
        "UPDATE events SET type='SOLVER_JOB_RECEIPT_SUBMITTED', payload_json=? WHERE revision=?",
        (json.dumps(payload, sort_keys=True), completed[0]),
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_nonempty_solver_result_refs_are_typed_legacy_unbound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_result_refs")
    store.update_solver_job(
        "job_result_refs",
        expected_job_revision=1,
        status="completed",
        result_refs={
            "result": {"path": "outputs/result.json", "sha256": "a" * 64}
        },
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID
    assert "legacy-unbound" in str(result.error_context)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("script", "../escape.py"),
        ("workdir", "../escape"),
        ("argv_json", '["ok", 1]'),
        ("runtime", ""),
    ],
)
def test_solver_row_authoritative_legacy_fields_are_strictly_validated(
    tmp_path: Path, column: str, value: object
) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_legacy_row")
    connection = sqlite3.connect(store.path)
    connection.execute(
        f'UPDATE solver_jobs SET "{column}"=? WHERE job_id="job_legacy_row"',
        (value,),
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_external_id_binds_latest_nonempty_lifecycle_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_external")
    store.update_solver_job(
        "job_external",
        expected_job_revision=1,
        status="submitting",
        external_id="external:1",
    )
    store.update_solver_job(
        "job_external",
        expected_job_revision=2,
        status="queued",
    )

    valid = build_project_snapshot_v0(str(store.path))
    assert valid.availability is SnapshotAvailabilityV0.AVAILABLE

    connection = sqlite3.connect(store.path)
    connection.execute(
        "UPDATE solver_jobs SET external_id='external:forged' WHERE job_id='job_external'"
    )
    connection.commit()
    connection.close()
    forged = build_project_snapshot_v0(str(store.path))
    assert forged.availability is SnapshotAvailabilityV0.ERROR
    assert forged.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_failure_binds_terminal_lifecycle_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_failure")
    store.update_solver_job(
        "job_failure",
        expected_job_revision=1,
        status="failed",
        failure={"type": "SyntheticFailure", "message": "synthetic"},
    )
    valid = build_project_snapshot_v0(str(store.path))
    assert valid.availability is SnapshotAvailabilityV0.AVAILABLE

    connection = sqlite3.connect(store.path)
    connection.execute(
        "UPDATE solver_jobs SET failure_json=? WHERE job_id='job_failure'",
        (json.dumps({"type": "ForgedFailure", "message": "synthetic"}),),
    )
    connection.commit()
    connection.close()
    forged = build_project_snapshot_v0(str(store.path))
    assert forged.availability is SnapshotAvailabilityV0.ERROR
    assert forged.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_receipt_path_must_bind_job_and_stage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_receipt_path")
    _record_solver_receipt(store, "job_receipt_path", "submitted")
    _mutate_solver_event_payload(
        store,
        "SOLVER_JOB_RECEIPT_SUBMITTED",
        lambda payload: payload.__setitem__(
            "receipt_path", ".factory/solver_receipts/other.submitted.json"
        ),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_active_solver_job_cannot_publish_completed_receipt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_active_completed")
    _record_solver_receipt(store, "job_active_completed", "submitted")
    _record_solver_receipt(store, "job_active_completed", "completed")

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID
    assert "active solver job" in str(result.error_context)


@pytest.mark.parametrize(
    "job_id",
    ["../escape", "nested/job", r"nested\job", ".", ".."],
)
def test_solver_job_id_is_a_source_authorized_non_path_identifier(
    tmp_path: Path, job_id: str
) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id=job_id)

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


@pytest.mark.parametrize(
    "receipt_path",
    [
        r".factory\solver_receipts\job_path.submitted.json",
        "/.factory/solver_receipts/job_path.submitted.json",
        ".factory/solver_receipts/./job_path.submitted.json",
        ".factory/solver_receipts//job_path.submitted.json",
    ],
)
def test_solver_receipt_path_requires_exact_safe_posix_source_form(
    tmp_path: Path, receipt_path: str
) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_path")
    store.record_solver_receipt(
        "job_path",
        stage="submitted",
        receipt_path=receipt_path,
        receipt_sha256="a" * 64,
        content_sha256="b" * 64,
        request_sha256="c" * 64,
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code in {
        SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID,
        SnapshotErrorCodeV0.IMMUTABLE_REF_CONTRACT_INVALID,
    }


def test_terminal_lifecycle_precedes_completed_receipt_for_valid_complex_job_id(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job_id = "job:valid_id-1"
    _create_submitting_solver_job(store, job_id=job_id)
    _record_solver_receipt(store, job_id, "submitted")
    store.update_solver_job(job_id, expected_job_revision=1, status="completed")
    _record_solver_receipt(store, job_id, "completed")

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.AVAILABLE
    assert result.snapshot is not None
    invocation = next(
        section
        for section in result.snapshot.sections
        if section.section_id is SnapshotSectionIdV0.INVOCATIONS_SOLVER
    )
    assert invocation.availability is SnapshotAvailabilityV0.AVAILABLE
    assert {
        fact.fact_key
        for fact in invocation.facts
        if fact.fact_type == SOLVER_RECEIPT_FACT_TYPE
    } == {f"{job_id}:submitted", f"{job_id}:completed"}


def test_completed_receipt_must_follow_terminal_lifecycle_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_terminal_order")
    _record_solver_receipt(store, "job_terminal_order", "submitted")
    store.update_solver_job(
        "job_terminal_order", expected_job_revision=1, status="completed"
    )
    _record_solver_receipt(store, "job_terminal_order", "completed")
    connection = sqlite3.connect(store.path)
    _permit_fixture_only_event_corruption(connection)
    terminal_revision = connection.execute(
        "SELECT revision FROM events WHERE type='SOLVER_JOB_COMPLETED'"
    ).fetchone()[0]
    receipt_revision = connection.execute(
        "SELECT revision FROM events WHERE type='SOLVER_JOB_RECEIPT_COMPLETED'"
    ).fetchone()[0]
    terminal_payload = connection.execute(
        "SELECT payload_json FROM events WHERE revision=?", (terminal_revision,)
    ).fetchone()[0]
    receipt_payload = connection.execute(
        "SELECT payload_json FROM events WHERE revision=?", (receipt_revision,)
    ).fetchone()[0]
    connection.execute(
        "UPDATE events SET type='SOLVER_JOB_RECEIPT_COMPLETED',payload_json=? WHERE revision=?",
        (receipt_payload, terminal_revision),
    )
    connection.execute(
        "UPDATE events SET type='SOLVER_JOB_COMPLETED',payload_json=? WHERE revision=?",
        (terminal_payload, receipt_revision),
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_submitted_receipt_must_follow_solver_submission_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_submission_order")
    _record_solver_receipt(store, "job_submission_order", "submitted")
    connection = sqlite3.connect(store.path)
    _permit_fixture_only_event_corruption(connection)
    submission = connection.execute(
        "SELECT revision,payload_json FROM events WHERE type='SOLVER_JOB_SUBMITTED'"
    ).fetchone()
    receipt = connection.execute(
        "SELECT revision,payload_json FROM events WHERE type='SOLVER_JOB_RECEIPT_SUBMITTED'"
    ).fetchone()
    connection.execute(
        "UPDATE events SET type='SOLVER_JOB_RECEIPT_SUBMITTED',payload_json=? WHERE revision=?",
        (receipt[1], submission[0]),
    )
    connection.execute(
        "UPDATE events SET type='SOLVER_JOB_SUBMITTED',payload_json=? WHERE revision=?",
        (submission[1], receipt[0]),
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


@pytest.mark.parametrize(
    "unsafe_path",
    ["../outside.json", "/absolute.json", r"nested\windows.json", "a//b.json", "a/./b.json"],
)
def test_generic_immutable_ref_path_is_safe_project_relative_posix(
    tmp_path: Path, unsafe_path: str
) -> None:
    store = _store(tmp_path)
    snapshot_json = {
        "project_id": "fixture:m03",
        "artifact_ref": {"path": unsafe_path, "sha256": "a" * 64},
    }
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO projector_snapshots(projector_name,projector_version,through_revision,state_hash,through_event_id,through_event_payload_sha256,source_chain_root_sha256,snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "workflow",
            1,
            0,
            canonical_hash(snapshot_json),
            None,
            None,
            None,
            json.dumps(snapshot_json, sort_keys=True),
            1,
        ),
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.AVAILABLE
    assert result.snapshot is not None
    immutable = next(
        section
        for section in result.snapshot.sections
        if section.section_id is SnapshotSectionIdV0.IMMUTABLE_REFS
    )
    assert immutable.availability is SnapshotAvailabilityV0.ERROR
    assert immutable.error_code is SnapshotErrorCodeV0.IMMUTABLE_REF_CONTRACT_INVALID


def test_event_head_fact_uses_explicit_source_authorized_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.AVAILABLE
    assert result.snapshot is not None
    event_head = next(
        section
        for section in result.snapshot.sections
        if section.section_id is SnapshotSectionIdV0.EVENT_HEAD_CHAIN
    )
    assert event_head.availability is SnapshotAvailabilityV0.AVAILABLE
    assert [(fact.fact_type, fact.fact_key) for fact in event_head.facts] == [
        (EVENT_HEAD_FACT_TYPE, "project")
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("event_version", 999, "version"),
        ("event_id", "forged-event-id", "identity"),
        ("canonical_type", "FORGED_EVENT_TYPE", "canonical type"),
        ("state_hash_after", "0" * 64, "after-state hash"),
    ],
)
def test_event_envelope_identity_and_replay_fields_fail_closed(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    store = _store(tmp_path)
    _mutate_solver_event_payload(
        store,
        "PROJECT_CREATED",
        lambda payload: payload["_workflow"].__setitem__(field, value),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert message in str(result.error_context)


def test_event_state_patch_runtime_shape_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _mutate_solver_event_payload(
        store,
        "PROJECT_CREATED",
        lambda payload: payload["_workflow"].__setitem__("state_patch", []),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert "state patch" in str(result.error_context)


def test_event_before_state_hash_is_verified_across_revisions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_before_hash")
    _mutate_solver_event_payload(
        store,
        "SOLVER_JOB_SUBMITTED",
        lambda payload: payload["_workflow"].__setitem__(
            "state_hash_before", "0" * 64
        ),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert "before-state hash" in str(result.error_context)


def test_event_replay_final_state_must_match_project_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute(
        "UPDATE project_state SET project_type='forged-project-type' WHERE singleton=1"
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert "final state" in str(result.error_context)


def test_legacy_event_without_workflow_envelope_is_not_available(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _mutate_solver_event_payload(
        store,
        "PROJECT_CREATED",
        lambda payload: payload.pop("_workflow"),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert "legacy-unbound" in str(result.error_context)


def test_event_domain_effect_values_are_canonical_sha256(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        effects = dict(payload["effect_hashes_after"])
        effects["solver_jobs"] = "not-a-sha"
        payload["effect_hashes_after"] = effects
        payload["_workflow"]["effect_hashes_after"] = dict(effects)

    _mutate_solver_event_payload(store, "PROJECT_CREATED", mutate)

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert "not SHA-256" in str(result.error_context)


def test_event_domain_aggregate_is_recomputed(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["aggregate_root_hash_after"] = "0" * 64
        payload["_workflow"]["aggregate_root_hash_after"] = "0" * 64

    _mutate_solver_event_payload(store, "PROJECT_CREATED", mutate)

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert "aggregate domain root" in str(result.error_context)


def test_event_top_level_and_envelope_domain_roots_are_identical(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        effects = dict(payload["effect_hashes_after"])
        effects["solver_jobs"] = "f" * 64
        payload["effect_hashes_after"] = effects

    _mutate_solver_event_payload(store, "PROJECT_CREATED", mutate)

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert "top-level and envelope" in str(result.error_context)


def test_current_event_head_domain_root_binds_same_transaction_business_rows(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO contest_policy(singleton,profile,contest_started_at,contest_deadline_at,content_freeze_at,delivery_freeze_at,delivery_reserve_seconds) VALUES (1,'forged-profile',1,100,80,90,10)"
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID
    assert "current business rows" in str(result.error_context)


def test_receipt_domain_root_top_level_must_match_workflow_envelope(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job_receipt_root")
    _record_solver_receipt(store, "job_receipt_root", "submitted")
    _mutate_solver_event_payload(
        store,
        "SOLVER_JOB_RECEIPT_SUBMITTED",
        lambda payload: payload.__setitem__("effect_hashes_after", {}),
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID


def test_unknown_solver_status_from_storage_update_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job:unknown-status")
    store.update_solver_job(
        "job:unknown-status",
        expected_job_revision=1,
        status="paused",
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID


def test_unknown_solver_lifecycle_event_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job:unknown-event")
    store.update_solver_job(
        "job:unknown-event",
        expected_job_revision=1,
        status="queued",
    )
    connection = sqlite3.connect(store.path)
    _permit_fixture_only_event_corruption(connection)
    connection.execute(
        "UPDATE events SET type='SOLVER_JOB_PAUSED' WHERE type='SOLVER_JOB_QUEUED'"
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.EVENT_CHAIN_INVALID


def test_solver_lifecycle_generation_gap_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_submitting_solver_job(store, job_id="job:generation-gap")
    store.update_solver_job(
        "job:generation-gap",
        expected_job_revision=1,
        status="queued",
    )
    connection = sqlite3.connect(store.path)
    _permit_fixture_only_event_corruption(connection)
    row = connection.execute(
        "SELECT revision,payload_json FROM events WHERE type='SOLVER_JOB_QUEUED'"
    ).fetchone()
    payload = json.loads(row[1])
    payload["job_revision"] = 3
    connection.execute(
        "UPDATE events SET payload_json=? WHERE revision=?",
        (json.dumps(payload, sort_keys=True), row[0]),
    )
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_solver_status_must_match_latest_lifecycle_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.load()
    store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": "job:status",
            "backend": "local",
            "runtime": "python",
            "script": "solve.py",
            "workdir": ".",
            "argv": [],
            "max_time_seconds": 30,
            "status": "submitting",
            "result_refs": {},
        },
    )
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE solver_jobs SET status='completed' WHERE job_id='job:status'")
    connection.commit()
    connection.close()

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID


def test_ordinary_path_cannot_be_upgraded_to_immutable_reference(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = {
        "project_id": "fixture:m03",
        "artifact_ref": "/tmp/must-not-be-opened",
    }
    state_hash = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO projector_snapshots(projector_name,projector_version,through_revision,state_hash,snapshot_json,created_at) VALUES ('workflow',1,0,?,?,1)",
        (state_hash, json.dumps(snapshot)),
    )
    connection.commit()
    connection.close()
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.AVAILABLE
    assert result.snapshot is not None
    section = next(
        item for item in result.snapshot.sections if item.section_id is SnapshotSectionIdV0.IMMUTABLE_REFS
    )
    assert section.availability is SnapshotAvailabilityV0.ERROR
    assert section.error_code is SnapshotErrorCodeV0.IMMUTABLE_REF_CONTRACT_INVALID


def test_solver_result_path_requires_an_immutable_hash_binding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.load()
    store.create_solver_job(
        expected_revision=state.revision,
        record={
            "job_id": "job:ordinary-ref",
            "backend": "local",
            "runtime": "python",
            "script": "solve.py",
            "workdir": ".",
            "argv": [],
            "max_time_seconds": 30,
            "status": "submitting",
            "result_refs": {"stdout": "logs/stdout.log"},
        },
    )

    result = build_project_snapshot_v0(str(store.path))

    assert result.availability is SnapshotAvailabilityV0.ERROR
    assert result.snapshot is None
    assert result.error_code is SnapshotErrorCodeV0.SOLVER_FACT_CONTRACT_INVALID
    assert "legacy-unbound" in str(result.error_context)


def test_redacted_and_paged_section_metadata_cannot_masquerade_as_empty_available(
    tmp_path: Path,
) -> None:
    result = build_project_snapshot_v0(str(_store(tmp_path).path))
    assert result.snapshot is not None
    snapshot = result.snapshot
    config_index = next(
        index
        for index, section in enumerate(snapshot.sections)
        if section.section_id is SnapshotSectionIdV0.CONFIG_POLICY
    )
    base = snapshot.sections[config_index]
    redacted = replace(
        base,
        availability=SnapshotAvailabilityV0.REDACTED,
        facts=(),
        policy_id="policy:redacted:test",
    )
    sections = snapshot.sections[:config_index] + (redacted,) + snapshot.sections[config_index + 1 :]
    validate_project_snapshot_v0(replace(snapshot, sections=sections))
    with pytest.raises(SnapshotV0ValidationError):
        validate_project_snapshot_v0(
            replace(snapshot, sections=snapshot.sections[:config_index] + (replace(redacted, policy_id=None),) + snapshot.sections[config_index + 1 :])
        )
    paged = replace(redacted, availability=SnapshotAvailabilityV0.PAGED, policy_id=None, page_cursor="a" * 64)
    validate_project_snapshot_v0(
        replace(snapshot, sections=snapshot.sections[:config_index] + (paged,) + snapshot.sections[config_index + 1 :])
    )


def test_large_dirty_history_is_explicitly_paged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.executemany(
        "INSERT INTO dirty_causes(cause_id,flag,owner_stage,cause_revision,cause_artifact,baseline_fingerprint,current_fingerprint,classifier_contract_sha256) VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                f"cause:{index:04d}",
                "MATH_DIRTY",
                8,
                1,
                f"artifact:{index:04d}",
                "a" * 64,
                "b" * 64,
                "c" * 64,
            )
            for index in range(257)
        ],
    )
    connection.commit()
    connection.close()
    state = store.load()
    store.transition(
        expected_revision=state.revision,
        event_type="RUNNER_INTERRUPTED",
        changes={},
        payload={"reason": "fixture domain root established"},
    )
    result = build_project_snapshot_v0(str(store.path))
    assert result.snapshot is not None
    section = next(
        item for item in result.snapshot.sections if item.section_id is SnapshotSectionIdV0.DIRTY_FACTS
    )
    assert section.availability is SnapshotAvailabilityV0.PAGED
    assert section.page_cursor is not None
    assert section.facts == ()


def test_recorded_immutable_reference_target_is_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT INTO projector_snapshots(projector_name,projector_version,through_revision,state_hash,through_event_id,through_event_payload_sha256,source_chain_root_sha256,snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "refs",
            1,
            1,
            "a" * 64,
            "event:1",
            "b" * 64,
            "c" * 64,
            '{"artifact_ref":"/tmp/must-not-be-opened"}',
            1,
        ),
    )
    connection.commit()
    connection.close()

    def forbidden_read(_self):
        raise AssertionError("snapshot builder read a referenced filesystem target")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    result = build_project_snapshot_v0(str(store.path))
    assert result.availability is SnapshotAvailabilityV0.AVAILABLE
    assert result.snapshot is not None
    section = next(
        item
        for item in result.snapshot.sections
        if item.section_id is SnapshotSectionIdV0.IMMUTABLE_REFS
    )
    assert section.availability is SnapshotAvailabilityV0.ERROR
    assert section.error_code is SnapshotErrorCodeV0.IMMUTABLE_REF_CONTRACT_INVALID


def test_uninitialized_exact_dto_and_subclasses_fail_serializer_boundary(tmp_path: Path) -> None:
    result = build_project_snapshot_v0(str(_store(tmp_path).path))
    assert result.snapshot is not None
    uninitialized = object.__new__(ProjectSnapshotV0)
    with pytest.raises(SnapshotV0ValidationError):
        validate_project_snapshot_v0(uninitialized)
    subclass = type("ForgedSnapshot", (ProjectSnapshotV0,), {})
    forged = object.__new__(subclass)
    for item in result.snapshot.__dataclass_fields__:
        object.__setattr__(forged, item, getattr(result.snapshot, item))
    with pytest.raises(SnapshotV0ValidationError):
        project_snapshot_v0_semantic_sha256(forged)
    uninitialized_result = object.__new__(SnapshotBuildResult)
    with pytest.raises(SnapshotV0ValidationError):
        validate_snapshot_build_result(uninitialized_result)


def test_sqlite_read_transaction_observes_all_n_or_all_n_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    original = snapshot_module._read_coordinate
    first_read = threading.Event()
    writer_done = threading.Event()
    calls = 0

    def barrier(connection, expected_project_id):
        nonlocal calls
        value = original(connection, expected_project_id)
        calls += 1
        if calls == 1:
            first_read.set()
            assert writer_done.wait(timeout=5)
        return value

    monkeypatch.setattr(snapshot_module, "_read_coordinate", barrier)
    captured: list[object] = []

    def reader():
        captured.append(build_project_snapshot_v0(str(store.path)))

    thread = threading.Thread(target=reader)
    thread.start()
    assert first_read.wait(timeout=5)
    store.transition(
        expected_revision=1,
        event_type="PAUSED",
        changes={"status": WorkflowStatus.PAUSED},
    )
    writer_done.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    result = captured[0]
    assert isinstance(result, SnapshotBuildResult)
    assert result.snapshot is not None
    assert result.snapshot.coordinate.project_revision == 1
    later = build_project_snapshot_v0(str(store.path))
    assert later.snapshot is not None
    assert later.snapshot.coordinate.project_revision == 2
