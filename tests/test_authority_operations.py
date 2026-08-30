from __future__ import annotations

import ast
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from factory_core.authority_operations import (
    AUTHORITY_PRIMARY,
    CANARY,
    V1_ONLY,
    AuthorityHealthPolicy,
    AuthorityOperationConflict,
    AuthorityOperations,
    AuthorityRestoreNotQuiet,
    RestoreEvidence,
    create_authority_backup,
    decide_auto_fallback,
    evaluate_authority_health,
    initial_database_identity_binding,
    preflight_authority_restore,
    restore_authority_backup,
)
from factory_core.authority_operator_workflow import run_authority_restore_operation
from factory_core.authority_production_schema import (
    AuthorityProductionMigrationRunner,
    migrate_authority_production_foundation,
    production_preflight,
)
from factory_core.authority_schema import migrate_authority_schema_v2
from factory_core.stages import STAGE_SCHEDULER_GENERATION
from factory_core.storage import SQLiteStateStore
from tests.support.authority_production import (
    configure_canary,
    create_real_schema_v9,
    install_foundation,
    persist_one,
    prepare_initial_backup,
)


ROOT = Path(__file__).resolve().parents[1]
OPERATOR = ROOT / "scripts" / "authority_operator.py"


def _policy(**overrides):
    values = {
        "max_backlog_depth": 10,
        "max_oldest_pending_age": 1000,
        "max_in_flight": 10,
        "max_expired_claims": 10,
        "max_retry_per_thousand": 1000,
        "max_dead_letter_count": 10,
        "max_backup_age": 1000,
        "require_restore_evidence": False,
    }
    values.update(overrides)
    return AuthorityHealthPolicy(**values)


def test_writer_and_consumer_configuration_use_explicit_epoch_cas_and_receipts(tmp_path):
    fixture = install_foundation(tmp_path)
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    writer = operations.configure_writer(
        new_writer_id="writer-a", enabled=True,
        expected_writer_epoch=0, expected_switch_epoch=0,
        operator_subject="operator-a", reason="configure writer", occurred_at=1000,
    )
    assert writer.changed and writer.writer_epoch == 1
    with pytest.raises(AuthorityOperationConflict, match="stale"):
        operations.configure_writer(
            new_writer_id="writer-b", enabled=True,
            expected_writer_epoch=0, expected_switch_epoch=0,
            operator_subject="operator-a", reason="stale handoff", occurred_at=1001,
        )
    consumer = operations.configure_consumer(
        new_consumer_id="consumer-a", enabled=True,
        expected_consumer_epoch=0, expected_switch_epoch=0,
        operator_subject="operator-a", reason="configure consumer", occurred_at=1002,
    )
    assert consumer.changed and consumer.consumer_epoch == 1
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM authority_production_control_receipts"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_switch_state_machine_requires_canary_and_never_automatically_advances(tmp_path):
    fixture = install_foundation(tmp_path)
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    with pytest.raises(AuthorityOperationConflict, match="unsupported switch transition"):
        operations.switch_mode(
            target_mode=AUTHORITY_PRIMARY,
            expected_switch_epoch=0,
            operator_subject="operator-a", reason="skip canary", occurred_at=1000,
        )
    configure_canary(fixture)
    promoted = operations.switch_mode(
        target_mode=AUTHORITY_PRIMARY,
        expected_switch_epoch=1,
        operator_subject="operator-a", reason="explicit promotion evidence", occurred_at=1001,
    )
    assert promoted.switch_mode == AUTHORITY_PRIMARY and promoted.switch_epoch == 2
    stopped = operations.switch_mode(
        target_mode=V1_ONLY,
        expected_switch_epoch=2,
        operator_subject="operator-a", reason="explicit rollback", occurred_at=1002,
    )
    assert stopped.switch_mode == V1_ONLY
    assert not stopped.writer_enabled and not stopped.consumer_enabled


def test_health_is_read_only_and_uses_only_explicit_thresholds_and_backup_time(tmp_path):
    fixture = install_foundation(tmp_path, with_backup=True)
    assert fixture.backup is not None
    writer = configure_canary(fixture)
    persist_one(writer, occurred_at=1200)
    healthy = evaluate_authority_health(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        policy=_policy(),
        evaluated_at=1200,
        backup_evidence=fixture.backup,
    )
    assert healthy.healthy
    assert healthy.backlog_depth == 1
    assert healthy.backup_age == 200
    strict = evaluate_authority_health(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        policy=_policy(max_backlog_depth=0, max_backup_age=100),
        evaluated_at=1200,
        backup_evidence=fixture.backup,
    )
    assert strict.hard_conditions == ("BACKLOG_DEPTH_EXCEEDED", "BACKUP_STALE")


