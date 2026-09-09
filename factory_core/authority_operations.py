"""Explicit Phase-2 Authority production operations and safety gates.

All paths are caller supplied.  Nothing scans for project databases, contacts
an external service, or changes the active v1 route.  File-mutating functions
are used only by the standalone operator entrypoint and require explicit
arguments; tests use temporary schema-v9 databases.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import TYPE_CHECKING, Callable, Mapping

if TYPE_CHECKING:
    from .phase9_run_generation import (
        RunGenerationCreationResult,
        RunGenerationRequestV1,
    )

from .authority_production_schema import (
    EMPTY_PRODUCTION_PREFIX_SHA256,
    AuthorityProductionSchemaDrift,
    AuthorityProductionSchemaError,
    ProductionDatabaseIdentityBinding,
    authority_database_path,
    backup_lineage_sha256,
    connect_authority_ro,
    connect_authority_rw,
    legacy_database_content_sha256,
    legacy_source_identity_sha256,
    production_prefix_sha256,
    validate_real_schema_v9,
    verified_database_identity,
    verify_production_control_structure,
    verify_production_installation,
)
from .canonical import canonical_bytes, canonical_sha256
from .domain import SCHEMA_VERSION
from .phase3_artifacts import (
    ArtifactOwnerOperatorClaim,
    validate_artifact_owner_operator_claim,
)


BACKUP_EVIDENCE_SCHEMA = "authority-production-backup-evidence-v2"
RESTORE_EVIDENCE_SCHEMA = "authority-production-restore-evidence-v2"
CONTROL_RECEIPT_SCHEMA = "authority-production-control-receipt-v1"
PHASE3_OWNER_OPERATOR_GRANT_SET_SCHEMA = (
    "authority-phase3-owner-operator-grant-set-v1"
)
HEALTH_REPORT_SCHEMA = "authority-production-health-report-v1"
AUTO_FALLBACK_SCHEMA = "authority-production-auto-fallback-v1"

V1_ONLY = "V1_ONLY"
CANARY = "CANARY"
AUTHORITY_PRIMARY = "AUTHORITY_PRIMARY"
SWITCH_MODES = frozenset({V1_ONLY, CANARY, AUTHORITY_PRIMARY})

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class AuthorityOperationError(RuntimeError):
    """Base error for explicit production-authority operations."""


class AuthorityOperationConflict(AuthorityOperationError):
    """Raised when a caller supplied fence or CAS value is stale."""


class AuthorityRestoreNotQuiet(AuthorityOperationError):
    """Raised when writer or delivery state is not stopped for restore."""


@dataclass(frozen=True)
class BackupEvidence:
    database_id: str
    occurred_at: int
    source_schema_version: int
    source_schema_identity_sha256: str
    source_fence_sha256: str
    source_database_content_sha256: str
    source_main_file_sha256: str
    source_main_file_size: int
    backup_sha256: str
    backup_size: int
    integrity_check: str
    authority_state: str
    production_state: str
    production_last_migration: str | None
    production_prefix_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": BACKUP_EVIDENCE_SCHEMA,
            "database_id": self.database_id,
            "occurred_at": self.occurred_at,
            "source_schema_version": self.source_schema_version,
            "source_schema_identity_sha256": self.source_schema_identity_sha256,
            "source_fence_sha256": self.source_fence_sha256,
            "source_database_content_sha256": self.source_database_content_sha256,
            "source_main_file_sha256": self.source_main_file_sha256,
            "source_main_file_size": self.source_main_file_size,
            "backup_sha256": self.backup_sha256,
            "backup_size": self.backup_size,
            "integrity_check": self.integrity_check,
            "authority_state": self.authority_state,
            "production_state": self.production_state,
            "production_last_migration": self.production_last_migration,
            "production_prefix_sha256": self.production_prefix_sha256,
        }

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def lineage_sha256(self) -> str:
        return backup_lineage_sha256(
            database_id=self.database_id,
            backup_kind=(
                "INITIAL_PRE_AUTHORITY"
                if self.authority_state == "ABSENT"
                and self.production_state == "ABSENT"
                else "RECORDED_CHECKPOINT"
            ),
            backup_sha256=self.backup_sha256,
            backup_size=self.backup_size,
            source_fence_sha256=self.source_fence_sha256,
            source_database_content_sha256=self.source_database_content_sha256,
            evidence_sha256=self.evidence_sha256,
        )


@dataclass(frozen=True)
class RestoreEvidence:
    database_id: str
    occurred_at: int
    expected_switch_epoch: int
    source_fence_sha256: str
    backup_sha256: str
    restored_sha256: str
    restored_size: int
    integrity_check: str
    restored_authority_state: str
    database_identity_sha256: str
    backup_lineage_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": RESTORE_EVIDENCE_SCHEMA,
            "database_id": self.database_id,
            "occurred_at": self.occurred_at,
            "expected_switch_epoch": self.expected_switch_epoch,
            "source_fence_sha256": self.source_fence_sha256,
            "backup_sha256": self.backup_sha256,
            "restored_sha256": self.restored_sha256,
            "restored_size": self.restored_size,
            "integrity_check": self.integrity_check,
            "restored_authority_state": self.restored_authority_state,
            "database_identity_sha256": self.database_identity_sha256,
            "backup_lineage_sha256": self.backup_lineage_sha256,
        }

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class RestorePreflight:
    database_id: str
    expected_switch_epoch: int
    source_fence_sha256: str
    backup_sha256: str
    backup_size: int
    integrity_check: str
    backup_authority_state: str
    database_identity_sha256: str
    backup_lineage_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "authority-production-restore-preflight-v1",
            "database_id": self.database_id,
            "expected_switch_epoch": self.expected_switch_epoch,
            "source_fence_sha256": self.source_fence_sha256,
            "backup_sha256": self.backup_sha256,
            "backup_size": self.backup_size,
            "integrity_check": self.integrity_check,
            "backup_authority_state": self.backup_authority_state,
            "database_identity_sha256": self.database_identity_sha256,
            "backup_lineage_sha256": self.backup_lineage_sha256,
        }

    @property
    def preflight_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class ControlResult:
    changed: bool
    switch_mode: str
    switch_epoch: int
    writer_id: str | None
    writer_epoch: int
    writer_enabled: bool
    consumer_id: str | None
    consumer_epoch: int
    consumer_enabled: bool
    receipt_id: str | None
    receipt_sha256: str | None


@dataclass(frozen=True)
class AuthorityHealthPolicy:
    max_backlog_depth: int
    max_oldest_pending_age: int
    max_in_flight: int
    max_expired_claims: int
    max_retry_per_thousand: int
    max_dead_letter_count: int
    max_backup_age: int
    require_restore_evidence: bool

    def __post_init__(self) -> None:
        values = (
            self.max_backlog_depth,
            self.max_oldest_pending_age,
            self.max_in_flight,
            self.max_expired_claims,
            self.max_retry_per_thousand,
            self.max_dead_letter_count,
            self.max_backup_age,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise AuthorityOperationError("health thresholds must be non-negative integers")
        if type(self.require_restore_evidence) is not bool:
            raise AuthorityOperationError("require_restore_evidence must be a bool")


@dataclass(frozen=True)
class AuthorityHealthReport:
    evaluated_at: int
    source_fence_sha256: str
    schema_ready: bool
    backlog_depth: int
    oldest_pending_age: int
    in_flight: int
    expired_claims: int
    retry_per_thousand: int
    dead_letter_count: int
    backup_age: int | None
    restore_evidence_present: bool
    hard_conditions: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.hard_conditions

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": HEALTH_REPORT_SCHEMA,
            "evaluated_at": self.evaluated_at,
            "source_fence_sha256": self.source_fence_sha256,
            "schema_ready": self.schema_ready,
            "backlog_depth": self.backlog_depth,
            "oldest_pending_age": self.oldest_pending_age,
            "in_flight": self.in_flight,
            "expired_claims": self.expired_claims,
            "retry_per_thousand": self.retry_per_thousand,
            "dead_letter_count": self.dead_letter_count,
            "backup_age": self.backup_age,
            "restore_evidence_present": self.restore_evidence_present,
            "hard_conditions": list(self.hard_conditions),
            "healthy": self.healthy,
        }

    @property
    def report_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class AutoFallbackDecision:
    current_mode: str
    target_mode: str
    action: str
    hard_conditions: tuple[str, ...]
    health_report_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": AUTO_FALLBACK_SCHEMA,
            "current_mode": self.current_mode,
            "target_mode": self.target_mode,
            "action": self.action,
            "hard_conditions": list(self.hard_conditions),
            "health_report_sha256": self.health_report_sha256,
        }

    @property
    def decision_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def _identifier(value: object, path: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise AuthorityOperationError(f"{path} must be a bounded authority identifier")
    return value


def _text(value: object, path: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AuthorityOperationError(f"{path} must be a non-empty trimmed string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AuthorityOperationError(f"{path} must contain valid UTF-8") from exc
    return value


def _sha(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise AuthorityOperationError(f"{path} must be lowercase SHA-256")
    return value


def _nonnegative(value: object, path: str) -> int:
    if type(value) is not int or value < 0:
        raise AuthorityOperationError(f"{path} must be a non-negative integer")
    return value


def _file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _regular_output(path: str | Path) -> Path:
    value = Path(path)
    if value.exists() or value.is_symlink():
        raise AuthorityOperationError(f"output already exists: {value}")
    parent = value.parent.resolve()
    metadata = parent.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AuthorityOperationError("output parent must be a non-symlink directory")
    return parent / value.name


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_operator_evidence(path: str | Path, payload: Mapping[str, object]) -> str:
    target = _regular_output(path)
    data = canonical_bytes(dict(payload)) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(data[:-1]).hexdigest()


def initial_database_identity_binding(
    evidence: BackupEvidence,
) -> ProductionDatabaseIdentityBinding:
    if type(evidence) is not BackupEvidence:
        raise AuthorityOperationError("initial backup evidence type is unsupported")
    if (
        evidence.authority_state != "ABSENT"
        or evidence.production_state != "ABSENT"
        or evidence.production_last_migration is not None
        or evidence.production_prefix_sha256 != EMPTY_PRODUCTION_PREFIX_SHA256
    ):
        raise AuthorityOperationConflict(
            "production identity requires the original pre-Authority backup"
        )
    return ProductionDatabaseIdentityBinding(
        evidence.database_id,
        evidence.source_schema_identity_sha256,
        evidence.source_fence_sha256,
        evidence.source_database_content_sha256,
        evidence.backup_sha256,
        evidence.backup_size,
        evidence.evidence_sha256,
        evidence.lineage_sha256,
        evidence.authority_state,
        evidence.production_state,
        evidence.production_last_migration,
        evidence.production_prefix_sha256,
        evidence.occurred_at,
    )


def create_authority_backup(
    database: str | Path,
    backup: str | Path,
    *,
    database_id: str,
    occurred_at: int,
    expected_source_fence_sha256: str,
    failure_injector: Callable[[str], None] | None = None,
) -> BackupEvidence:
    source = authority_database_path(database)
    target = _regular_output(backup)
    identity = _identifier(database_id, "database_id")
    now = _nonnegative(occurred_at, "occurred_at")
    expected_fence = _sha(expected_source_fence_sha256, "expected_source_fence_sha256")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".sqlite.tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    source_connection = connect_authority_ro(source)
    try:
        source_connection.execute("BEGIN")
        source_main_sha, source_main_size = _file_identity(source)
        schema_identity = validate_real_schema_v9(source_connection)
        source_fence = legacy_source_identity_sha256(source_connection)
        source_content = legacy_database_content_sha256(source_connection)
        if source_fence != expected_fence:
            raise AuthorityOperationConflict("backup source fence differs")
        integrity = str(source_connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise AuthorityOperationError("source database integrity_check failed")
        authority_row = source_connection.execute(
            "SELECT state FROM authority_schema_state WHERE singleton=1"
        ).fetchone() if source_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authority_schema_state'"
        ).fetchone() else None
        production_row = source_connection.execute(
            "SELECT state, last_completed_migration "
            "FROM authority_production_schema_state WHERE singleton=1"
        ).fetchone() if source_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='authority_production_schema_state'"
        ).fetchone() else None
        prefix_sha256 = production_prefix_sha256(source_connection)
        identity_exists = source_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='authority_production_database_identity'"
        ).fetchone()
        if identity_exists:
            persisted_identity = verified_database_identity(source_connection)
            if persisted_identity["database_id"] != identity:
                raise AuthorityOperationConflict(
                    "backup database_id differs from persisted database identity"
                )
        destination = sqlite3.connect(temporary)
        try:
            source_connection.backup(destination)
            destination.commit()
        finally:
            destination.close()
        if failure_injector is not None:
            failure_injector("after_sqlite_backup")
        if legacy_source_identity_sha256(source_connection) != source_fence:
            raise AuthorityOperationConflict("backup source fence changed during snapshot")
        if legacy_database_content_sha256(source_connection) != source_content:
            raise AuthorityOperationConflict("backup database content changed during snapshot")
        source_connection.commit()
    except Exception:
        source_connection.rollback()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        source_connection.close()
    check = sqlite3.connect(f"file:{temporary.as_posix()}?mode=ro", uri=True)
    check.row_factory = sqlite3.Row
    try:
        backup_integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
        if backup_integrity != "ok":
            raise AuthorityOperationError("backup integrity_check failed")
        if legacy_source_identity_sha256(check) != source_fence:
            raise AuthorityOperationConflict("backup source fence differs after copy")
        if legacy_database_content_sha256(check) != source_content:
            raise AuthorityOperationConflict("backup database content differs after copy")
    finally:
        check.close()
    try:
        _fsync_file(temporary)
        backup_sha, backup_size = _file_identity(temporary)
        if failure_injector is not None:
            failure_injector("before_atomic_publish")
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    published_sha, published_size = _file_identity(target)
    if (published_sha, published_size) != (backup_sha, backup_size):
        raise AuthorityOperationError("published backup identity differs")
    return BackupEvidence(
        str(identity), now, 9, schema_identity, source_fence, source_content,
        source_main_sha, source_main_size, backup_sha, backup_size,
        backup_integrity, "ABSENT" if authority_row is None else str(authority_row[0]),
        "ABSENT" if production_row is None else str(production_row[0]),
        None if production_row is None else production_row[1],
        prefix_sha256,
    )


def _authority_state_label(connection: sqlite3.Connection) -> str:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authority_schema_state'"
    ).fetchone()
    if not exists:
        return "ABSENT"
    row = connection.execute(
        "SELECT state FROM authority_schema_state WHERE singleton=1"
    ).fetchone()
    return "MISSING" if row is None else str(row[0])


def _production_state_facts(
    connection: sqlite3.Connection,
) -> tuple[str, str | None, str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='authority_production_schema_state'"
    ).fetchone()
    if not exists:
        return "ABSENT", None, EMPTY_PRODUCTION_PREFIX_SHA256
    row = connection.execute(
        "SELECT state, last_completed_migration "
        "FROM authority_production_schema_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        return "MISSING", None, production_prefix_sha256(connection)
    return str(row[0]), row[1], production_prefix_sha256(connection)


def verify_authority_backup_file(
    backup: str | Path,
    evidence: BackupEvidence,
) -> Path:
    if type(evidence) is not BackupEvidence:
        raise AuthorityOperationError("backup evidence type is unsupported")
    path = authority_database_path(backup)
    if _file_identity(path) != (evidence.backup_sha256, evidence.backup_size):
        raise AuthorityOperationConflict("backup file identity differs from evidence")
    connection = connect_authority_ro(path)
    try:
        connection.execute("BEGIN")
        schema_identity = validate_real_schema_v9(connection)
        source_fence = legacy_source_identity_sha256(connection)
        source_content = legacy_database_content_sha256(connection)
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        base_state = _authority_state_label(connection)
        production_state, last_migration, prefix = _production_state_facts(connection)
        connection.commit()
    finally:
        connection.close()
    if (
        schema_identity != evidence.source_schema_identity_sha256
        or source_fence != evidence.source_fence_sha256
        or source_content != evidence.source_database_content_sha256
        or integrity != evidence.integrity_check
        or integrity != "ok"
        or base_state != evidence.authority_state
        or production_state != evidence.production_state
        or last_migration != evidence.production_last_migration
        or prefix != evidence.production_prefix_sha256
    ):
        raise AuthorityOperationConflict("backup file facts differ from evidence")
    return path


def _assert_restore_quiet(
    connection: sqlite3.Connection,
    *,
    expected_switch_epoch: int,
    expected_source_fence_sha256: str,
) -> None:
    verify_production_installation(connection, require_ready=True)
    if legacy_source_identity_sha256(connection) != expected_source_fence_sha256:
        raise AuthorityOperationConflict("restore source fence differs")
    writer = connection.execute(
        "SELECT * FROM authority_production_writer_state WHERE singleton=1"
    ).fetchone()
    consumer = connection.execute(
        "SELECT * FROM authority_production_consumer_state WHERE singleton=1"
    ).fetchone()
    if writer is None or consumer is None:
        raise AuthorityRestoreNotQuiet("restore control state is missing")
    if writer["switch_epoch"] != expected_switch_epoch:
        raise AuthorityOperationConflict("restore switch epoch is stale")
    if (
        writer["switch_mode"] != V1_ONLY
        or writer["writer_enabled"] != 0
        or consumer["consumer_enabled"] != 0
    ):
        raise AuthorityRestoreNotQuiet("restore requires V1_ONLY and disabled actors")
    active = connection.execute(
        "SELECT COUNT(*) FROM authority_production_outbox_delivery_state "
        "WHERE status IN ('CLAIMED', 'RECONCILIATION_REQUIRED')"
    ).fetchone()[0]
    if active:
        raise AuthorityRestoreNotQuiet("restore requires no in-flight delivery")


def preflight_authority_restore(
    database: str | Path,
    backup: str | Path,
    *,
    database_id: str,
    expected_current_source_fence_sha256: str,
    expected_backup_sha256: str,
    expected_switch_epoch: int,
) -> RestorePreflight:
    target = authority_database_path(database)
    source_backup = authority_database_path(backup)
    if target == source_backup:
        raise AuthorityOperationError("backup and restore target must differ")
    identity = _identifier(database_id, "database_id")
    expected_fence = _sha(
        expected_current_source_fence_sha256, "expected_current_source_fence_sha256"
    )
    expected_backup = _sha(expected_backup_sha256, "expected_backup_sha256")
    switch_epoch = _nonnegative(expected_switch_epoch, "expected_switch_epoch")
    backup_sha, backup_size = _file_identity(source_backup)
    if backup_sha != expected_backup:
        raise AuthorityOperationConflict("backup hash differs")
    check = connect_authority_ro(source_backup)
    try:
        check.execute("BEGIN")
        validate_real_schema_v9(check)
        backup_integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
        backup_fence = legacy_source_identity_sha256(check)
        backup_content = legacy_database_content_sha256(check)
        restored_authority_state = _authority_state_label(check)
        backup_production_state, backup_last_migration, backup_prefix = (
            _production_state_facts(check)
        )
        check.commit()
    finally:
        check.close()
    if backup_integrity != "ok" or backup_fence != expected_fence:
        raise AuthorityOperationConflict("backup integrity or source fence differs")
    current = connect_authority_ro(target)
    try:
        current.execute("BEGIN")
        _assert_restore_quiet(
            current,
            expected_switch_epoch=switch_epoch,
            expected_source_fence_sha256=expected_fence,
        )
        persisted_identity = verified_database_identity(current)
        if persisted_identity["database_id"] != identity:
            raise AuthorityOperationConflict(
                "restore database_id differs from persisted database identity"
            )
        lineage = current.execute(
            "SELECT * FROM authority_production_backup_lineage "
            "WHERE backup_sha256=?",
            (backup_sha,),
        ).fetchone()
        if lineage is None:
            raise AuthorityOperationConflict(
                "restore backup is not in the persisted database lineage"
            )
        if (
            lineage["database_id"] != identity
            or lineage["backup_size"] != backup_size
            or lineage["source_fence_sha256"] != expected_fence
            or lineage["source_database_content_sha256"] != backup_content
            or backup_lineage_sha256(
                database_id=str(lineage["database_id"]),
                backup_kind=str(lineage["backup_kind"]),
                backup_sha256=backup_sha,
                backup_size=backup_size,
                source_fence_sha256=backup_fence,
                source_database_content_sha256=backup_content,
                evidence_sha256=str(lineage["evidence_sha256"]),
            )
            != lineage["lineage_sha256"]
        ):
            raise AuthorityOperationConflict("restore backup lineage differs")
        if lineage["backup_kind"] == "INITIAL_PRE_AUTHORITY" and (
            restored_authority_state != "ABSENT"
            or backup_production_state != "ABSENT"
            or backup_last_migration is not None
            or backup_prefix != EMPTY_PRODUCTION_PREFIX_SHA256
        ):
            raise AuthorityOperationConflict(
                "initial restore backup is not pre-Authority"
            )
        current.commit()
    finally:
        current.close()
    return RestorePreflight(
        str(identity), switch_epoch, expected_fence, backup_sha, backup_size,
        backup_integrity, restored_authority_state,
        str(persisted_identity["binding_sha256"]), str(lineage["lineage_sha256"]),
    )


def restore_authority_backup(
    database: str | Path,
    backup: str | Path,
    *,
    database_id: str,
    occurred_at: int,
    expected_current_source_fence_sha256: str,
    expected_backup_sha256: str,
    expected_switch_epoch: int,
    failure_injector: Callable[[str], None] | None = None,
) -> RestoreEvidence:
    """Restore under the shared lease when the target is project workflow state."""

    from .phase9_authority_lease import authority_database_commit_lease

    target = authority_database_path(database)
    with authority_database_commit_lease(target):
        return _restore_authority_backup_under_commit_lease(
            target,
            backup,
            database_id=database_id,
            occurred_at=occurred_at,
            expected_current_source_fence_sha256=(
                expected_current_source_fence_sha256
            ),
            expected_backup_sha256=expected_backup_sha256,
            expected_switch_epoch=expected_switch_epoch,
            failure_injector=failure_injector,
        )


def _restore_authority_backup_under_commit_lease(
    database: str | Path,
    backup: str | Path,
    *,
    database_id: str,
    occurred_at: int,
    expected_current_source_fence_sha256: str,
    expected_backup_sha256: str,
    expected_switch_epoch: int,
    failure_injector: Callable[[str], None] | None = None,
) -> RestoreEvidence:
    preflight = preflight_authority_restore(
        database,
        backup,
        database_id=database_id,
        expected_current_source_fence_sha256=expected_current_source_fence_sha256,
        expected_backup_sha256=expected_backup_sha256,
        expected_switch_epoch=expected_switch_epoch,
    )
    target = authority_database_path(database)
    source_backup = authority_database_path(backup)
    identity = preflight.database_id
    now = _nonnegative(occurred_at, "occurred_at")
    expected_fence = preflight.source_fence_sha256
    switch_epoch = preflight.expected_switch_epoch
    backup_sha = preflight.backup_sha256
    backup_size = preflight.backup_size
    restored_authority_state = preflight.backup_authority_state
    current = connect_authority_rw(target)
    try:
        current.execute("BEGIN IMMEDIATE")
        _assert_restore_quiet(
            current,
            expected_switch_epoch=switch_epoch,
            expected_source_fence_sha256=expected_fence,
        )
        current.commit()
    except Exception:
        current.rollback()
        raise
    finally:
        current.close()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".restore.tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with source_backup.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(block)
            destination.flush()
            os.fsync(destination.fileno())
        if _file_identity(temporary) != (backup_sha, backup_size):
            raise AuthorityOperationError("restore staging identity differs")
        if failure_injector is not None:
            failure_injector("before_restore_publish")
        quiet = connect_authority_rw(target)
        try:
            quiet.execute("PRAGMA journal_mode=DELETE")
            quiet.execute("BEGIN EXCLUSIVE")
            _assert_restore_quiet(
                quiet,
                expected_switch_epoch=switch_epoch,
                expected_source_fence_sha256=expected_fence,
            )
            quiet.commit()
        except Exception:
            quiet.rollback()
            raise
        finally:
            quiet.close()
        for suffix in ("-wal", "-shm"):
            companion = Path(str(target) + suffix)
            if companion.is_symlink():
                raise AuthorityOperationError("restore companion must not be a symlink")
            if companion.exists():
                raise AuthorityRestoreNotQuiet(
                    f"SQLite companion remained after exclusive quieting: {suffix}"
                )
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        if failure_injector is not None:
            failure_injector("after_restore_publish")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    restored_sha, restored_size = _file_identity(target)
    if (restored_sha, restored_size) != (backup_sha, backup_size):
        raise AuthorityOperationError("restored database bytes differ from backup")
    restored = connect_authority_ro(target)
    try:
        restored.execute("BEGIN")
        validate_real_schema_v9(restored)
        integrity = str(restored.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok" or legacy_source_identity_sha256(restored) != expected_fence:
            raise AuthorityOperationError("restored database verification failed")
        actual_authority_state = _authority_state_label(restored)
        if actual_authority_state != restored_authority_state:
            raise AuthorityOperationError("restored authority state differs from backup")
        restored.commit()
    finally:
        restored.close()
    if failure_injector is not None:
        failure_injector("after_restore_verification")
    return RestoreEvidence(
        str(identity), now, switch_epoch, expected_fence, backup_sha,
        restored_sha, restored_size, integrity, restored_authority_state,
        preflight.database_identity_sha256, preflight.backup_lineage_sha256,
    )


def _control_projection(
    writer: sqlite3.Row, consumer: sqlite3.Row
) -> dict[str, object]:
    return {
        "switch_mode": str(writer["switch_mode"]),
        "switch_epoch": int(writer["switch_epoch"]),
        "writer_id": writer["writer_id"],
        "writer_epoch": int(writer["writer_epoch"]),
        "writer_enabled": bool(writer["writer_enabled"]),
        "consumer_id": consumer["consumer_id"],
        "consumer_epoch": int(consumer["consumer_epoch"]),
        "consumer_enabled": bool(consumer["consumer_enabled"]),
    }


def _control_result(
    writer: sqlite3.Row,
    consumer: sqlite3.Row,
    *,
    changed: bool,
    receipt_id: str | None,
    receipt_sha256: str | None,
) -> ControlResult:
    return ControlResult(
        changed,
        str(writer["switch_mode"]),
        int(writer["switch_epoch"]),
        writer["writer_id"],
        int(writer["writer_epoch"]),
        bool(writer["writer_enabled"]),
        consumer["consumer_id"],
        int(consumer["consumer_epoch"]),
        bool(consumer["consumer_enabled"]),
        receipt_id,
        receipt_sha256,
    )


class AuthorityOperations:
    """CAS-only administration; no workflow command or delivery mutation API."""

    def __init__(self, database: str | Path, *, expected_source_fence_sha256: str):
        self.path = authority_database_path(database)
        self.expected_source_fence_sha256 = _sha(
            expected_source_fence_sha256, "expected_source_fence_sha256"
        )

    def _state(
        self, connection: sqlite3.Connection
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        writer = connection.execute(
            "SELECT * FROM authority_production_writer_state WHERE singleton=1"
        ).fetchone()
        consumer = connection.execute(
            "SELECT * FROM authority_production_consumer_state WHERE singleton=1"
        ).fetchone()
        if writer is None or consumer is None:
            raise AuthorityOperationError("authority control state is missing")
        return writer, consumer

    def _verify(self, connection: sqlite3.Connection) -> None:
        verify_production_installation(connection, require_ready=True)
        if legacy_source_identity_sha256(connection) != self.expected_source_fence_sha256:
            raise AuthorityOperationConflict("authority source fence differs")

    @staticmethod
    def _receipt(
        connection: sqlite3.Connection,
        *,
        kind: str,
        prior: Mapping[str, object],
        next_state: Mapping[str, object],
        operator_subject: str,
        reason: str,
        occurred_at: int,
        evidence: Mapping[str, object] | None = None,
    ) -> tuple[str, str]:
        body = {
            "schema": CONTROL_RECEIPT_SCHEMA,
            "receipt_kind": kind,
            "prior": dict(prior),
            "next": dict(next_state),
            "operator_subject": operator_subject,
            "reason": reason,
            "occurred_at": occurred_at,
            "evidence": None if evidence is None else dict(evidence),
        }
        digest = canonical_sha256(body)
        receipt_id = f"control:{digest[:32]}"
        connection.execute(
            """
            INSERT INTO authority_production_control_receipts(
                receipt_id, receipt_kind, prior_switch_epoch, next_switch_epoch,
                prior_writer_epoch, next_writer_epoch, operator_subject, reason,
                occurred_at, receipt_json, receipt_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id, kind,
                int(prior["switch_epoch"]), int(next_state["switch_epoch"]),
                int(prior["writer_epoch"]), int(next_state["writer_epoch"]),
                operator_subject, reason, occurred_at,
                canonical_bytes(body).decode("utf-8"), digest,
            ),
        )
        connection.execute(
            "UPDATE authority_production_writer_state SET last_receipt_sha256=? "
            "WHERE singleton=1",
            (digest,),
        )
        return receipt_id, digest

    def configure_writer(
        self,
        *,
        new_writer_id: str | None,
        enabled: bool,
        expected_writer_epoch: int,
        expected_switch_epoch: int,
        operator_subject: str,
        reason: str,
        occurred_at: int,
        phase3_owner_claims: tuple[ArtifactOwnerOperatorClaim, ...] = (),
    ) -> ControlResult:
        writer_id = _identifier(new_writer_id, "new_writer_id", optional=True)
        if type(enabled) is not bool or (enabled and writer_id is None):
            raise AuthorityOperationError("enabled writer requires a writer_id")
        writer_epoch = _nonnegative(expected_writer_epoch, "expected_writer_epoch")
        switch_epoch = _nonnegative(expected_switch_epoch, "expected_switch_epoch")
        operator = _identifier(operator_subject, "operator_subject")
        reason_value = _text(reason, "reason")
        now = _nonnegative(occurred_at, "occurred_at")
        if type(phase3_owner_claims) is not tuple:
            raise AuthorityOperationError(
                "Phase-3 owner claims must be a frozen tuple"
            )
        try:
            claims = tuple(
                validate_artifact_owner_operator_claim(item)
                for item in phase3_owner_claims
            )
        except ValueError as exc:
            raise AuthorityOperationError(str(exc)) from exc
        if tuple(sorted(claims, key=lambda item: item.claim_sha256)) != claims or len(
            {item.claim_sha256 for item in claims}
        ) != len(claims):
            raise AuthorityOperationError(
                "Phase-3 owner claims must be uniquely identity sorted"
            )
        if any(item.operator_subject != operator for item in claims):
            raise AuthorityOperationError(
                "Phase-3 owner claim operator differs from receipt operator"
            )
        if claims and (not enabled or writer_id is None):
            raise AuthorityOperationError(
                "Phase-3 owner claims require an enabled configured writer"
            )
        connection = connect_authority_rw(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify(connection)
            writer, consumer = self._state(connection)
            if writer["switch_mode"] != V1_ONLY:
                raise AuthorityOperationConflict("writer configuration requires V1_ONLY")
            if writer["writer_epoch"] != writer_epoch or writer["switch_epoch"] != switch_epoch:
                raise AuthorityOperationConflict("writer configuration CAS is stale")
            if writer["writer_id"] == writer_id and bool(writer["writer_enabled"]) == enabled:
                if claims:
                    raise AuthorityOperationConflict(
                        "Phase-3 owner claims require a new writer configuration receipt"
                    )
                connection.commit()
                return _control_result(
                    writer, consumer, changed=False, receipt_id=None, receipt_sha256=None
                )
            prior = _control_projection(writer, consumer)
            next_epoch = writer_epoch + 1
            connection.execute(
                "UPDATE authority_production_writer_state "
                "SET writer_id=?, writer_epoch=?, writer_enabled=? WHERE singleton=1",
                (writer_id, next_epoch, int(enabled)),
            )
            updated_writer, updated_consumer = self._state(connection)
            next_state = _control_projection(updated_writer, updated_consumer)
            grant_set_body = {
                "schema": PHASE3_OWNER_OPERATOR_GRANT_SET_SCHEMA,
                "claims": tuple(item.as_dict() for item in claims),
            }
            grant_evidence = (
                None
                if not claims
                else {
                    **grant_set_body,
                    "grant_set_sha256": canonical_sha256(grant_set_body),
                }
            )
            receipt_id, digest = self._receipt(
                connection, kind="WRITER_CONFIG", prior=prior, next_state=next_state,
                operator_subject=str(operator), reason=reason_value, occurred_at=now,
                evidence=grant_evidence,
            )
            connection.commit()
            return _control_result(
                updated_writer, updated_consumer, changed=True,
                receipt_id=receipt_id, receipt_sha256=digest,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def configure_consumer(
        self,
        *,
        new_consumer_id: str | None,
        enabled: bool,
        expected_consumer_epoch: int,
        expected_switch_epoch: int,
        operator_subject: str,
        reason: str,
        occurred_at: int,
    ) -> ControlResult:
        consumer_id = _identifier(new_consumer_id, "new_consumer_id", optional=True)
        if type(enabled) is not bool or (enabled and consumer_id is None):
            raise AuthorityOperationError("enabled consumer requires a consumer_id")
        consumer_epoch = _nonnegative(expected_consumer_epoch, "expected_consumer_epoch")
        switch_epoch = _nonnegative(expected_switch_epoch, "expected_switch_epoch")
        operator = _identifier(operator_subject, "operator_subject")
        reason_value = _text(reason, "reason")
        now = _nonnegative(occurred_at, "occurred_at")
        connection = connect_authority_rw(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify(connection)
            writer, consumer = self._state(connection)
            if writer["switch_mode"] != V1_ONLY:
                raise AuthorityOperationConflict("consumer configuration requires V1_ONLY")
            if consumer["consumer_epoch"] != consumer_epoch or writer["switch_epoch"] != switch_epoch:
                raise AuthorityOperationConflict("consumer configuration CAS is stale")
            if consumer["consumer_id"] == consumer_id and bool(consumer["consumer_enabled"]) == enabled:
                connection.commit()
                return _control_result(
                    writer, consumer, changed=False, receipt_id=None, receipt_sha256=None
                )
            prior = _control_projection(writer, consumer)
            connection.execute(
                "UPDATE authority_production_consumer_state "
                "SET consumer_id=?, consumer_epoch=?, consumer_enabled=? WHERE singleton=1",
                (consumer_id, consumer_epoch + 1, int(enabled)),
            )
            updated_writer, updated_consumer = self._state(connection)
            next_state = _control_projection(updated_writer, updated_consumer)
            receipt_id, digest = self._receipt(
                connection, kind="CONSUMER_CONFIG", prior=prior, next_state=next_state,
                operator_subject=str(operator), reason=reason_value, occurred_at=now,
            )
            connection.commit()
            return _control_result(
                updated_writer, updated_consumer, changed=True,
                receipt_id=receipt_id, receipt_sha256=digest,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def switch_mode(
        self,
        *,
        target_mode: str,
        expected_switch_epoch: int,
        operator_subject: str,
        reason: str,
        occurred_at: int,
    ) -> ControlResult:
        if target_mode not in SWITCH_MODES:
            raise AuthorityOperationError("unsupported switch mode")
        switch_epoch = _nonnegative(expected_switch_epoch, "expected_switch_epoch")
        operator = _identifier(operator_subject, "operator_subject")
        reason_value = _text(reason, "reason")
        now = _nonnegative(occurred_at, "occurred_at")
        connection = connect_authority_rw(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify(connection)
            writer, consumer = self._state(connection)
            if writer["switch_epoch"] != switch_epoch:
                raise AuthorityOperationConflict("switch epoch CAS is stale")
            current_mode = str(writer["switch_mode"])
            if current_mode == target_mode:
                connection.commit()
                return _control_result(
                    writer, consumer, changed=False, receipt_id=None, receipt_sha256=None
                )
            allowed = (
                (current_mode == V1_ONLY and target_mode == CANARY)
                or (current_mode == CANARY and target_mode == AUTHORITY_PRIMARY)
                or target_mode == V1_ONLY
            )
            if not allowed:
                raise AuthorityOperationConflict("unsupported switch transition")
            if target_mode != V1_ONLY and (
                not writer["writer_enabled"] or not consumer["consumer_enabled"]
            ):
                raise AuthorityOperationConflict("forward switch requires fenced writer and consumer")
            prior = _control_projection(writer, consumer)
            connection.execute(
                "UPDATE authority_production_writer_state "
                "SET switch_mode=?, switch_epoch=?, writer_enabled=? WHERE singleton=1",
                (
                    target_mode,
                    switch_epoch + 1,
                    0 if target_mode == V1_ONLY else int(writer["writer_enabled"]),
                ),
            )
            if target_mode == V1_ONLY:
                connection.execute(
                    "UPDATE authority_production_consumer_state "
                    "SET consumer_enabled=0 WHERE singleton=1"
                )
            updated_writer, updated_consumer = self._state(connection)
            next_state = _control_projection(updated_writer, updated_consumer)
            receipt_id, digest = self._receipt(
                connection, kind="SWITCH", prior=prior, next_state=next_state,
                operator_subject=str(operator), reason=reason_value, occurred_at=now,
            )
            connection.commit()
            return _control_result(
                updated_writer, updated_consumer, changed=True,
                receipt_id=receipt_id, receipt_sha256=digest,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def apply_auto_fallback(
        self,
        decision: AutoFallbackDecision,
        *,
        expected_switch_epoch: int,
        operator_subject: str,
        reason: str,
        occurred_at: int,
    ) -> ControlResult:
        if type(decision) is not AutoFallbackDecision:
            raise AuthorityOperationError("fallback decision type is unsupported")
        if (
            decision.current_mode not in SWITCH_MODES
            or decision.target_mode not in SWITCH_MODES
            or type(decision.hard_conditions) is not tuple
            or any(type(item) is not str or not item for item in decision.hard_conditions)
        ):
            raise AuthorityOperationError("fallback decision fields are unsupported")
        _sha(decision.health_report_sha256, "decision.health_report_sha256")
        expected_action = (
            "FALLBACK_TO_V1"
            if decision.current_mode != V1_ONLY and decision.hard_conditions
            else "NO_CHANGE"
        )
        expected_target = (
            V1_ONLY if expected_action == "FALLBACK_TO_V1" else decision.current_mode
        )
        if decision.action != expected_action or decision.target_mode != expected_target:
            raise AuthorityOperationConflict("fallback decision is internally inconsistent")
        switch_epoch = _nonnegative(expected_switch_epoch, "expected_switch_epoch")
        operator = _identifier(operator_subject, "operator_subject")
        reason_value = _text(reason, "reason")
        now = _nonnegative(occurred_at, "occurred_at")
        connection = connect_authority_rw(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            verify_production_control_structure(connection)
            writer, consumer = self._state(connection)
            if writer["switch_epoch"] != switch_epoch:
                raise AuthorityOperationConflict("fallback switch epoch CAS is stale")
            current_mode = str(writer["switch_mode"])
            if decision.current_mode != current_mode:
                if current_mode != V1_ONLY:
                    raise AuthorityOperationConflict("fallback current mode differs")
            if current_mode == V1_ONLY or decision.action == "NO_CHANGE":
                connection.commit()
                return _control_result(
                    writer, consumer, changed=False, receipt_id=None, receipt_sha256=None
                )
            if not decision.hard_conditions or decision.target_mode != V1_ONLY:
                raise AuthorityOperationConflict("fallback requires a hard condition and V1 target")
            prior = _control_projection(writer, consumer)
            connection.execute(
                "UPDATE authority_production_writer_state "
                "SET switch_mode='V1_ONLY', switch_epoch=?, writer_enabled=0 "
                "WHERE singleton=1 AND switch_epoch=?",
                (switch_epoch + 1, switch_epoch),
            )
            connection.execute(
                "UPDATE authority_production_consumer_state "
                "SET consumer_enabled=0 WHERE singleton=1"
            )
            updated_writer, updated_consumer = self._state(connection)
            next_state = _control_projection(updated_writer, updated_consumer)
            receipt_id, digest = self._receipt(
                connection, kind="AUTO_FALLBACK", prior=prior, next_state=next_state,
                operator_subject=str(operator), reason=reason_value, occurred_at=now,
                evidence={
                    "decision": decision.as_dict(),
                    "decision_sha256": decision.decision_sha256,
                },
            )
            connection.commit()
            return _control_result(
                updated_writer, updated_consumer, changed=True,
                receipt_id=receipt_id, receipt_sha256=digest,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_backup_evidence(
        self,
        evidence: BackupEvidence,
        *,
        backup: str | Path,
        occurred_at: int,
    ) -> str:
        if type(evidence) is not BackupEvidence:
            raise AuthorityOperationError("backup evidence type is unsupported")
        if evidence.source_fence_sha256 != self.expected_source_fence_sha256:
            raise AuthorityOperationConflict("backup evidence source fence differs")
        verify_authority_backup_file(backup, evidence)
        now = _nonnegative(occurred_at, "occurred_at")
        body = {
            "schema": "authority-production-operation-receipt-v1",
            "operation_kind": "BACKUP_RECORDED",
            "occurred_at": now,
            "evidence": evidence.as_dict(),
            "evidence_sha256": evidence.evidence_sha256,
        }
        digest = canonical_sha256(body)
        receipt_id = f"operation:{digest[:32]}"
        connection = connect_authority_rw(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify(connection)
            identity = verified_database_identity(connection)
            if evidence.database_id != identity["database_id"]:
                raise AuthorityOperationConflict(
                    "backup evidence database identity differs"
                )
            kind = (
                "INITIAL_PRE_AUTHORITY"
                if evidence.authority_state == "ABSENT"
                and evidence.production_state == "ABSENT"
                else "RECORDED_CHECKPOINT"
            )
            lineage = connection.execute(
                "SELECT * FROM authority_production_backup_lineage "
                "WHERE backup_sha256=?",
                (evidence.backup_sha256,),
            ).fetchone()
            lineage_values = (
                evidence.database_id,
                kind,
                evidence.backup_size,
                evidence.source_fence_sha256,
                evidence.source_database_content_sha256,
                evidence.evidence_sha256,
                evidence.lineage_sha256,
                evidence.occurred_at,
            )
            if lineage is None:
                connection.execute(
                    """
                    INSERT INTO authority_production_backup_lineage(
                        backup_sha256, database_id, backup_kind, backup_size,
                        source_fence_sha256, source_database_content_sha256,
                        evidence_sha256, lineage_sha256, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (evidence.backup_sha256, *lineage_values),
                )
            elif tuple(lineage[column] for column in (
                "database_id", "backup_kind", "backup_size",
                "source_fence_sha256", "source_database_content_sha256",
                "evidence_sha256", "lineage_sha256", "recorded_at",
            )) != lineage_values:
                raise AuthorityOperationConflict("backup lineage identity differs")
            existing = connection.execute(
                "SELECT receipt_sha256 FROM authority_production_operation_receipts "
                "WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO authority_production_operation_receipts "
                    "VALUES (?, 'BACKUP_RECORDED', ?, ?, ?, ?, ?)",
                    (
                        receipt_id, now,
                        canonical_bytes(evidence.as_dict()).decode("utf-8"),
                        evidence.evidence_sha256,
                        canonical_bytes(body).decode("utf-8"), digest,
                    ),
                )
            elif existing[0] != digest:
                raise AuthorityOperationConflict("operation receipt identity differs")
            connection.commit()
            return digest
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_or_rotate_run_generation(
        self,
        request: "RunGenerationRequestV1",
        *,
        source_repository: str | Path,
        official_input_root: str | Path,
        execution_context_receipt_path: str | Path,
        clock: Callable[[], int] | None = None,
    ) -> "RunGenerationCreationResult":
        """Use the reviewed Phase9 service without exposing SQLite or SQL.

        The local import keeps Phase9 creation out of older operational import
        graphs until an operator explicitly invokes this narrow method.
        """

        from .phase9_run_generation import Phase9RunGenerationService

        return Phase9RunGenerationService(
            self.path,
            expected_source_fence_sha256=self.expected_source_fence_sha256,
            source_repository=source_repository,
            official_input_root=official_input_root,
            execution_context_receipt_path=execution_context_receipt_path,
            clock=clock,
        ).create_or_rotate(request)


def evaluate_authority_health(
    database: str | Path,
    *,
    expected_source_fence_sha256: str,
    policy: AuthorityHealthPolicy,
    evaluated_at: int,
    backup_evidence: BackupEvidence | None = None,
    restore_evidence: RestoreEvidence | None = None,
) -> AuthorityHealthReport:
    path = authority_database_path(database)
    expected_fence = _sha(expected_source_fence_sha256, "expected_source_fence_sha256")
    now = _nonnegative(evaluated_at, "evaluated_at")
    if type(policy) is not AuthorityHealthPolicy:
        raise AuthorityOperationError("health policy type is unsupported")
    conditions: list[str] = []
    metrics = {
        "backlog": 0,
        "oldest": 0,
        "in_flight": 0,
        "expired": 0,
        "retry_rate": 0,
        "dead": 0,
    }
    schema_ready = True
    source_schema_identity_sha256: str | None = None
    persisted_database_id: str | None = None
    persisted_database_identity_sha256: str | None = None
    recorded_lineages: dict[str, dict[str, object]] = {}
    connection = connect_authority_ro(path)
    try:
        connection.execute("BEGIN")
        try:
            state = verify_production_installation(connection, require_ready=True)
            source_schema_identity_sha256 = str(
                state["source_schema_identity_sha256"]
            )
            identity = verified_database_identity(connection)
            persisted_database_id = str(identity["database_id"])
            persisted_database_identity_sha256 = str(identity["binding_sha256"])
            recorded_lineages = {
                str(row["backup_sha256"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM authority_production_backup_lineage"
                ).fetchall()
            }
            actual_fence = legacy_source_identity_sha256(connection)
            if actual_fence != expected_fence:
                conditions.append("SOURCE_FENCE_DRIFT")
        except AuthorityProductionSchemaError as exc:
            schema_ready = False
            code = "SOURCE_FENCE_DRIFT" if "source fence" in str(exc).lower() else "SCHEMA_OR_MIGRATION_NOT_READY"
            conditions.append(code)
        if schema_ready:
            rows = connection.execute(
                "SELECT status, attempt_count, lease_expires_at, next_attempt_at, updated_at "
                "FROM authority_production_outbox_delivery_state"
            ).fetchall()
            pending = [
                row for row in rows
                if row["status"] in {
                    "PENDING", "CLAIMED", "RETRY_WAIT", "RECONCILIATION_REQUIRED"
                }
            ]
            metrics["backlog"] = len(pending)
            if pending:
                metrics["oldest"] = max(0, now - min(int(row["updated_at"]) for row in pending))
            metrics["in_flight"] = sum(row["status"] == "CLAIMED" for row in rows)
            metrics["expired"] = sum(
                row["status"] == "CLAIMED"
                and row["lease_expires_at"] is not None
                and int(row["lease_expires_at"]) <= now
                for row in rows
            )
            attempts = sum(int(row["attempt_count"]) for row in rows)
            retries = sum(max(0, int(row["attempt_count"]) - 1) for row in rows)
            metrics["retry_rate"] = 0 if attempts == 0 else (retries * 1000) // attempts
            metrics["dead"] = sum(row["status"] == "DEAD_LETTER" for row in rows)
        connection.commit()
    finally:
        connection.close()
    comparisons = (
        (metrics["backlog"] > policy.max_backlog_depth, "BACKLOG_DEPTH_EXCEEDED"),
        (metrics["oldest"] > policy.max_oldest_pending_age, "OLDEST_PENDING_AGE_EXCEEDED"),
        (metrics["in_flight"] > policy.max_in_flight, "IN_FLIGHT_EXCEEDED"),
        (metrics["expired"] > policy.max_expired_claims, "EXPIRED_CLAIM_EXCEEDED"),
        (metrics["retry_rate"] > policy.max_retry_per_thousand, "RETRY_RATE_EXCEEDED"),
        (metrics["dead"] > policy.max_dead_letter_count, "DEAD_LETTER_EXCEEDED"),
    )
    conditions.extend(code for tripped, code in comparisons if tripped)
    backup_age: int | None = None
    if backup_evidence is None:
        conditions.append("BACKUP_EVIDENCE_MISSING")
    else:
        backup_valid = type(backup_evidence) is BackupEvidence
        if backup_valid:
            try:
                _identifier(backup_evidence.database_id, "backup.database_id")
                _nonnegative(backup_evidence.occurred_at, "backup.occurred_at")
                _sha(
                    backup_evidence.source_schema_identity_sha256,
                    "backup.source_schema_identity_sha256",
                )
                _sha(
                    backup_evidence.source_fence_sha256,
                    "backup.source_fence_sha256",
                )
                _sha(
                    backup_evidence.source_database_content_sha256,
                    "backup.source_database_content_sha256",
                )
                _sha(
                    backup_evidence.source_main_file_sha256,
                    "backup.source_main_file_sha256",
                )
                _sha(backup_evidence.backup_sha256, "backup.backup_sha256")
                _nonnegative(
                    backup_evidence.source_main_file_size,
                    "backup.source_main_file_size",
                )
                _nonnegative(backup_evidence.backup_size, "backup.backup_size")
            except AuthorityOperationError:
                backup_valid = False
        if backup_valid and (
            backup_evidence.source_schema_version != SCHEMA_VERSION
            or backup_evidence.source_main_file_size < 1
            or backup_evidence.backup_size < 1
            or backup_evidence.integrity_check != "ok"
            or backup_evidence.authority_state
            not in {"ABSENT", "RUNNING", "INTERRUPTED", "READY"}
            or backup_evidence.production_state
            not in {"ABSENT", "RUNNING", "INTERRUPTED", "READY"}
            or (
                backup_evidence.production_last_migration is not None
                and type(backup_evidence.production_last_migration) is not str
            )
            or not _SHA256.fullmatch(backup_evidence.production_prefix_sha256)
            or backup_evidence.occurred_at > now
            or (
                source_schema_identity_sha256 is not None
                and backup_evidence.source_schema_identity_sha256
                != source_schema_identity_sha256
            )
        ):
            backup_valid = False
        if not backup_valid:
            conditions.append("BACKUP_EVIDENCE_INVALID")
        elif backup_evidence.database_id != persisted_database_id:
            conditions.append("BACKUP_DATABASE_IDENTITY_MISMATCH")
        elif backup_evidence.source_fence_sha256 != expected_fence:
            conditions.append("BACKUP_SOURCE_FENCE_DRIFT")
        else:
            lineage = recorded_lineages.get(backup_evidence.backup_sha256)
            expected_kind = (
                "INITIAL_PRE_AUTHORITY"
                if backup_evidence.authority_state == "ABSENT"
                and backup_evidence.production_state == "ABSENT"
                else "RECORDED_CHECKPOINT"
            )
            if lineage is None or (
                lineage["database_id"] != backup_evidence.database_id
                or lineage["backup_kind"] != expected_kind
                or lineage["backup_size"] != backup_evidence.backup_size
                or lineage["source_fence_sha256"]
                != backup_evidence.source_fence_sha256
                or lineage["source_database_content_sha256"]
                != backup_evidence.source_database_content_sha256
                or lineage["evidence_sha256"] != backup_evidence.evidence_sha256
                or lineage["lineage_sha256"] != backup_evidence.lineage_sha256
            ):
                conditions.append("BACKUP_LINEAGE_MISMATCH")
            backup_age = now - backup_evidence.occurred_at
            if backup_age > policy.max_backup_age:
                conditions.append("BACKUP_STALE")
    restore_present = restore_evidence is not None
    if policy.require_restore_evidence and not restore_present:
        conditions.append("RESTORE_EVIDENCE_MISSING")
    if restore_evidence is not None:
        restore_valid = type(restore_evidence) is RestoreEvidence
        if restore_valid:
            try:
                _identifier(restore_evidence.database_id, "restore.database_id")
                _nonnegative(restore_evidence.occurred_at, "restore.occurred_at")
                _nonnegative(
                    restore_evidence.expected_switch_epoch,
                    "restore.expected_switch_epoch",
                )
                _sha(restore_evidence.source_fence_sha256, "restore.source_fence_sha256")
                _sha(restore_evidence.backup_sha256, "restore.backup_sha256")
                _sha(restore_evidence.restored_sha256, "restore.restored_sha256")
                _sha(
                    restore_evidence.database_identity_sha256,
                    "restore.database_identity_sha256",
                )
                _sha(
                    restore_evidence.backup_lineage_sha256,
                    "restore.backup_lineage_sha256",
                )
                _nonnegative(restore_evidence.restored_size, "restore.restored_size")
            except AuthorityOperationError:
                restore_valid = False
        if restore_valid and (
            restore_evidence.occurred_at > now
            or restore_evidence.source_fence_sha256 != expected_fence
            or restore_evidence.backup_sha256 != restore_evidence.restored_sha256
            or restore_evidence.restored_size < 1
            or restore_evidence.integrity_check != "ok"
            or restore_evidence.restored_authority_state
            not in {"ABSENT", "RUNNING", "INTERRUPTED", "READY"}
            or restore_evidence.database_id != persisted_database_id
            or restore_evidence.database_identity_sha256
            != persisted_database_identity_sha256
            or (
                restore_evidence.backup_sha256 in recorded_lineages
                and restore_evidence.backup_lineage_sha256
                != recorded_lineages[restore_evidence.backup_sha256]["lineage_sha256"]
            )
            or restore_evidence.backup_sha256 not in recorded_lineages
            or (
                type(backup_evidence) is BackupEvidence
                and (
                    restore_evidence.database_id != backup_evidence.database_id
                    or restore_evidence.backup_sha256 != backup_evidence.backup_sha256
                    or restore_evidence.restored_size != backup_evidence.backup_size
                )
            )
        ):
            restore_valid = False
        if not restore_valid:
            conditions.append("RESTORE_EVIDENCE_INVALID")
    return AuthorityHealthReport(
        now, expected_fence, schema_ready,
        metrics["backlog"], metrics["oldest"], metrics["in_flight"],
        metrics["expired"], metrics["retry_rate"], metrics["dead"],
        backup_age, restore_present, tuple(sorted(set(conditions))),
    )


def decide_auto_fallback(
    current_mode: str, health: AuthorityHealthReport
) -> AutoFallbackDecision:
    if current_mode not in SWITCH_MODES:
        raise AuthorityOperationError("unsupported current switch mode")
    if type(health) is not AuthorityHealthReport:
        raise AuthorityOperationError("health report type is unsupported")
    action = (
        "FALLBACK_TO_V1"
        if current_mode != V1_ONLY and health.hard_conditions
        else "NO_CHANGE"
    )
    return AutoFallbackDecision(
        current_mode,
        V1_ONLY if action == "FALLBACK_TO_V1" else current_mode,
        action,
        health.hard_conditions,
        health.report_sha256,
    )
