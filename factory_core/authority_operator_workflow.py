"""Crash-replayable standalone operator workflows for Phase-2 Authority.

This module is imported only by ``scripts/authority_operator.py`` and direct
tests.  The explicit evidence output is first atomically reserved as a durable
operation journal, then atomically replaced by final evidence.  It never scans
for a database or calls an external service.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Callable, Mapping

from .authority_operations import (
    BackupEvidence,
    RestoreEvidence,
    AuthorityOperationConflict,
    AuthorityOperationError,
    AuthorityOperations,
    _file_identity,
    _fsync_directory,
    _production_state_facts,
    create_authority_backup,
    initial_database_identity_binding,
    preflight_authority_restore,
    restore_authority_backup,
    verify_authority_backup_file,
)
from .authority_production_schema import (
    AuthorityProductionMigrationRunner,
    authority_database_path,
    connect_authority_ro,
    legacy_database_content_sha256,
    legacy_source_identity_sha256,
    production_preflight,
    validate_real_schema_v9,
    verified_database_identity,
    verify_production_installation,
)
from .authority_schema import migrate_authority_schema_v2
from .canonical import canonical_bytes, canonical_sha256


MIGRATE_JOURNAL_SCHEMA = "authority-production-migrate-operation-journal-v1"
MIGRATE_EVIDENCE_SCHEMA = "authority-production-migrate-operation-v2"
RESTORE_JOURNAL_SCHEMA = "authority-production-restore-operation-journal-v1"
RESTORE_EVIDENCE_SCHEMA = "authority-production-restore-operation-v2"


def _output_path(path: str | Path) -> Path:
    value = Path(path)
    parent_input = value.parent
    metadata = parent_input.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AuthorityOperationError("operation output parent must be a regular directory")
    parent = parent_input.resolve()
    if value.is_symlink():
        raise AuthorityOperationError("operation output must not be a symlink")
    if value.exists() and not value.is_file():
        raise AuthorityOperationError("operation output must be a regular file")
    return parent / value.name


def _prospective_output_path(path: str | Path) -> Path:
    value = Path(path)
    parent_input = value.parent
    metadata = parent_input.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AuthorityOperationError("backup parent must be a regular directory")
    parent = parent_input.resolve()
    if value.is_symlink():
        raise AuthorityOperationError("backup must not be a symlink")
    if value.exists() and not value.is_file():
        raise AuthorityOperationError("backup must be a regular file")
    return parent / value.name


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityOperationConflict("operation evidence output is not reusable") from exc
    if type(payload) is not dict:
        raise AuthorityOperationConflict("operation evidence output is not a JSON object")
    return payload


def _journal_payload(
    *,
    schema: str,
    operation_id: str,
    state: str,
    request: Mapping[str, object],
    facts: Mapping[str, object],
) -> dict[str, object]:
    body = {
        "schema": schema,
        "operation_id": operation_id,
        "state": state,
        "request": dict(request),
        "facts": dict(facts),
    }
    return {**body, "journal_sha256": canonical_sha256(body)}


def _verify_operation_file(
    payload: Mapping[str, object], *, operation_id: str, journal_schema: str,
    final_schema: str,
) -> str:
    if payload.get("operation_id") != operation_id:
        raise AuthorityOperationConflict("operation evidence belongs to another operation")
    schema = payload.get("schema")
    if schema == journal_schema:
        body = {key: payload[key] for key in ("schema", "operation_id", "state", "request", "facts")}
        if payload.get("journal_sha256") != canonical_sha256(body):
            raise AuthorityOperationConflict("operation journal hash differs")
        return "JOURNAL"
    if schema == final_schema:
        body = dict(payload)
        digest = body.pop("evidence_sha256", None)
        if digest != canonical_sha256(body):
            raise AuthorityOperationConflict("final operation evidence hash differs")
        return "FINAL"
    raise AuthorityOperationConflict("operation evidence schema is unsupported")


def _publish(path: Path, payload: Mapping[str, object], *, create: bool) -> None:
    data = canonical_bytes(dict(payload)) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if create:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise AuthorityOperationConflict(
                    "operation evidence output was concurrently reserved"
                ) from exc
            temporary.unlink()
        else:
            os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _fail(
    failure_injector: Callable[[str], None] | None,
    stage: str,
) -> None:
    if failure_injector is not None:
        failure_injector(stage)


def _backup_from_journal(
    backup_path: Path,
    *,
    request: Mapping[str, object],
    preflight: Mapping[str, object],
) -> BackupEvidence:
    backup_sha256, backup_size = _file_identity(backup_path)
    connection = connect_authority_ro(backup_path)
    try:
        connection.execute("BEGIN")
        schema_identity = validate_real_schema_v9(connection)
        source_fence = legacy_source_identity_sha256(connection)
        source_content = legacy_database_content_sha256(connection)
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        authority_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='authority_schema_state'"
        ).fetchone()
        authority_state = "ABSENT"
        if authority_exists:
            row = connection.execute(
                "SELECT state FROM authority_schema_state WHERE singleton=1"
            ).fetchone()
            authority_state = "MISSING" if row is None else str(row[0])
        production_state, last_migration, prefix = _production_state_facts(connection)
        connection.commit()
    finally:
        connection.close()
    if (
        schema_identity != preflight["source_schema_identity_sha256"]
        or source_fence != preflight["source_fence_sha256"]
        or source_content != preflight["source_database_content_sha256"]
        or integrity != "ok"
    ):
        raise AuthorityOperationConflict("published backup differs from prepared source")
    evidence = BackupEvidence(
        str(request["database_id"]),
        int(request["occurred_at"]),
        int(preflight["source_schema_version"]),
        schema_identity,
        source_fence,
        source_content,
        str(preflight["main_file_sha256"]),
        int(preflight["main_file_size"]),
        backup_sha256,
        backup_size,
        integrity,
        authority_state,
        production_state,
        last_migration,
        prefix,
    )
    verify_authority_backup_file(backup_path, evidence)
    return evidence


def _final(payload: Mapping[str, object]) -> dict[str, object]:
    body = dict(payload)
    return {**body, "evidence_sha256": canonical_sha256(body)}


def _backup_evidence_value(value: object) -> BackupEvidence:
    if type(value) is not dict:
        raise AuthorityOperationConflict("operation backup evidence is malformed")
    fields = dict(value)
    if fields.pop("schema", None) != "authority-production-backup-evidence-v2":
        raise AuthorityOperationConflict("operation backup evidence schema differs")
    try:
        return BackupEvidence(**fields)
    except TypeError as exc:
        raise AuthorityOperationConflict("operation backup evidence fields differ") from exc


def _ready_identity(
    database: Path,
    *,
    database_id: str,
    backup: BackupEvidence,
) -> dict[str, object]:
    connection = connect_authority_ro(database)
    try:
        connection.execute("BEGIN")
        verify_production_installation(connection, require_ready=True)
        identity = verified_database_identity(connection)
        lineage = connection.execute(
            "SELECT * FROM authority_production_backup_lineage WHERE backup_sha256=?",
            (backup.backup_sha256,),
        ).fetchone()
        connection.commit()
    finally:
        connection.close()
    if (
        identity["database_id"] != database_id
        or identity["pre_authority_backup_sha256"] != backup.backup_sha256
        or lineage is None
        or lineage["lineage_sha256"] != backup.lineage_sha256
    ):
        raise AuthorityOperationConflict("READY database identity or lineage differs")
    return dict(identity)


def run_authority_migrate_operation(
    database: str | Path,
    *,
    database_id: str,
    expected_source_fence_sha256: str,
    backup: str | Path,
    evidence_output: str | Path,
    owner_token: str,
    occurred_at: int,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, object]:
    database_path = authority_database_path(database)
    backup_path = _prospective_output_path(backup)
    output_path = _output_path(evidence_output)
    if backup_path == output_path or backup_path == database_path:
        raise AuthorityOperationError("database, backup, and evidence paths must differ")
    request = {
        "schema": "authority-production-migrate-operation-request-v1",
        "database_path": str(database_path),
        "database_id": database_id,
        "expected_source_fence_sha256": expected_source_fence_sha256,
        "backup_path": str(backup_path),
        "evidence_output": str(output_path),
        "owner_token": owner_token,
        "occurred_at": occurred_at,
    }
    operation_id = canonical_sha256(request)
    existing = _read_json(output_path) if output_path.exists() else None
    existing_kind = None
    if existing is not None:
        existing_kind = _verify_operation_file(
            existing,
            operation_id=operation_id,
            journal_schema=MIGRATE_JOURNAL_SCHEMA,
            final_schema=MIGRATE_EVIDENCE_SCHEMA,
        )
        if existing_kind == "FINAL":
            backup_evidence = _backup_evidence_value(existing["backup"])
            verify_authority_backup_file(backup_path, backup_evidence)
            _ready_identity(
                database_path, database_id=database_id, backup=backup_evidence
            )
            return dict(existing)
    elif backup_path.exists():
        raise AuthorityOperationConflict(
            "backup already exists without the matching operation journal"
        )

    if existing_kind == "JOURNAL":
        journal = dict(existing)
        facts = dict(journal["facts"])
        prepared = dict(facts["preflight"])
    else:
        preflight = production_preflight(
            database_path,
            database_id=database_id,
            expected_source_fence_sha256=expected_source_fence_sha256,
        )
        if (
            preflight.base_authority_state != "ABSENT"
            or preflight.production_state != "ABSENT"
        ):
            raise AuthorityOperationConflict(
                "new migrate operation requires a pre-Authority database; "
                "reuse the original operation owner, backup path, and evidence path"
            )
        prepared = preflight.as_dict()
        journal = _journal_payload(
            schema=MIGRATE_JOURNAL_SCHEMA,
            operation_id=operation_id,
            state="PREPARED",
            request=request,
            facts={"preflight": prepared},
        )
        _publish(output_path, journal, create=True)
        facts = dict(journal["facts"])
        _fail(failure_injector, "after_prepare")

    if "backup" in facts and not backup_path.exists():
        raise AuthorityOperationConflict(
            "journaled immutable backup is missing and cannot be regenerated"
        )
    if backup_path.exists():
        backup_evidence = _backup_from_journal(
            backup_path, request=request, preflight=prepared
        )
    else:
        backup_evidence = create_authority_backup(
            database_path,
            backup_path,
            database_id=database_id,
            occurred_at=occurred_at,
            expected_source_fence_sha256=expected_source_fence_sha256,
        )
        _fail(failure_injector, "after_backup_publish")
    if "backup" in facts and facts["backup"] != backup_evidence.as_dict():
        raise AuthorityOperationConflict("journaled backup evidence differs")
    facts["backup"] = backup_evidence.as_dict()
    facts["backup_evidence_sha256"] = backup_evidence.evidence_sha256
    facts["backup_lineage_sha256"] = backup_evidence.lineage_sha256
    journal = _journal_payload(
        schema=MIGRATE_JOURNAL_SCHEMA, operation_id=operation_id,
        state="BACKUP_PUBLISHED", request=request, facts=facts,
    )
    _publish(output_path, journal, create=False)
    _fail(failure_injector, "after_backup_recorded")

    base = migrate_authority_schema_v2(
        database_path, owner_token=f"{owner_token}:base"
    )
    facts.setdefault(
        "base_migration",
        {
            "state": base.state,
            "applied_now": list(base.applied_now),
            "already_applied": list(base.already_applied),
        },
    )
    journal = _journal_payload(
        schema=MIGRATE_JOURNAL_SCHEMA, operation_id=operation_id,
        state="BASE_READY", request=request, facts=facts,
    )
    _publish(output_path, journal, create=False)
    _fail(failure_injector, "after_base_ready")

    def after_migration(migration_id: str) -> None:
        facts["last_completed_production_migration"] = migration_id
        step = _journal_payload(
            schema=MIGRATE_JOURNAL_SCHEMA, operation_id=operation_id,
            state="MIGRATING", request=request, facts=facts,
        )
        _publish(output_path, step, create=False)
        _fail(failure_injector, f"after_migration:{migration_id}")

    production = AuthorityProductionMigrationRunner(
        database_path,
        database_id=database_id,
        expected_source_fence_sha256=expected_source_fence_sha256,
        identity_binding=initial_database_identity_binding(backup_evidence),
        pre_authority_backup=backup_path,
        after_migration=after_migration,
    ).run(f"{owner_token}:production")
    facts["production_migration"] = production.as_dict()
    identity = _ready_identity(
        database_path, database_id=database_id, backup=backup_evidence
    )
    facts["database_identity_sha256"] = identity["binding_sha256"]
    journal = _journal_payload(
        schema=MIGRATE_JOURNAL_SCHEMA, operation_id=operation_id,
        state="DATABASE_READY", request=request, facts=facts,
    )
    _publish(output_path, journal, create=False)
    _fail(failure_injector, "after_database_ready")

    receipt = AuthorityOperations(
        database_path,
        expected_source_fence_sha256=expected_source_fence_sha256,
    ).record_backup_evidence(
        backup_evidence, backup=backup_path, occurred_at=occurred_at
    )
    facts["recorded_backup_receipt_sha256"] = receipt
    journal = _journal_payload(
        schema=MIGRATE_JOURNAL_SCHEMA, operation_id=operation_id,
        state="INTERNAL_RECEIPT_RECORDED", request=request, facts=facts,
    )
    _publish(output_path, journal, create=False)
    _fail(failure_injector, "after_internal_receipt")

    final = _final(
        {
            "schema": MIGRATE_EVIDENCE_SCHEMA,
            "operation_id": operation_id,
            "request_sha256": canonical_sha256(request),
            "preflight": prepared,
            "backup": backup_evidence.as_dict(),
            "backup_evidence_sha256": backup_evidence.evidence_sha256,
            "backup_lineage_sha256": backup_evidence.lineage_sha256,
            "base_migration": facts["base_migration"],
            "production_migration": facts["production_migration"],
            "database_identity_sha256": facts["database_identity_sha256"],
            "recorded_backup_receipt_sha256": receipt,
        }
    )
    _fail(failure_injector, "before_external_evidence_publish")
    _publish(output_path, final, create=False)
    return final


def _restored_evidence_from_journal(
    database: Path,
    backup: Path,
    *,
    request: Mapping[str, object],
    preflight: Mapping[str, object],
) -> RestoreEvidence:
    target_identity = _file_identity(database)
    backup_identity = _file_identity(backup)
    if target_identity != backup_identity or target_identity != (
        preflight["backup_sha256"], preflight["backup_size"]
    ):
        raise AuthorityOperationConflict("restore target is not the journaled backup")
    connection = connect_authority_ro(database)
    try:
        connection.execute("BEGIN")
        validate_real_schema_v9(connection)
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        fence = legacy_source_identity_sha256(connection)
        authority_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='authority_schema_state'"
        ).fetchone()
        authority_state = "ABSENT"
        if authority_exists:
            row = connection.execute(
                "SELECT state FROM authority_schema_state WHERE singleton=1"
            ).fetchone()
            authority_state = "MISSING" if row is None else str(row[0])
        connection.commit()
    finally:
        connection.close()
    if (
        integrity != "ok"
        or fence != preflight["source_fence_sha256"]
        or authority_state != preflight["backup_authority_state"]
    ):
        raise AuthorityOperationConflict("restored target facts differ from journal")
    return RestoreEvidence(
        str(request["database_id"]),
        int(request["occurred_at"]),
        int(request["expected_switch_epoch"]),
        str(preflight["source_fence_sha256"]),
        str(preflight["backup_sha256"]),
        target_identity[0],
        target_identity[1],
        integrity,
        authority_state,
        str(preflight["database_identity_sha256"]),
        str(preflight["backup_lineage_sha256"]),
    )


def run_authority_restore_operation(
    database: str | Path,
    backup: str | Path,
    *,
    database_id: str,
    occurred_at: int,
    expected_current_source_fence_sha256: str,
    expected_backup_sha256: str,
    expected_switch_epoch: int,
    evidence_output: str | Path,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, object]:
    database_path = authority_database_path(database)
    backup_path = authority_database_path(backup)
    output_path = _output_path(evidence_output)
    if output_path in {database_path, backup_path}:
        raise AuthorityOperationError("database, backup, and evidence paths must differ")
    request = {
        "schema": "authority-production-restore-operation-request-v1",
        "database_path": str(database_path),
        "database_id": database_id,
        "backup_path": str(backup_path),
        "expected_source_fence_sha256": expected_current_source_fence_sha256,
        "expected_backup_sha256": expected_backup_sha256,
        "expected_switch_epoch": expected_switch_epoch,
        "evidence_output": str(output_path),
        "occurred_at": occurred_at,
    }
    operation_id = canonical_sha256(request)
    existing = _read_json(output_path) if output_path.exists() else None
    existing_kind = None
    if existing is not None:
        existing_kind = _verify_operation_file(
            existing,
            operation_id=operation_id,
            journal_schema=RESTORE_JOURNAL_SCHEMA,
            final_schema=RESTORE_EVIDENCE_SCHEMA,
        )
        if existing_kind == "FINAL":
            _restored_evidence_from_journal(
                database_path,
                backup_path,
                request=request,
                preflight=dict(existing["preflight"]),
            )
            return dict(existing)

    if existing_kind == "JOURNAL":
        journal = dict(existing)
        facts = dict(journal["facts"])
        prepared = dict(facts["preflight"])
    else:
        preflight = preflight_authority_restore(
            database_path,
            backup_path,
            database_id=database_id,
            expected_current_source_fence_sha256=expected_current_source_fence_sha256,
            expected_backup_sha256=expected_backup_sha256,
            expected_switch_epoch=expected_switch_epoch,
        )
        prepared = preflight.as_dict()
        journal = _journal_payload(
            schema=RESTORE_JOURNAL_SCHEMA,
            operation_id=operation_id,
            state="PREPARED",
            request=request,
            facts={"preflight": prepared},
        )
        _publish(output_path, journal, create=True)
        facts = dict(journal["facts"])
        _fail(failure_injector, "after_prepare")

    if _file_identity(database_path) == _file_identity(backup_path):
        evidence = _restored_evidence_from_journal(
            database_path, backup_path, request=request, preflight=prepared
        )
    else:
        current_preflight = preflight_authority_restore(
            database_path,
            backup_path,
            database_id=database_id,
            expected_current_source_fence_sha256=expected_current_source_fence_sha256,
            expected_backup_sha256=expected_backup_sha256,
            expected_switch_epoch=expected_switch_epoch,
        )
        if current_preflight.as_dict() != prepared:
            raise AuthorityOperationConflict("restore preflight changed during replay")
        evidence = restore_authority_backup(
            database_path,
            backup_path,
            database_id=database_id,
            occurred_at=occurred_at,
            expected_current_source_fence_sha256=expected_current_source_fence_sha256,
            expected_backup_sha256=expected_backup_sha256,
            expected_switch_epoch=expected_switch_epoch,
            failure_injector=failure_injector,
        )
    facts["restore"] = evidence.as_dict()
    journal = _journal_payload(
        schema=RESTORE_JOURNAL_SCHEMA, operation_id=operation_id,
        state="RESTORED_VERIFIED", request=request, facts=facts,
    )
    _publish(output_path, journal, create=False)
    _fail(failure_injector, "after_restore_journaled")
    final = _final(
        {
            "schema": RESTORE_EVIDENCE_SCHEMA,
            "operation_id": operation_id,
            "request_sha256": canonical_sha256(request),
            "preflight": prepared,
            "restore": evidence.as_dict(),
            "restore_evidence_sha256": evidence.evidence_sha256,
        }
    )
    _fail(failure_injector, "before_external_evidence_publish")
    _publish(output_path, final, create=False)
    return final