def test_health_reports_dead_letter_and_expired_claim_metrics(tmp_path):
    fixture = install_foundation(tmp_path, with_backup=True)
    assert fixture.backup is not None
    writer = configure_canary(fixture)
    persist_one(writer, occurred_at=1200)
    connection = sqlite3.connect(fixture.database)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        """
        UPDATE authority_production_outbox_delivery_state
        SET status='CLAIMED', attempt_count=3, lease_expires_at=1200,
            claim_consumer_id='consumer-a', claim_consumer_epoch=1,
            claim_epoch=1, updated_at=1100
        """
    )
    connection.commit()
    connection.close()
    report = evaluate_authority_health(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        policy=_policy(max_expired_claims=0, max_retry_per_thousand=0),
        evaluated_at=1200,
        backup_evidence=fixture.backup,
    )
    assert "EXPIRED_CLAIM_EXCEEDED" in report.hard_conditions
    assert "RETRY_RATE_EXCEEDED" in report.hard_conditions

    connection = sqlite3.connect(fixture.database)
    connection.execute(
        "UPDATE authority_production_outbox_delivery_state SET status='DEAD_LETTER'"
    )
    connection.commit()
    connection.close()
    dead = evaluate_authority_health(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        policy=_policy(max_dead_letter_count=0),
        evaluated_at=1201,
        backup_evidence=fixture.backup,
    )
    assert "DEAD_LETTER_EXCEEDED" in dead.hard_conditions


def test_health_validates_backup_freshness_and_restore_evidence_binding(tmp_path):
    fixture = install_foundation(tmp_path, with_backup=True)
    assert fixture.backup is not None
    valid_restore = RestoreEvidence(
        fixture.backup.database_id,
        1100,
        0,
        fixture.backup.source_fence_sha256,
        fixture.backup.backup_sha256,
        fixture.backup.backup_sha256,
        fixture.backup.backup_size,
        "ok",
        fixture.backup.authority_state,
        initial_database_identity_binding(fixture.backup).binding_sha256,
        fixture.backup.lineage_sha256,
    )
    healthy = evaluate_authority_health(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        policy=_policy(require_restore_evidence=True),
        evaluated_at=1200,
        backup_evidence=fixture.backup,
        restore_evidence=valid_restore,
    )
    assert healthy.healthy

    future_backup = replace(fixture.backup, occurred_at=1201)
    wrong_backup_restore = replace(
        valid_restore,
        backup_sha256="a" * 64,
        restored_sha256="a" * 64,
    )
    invalid = evaluate_authority_health(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        policy=_policy(require_restore_evidence=True),
        evaluated_at=1200,
        backup_evidence=future_backup,
        restore_evidence=wrong_backup_restore,
    )
    assert "BACKUP_EVIDENCE_INVALID" in invalid.hard_conditions
    assert "RESTORE_EVIDENCE_INVALID" in invalid.hard_conditions


def test_cross_database_backup_lineage_is_rejected_by_health_and_restore(tmp_path):
    def configured_database(name: str, mode: str, threshold: int):
        project = tmp_path / name
        store = SQLiteStateStore(project, clock=lambda: 100)
        state = store.initialize(
            project_id="same-project",
            project_type="modeling",
            last_completed_step=1,
            scheduler_generation=STAGE_SCHEDULER_GENERATION,
        )
        store.configure_solver_policy(
            expected_revision=state.revision,
            mode=mode,
            threshold_seconds=threshold,
            allowed_runtimes=["python"],
        )
        return store.path

    database_a = configured_database("identity-a", "local", 111)
    database_b = configured_database("identity-b", "cloud", 222)
    preflight_a = production_preflight(database_a, database_id="a-db")
    preflight_b = production_preflight(database_b, database_id="b-db")
    assert preflight_a.source_fence_sha256 == preflight_b.source_fence_sha256
    assert (
        preflight_a.source_database_content_sha256
        != preflight_b.source_database_content_sha256
    )
    backup_a_path = tmp_path / "identity-a.backup.db"
    backup_b_path = tmp_path / "identity-b.backup.db"
    backup_a = create_authority_backup(
        database_a,
        backup_a_path,
        database_id="a-db",
        occurred_at=1000,
        expected_source_fence_sha256=preflight_a.source_fence_sha256,
    )
    backup_b = create_authority_backup(
        database_b,
        backup_b_path,
        database_id="b-db",
        occurred_at=1000,
        expected_source_fence_sha256=preflight_b.source_fence_sha256,
    )
    migrate_authority_schema_v2(database_a, owner_token="identity-a-base")
    migrate_authority_production_foundation(
        database_a,
        database_id="a-db",
        expected_source_fence_sha256=preflight_a.source_fence_sha256,
        owner_token="identity-a-production",
        identity_binding=initial_database_identity_binding(backup_a),
        pre_authority_backup=backup_a_path,
    )

    report = evaluate_authority_health(
        database_a,
        expected_source_fence_sha256=preflight_a.source_fence_sha256,
        policy=_policy(),
        evaluated_at=1100,
        backup_evidence=backup_b,
    )
    assert "BACKUP_DATABASE_IDENTITY_MISMATCH" in report.hard_conditions
    assert decide_auto_fallback(CANARY, report).action == "FALLBACK_TO_V1"

    before = database_a.read_bytes()
    with pytest.raises(AuthorityOperationConflict, match="persisted database lineage"):
        preflight_authority_restore(
            database_a,
            backup_b_path,
            database_id="a-db",
            expected_current_source_fence_sha256=preflight_a.source_fence_sha256,
            expected_backup_sha256=backup_b.backup_sha256,
            expected_switch_epoch=0,
        )
    assert database_a.read_bytes() == before
    connection = sqlite3.connect(database_a)
    try:
        assert connection.execute(
            "SELECT solver_mode, solver_threshold_seconds FROM project_config"
        ).fetchone() == ("local", 111)
    finally:
        connection.close()


def test_source_fence_drift_triggers_durable_one_way_auto_fallback(tmp_path):
    fixture = install_foundation(tmp_path)
    configure_canary(fixture)
    connection = sqlite3.connect(fixture.database)
    connection.execute("UPDATE project_state SET status='paused'")
    connection.commit()
    connection.close()
    health = evaluate_authority_health(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        policy=_policy(),
        evaluated_at=1300,
    )
    assert "SOURCE_FENCE_DRIFT" in health.hard_conditions
    decision = decide_auto_fallback(CANARY, health)
    assert decision.action == "FALLBACK_TO_V1"
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    applied = operations.apply_auto_fallback(
        decision,
        expected_switch_epoch=1,
        operator_subject="auto-fallback-controller",
        reason="source fence health condition",
        occurred_at=1301,
    )
    assert applied.changed and applied.switch_mode == V1_ONLY
    connection = sqlite3.connect(fixture.database)
    try:
        receipt = json.loads(
            connection.execute(
                "SELECT receipt_json FROM authority_production_control_receipts "
                "WHERE receipt_id=?",
                (applied.receipt_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert receipt["evidence"]["decision_sha256"] == decision.decision_sha256
    assert receipt["evidence"]["decision"]["hard_conditions"] == list(
        decision.hard_conditions
    )
    replay = operations.apply_auto_fallback(
        decision,
        expected_switch_epoch=2,
        operator_subject="auto-fallback-controller",
        reason="idempotent source fence fallback",
        occurred_at=1302,
    )
    assert replay.changed is False and replay.switch_mode == V1_ONLY
    with pytest.raises(AuthorityOperationConflict, match="stale"):
        operations.apply_auto_fallback(
            decision,
            expected_switch_epoch=1,
            operator_subject="auto-fallback-controller",
            reason="stale fallback",
            occurred_at=1303,
        )
    forged_no_change = replace(decision, action="NO_CHANGE", target_mode=CANARY)
    with pytest.raises(AuthorityOperationConflict, match="internally inconsistent"):
        operations.apply_auto_fallback(
            forged_no_change,
            expected_switch_epoch=2,
            operator_subject="auto-fallback-controller",
            reason="forged no-change decision",
            occurred_at=1304,
        )


def test_migration_interruption_and_invalid_restore_evidence_are_hard_health_conditions(tmp_path):
    _project, database = create_real_schema_v9(tmp_path)
    preflight = production_preflight(database, database_id="interrupted-db")
    _backup, backup_path, binding = prepare_initial_backup(
        tmp_path,
        database,
        preflight,
        database_id="interrupted-db",
        name="interrupted",
    )
    migrate_authority_schema_v2(database, owner_token="base-owner")

    def stop(_migration_id: str) -> None:
        raise RuntimeError("stop after first migration")

    with pytest.raises(Exception):
        AuthorityProductionMigrationRunner(
            database,
            database_id="interrupted-db",
            expected_source_fence_sha256=preflight.source_fence_sha256,
            identity_binding=binding,
            pre_authority_backup=backup_path,
            after_migration=stop,
        ).run("production-owner")
    report = evaluate_authority_health(
        database,
        expected_source_fence_sha256=preflight.source_fence_sha256,
        policy=_policy(),
        evaluated_at=1400,
    )
    assert "SCHEMA_OR_MIGRATION_NOT_READY" in report.hard_conditions

    fixture = install_foundation(tmp_path, name="invalid-restore", with_backup=True)
    assert fixture.backup is not None
    invalid = RestoreEvidence(
        "invalid-restore-db", 1300, 0,
        fixture.preflight.source_fence_sha256,
        "1" * 64, "2" * 64, 1, "ok", "ABSENT",
        "3" * 64, "4" * 64,
    )
    invalid_report = evaluate_authority_health(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        policy=_policy(require_restore_evidence=True),
        evaluated_at=1400,
        backup_evidence=fixture.backup,
        restore_evidence=invalid,
    )
    assert "RESTORE_EVIDENCE_INVALID" in invalid_report.hard_conditions


def test_future_production_schema_is_a_fail_closed_health_condition(tmp_path):
    fixture = install_foundation(tmp_path)
    connection = sqlite3.connect(fixture.database)
    connection.execute(
        "UPDATE authority_production_schema_state "
        "SET production_schema_version=3 WHERE singleton=1"
    )
    connection.commit()
    connection.close()

    report = evaluate_authority_health(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        policy=_policy(),
        evaluated_at=1450,
    )

    assert report.healthy is False
    assert "SCHEMA_OR_MIGRATION_NOT_READY" in report.hard_conditions


def test_restore_requires_v1_only_disabled_writer_consumer_and_exact_epoch(tmp_path):
    fixture = install_foundation(tmp_path, with_backup=True)
    assert fixture.backup is not None and fixture.backup_path is not None
    configure_canary(fixture)
    with pytest.raises(AuthorityRestoreNotQuiet):
        restore_authority_backup(
            fixture.database, fixture.backup_path,
            database_id="authority-project-db", occurred_at=1500,
            expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
            expected_backup_sha256=fixture.backup.backup_sha256,
            expected_switch_epoch=1,
        )
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    operations.switch_mode(
        target_mode=V1_ONLY, expected_switch_epoch=1,
        operator_subject="operator-a", reason="prepare restore", occurred_at=1501,
    )
    with pytest.raises(AuthorityOperationConflict, match="switch epoch"):
        restore_authority_backup(
            fixture.database, fixture.backup_path,
            database_id="authority-project-db", occurred_at=1502,
            expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
            expected_backup_sha256=fixture.backup.backup_sha256,
            expected_switch_epoch=1,
        )


def test_restore_evidence_collision_fails_before_database_replacement(tmp_path):
    fixture = install_foundation(tmp_path, name="restore-collision", with_backup=True)
    assert fixture.backup is not None and fixture.backup_path is not None
    evidence = tmp_path / "restore-collision.evidence.json"
    evidence.write_text("occupied\n", encoding="utf-8", newline="\n")
    before = fixture.database.read_bytes()

    with pytest.raises(AuthorityOperationConflict, match="not reusable"):
        run_authority_restore_operation(
            fixture.database,
            fixture.backup_path,
            database_id="restore-collision-db",
            occurred_at=1550,
            expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
            expected_backup_sha256=fixture.backup.backup_sha256,
            expected_switch_epoch=0,
            evidence_output=evidence,
        )

    assert fixture.database.read_bytes() == before
    assert evidence.read_text(encoding="utf-8") == "occupied\n"


@pytest.mark.parametrize(
    "crash_stage",
    (
        "after_restore_publish",
        "after_restore_verification",
        "after_restore_journaled",
        "before_external_evidence_publish",
    ),
)
def test_restore_operation_replays_every_post_replace_crash_window(
    tmp_path, crash_stage
):
    suffix = crash_stage.replace("_", "-")
    name = f"restore-replay-{suffix}"
    fixture = install_foundation(tmp_path, name=name, with_backup=True)
    assert fixture.backup is not None and fixture.backup_path is not None
    evidence_path = tmp_path / f"{suffix}.restore.evidence.json"

    def interrupt(stage: str) -> None:
        if stage == crash_stage:
            raise RuntimeError(f"crash at {stage}")

    with pytest.raises(RuntimeError, match="crash at"):
        run_authority_restore_operation(
            fixture.database,
            fixture.backup_path,
            database_id=f"{name}-db",
            occurred_at=1560,
            expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
            expected_backup_sha256=fixture.backup.backup_sha256,
            expected_switch_epoch=0,
            evidence_output=evidence_path,
            failure_injector=interrupt,
        )

    assert fixture.database.read_bytes() == fixture.backup_path.read_bytes()
    assert evidence_path.exists()
    completed = run_authority_restore_operation(
        fixture.database,
        fixture.backup_path,
        database_id=f"{name}-db",
        occurred_at=1560,
        expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
        expected_backup_sha256=fixture.backup.backup_sha256,
        expected_switch_epoch=0,
        evidence_output=evidence_path,
    )
    replay = run_authority_restore_operation(
        fixture.database,
        fixture.backup_path,
        database_id=f"{name}-db",
        occurred_at=1560,
        expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
        expected_backup_sha256=fixture.backup.backup_sha256,
        expected_switch_epoch=0,
        evidence_output=evidence_path,
    )

    assert replay == completed == json.loads(
        evidence_path.read_text(encoding="utf-8")
    )
    assert completed["schema"] == "authority-production-restore-operation-v2"
    assert completed["restore"]["restored_sha256"] == fixture.backup.backup_sha256
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'authority_%'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_operator_mutations_are_default_dry_run_and_help_is_standalone(tmp_path):
    _project, database = create_real_schema_v9(tmp_path)
    preflight = production_preflight(database, database_id="operator-db")
    backup = tmp_path / "operator.backup.db"
    evidence = tmp_path / "operator.evidence.json"
    result = subprocess.run(
        [
            sys.executable, str(OPERATOR), "migrate",
            "--database", str(database), "--database-id", "operator-db",
            "--expected-source-fence", preflight.source_fence_sha256,
            "--backup", str(backup), "--evidence-output", str(evidence),
            "--owner-token", "operator-owner", "--occurred-at", "1600",
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["confirmed"] is False
    assert not backup.exists() and not evidence.exists()
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'authority_%'"
        ).fetchone()[0] == 0
    finally:
        connection.close()
    help_result = subprocess.run(
        [sys.executable, str(OPERATOR), "--help"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert help_result.returncode == 0
    assert "dry-run unless --confirm" in help_result.stdout


def test_operator_restore_dry_run_performs_preflight_without_mutating_target(tmp_path):
    fixture = install_foundation(tmp_path, with_backup=True)
    assert fixture.backup is not None and fixture.backup_path is not None
    evidence = tmp_path / "restore.evidence.json"
    before = fixture.database.read_bytes()

    result = subprocess.run(
        [
            sys.executable, str(OPERATOR), "restore",
            "--database", str(fixture.database),
            "--database-id", "authority-project-db",
            "--backup", str(fixture.backup_path),
            "--expected-source-fence", fixture.preflight.source_fence_sha256,
            "--expected-backup-sha256", fixture.backup.backup_sha256,
            "--expected-switch-epoch", "0",
            "--occurred-at", "1650",
            "--evidence-output", str(evidence),
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["confirmed"] is False
    assert payload["preflight"]["integrity_check"] == "ok"
    assert payload["preflight"]["backup_sha256"] == fixture.backup.backup_sha256
    assert fixture.database.read_bytes() == before
    assert not evidence.exists()


def test_operator_restore_confirmed_replays_after_replacement_crash(tmp_path):
    fixture = install_foundation(
        tmp_path, name="operator-restore-replay", with_backup=True
    )
    assert fixture.backup is not None and fixture.backup_path is not None
    evidence = tmp_path / "operator-restore-replay.evidence.json"

    def interrupt(stage: str) -> None:
        if stage == "after_restore_publish":
            raise RuntimeError("crash after replacement")

    with pytest.raises(RuntimeError, match="crash after replacement"):
        run_authority_restore_operation(
            fixture.database,
            fixture.backup_path,
            database_id="operator-restore-replay-db",
            occurred_at=1660,
            expected_current_source_fence_sha256=fixture.preflight.source_fence_sha256,
            expected_backup_sha256=fixture.backup.backup_sha256,
            expected_switch_epoch=0,
            evidence_output=evidence,
            failure_injector=interrupt,
        )

    result = subprocess.run(
        [
            sys.executable, str(OPERATOR), "restore",
            "--database", str(fixture.database),
            "--database-id", "operator-restore-replay-db",
            "--backup", str(fixture.backup_path),
            "--expected-source-fence", fixture.preflight.source_fence_sha256,
            "--expected-backup-sha256", fixture.backup.backup_sha256,
            "--expected-switch-epoch", "0",
            "--occurred-at", "1660",
            "--evidence-output", str(evidence),
            "--confirm",
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "authority-production-restore-operation-v2"
    assert payload == json.loads(evidence.read_text(encoding="utf-8"))
    assert fixture.database.read_bytes() == fixture.backup_path.read_bytes()


def test_operator_health_reads_explicit_backup_and_restore_evidence(tmp_path):
    fixture = install_foundation(tmp_path, with_backup=True)
    assert fixture.backup is not None
    restore = RestoreEvidence(
        fixture.backup.database_id,
        1100,
        0,
        fixture.backup.source_fence_sha256,
        fixture.backup.backup_sha256,
        fixture.backup.backup_sha256,
        fixture.backup.backup_size,
        "ok",
        fixture.backup.authority_state,
        initial_database_identity_binding(fixture.backup).binding_sha256,
        fixture.backup.lineage_sha256,
    )
    backup_path = tmp_path / "backup-evidence.json"
    restore_path = tmp_path / "restore-evidence.json"
    backup_path.write_text(
        json.dumps(fixture.backup.as_dict()), encoding="utf-8", newline="\n"
    )
    restore_payload = {
        **restore.as_dict(),
        "evidence_sha256": restore.evidence_sha256,
    }
    restore_path.write_text(
        json.dumps(restore_payload), encoding="utf-8", newline="\n"
    )

    result = subprocess.run(
        [
            sys.executable, str(OPERATOR), "health",
            "--database", str(fixture.database),
            "--expected-source-fence", fixture.preflight.source_fence_sha256,
            "--evaluated-at", "1200",
            "--max-backlog-depth", "0",
            "--max-oldest-pending-age", "0",
            "--max-in-flight", "0",
            "--max-expired-claims", "0",
            "--max-retry-per-thousand", "0",
            "--max-dead-letter-count", "0",
            "--max-backup-age", "1000",
            "--require-restore-evidence",
            "--backup-evidence", str(backup_path),
            "--restore-evidence", str(restore_path),
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["healthy"] is True
    assert payload["backup_age"] == 200
    assert payload["restore_evidence_present"] is True


def test_fresh_active_cli_imports_no_production_authority_modules_and_has_no_callers():
    code = (
        "import sys; import factory_core.cli; "
        "bad=[n for n in sys.modules if n.startswith('factory_core.authority_production') "
        "or n in {'factory_core.authority_operations','factory_core.authority_read_repository',"
        "'factory_core.authority_outbox_delivery','factory_core.authority_operator_workflow'}]; "
        "assert not bad, bad"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    forbidden_modules = (
        "authority_production_writer", "authority_read_repository",
        "authority_outbox_delivery", "authority_operations",
        "authority_production_schema", "authority_operator_workflow",
    )
    active_files = [
        path for root in (ROOT / "factory_core", ROOT / "web")
        for path in root.rglob("*.py")
        if path.name not in {
            "authority_production_writer.py", "authority_read_repository.py",
            "authority_outbox_delivery.py", "authority_operations.py",
            "authority_production_schema.py", "authority_operator_workflow.py",
        }
    ]
    matches = []
    for path in active_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.append(node.module)
        if any(
            module.rsplit(".", 1)[-1] in forbidden_modules
            for module in imports
        ):
            matches.append(str(path.relative_to(ROOT)))
    assert matches == []
