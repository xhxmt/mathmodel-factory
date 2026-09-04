"""Default-off Phase9-A forensic replay evidence finalization.

The service in this module is deliberately narrower than a worker or provider
runner.  It verifies a candidate-bound entry result, controlled-account start
authorization, packet/role/verdict/snapshot and fault-safety receipts, then
commits their hash-bound state machine in one Authority transaction.  It never
starts a process, contacts a network, dispatches an outbox item, publishes a
release, applies a migration, or enables delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import sqlite3
import stat
import sys
import time
from typing import Callable, Mapping
import unicodedata
import xml.etree.ElementTree as ET

from .authority_production_schema import (
    authority_database_path,
    connect_authority_ro,
    connect_authority_rw,
    legacy_source_identity_sha256,
    verify_production_installation,
)
from .canonical import canonical_bytes, canonical_sha256
from .phase9_authority_lease import (
    AuthorityStateLeaseError,
    authority_state_commit_lease,
    isolated_authority_snapshot_ro,
)
from .phase9_entry import (
    PHASE9_ENTRY_GATE_SCHEMA,
    P0_REQUIREMENTS,
    CandidateIdentity,
    Phase9EntryError,
    collect_phase9_entry_state_in_transaction,
)
from .phase9_run_generation import (
    GIT_TRACKED_SOURCE_ENTRY_SCHEMA,
    GIT_TRACKED_SOURCE_INVENTORY_SCHEMA,
    PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS,
    RUN_GENERATION_AUTHORIZATION_CONSUMPTION_SCHEMA,
    RUN_GENERATION_RECEIPT_SCHEMA,
    Phase9RunGenerationError,
    Phase9RunGenerationService,
    RunGenerationRequestV1,
    _StableDirectoryTree,
    _authorization_consumption_body,
    _run_generation_request_from_dict_binding,
    _verify_stored_source_inventory,
    read_current_git_source_snapshot,
    read_verified_execution_source_snapshot,
    verify_execution_context_receipt,
    verify_official_input_snapshot,
)


PHASE9_REPLAY_REQUEST_SCHEMA = "authority-phase9-forensic-replay-request-v2"
PHASE9_REPLAY_RESULT_SCHEMA = "authority-phase9-forensic-replay-result-v1"
PHASE9_REPLAY_PREFLIGHT_SCHEMA = "authority-phase9-forensic-preflight-v1"
PHASE9_REPLAY_STATE_SCHEMA = "authority-phase9-forensic-state-v2"
PHASE9_START_AUTHORIZATION_SCHEMA = "authority-phase9-start-authorization-v3"
PHASE9_TERMINAL_RECEIPT_SCHEMA = "authority-phase9-forensic-terminal-receipt-v3"
PHASE9_ROLE_PROCESS_RECEIPT_SCHEMA = "authority-phase9-role-process-receipt-v2"
PHASE9_ROLE_PROVIDER_RECEIPT_SCHEMA = "authority-phase9-role-provider-receipt-v2"
PHASE9_PROCESS_SCOPE_RECEIPT_SCHEMA = "authority-phase9-process-scope-receipt-v2"
PHASE9_ACCEPTANCE_CASE_RECEIPT_SCHEMA = "authority-phase9-acceptance-case-receipt-v3"
PHASE9_PACKET_COMPONENT_RECEIPT_SCHEMA = "authority-phase9-packet-component-receipt-v1"
PHASE9_OUTBOX_COMPONENT_RECEIPT_SCHEMA = "authority-phase9-outbox-component-receipt-v1"
PHASE9_SNAPSHOT_COMPONENT_RECEIPT_SCHEMA = "authority-phase9-snapshot-component-receipt-v1"
PHASE9_VERDICT_COMPONENT_RECEIPT_SCHEMA = "authority-phase9-verdict-component-receipt-v1"
PHASE9_ACCEPTANCE_COMMAND_SCHEMA = "authority-phase9-acceptance-command-record-v4"
PHASE9_ACCEPTANCE_RESULT_SCHEMA = "authority-phase9-acceptance-test-result-v3"
PHASE9_ACCEPTANCE_PYTHON_SCHEMA = "authority-phase9-python-executable-v1"
PHASE9_ACCEPTANCE_ENVIRONMENT_SCHEMA = (
    "authority-phase9-sanitized-acceptance-environment-v1"
)
PHASE9_EVIDENCE_PRODUCER_SCHEMA = "authority-phase9-evidence-producer-v1"
PHASE9_PACKET_PAYLOAD_SCHEMA = "authority-phase9-packet-v2"
PHASE9_GATE_CONSUMPTION_SCHEMA = "authority-phase9-entry-gate-consumption-v1"
PHASE9_TYPED_RECEIPT_SET_SCHEMA = "authority-phase9-typed-evidence-receipt-set-v1"
PHASE9_ROLE_EVIDENCE_SCHEMA = "authority-phase9-role-evidence-v2"
PHASE9_RUNTIME_EVIDENCE_SCHEMA = "authority-phase9-runtime-safety-evidence-v2"
PHASE9_ACCEPTANCE_EVIDENCE_SCHEMA = "authority-phase9-acceptance-evidence-v2"
PHASE9_REPLAY_EVIDENCE_ATTESTATION_SCHEMA = (
    "authority-phase9-replay-evidence-attestation-v1"
)
PHASE9_REPLAY_START_CONSUMPTION_SCHEMA = (
    "authority-phase9-start-authorization-consumption-v1"
)

CREATE = "CREATE"
ROTATE = "ROTATE"
TECHNICAL = "TECHNICAL"
ABLATE_NO_JUDGE = "ABLATE_NO_JUDGE"
DELIVERY_DISABLED = "DISABLED"
RESUME_TARGET = "STEP13_PACKET_REBUILD"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OID = re.compile(r"[0-9a-f]{40}\Z")
_CONTROL_FILES = {
    "acceptance.json",
    "entry_gate.json",
    "outbox_supervisor.json",
    "packet.json",
    "roles.json",
    "snapshot.json",
    "start_authorization.json",
    "verdict.json",
}
_COMPONENT_RECEIPTS = {
    "PACKET": (
        "receipts/components/packet.json",
        PHASE9_PACKET_COMPONENT_RECEIPT_SCHEMA,
        "payload/packet.bin",
        "packet-evidence-finalizer",
    ),
    "OUTBOX": (
        "receipts/components/outbox.json",
        PHASE9_OUTBOX_COMPONENT_RECEIPT_SCHEMA,
        "outbox_supervisor.json",
        "outbox-evidence-finalizer",
    ),
    "SNAPSHOT": (
        "receipts/components/snapshot.json",
        PHASE9_SNAPSHOT_COMPONENT_RECEIPT_SCHEMA,
        "snapshot.json",
        "snapshot-evidence-finalizer",
    ),
    "VERDICT": (
        "receipts/components/verdict.json",
        PHASE9_VERDICT_COMPONENT_RECEIPT_SCHEMA,
        "verdict.json",
        "verdict-evidence-finalizer",
    ),
}
PHASE9_ACCEPTANCE_CASES = (
    "AC-DEL-001",
    "AC-DEL-002",
    "AC-OUT-001",
    "AC-OUT-002",
    "AC-OUT-004",
    "AC-PACKET-001",
    "AC-PACKET-002",
    "AC-PACKET-003",
    "AC-RUN4-001",
    "AC-RUN4-002",
    "AC-SNAP-001",
    "AC-SNAP-002",
    "AC-SUP-001",
    "AC-SUP-002",
    "AC-SUP-004",
    "AC-VERDICT-001",
    "AC-VERDICT-003",
)
PHASE9_ACCEPTANCE_TEST_NODES = {
    case_id: (
        "tests/test_phase9_acceptance_probes.py::test_"
        + case_id.lower().replace("-", "_")
    )
    for case_id in PHASE9_ACCEPTANCE_CASES
}

_ACCEPTANCE_BLOCKED_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "AUTHORITY_DATABASE",
    "AUTHORITY_DB",
    "CLOUD_SOLVER_URL",
    "DATABASE_URL",
    "DEPLOYMENT_ENV",
    "OPENAI_API_KEY",
    "PHASE78_ENABLED",
    "PHASE9_ENABLED",
    "PRODUCTION_DATABASE",
    "PRODUCTION_DB",
    "PRODUCTION_OUTBOX",
    "PRODUCTION_RELEASE",
    "PROVIDER_API_KEY",
    "SOLVER_API_KEY",
)
_ACCEPTANCE_DISABLED_CAPABILITIES = {
    "provider_or_network": False,
    "production_outbox_or_delivery": False,
    "release": False,
    "deployment": False,
    "migration": False,
    "cutover": False,
}
PHASE9_ACCEPTANCE_SPEC_SHA256 = canonical_sha256(
    {
        "schema": "authority-phase9-acceptance-spec-v1",
        "cases": [
            {"case_id": case_id, "test_node": PHASE9_ACCEPTANCE_TEST_NODES[case_id]}
            for case_id in PHASE9_ACCEPTANCE_CASES
        ],
    }
)


class Phase9ForensicReplayError(RuntimeError):
    """Base Phase9-A finalization error."""


class Phase9ForensicReplayConflict(Phase9ForensicReplayError):
    """A current pointer, replay key, coordinate, or content binding differs."""


class Phase9ForensicReplaySafetyError(Phase9ForensicReplayError):
    """A default-off or evidence invariant is not satisfied."""


@dataclass(frozen=True)
class ReplayEvidenceFileV1:
    logical_path: str
    byte_length: int
    raw_bytes_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_path": self.logical_path,
            "byte_length": self.byte_length,
            "raw_bytes_sha256": self.raw_bytes_sha256,
        }


@dataclass(frozen=True)
class ValidatedEvidenceReceiptV1:
    receipt_kind: str
    logical_id: str
    logical_path: str
    byte_length: int
    raw_bytes_sha256: str
    receipt_json: str
    receipt_sha256: str
    occurred_at: int

    def set_item(self) -> dict[str, object]:
        return {
            "receipt_kind": self.receipt_kind,
            "logical_id": self.logical_id,
            "logical_path": self.logical_path,
            "byte_length": self.byte_length,
            "raw_bytes_sha256": self.raw_bytes_sha256,
            "receipt_sha256": self.receipt_sha256,
        }


@dataclass(frozen=True)
class Phase9ForensicReplayRequestV1:
    schema_version: str
    idempotency_key: str
    operation_kind: str
    project_id: str
    workflow_id: str
    project_revision: int
    project_generation: str
    run_generation: str
    run_generation_creation_receipt_sha256: str
    predecessor_replay_id: str | None
    predecessor_terminal_receipt_sha256: str | None
    replay_mode: str
    requested_resume_target: str
    delivery_capability: str
    source_commit: str
    source_tree: str
    source_parent: str
    source_inventory_sha256: str
    entry_gate_result_sha256: str
    evidence_files: tuple[ReplayEvidenceFileV1, ...]
    occurred_at: int

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "idempotency_key": self.idempotency_key,
            "operation_kind": self.operation_kind,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "project_revision": self.project_revision,
            "project_generation": self.project_generation,
            "run_generation": self.run_generation,
            "run_generation_creation_receipt_sha256": (
                self.run_generation_creation_receipt_sha256
            ),
            "predecessor_replay_id": self.predecessor_replay_id,
            "predecessor_terminal_receipt_sha256": (
                self.predecessor_terminal_receipt_sha256
            ),
            "replay_mode": self.replay_mode,
            "requested_resume_target": self.requested_resume_target,
            "delivery_capability": self.delivery_capability,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "source_parent": self.source_parent,
            "source_inventory_sha256": self.source_inventory_sha256,
            "entry_gate_result_sha256": self.entry_gate_result_sha256,
            "evidence_files": [item.as_dict() for item in self.evidence_files],
            "occurred_at": self.occurred_at,
        }

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def replay_id(self) -> str:
        return f"phase9-replay:{self.request_sha256}"

    @property
    def evidence_set_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "authority-phase9-replay-evidence-set-v1",
                "files": [item.as_dict() for item in self.evidence_files],
            }
        )


@dataclass(frozen=True)
class Phase9ForensicReplayResult:
    """Immutable terminal result.

    ``replayed`` is retained for wire compatibility but is itself part of the
    frozen result object.  Both an original commit and exact recovery therefore
    return ``False``; callers must not infer the service path from immutable
    result bytes.
    """

    replay_id: str
    workflow_id: str
    run_generation: str
    request_sha256: str
    terminal_reason: str
    effective_verdict: str
    receipt_id: str
    receipt_sha256: str
    occurred_at: int
    replayed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": PHASE9_REPLAY_RESULT_SCHEMA,
            "status": "COMPLETED",
            "replay_id": self.replay_id,
            "workflow_id": self.workflow_id,
            "run_generation": self.run_generation,
            "request_sha256": self.request_sha256,
            "terminal_reason": self.terminal_reason,
            "effective_verdict": self.effective_verdict,
            "delivery_capability": DELIVERY_DISABLED,
            "receipt_id": self.receipt_id,
            "receipt_sha256": self.receipt_sha256,
            "occurred_at": self.occurred_at,
            "replayed": self.replayed,
        }


def _text(value: object, path: str, *, identifier: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise Phase9ForensicReplaySafetyError(
            f"{path} must be a non-empty trimmed string"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise Phase9ForensicReplaySafetyError(f"{path} must be valid UTF-8") from exc
    if identifier and _IDENTIFIER.fullmatch(value) is None:
        raise Phase9ForensicReplaySafetyError(f"{path} is not a safe identifier")
    return value


def _sha(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise Phase9ForensicReplaySafetyError(f"{path} must be lowercase SHA-256")
    return value


def _git_oid(value: object, path: str) -> str:
    if type(value) is not str or _GIT_OID.fullmatch(value) is None:
        raise Phase9ForensicReplaySafetyError(f"{path} must be a Git object ID")
    return value


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise Phase9ForensicReplaySafetyError(
            f"{path} must be a plain integer >= {minimum}"
        )
    return value


def _mapping(
    value: object, path: str, keys: set[str] | None = None
) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise Phase9ForensicReplaySafetyError(f"{path} must be one JSON object")
    result = dict(value)
    if keys is not None and set(result) != keys:
        raise Phase9ForensicReplaySafetyError(f"{path} keys differ")
    return result


def _strict_json(raw: bytes, path: str) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise Phase9ForensicReplaySafetyError(
                    f"{path} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise Phase9ForensicReplaySafetyError(f"{path} is not strict JSON") from exc
    result = _mapping(value, path)
    if canonical_bytes(result) != raw:
        raise Phase9ForensicReplaySafetyError(f"{path} must use canonical JSON bytes")
    return result


def _phase9_forensic_replay_request_from_dict_binding(
    value: object,
) -> Phase9ForensicReplayRequestV1:
    """Decode strict JSON structure without semantic or live gates."""

    keys = {
        "schema_version", "idempotency_key", "operation_kind", "project_id",
        "workflow_id", "project_revision", "project_generation",
        "run_generation", "run_generation_creation_receipt_sha256",
        "predecessor_replay_id", "predecessor_terminal_receipt_sha256",
        "replay_mode", "requested_resume_target", "delivery_capability",
        "source_commit", "source_tree", "source_parent", "source_inventory_sha256",
        "entry_gate_result_sha256", "evidence_files", "occurred_at",
    }
    body = _mapping(value, "request", keys)
    files_value = body["evidence_files"]
    if type(files_value) is not list or not files_value:
        raise Phase9ForensicReplaySafetyError("request.evidence_files must be non-empty")
    files: list[ReplayEvidenceFileV1] = []
    for index, raw in enumerate(files_value):
        item = _mapping(
            raw,
            f"request.evidence_files[{index}]",
            {"logical_path", "byte_length", "raw_bytes_sha256"},
        )
        files.append(
            ReplayEvidenceFileV1(
                item["logical_path"],
                item["byte_length"],
                item["raw_bytes_sha256"],
            )
        )
    request = Phase9ForensicReplayRequestV1(
        body["schema_version"],
        body["idempotency_key"],
        body["operation_kind"],
        body["project_id"],
        body["workflow_id"],
        body["project_revision"],
        body["project_generation"],
        body["run_generation"],
        body["run_generation_creation_receipt_sha256"],
        body["predecessor_replay_id"],
        body["predecessor_terminal_receipt_sha256"],
        body["replay_mode"],
        body["requested_resume_target"],
        body["delivery_capability"],
        body["source_commit"],
        body["source_tree"],
        body["source_parent"],
        body["source_inventory_sha256"],
        body["entry_gate_result_sha256"],
        tuple(files),
        body["occurred_at"],
    )
    return request


def phase9_forensic_replay_request_from_dict(
    value: object,
) -> Phase9ForensicReplayRequestV1:
    return validate_phase9_forensic_replay_request(
        _phase9_forensic_replay_request_from_dict_binding(value)
    )


def read_phase9_forensic_replay_request(
    path: str | Path,
) -> Phase9ForensicReplayRequestV1:
    """Read one bounded, canonical, non-symlink request file."""

    raw = _regular_file_bytes(
        Path(path), maximum=4 * 1024 * 1024, label="Phase9 replay request"
    )
    return phase9_forensic_replay_request_from_dict(
        _strict_json(raw, "Phase9 replay request")
    )


def read_phase9_forensic_replay_request_binding(
    path: str | Path,
) -> Phase9ForensicReplayRequestV1:
    """Decode canonical request bytes for service-owned exact recovery.

    The returned object must be passed directly to ``execute``.  That service
    resolves an existing idempotency key before applying the full immutable
    and live new-write contract.
    """

    raw = _regular_file_bytes(
        Path(path), maximum=4 * 1024 * 1024, label="Phase9 replay request"
    )
    return _phase9_forensic_replay_request_from_dict_binding(
        _strict_json(raw, "Phase9 replay request")
    )


def validate_phase9_forensic_replay_request(
    request: Phase9ForensicReplayRequestV1,
) -> Phase9ForensicReplayRequestV1:
    if type(request) is not Phase9ForensicReplayRequestV1:
        raise Phase9ForensicReplaySafetyError("request type is unsupported")
    if request.schema_version != PHASE9_REPLAY_REQUEST_SCHEMA:
        raise Phase9ForensicReplaySafetyError("request schema is unsupported")
    for field, value in (
        ("idempotency_key", request.idempotency_key),
        ("project_id", request.project_id),
        ("workflow_id", request.workflow_id),
        ("project_generation", request.project_generation),
        ("run_generation", request.run_generation),
    ):
        _text(value, f"request.{field}", identifier=True)
        if value == "legacy_unknown":
            raise Phase9ForensicReplaySafetyError(f"request.{field} must be concrete")
    _integer(request.project_revision, "request.project_revision")
    _integer(request.occurred_at, "request.occurred_at")
    _sha(
        request.run_generation_creation_receipt_sha256,
        "request.run_generation_creation_receipt_sha256",
    )
    _sha(request.entry_gate_result_sha256, "request.entry_gate_result_sha256")
    for field, value in (
        ("source_commit", request.source_commit),
        ("source_tree", request.source_tree),
        ("source_parent", request.source_parent),
    ):
        _git_oid(value, f"request.{field}")
    _sha(request.source_inventory_sha256, "request.source_inventory_sha256")
    if request.operation_kind not in {CREATE, ROTATE}:
        raise Phase9ForensicReplaySafetyError("operation_kind must be CREATE or ROTATE")
    if request.replay_mode not in {TECHNICAL, ABLATE_NO_JUDGE}:
        raise Phase9ForensicReplaySafetyError("replay_mode is unsupported")
    if request.requested_resume_target != RESUME_TARGET:
        raise Phase9ForensicReplaySafetyError("resume target must be Step 13")
    if request.delivery_capability != DELIVERY_DISABLED:
        raise Phase9ForensicReplaySafetyError("delivery must remain DISABLED")
    if request.operation_kind == CREATE:
        if (
            request.predecessor_replay_id is not None
            or request.predecessor_terminal_receipt_sha256 is not None
        ):
            raise Phase9ForensicReplaySafetyError("CREATE cannot name a predecessor")
    else:
        _text(request.predecessor_replay_id, "request.predecessor_replay_id", identifier=True)
        _sha(
            request.predecessor_terminal_receipt_sha256,
            "request.predecessor_terminal_receipt_sha256",
        )
    if type(request.evidence_files) is not tuple or not request.evidence_files:
        raise Phase9ForensicReplaySafetyError(
            "request.evidence_files must be a non-empty tuple"
        )
    paths: list[str] = []
    ambiguity_keys: list[str] = []
    for index, item in enumerate(request.evidence_files):
        if type(item) is not ReplayEvidenceFileV1:
            raise Phase9ForensicReplaySafetyError(
                f"evidence_files[{index}] type is unsupported"
            )
        _text(item.logical_path, f"evidence_files[{index}].logical_path")
        _integer(
            item.byte_length,
            f"evidence_files[{index}].byte_length",
            minimum=1,
        )
        _sha(
            item.raw_bytes_sha256,
            f"evidence_files[{index}].raw_bytes_sha256",
        )
        pure = PurePosixPath(item.logical_path)
        if (
            pure.is_absolute()
            or item.logical_path != pure.as_posix()
            or ".." in pure.parts
            or "\\" in item.logical_path
            or any(part.endswith((".", " ")) for part in pure.parts)
        ):
            raise Phase9ForensicReplaySafetyError(
                f"evidence_files[{index}] path is not normalized relative POSIX"
            )
        if item.byte_length > 16 * 1024 * 1024:
            raise Phase9ForensicReplaySafetyError("one evidence file is too large")
        paths.append(item.logical_path)
        ambiguity_keys.append(
            unicodedata.normalize("NFC", item.logical_path).casefold()
        )
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise Phase9ForensicReplaySafetyError("evidence paths must be sorted and unique")
    if len(ambiguity_keys) != len(set(ambiguity_keys)):
        raise Phase9ForensicReplaySafetyError(
            "evidence paths contain Unicode or case-folding ambiguity"
        )
    if not _CONTROL_FILES.issubset(paths):
        raise Phase9ForensicReplaySafetyError("required control evidence files are missing")
    if sum(item.byte_length for item in request.evidence_files) > 64 * 1024 * 1024:
        raise Phase9ForensicReplaySafetyError("evidence set is too large")
    return request


def _validate_phase9_forensic_recovery_identity(
    request: Phase9ForensicReplayRequestV1,
) -> Phase9ForensicReplayRequestV1:
    if type(request) is not Phase9ForensicReplayRequestV1:
        raise Phase9ForensicReplaySafetyError("request type is unsupported")
    _text(request.workflow_id, "request.workflow_id", identifier=True)
    _text(request.idempotency_key, "request.idempotency_key", identifier=True)
    return request


def _regular_file_bytes(path: Path, *, maximum: int, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise Phase9ForensicReplaySafetyError(f"{label} is unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum
    ):
        raise Phase9ForensicReplaySafetyError(
            f"{label} must be a bounded non-hardlinked regular file"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        final = path.lstat()
    except OSError as exc:
        raise Phase9ForensicReplaySafetyError(f"{label} cannot be read safely") from exc
    identity = lambda item: (
        item.st_dev, item.st_ino, item.st_mode, item.st_nlink, item.st_size,
        item.st_mtime_ns, item.st_ctime_ns,
    )
    if (
        len(raw) > maximum
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or identity(before) != identity(opened)
        or identity(opened) != identity(after)
        or identity(after) != identity(final)
    ):
        raise Phase9ForensicReplaySafetyError(f"{label} changed while being read")
    return raw


def _read_evidence_set(
    root_value: str | Path, request: Phase9ForensicReplayRequestV1
) -> tuple[dict[str, bytes], tuple[tuple[object, ...], ...]]:
    def identity(metadata: os.stat_result) -> tuple[object, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    expected_directories = {"."}
    for file_descriptor in request.evidence_files:
        parent = PurePosixPath(file_descriptor.logical_path).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    expected = {item.logical_path: item for item in request.evidence_files}
    actual_files: set[str] = set()
    actual_directories = {"."}
    collision_keys: set[str] = set()
    values: dict[str, bytes] = {}
    records: list[tuple[object, ...]] = []
    root = Path(os.path.abspath(os.fspath(root_value)))
    try:
        with _StableDirectoryTree(root, label="Phase9 evidence root") as tree:
            root_metadata = root.lstat()
            records.append(("DIRECTORY", ".", *identity(root_metadata)))
            pending: list[tuple[str, ...]] = [()]
            while pending:
                parts = pending.pop()
                for name in tree.list_directory(parts):
                    logical_path = PurePosixPath(*parts, name).as_posix()
                    collision_key = unicodedata.normalize(
                        "NFC", logical_path
                    ).casefold()
                    if collision_key in collision_keys:
                        raise Phase9ForensicReplaySafetyError(
                            "evidence tree paths collide by Unicode normalization or case"
                        )
                    collision_keys.add(collision_key)
                    metadata = tree.member_stat(parts, name)
                    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                        metadata.st_mode
                    ):
                        if logical_path not in expected_directories:
                            raise Phase9ForensicReplaySafetyError(
                                "evidence inventory differs"
                            )
                        actual_directories.add(logical_path)
                        tree.directory((*parts, name))
                        pending.append((*parts, name))
                        records.append(
                            ("DIRECTORY", logical_path, *identity(metadata))
                        )
                        continue
                    if (
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                    ):
                        raise Phase9ForensicReplaySafetyError(
                            "evidence tree contains a symlink, hardlink, or special file"
                        )
                    item = expected.get(logical_path)
                    if item is None:
                        raise Phase9ForensicReplaySafetyError(
                            "evidence inventory differs"
                        )
                    raw, opened = tree.read_regular_file(
                        parts,
                        name,
                        maximum_bytes=item.byte_length,
                    )
                    if (
                        len(raw) != item.byte_length
                        or hashlib.sha256(raw).hexdigest()
                        != item.raw_bytes_sha256
                    ):
                        raise Phase9ForensicReplaySafetyError(
                            f"evidence bytes differ: {item.logical_path}"
                        )
                    actual_files.add(logical_path)
                    values[logical_path] = raw
                    records.append(("FILE", logical_path, *identity(opened)))
            if (
                actual_files != set(expected)
                or actual_directories != expected_directories
            ):
                raise Phase9ForensicReplaySafetyError("evidence inventory differs")
            tree.verify_unchanged()
    except Phase9ForensicReplayError:
        raise
    except Phase9RunGenerationError as exc:
        raise Phase9ForensicReplaySafetyError(str(exc)) from exc
    except OSError as exc:
        raise Phase9ForensicReplaySafetyError(
            "evidence tree changed while being inventoried"
        ) from exc
    return values, tuple(
        sorted(records, key=lambda item: (str(item[0]), str(item[1])))
    )


def _self_hash(body: Mapping[str, object], field: str, path: str) -> str:
    value = _mapping(body, path)
    recorded = _sha(value.get(field), f"{path}.{field}")
    unsigned = dict(value)
    unsigned.pop(field)
    if canonical_sha256(unsigned) != recorded:
        raise Phase9ForensicReplaySafetyError(f"{path} self-hash differs")
    return recorded


_FILE_REFERENCE_KEYS = {
    "logical_path", "byte_length", "raw_bytes_sha256", "receipt_sha256",
}
_RAW_FILE_REFERENCE_KEYS = {"logical_path", "byte_length", "raw_bytes_sha256"}
_ROLE_PROVIDER_RECEIPT_KEYS = {
    "schema", "receipt_id", "candidate", "project_id", "workflow_id",
    "run_generation", "producer", "replay_coordinate_sha256",
    "source_run_generation", "dependency_fingerprint_sha256", "event_id",
    "event_sequence", "predecessor_event_id", "predecessor_receipt_sha256",
    "input_sha256", "inherited", "predecessor_role_generation", "role",
    "role_generation", "invocation_id",
    "attempt_id", "process_scope_id", "packet_sha256", "output_path",
    "output_byte_length", "output_sha256", "provider_call_id",
    "provider_status", "occurred_at", "receipt_sha256",
}
_ROLE_PROCESS_RECEIPT_KEYS = {
    "schema", "receipt_id", "candidate", "project_id", "workflow_id",
    "run_generation", "producer", "replay_coordinate_sha256",
    "source_run_generation", "dependency_fingerprint_sha256", "event_id",
    "event_sequence", "predecessor_event_id", "predecessor_receipt_sha256",
    "input_sha256", "inherited", "predecessor_role_generation", "role",
    "role_generation", "invocation_id",
    "attempt_id", "process_scope_id", "process_kind", "process_status",
    "exit_code", "packet_sha256", "output_path", "output_byte_length",
    "output_sha256", "provider_receipt", "occurred_at", "receipt_sha256",
}
_PROCESS_SCOPE_RECEIPT_KEYS = {
    "schema", "receipt_id", "candidate", "project_id", "workflow_id",
    "run_generation", "producer", "replay_coordinate_sha256",
    "source_run_generation", "dependency_fingerprint_sha256", "event_id",
    "event_sequence", "predecessor_event_id", "predecessor_receipt_sha256",
    "input_sha256", "output_sha256", "action", "invocation_id", "attempt_id",
    "process_scope_id", "scope_kind", "process_identity_sha256", "result",
    "active_descendant_count", "occurred_at", "receipt_sha256",
}
_ACCEPTANCE_CASE_RECEIPT_KEYS = {
    "schema", "receipt_id", "candidate", "project_id", "workflow_id",
    "run_generation", "producer", "replay_coordinate_sha256",
    "source_run_generation", "dependency_fingerprint_sha256", "event_id",
    "event_sequence", "predecessor_event_id", "predecessor_receipt_sha256",
    "input_sha256", "output_sha256", "case_id", "result", "command_record", "raw_log",
    "test_result", "aggregate_command_sha256", "aggregate_raw_log_sha256",
    "aggregate_junit_sha256", "aggregate_event_log_sha256",
    "aggregate_outcome_sha256", "occurred_at", "receipt_sha256",
}
_ACCEPTANCE_COMMAND_KEYS = {
    "schema", "execution_domain", "candidate", "project_id", "workflow_id",
    "run_generation", "producer", "replay_coordinate_sha256",
    "source_run_generation", "dependency_fingerprint_sha256", "event_id",
    "event_sequence", "predecessor_event_id", "predecessor_receipt_sha256",
    "input_sha256", "output_sha256", "case_id", "acceptance_spec_sha256",
    "test_node", "source_inventory_sha256", "command_argv", "working_directory",
    "python_executable", "python_executable_descriptor", "environment",
    "started_at", "completed_at", "exit_code",
    "raw_log", "aggregate_command_sha256",
    "aggregate_raw_log_sha256", "aggregate_junit_sha256",
    "aggregate_event_log_sha256", "aggregate_outcome_sha256", "record_sha256",
}
_ACCEPTANCE_RESULT_KEYS = {
    "schema", "execution_domain", "candidate", "project_id", "workflow_id",
    "run_generation", "producer", "replay_coordinate_sha256",
    "source_run_generation", "dependency_fingerprint_sha256", "event_id",
    "event_sequence", "predecessor_event_id", "predecessor_receipt_sha256",
    "input_sha256", "output_sha256", "command_record", "raw_log",
    "case_id", "status",
    "collected", "passed", "failed", "errors", "skipped", "xfailed",
    "xpassed", "warnings", "exit_code", "aggregate_command_sha256",
    "aggregate_raw_log_sha256", "aggregate_junit_sha256",
    "aggregate_event_log_sha256", "aggregate_outcome_sha256", "result_sha256",
}
_ACCEPTANCE_AGGREGATE_FIELDS = (
    "aggregate_command_sha256", "aggregate_raw_log_sha256",
    "aggregate_junit_sha256", "aggregate_event_log_sha256",
    "aggregate_outcome_sha256",
)
_ACCEPTANCE_PYTHON_KEYS = {
    "schema", "requested_path", "resolved_path", "byte_length",
    "raw_bytes_sha256", "mode",
}
_ACCEPTANCE_ENVIRONMENT_KEYS = {
    "schema", "inherited", "variables", "blocked_host_variable_names",
    "capabilities", "environment_sha256",
}


def _file_reference(
    value: object,
    *,
    path: str,
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    self_hash_field: str,
) -> tuple[dict[str, object], dict[str, object]]:
    reference = _mapping(value, path, _FILE_REFERENCE_KEYS)
    logical_path = _text(reference["logical_path"], f"{path}.logical_path")
    byte_length = _integer(reference["byte_length"], f"{path}.byte_length", minimum=1)
    raw_sha256 = _sha(reference["raw_bytes_sha256"], f"{path}.raw_bytes_sha256")
    receipt_sha256 = _sha(reference["receipt_sha256"], f"{path}.receipt_sha256")
    indexed = {
        item.logical_path: item for item in request.evidence_files
    }.get(logical_path)
    raw = values.get(logical_path)
    if (
        indexed is None
        or raw is None
        or indexed.byte_length != byte_length
        or indexed.raw_bytes_sha256 != raw_sha256
        or len(raw) != byte_length
        or hashlib.sha256(raw).hexdigest() != raw_sha256
    ):
        raise Phase9ForensicReplaySafetyError(f"{path} file binding differs")
    body = _strict_json(raw, logical_path)
    if _self_hash(body, self_hash_field, logical_path) != receipt_sha256:
        raise Phase9ForensicReplaySafetyError(f"{path} semantic receipt hash differs")
    return reference, body


def _receipt_coordinate(
    body: Mapping[str, object],
    request: Phase9ForensicReplayRequestV1,
    *,
    path: str,
) -> None:
    if body.get("candidate") != {
        "commit": request.source_commit,
        "tree": request.source_tree,
        "parent": request.source_parent,
    }:
        raise Phase9ForensicReplaySafetyError(f"{path} candidate differs")
    if (
        body.get("project_id") != request.project_id
        or body.get("workflow_id") != request.workflow_id
        or body.get("run_generation") != request.run_generation
    ):
        raise Phase9ForensicReplaySafetyError(f"{path} coordinate differs")


def _replay_coordinate_sha256(request: Phase9ForensicReplayRequestV1) -> str:
    return canonical_sha256(
        {
            "schema": "authority-phase9-replay-coordinate-v1",
            "idempotency_key": request.idempotency_key,
            "operation_kind": request.operation_kind,
            "candidate": {
                "commit": request.source_commit,
                "tree": request.source_tree,
                "parent": request.source_parent,
            },
            "source_inventory_sha256": request.source_inventory_sha256,
            "project_id": request.project_id,
            "workflow_id": request.workflow_id,
            "project_revision": request.project_revision,
            "project_generation": request.project_generation,
            "run_generation": request.run_generation,
            "run_generation_creation_receipt_sha256": (
                request.run_generation_creation_receipt_sha256
            ),
            "predecessor_replay_id": request.predecessor_replay_id,
            "predecessor_terminal_receipt_sha256": (
                request.predecessor_terminal_receipt_sha256
            ),
            "replay_mode": request.replay_mode,
            "requested_resume_target": request.requested_resume_target,
            "delivery_capability": request.delivery_capability,
            "entry_gate_result_sha256": request.entry_gate_result_sha256,
            "occurred_at": request.occurred_at,
        }
    )


def _dependency_fingerprint_sha256(
    request: Phase9ForensicReplayRequestV1,
    *,
    receipt_kind: str,
    logical_id: str,
    input_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "schema": "authority-phase9-evidence-dependency-fingerprint-v1",
            "receipt_kind": receipt_kind,
            "logical_id": logical_id,
            "replay_coordinate_sha256": _replay_coordinate_sha256(request),
            "source_run_generation": request.run_generation,
            "source_inventory_sha256": request.source_inventory_sha256,
            "input_sha256": input_sha256,
        }
    )


def _acceptance_command_input_sha256(
    request: Phase9ForensicReplayRequestV1,
    *,
    case_id: str,
) -> str:
    return canonical_sha256(
        {
            "schema": "authority-phase9-acceptance-command-input-v1",
            "case_id": case_id,
            "test_node": PHASE9_ACCEPTANCE_TEST_NODES[case_id],
            "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
            "source_inventory_sha256": request.source_inventory_sha256,
        }
    )


def _evidence_event_id(
    *, receipt_kind: str, logical_id: str, dependency_fingerprint_sha256: str
) -> str:
    digest = canonical_sha256(
        {
            "schema": "authority-phase9-evidence-event-identity-v1",
            "receipt_kind": receipt_kind,
            "logical_id": logical_id,
            "dependency_fingerprint_sha256": dependency_fingerprint_sha256,
        }
    )
    return f"phase9-evidence-event:{digest}"


def _role_generation_id(
    request: Phase9ForensicReplayRequestV1,
    *,
    role: str,
    dependency_fingerprint_sha256: str,
) -> str:
    digest = canonical_sha256(
        {
            "schema": "authority-phase9-role-generation-identity-v1",
            "role": role,
            "replay_coordinate_sha256": _replay_coordinate_sha256(request),
            "source_run_generation": request.run_generation,
            "dependency_fingerprint_sha256": dependency_fingerprint_sha256,
        }
    )
    return f"phase9-role-generation:{digest}"


def _producer(
    value: object,
    *,
    request: Phase9ForensicReplayRequestV1,
    component: str,
    path: str,
) -> None:
    body = _mapping(
        value,
        path,
        {
            "schema", "execution_domain", "component", "component_version",
            "source_commit", "source_tree", "source_parent",
            "source_inventory_sha256",
        },
    )
    if body != {
        "schema": PHASE9_EVIDENCE_PRODUCER_SCHEMA,
        "execution_domain": "FORMAL_PHASE9_A",
        "component": component,
        "component_version": "2",
        "source_commit": request.source_commit,
        "source_tree": request.source_tree,
        "source_parent": request.source_parent,
        "source_inventory_sha256": request.source_inventory_sha256,
    }:
        raise Phase9ForensicReplaySafetyError(f"{path} producer differs")


def _validate_provenance(
    body: Mapping[str, object],
    *,
    request: Phase9ForensicReplayRequestV1,
    receipt_kind: str,
    logical_id: str,
    component: str,
    input_sha256: str,
    dependency_kind: str | None = None,
    dependency_input_sha256: str | None = None,
    event_sequence: int,
    predecessor_event_id: str | None,
    predecessor_receipt_sha256: str | None,
    path: str,
) -> tuple[str, str]:
    _producer(body.get("producer"), request=request, component=component, path=path)
    input_digest = _sha(input_sha256, f"{path}.expected_input_sha256")
    dependency = _dependency_fingerprint_sha256(
        request,
        receipt_kind=receipt_kind if dependency_kind is None else dependency_kind,
        logical_id=logical_id,
        input_sha256=(
            input_digest
            if dependency_input_sha256 is None
            else _sha(dependency_input_sha256, f"{path}.dependency_input_sha256")
        ),
    )
    event_id = _evidence_event_id(
        receipt_kind=receipt_kind,
        logical_id=logical_id,
        dependency_fingerprint_sha256=dependency,
    )
    if (
        body.get("replay_coordinate_sha256") != _replay_coordinate_sha256(request)
        or body.get("source_run_generation") != request.run_generation
        or body.get("dependency_fingerprint_sha256") != dependency
        or body.get("event_id") != event_id
        or _integer(body.get("event_sequence"), f"{path}.event_sequence", minimum=1)
        != event_sequence
        or body.get("predecessor_event_id") != predecessor_event_id
        or body.get("predecessor_receipt_sha256")
        != predecessor_receipt_sha256
        or body.get("input_sha256") != input_digest
    ):
        raise Phase9ForensicReplaySafetyError(f"{path} provenance differs")
    return dependency, event_id


def _extract_packet_claims(raw: bytes) -> tuple[list[str], list[str]]:
    """Derive the claim inventories from the exact canonical packet-v2 bytes."""

    packet = _strict_json(raw, "packet raw bytes")
    _mapping(
        packet,
        "packet raw bytes",
        {"schema", "rebuild_start", "required_claims", "claims"},
    )
    if (
        packet.get("schema") != PHASE9_PACKET_PAYLOAD_SCHEMA
        or packet.get("rebuild_start") != RESUME_TARGET
    ):
        raise Phase9ForensicReplaySafetyError(
            "packet raw bytes are not the required Step-13 packet-v2"
        )
    if canonical_bytes(packet) != raw:
        raise Phase9ForensicReplaySafetyError(
            "packet raw bytes must use canonical JSON serialization"
        )
    required = packet.get("required_claims")
    claims = packet.get("claims")
    if (
        type(required) is not list
        or type(claims) is not list
        or any(type(item) is not str or not item for item in required)
    ):
        raise Phase9ForensicReplaySafetyError(
            "packet raw claim inventory is malformed"
        )
    present: list[str] = []
    for index, raw_claim in enumerate(claims):
        claim = _mapping(
            raw_claim,
            f"packet raw bytes.claims[{index}]",
            {"claim_id", "content_sha256"},
        )
        present.append(
            _text(
                claim.get("claim_id"),
                f"packet raw bytes.claims[{index}].claim_id",
                identifier=True,
            )
        )
        _sha(
            claim.get("content_sha256"),
            f"packet raw bytes.claims[{index}].content_sha256",
        )
    if required != sorted(set(required)) or present != sorted(set(present)):
        raise Phase9ForensicReplaySafetyError(
            "packet raw claim inventories must be sorted and unique"
        )
    return list(required), present


_PYTEST_OUTCOME_NAMES = {
    "passed": "passed",
    "failed": "failed",
    "error": "errors",
    "errors": "errors",
    "skipped": "skipped",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
    "warning": "warnings",
    "warnings": "warnings",
}


def _parse_pytest_case_log(
    raw: bytes, *, case_id: str, expected_node: str
) -> dict[str, int]:
    """Derive one case outcome from a complete one-case or fixed-suite log."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Phase9ForensicReplaySafetyError(
            "acceptance raw log is not UTF-8"
        ) from exc
    if not text.endswith("\n") or "\x1b" in text or "\x00" in text:
        raise Phase9ForensicReplaySafetyError(
            "acceptance raw log is truncated or contains control encoding"
        )
    collected_matches = re.findall(
        r"(?m)^collected ([0-9]+) items?\s*$", text
    )
    if collected_matches not in (["1"], [str(len(PHASE9_ACCEPTANCE_CASES))]):
        raise Phase9ForensicReplaySafetyError(
            "acceptance raw log must contain one complete reviewed collected count"
        )
    node_outcomes = re.findall(
        r"(?m)^(\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
        r"(?:\s+\[[^\]]+\])?\s*$",
        text,
    )
    expected_nodes = [
        PHASE9_ACCEPTANCE_TEST_NODES[value]
        for value in PHASE9_ACCEPTANCE_CASES
    ]
    observed_nodes = [value[0] for value in node_outcomes]
    single_case = observed_nodes == [expected_node]
    fixed_suite = observed_nodes == expected_nodes
    if (
        not (single_case or fixed_suite)
        or any(value[1] != "PASSED" for value in node_outcomes)
        or (single_case and collected_matches != ["1"])
        or (fixed_suite and collected_matches != [str(len(expected_nodes))])
    ):
        raise Phase9ForensicReplaySafetyError(
            "acceptance raw log does not show the exact reviewed passing node "
            f"inventory for {case_id}"
        )
    nonempty = [line for line in text.splitlines() if line.strip()]
    if not nonempty:
        raise Phase9ForensicReplaySafetyError("acceptance raw log is empty")
    summary_match = re.fullmatch(r"=+\s*(.*?)\s*=+", nonempty[-1])
    if summary_match is None:
        raise Phase9ForensicReplaySafetyError(
            "acceptance raw log lacks a complete terminal pytest summary"
        )
    summary = summary_match.group(1)
    duration = re.fullmatch(r"(.+?)\s+in\s+[0-9]+(?:\.[0-9]+)?s", summary)
    if duration is None:
        raise Phase9ForensicReplaySafetyError(
            "acceptance raw log terminal summary is malformed"
        )
    counts = {
        "collected": 1,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "warnings": 0,
    }
    observed: set[str] = set()
    for token in duration.group(1).split(", "):
        match = re.fullmatch(
            r"([0-9]+) (passed|failed|errors?|skipped|xfailed|xpassed|warnings?)",
            token,
        )
        if match is None:
            raise Phase9ForensicReplaySafetyError(
                "acceptance raw log terminal outcome is unsupported"
            )
        name = _PYTEST_OUTCOME_NAMES[match.group(2)]
        if name in observed:
            raise Phase9ForensicReplaySafetyError(
                "acceptance raw log terminal outcome is duplicated"
            )
        observed.add(name)
        counts[name] = int(match.group(1))
    expected_passed = 1 if single_case else len(expected_nodes)
    if observed != {"passed"} or counts["passed"] != expected_passed or sum(
        counts[name]
        for name in (
            "failed", "errors", "skipped", "xfailed", "xpassed", "warnings"
        )
    ):
        raise Phase9ForensicReplaySafetyError(
            "acceptance raw log is not a warning-free reviewed passing run"
        )
    # The returned typed result is the selected case projection, not a claim
    # that the aggregate command was executed separately for every case.
    counts["collected"] = 1
    counts["passed"] = 1
    return counts


def _validate_acceptance_python_descriptor(
    value: object, *, python_executable: str, path: str
) -> dict[str, object]:
    descriptor = _mapping(value, path, _ACCEPTANCE_PYTHON_KEYS)
    requested = _text(descriptor.get("requested_path"), f"{path}.requested_path")
    resolved = _text(descriptor.get("resolved_path"), f"{path}.resolved_path")
    if (
        not Path(requested).is_absolute()
        or not Path(resolved).is_absolute()
        or requested != python_executable
        or descriptor.get("schema") != PHASE9_ACCEPTANCE_PYTHON_SCHEMA
    ):
        raise Phase9ForensicReplaySafetyError(
            "acceptance Python executable descriptor differs"
        )
    _integer(descriptor.get("byte_length"), f"{path}.byte_length", minimum=1)
    _sha(descriptor.get("raw_bytes_sha256"), f"{path}.raw_bytes_sha256")
    mode = _integer(descriptor.get("mode"), f"{path}.mode", minimum=1)
    if mode > 0o7777 or not mode & 0o111:
        raise Phase9ForensicReplaySafetyError(
            "acceptance Python executable mode is not executable"
        )
    return descriptor


def _validate_acceptance_environment(
    value: object, *, working_directory: str, path: str
) -> dict[str, object]:
    environment = _mapping(value, path, _ACCEPTANCE_ENVIRONMENT_KEYS)
    variables = _mapping(
        environment.get("variables"),
        f"{path}.variables",
        {
            "LC_ALL", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONPATH",
        },
    )
    expected_variables = {
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": working_directory,
    }
    blocked = environment.get("blocked_host_variable_names")
    capabilities = _mapping(
        environment.get("capabilities"),
        f"{path}.capabilities",
        set(_ACCEPTANCE_DISABLED_CAPABILITIES),
    )
    if (
        environment.get("schema") != PHASE9_ACCEPTANCE_ENVIRONMENT_SCHEMA
        or environment.get("inherited") is not False
        or variables != expected_variables
        or blocked != list(_ACCEPTANCE_BLOCKED_ENVIRONMENT_NAMES)
        or capabilities != _ACCEPTANCE_DISABLED_CAPABILITIES
        or any(type(value) is not bool for value in capabilities.values())
        or _self_hash(environment, "environment_sha256", path)
        != environment.get("environment_sha256")
    ):
        raise Phase9ForensicReplaySafetyError(
            "acceptance sanitized environment differs"
        )
    return environment


def _validated_receipt(
    *,
    receipt_kind: str,
    logical_id: str,
    reference: Mapping[str, object],
    body: Mapping[str, object],
    occurred_at: int,
) -> ValidatedEvidenceReceiptV1:
    return ValidatedEvidenceReceiptV1(
        receipt_kind=receipt_kind,
        logical_id=logical_id,
        logical_path=str(reference["logical_path"]),
        byte_length=int(reference["byte_length"]),
        raw_bytes_sha256=str(reference["raw_bytes_sha256"]),
        receipt_json=canonical_bytes(body).decode("utf-8"),
        receipt_sha256=str(reference["receipt_sha256"]),
        occurred_at=occurred_at,
    )


_COMPONENT_RECEIPT_KEYS = {
    "schema", "execution_domain", "receipt_id", "candidate", "project_id",
    "workflow_id", "run_generation", "producer",
    "replay_coordinate_sha256", "source_run_generation",
    "dependency_fingerprint_sha256", "event_id", "event_sequence",
    "predecessor_event_id", "predecessor_receipt_sha256", "input_sha256",
    "output_sha256", "component", "evidence_logical_path",
    "evidence_sha256", "authority_source_sha256", "occurred_at",
    "receipt_sha256",
}


def _component_input_sha256(
    kind: str,
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    *,
    entry_state_receipt_sha256: str,
    runtime_counts: Mapping[str, object],
) -> str:
    if kind == "PACKET":
        return hashlib.sha256(values["payload/packet.bin"]).hexdigest()
    if kind == "VERDICT":
        return hashlib.sha256(values["roles.json"]).hexdigest()
    if kind == "OUTBOX":
        return canonical_sha256(
            {
                "schema": "authority-phase9-outbox-live-input-v1",
                "entry_state_receipt_sha256": entry_state_receipt_sha256,
                "runtime_counts": dict(runtime_counts),
            }
        )
    if kind == "SNAPSHOT":
        return canonical_sha256(
            {
                "schema": "authority-phase9-snapshot-live-input-v1",
                "project_id": request.project_id,
                "project_revision": request.project_revision,
                "project_generation": request.project_generation,
                "workflow_id": request.workflow_id,
                "run_generation": request.run_generation,
                "entry_state_receipt_sha256": entry_state_receipt_sha256,
            }
        )
    raise Phase9ForensicReplaySafetyError("unknown component receipt kind")


def _component_authority_source_sha256(
    kind: str,
    request: Phase9ForensicReplayRequestV1,
    *,
    entry_state_receipt_sha256: str,
    input_sha256: str,
    output_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "schema": "authority-phase9-component-source-v1",
            "component": kind,
            "replay_coordinate_sha256": _replay_coordinate_sha256(request),
            "entry_gate_result_sha256": request.entry_gate_result_sha256,
            "entry_state_receipt_sha256": entry_state_receipt_sha256,
            "input_sha256": input_sha256,
            "output_sha256": output_sha256,
        }
    )


def _validate_component_receipts(
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    *,
    entry_state_receipt_sha256: str,
    runtime_counts: Mapping[str, object],
) -> tuple[ValidatedEvidenceReceiptV1, ...]:
    receipts: list[ValidatedEvidenceReceiptV1] = []
    descriptors = {item.logical_path: item for item in request.evidence_files}
    for kind, (logical_path, schema, evidence_path, producer_component) in (
        _COMPONENT_RECEIPTS.items()
    ):
        descriptor = descriptors.get(logical_path)
        raw = values.get(logical_path)
        if descriptor is None or raw is None:
            raise Phase9ForensicReplaySafetyError(
                f"formal evidence lacks the {kind} typed component receipt"
            )
        body = _strict_json(raw, logical_path)
        _mapping(body, logical_path, _COMPONENT_RECEIPT_KEYS)
        if (
            body.get("schema") != schema
            or body.get("execution_domain") != "FORMAL_PHASE9_A"
            or body.get("component") != kind
            or body.get("evidence_logical_path") != evidence_path
        ):
            raise Phase9ForensicReplaySafetyError(
                f"{kind} typed component receipt domain differs"
            )
        _receipt_coordinate(body, request, path=logical_path)
        evidence_descriptor = descriptors.get(evidence_path)
        if evidence_descriptor is None:
            raise Phase9ForensicReplaySafetyError(
                f"{kind} component evidence is absent from the request"
            )
        input_sha256 = _component_input_sha256(
            kind,
            request,
            values,
            entry_state_receipt_sha256=entry_state_receipt_sha256,
            runtime_counts=runtime_counts,
        )
        output_sha256 = evidence_descriptor.raw_bytes_sha256
        _validate_provenance(
            body,
            request=request,
            receipt_kind=kind,
            logical_id=kind.lower(),
            component=producer_component,
            input_sha256=input_sha256,
            event_sequence=1,
            predecessor_event_id=None,
            predecessor_receipt_sha256=None,
            path=logical_path,
        )
        expected_authority_source = _component_authority_source_sha256(
            kind,
            request,
            entry_state_receipt_sha256=entry_state_receipt_sha256,
            input_sha256=input_sha256,
            output_sha256=output_sha256,
        )
        if (
            body.get("receipt_id")
            != f"phase9-{kind.lower()}-component:{request.run_generation}"
            or body.get("evidence_sha256") != output_sha256
            or body.get("output_sha256") != output_sha256
            or body.get("authority_source_sha256")
            != expected_authority_source
            or body.get("occurred_at") != request.occurred_at
            or body.get("receipt_sha256")
            != canonical_sha256(
                {key: value for key, value in body.items() if key != "receipt_sha256"}
            )
            or len(raw) != descriptor.byte_length
            or hashlib.sha256(raw).hexdigest() != descriptor.raw_bytes_sha256
        ):
            raise Phase9ForensicReplaySafetyError(
                f"{kind} typed component receipt semantics differ"
            )
        receipts.append(
            ValidatedEvidenceReceiptV1(
                receipt_kind=kind,
                logical_id=kind.lower(),
                logical_path=logical_path,
                byte_length=descriptor.byte_length,
                raw_bytes_sha256=descriptor.raw_bytes_sha256,
                receipt_json=canonical_bytes(body).decode("utf-8"),
                receipt_sha256=str(body["receipt_sha256"]),
                occurred_at=request.occurred_at,
            )
        )
    return tuple(receipts)


def _validate_provider_receipt(
    value: object,
    *,
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    role: str,
    role_generation: str,
    packet_sha256: str,
    output_path: str,
    output_byte_length: int,
    output_sha256: str,
) -> tuple[ValidatedEvidenceReceiptV1, dict[str, object]]:
    reference, body = _file_reference(
        value,
        path=f"roles.{role}.provider_receipt",
        request=request,
        values=values,
        self_hash_field="receipt_sha256",
    )
    _mapping(
        body, f"roles.{role}.provider_receipt.body", _ROLE_PROVIDER_RECEIPT_KEYS
    )
    if body.get("schema") != PHASE9_ROLE_PROVIDER_RECEIPT_SCHEMA:
        raise Phase9ForensicReplaySafetyError("role provider receipt schema differs")
    _receipt_coordinate(body, request, path=f"roles.{role}.provider_receipt")
    dependency, _provider_event_id = _validate_provenance(
        body,
        request=request,
        receipt_kind="ROLE_PROVIDER",
        dependency_kind="ROLE",
        logical_id=role,
        component="provider-runtime",
        input_sha256=packet_sha256,
        event_sequence=1,
        predecessor_event_id=None,
        predecessor_receipt_sha256=None,
        path=f"roles.{role}.provider_receipt",
    )
    expected_role_generation = _role_generation_id(
        request,
        role=role,
        dependency_fingerprint_sha256=dependency,
    )
    for name in (
        "receipt_id", "invocation_id", "attempt_id", "process_scope_id",
        "provider_call_id",
    ):
        _text(body.get(name), f"roles.{role}.provider_receipt.{name}", identifier=True)
    occurred_at = _integer(
        body.get("occurred_at"), f"roles.{role}.provider_receipt.occurred_at"
    )
    _integer(
        body.get("output_byte_length"),
        f"roles.{role}.provider_receipt.output_byte_length",
        minimum=1,
    )
    if occurred_at > request.occurred_at:
        raise Phase9ForensicReplaySafetyError(
            "role provider receipt occurs after replay metadata"
        )
    if (
        body.get("role") != role
        or role_generation != expected_role_generation
        or body.get("role_generation") != expected_role_generation
        or body.get("inherited") is not False
        or body.get("predecessor_role_generation") is not None
        or body.get("packet_sha256") != packet_sha256
        or body.get("output_path") != output_path
        or body.get("output_byte_length") != output_byte_length
        or body.get("output_sha256") != output_sha256
        or body.get("provider_status") != "SUCCEEDED"
    ):
        raise Phase9ForensicReplaySafetyError("role provider receipt binding differs")
    return (
        _validated_receipt(
            receipt_kind="ROLE_PROVIDER",
            logical_id=role,
            reference=reference,
            body=body,
            occurred_at=occurred_at,
        ),
        body,
    )


def _validate_role_process_receipt(
    value: object,
    *,
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    role: str,
    role_generation: str,
    packet_sha256: str,
    output_path: str,
    output: bytes,
) -> tuple[ValidatedEvidenceReceiptV1, ValidatedEvidenceReceiptV1]:
    reference, body = _file_reference(
        value,
        path=f"roles.{role}.process_receipt",
        request=request,
        values=values,
        self_hash_field="receipt_sha256",
    )
    _mapping(body, f"roles.{role}.process_receipt.body", _ROLE_PROCESS_RECEIPT_KEYS)
    if body.get("schema") != PHASE9_ROLE_PROCESS_RECEIPT_SCHEMA:
        raise Phase9ForensicReplaySafetyError("role process receipt schema differs")
    _receipt_coordinate(body, request, path=f"roles.{role}.process_receipt")
    for name in ("receipt_id", "invocation_id", "attempt_id", "process_scope_id"):
        _text(body.get(name), f"roles.{role}.process_receipt.{name}", identifier=True)
    occurred_at = _integer(
        body.get("occurred_at"), f"roles.{role}.process_receipt.occurred_at"
    )
    _integer(body.get("exit_code"), f"roles.{role}.process_receipt.exit_code")
    _integer(
        body.get("output_byte_length"),
        f"roles.{role}.process_receipt.output_byte_length",
        minimum=1,
    )
    if occurred_at > request.occurred_at:
        raise Phase9ForensicReplaySafetyError(
            "role process receipt occurs after replay metadata"
        )
    output_sha256 = hashlib.sha256(output).hexdigest()
    if (
        body.get("role") != role
        or body.get("role_generation") != role_generation
        or body.get("inherited") is not False
        or body.get("predecessor_role_generation") is not None
        or body.get("process_kind") != "ROLE"
        or body.get("process_status") != "COMPLETED"
        or body.get("exit_code") != 0
        or body.get("packet_sha256") != packet_sha256
        or body.get("output_path") != output_path
        or body.get("output_byte_length") != len(output)
        or body.get("output_sha256") != output_sha256
    ):
        raise Phase9ForensicReplaySafetyError("role process receipt binding differs")
    provider, provider_body = _validate_provider_receipt(
        body.get("provider_receipt"),
        request=request,
        values=values,
        role=role,
        role_generation=role_generation,
        packet_sha256=packet_sha256,
        output_path=output_path,
        output_byte_length=len(output),
        output_sha256=output_sha256,
    )
    process_dependency, _process_event_id = _validate_provenance(
        body,
        request=request,
        receipt_kind="ROLE_PROCESS",
        dependency_kind="ROLE",
        logical_id=role,
        component="role-process-supervisor",
        input_sha256=packet_sha256,
        event_sequence=2,
        predecessor_event_id=str(provider_body["event_id"]),
        predecessor_receipt_sha256=provider.receipt_sha256,
        path=f"roles.{role}.process_receipt",
    )
    if process_dependency != provider_body.get("dependency_fingerprint_sha256"):
        raise Phase9ForensicReplaySafetyError(
            "role provider/process dependency fingerprint differs"
        )
    for field in ("invocation_id", "attempt_id", "process_scope_id"):
        if provider_body.get(field) != body.get(field):
            raise Phase9ForensicReplaySafetyError(
                f"role provider/process {field} binding differs"
            )
    if _integer(
        provider_body.get("occurred_at"),
        f"roles.{role}.provider_receipt.occurred_at",
    ) > occurred_at:
        raise Phase9ForensicReplaySafetyError(
            "role provider receipt occurs after process completion"
        )
    return (
        _validated_receipt(
            receipt_kind="ROLE_PROCESS",
            logical_id=role,
            reference=reference,
            body=body,
            occurred_at=occurred_at,
        ),
        provider,
    )


def _validate_process_scope_receipt(
    value: object,
    *,
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    action_name: str,
) -> ValidatedEvidenceReceiptV1:
    reference, body = _file_reference(
        value,
        path=f"process_scope_receipts.{action_name}",
        request=request,
        values=values,
        self_hash_field="receipt_sha256",
    )
    _mapping(
        body,
        f"process_scope_receipts.{action_name}.body",
        _PROCESS_SCOPE_RECEIPT_KEYS,
    )
    if body.get("schema") != PHASE9_PROCESS_SCOPE_RECEIPT_SCHEMA:
        raise Phase9ForensicReplaySafetyError("process-scope receipt schema differs")
    _receipt_coordinate(body, request, path=f"process_scope_receipts.{action_name}")
    for name in (
        "receipt_id", "invocation_id", "attempt_id", "process_scope_id", "scope_kind",
    ):
        _text(body.get(name), f"process_scope_receipts.{action_name}.{name}", identifier=True)
    process_identity_sha256 = _sha(
        body.get("process_identity_sha256"),
        f"process_scope_receipts.{action_name}.process_identity_sha256",
    )
    _validate_provenance(
        body,
        request=request,
        receipt_kind="PROCESS_SCOPE",
        logical_id=action_name,
        component="process-scope-supervisor",
        input_sha256=process_identity_sha256,
        event_sequence=1,
        predecessor_event_id=None,
        predecessor_receipt_sha256=None,
        path=f"process_scope_receipts.{action_name}",
    )
    occurred_at = _integer(
        body.get("occurred_at"), f"process_scope_receipts.{action_name}.occurred_at"
    )
    _integer(
        body.get("active_descendant_count"),
        f"process_scope_receipts.{action_name}.active_descendant_count",
    )
    if occurred_at > request.occurred_at:
        raise Phase9ForensicReplaySafetyError(
            "process-scope receipt occurs after replay metadata"
        )
    if (
        body.get("action") != action_name.upper()
        or body.get("scope_kind") != "WORKER"
        or body.get("result") != "PASS"
        or body.get("active_descendant_count") != 0
        or body.get("output_sha256")
        != canonical_sha256(
            {
                "schema": "authority-phase9-process-scope-result-v1",
                "action": action_name.upper(),
                "process_identity_sha256": process_identity_sha256,
                "result": "PASS",
                "active_descendant_count": 0,
            }
        )
    ):
        raise Phase9ForensicReplaySafetyError("process-scope receipt result differs")
    return _validated_receipt(
        receipt_kind="PROCESS_SCOPE",
        logical_id=action_name,
        reference=reference,
        body=body,
        occurred_at=occurred_at,
    )


def _validate_acceptance_case_receipt(
    value: object,
    *,
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    case_id: str,
) -> tuple[ValidatedEvidenceReceiptV1, dict[str, object]]:
    reference, body = _file_reference(
        value,
        path=f"acceptance.{case_id}.receipt",
        request=request,
        values=values,
        self_hash_field="receipt_sha256",
    )
    _mapping(
        body,
        f"acceptance.{case_id}.receipt.body",
        _ACCEPTANCE_CASE_RECEIPT_KEYS,
    )
    if body.get("schema") != PHASE9_ACCEPTANCE_CASE_RECEIPT_SCHEMA:
        raise Phase9ForensicReplaySafetyError("acceptance receipt schema differs")
    _receipt_coordinate(body, request, path=f"acceptance.{case_id}.receipt")
    _text(body.get("receipt_id"), f"acceptance.{case_id}.receipt_id", identifier=True)
    occurred_at = _integer(body.get("occurred_at"), f"acceptance.{case_id}.occurred_at")
    if occurred_at > request.occurred_at + PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS:
        raise Phase9ForensicReplaySafetyError(
            "acceptance receipt occurs after replay metadata"
        )
    if body.get("case_id") != case_id or body.get("result") != "PASS":
        raise Phase9ForensicReplaySafetyError("acceptance receipt result differs")

    command_reference, command = _file_reference(
        body.get("command_record"),
        path=f"acceptance.{case_id}.command_record",
        request=request,
        values=values,
        self_hash_field="record_sha256",
    )
    raw_reference = _mapping(
        body.get("raw_log"), f"acceptance.{case_id}.raw_log", _RAW_FILE_REFERENCE_KEYS
    )
    result_reference, result = _file_reference(
        body.get("test_result"),
        path=f"acceptance.{case_id}.test_result",
        request=request,
        values=values,
        self_hash_field="result_sha256",
    )
    raw_path = _text(raw_reference["logical_path"], f"acceptance.{case_id}.raw_log.path")
    raw_length = _integer(
        raw_reference["byte_length"],
        f"acceptance.{case_id}.raw_log.byte_length",
        minimum=1,
    )
    raw_sha256 = _sha(
        raw_reference["raw_bytes_sha256"],
        f"acceptance.{case_id}.raw_log.raw_bytes_sha256",
    )
    raw_index = {item.logical_path: item for item in request.evidence_files}.get(raw_path)
    raw = values.get(raw_path)
    if (
        raw_index is None
        or raw is None
        or raw_index.byte_length != raw_length
        or raw_index.raw_bytes_sha256 != raw_sha256
        or len(raw) != raw_length
        or hashlib.sha256(raw).hexdigest() != raw_sha256
    ):
        raise Phase9ForensicReplaySafetyError("acceptance raw log binding differs")
    expected_node = PHASE9_ACCEPTANCE_TEST_NODES.get(case_id)
    if expected_node is None:
        raise Phase9ForensicReplaySafetyError(
            "acceptance case is absent from the fixed reviewed spec"
        )
    parsed_counts = _parse_pytest_case_log(
        raw, case_id=case_id, expected_node=expected_node
    )

    _mapping(
        result,
        f"acceptance.{case_id}.test_result.body",
        _ACCEPTANCE_RESULT_KEYS,
    )
    result_counts = {
        name: _integer(result.get(name), f"acceptance.{case_id}.{name}")
        for name in parsed_counts
    }
    result_exit_code = _integer(
        result.get("exit_code"), f"acceptance.{case_id}.result.exit_code"
    )
    if (
        result.get("schema") != PHASE9_ACCEPTANCE_RESULT_SCHEMA
        or result.get("execution_domain") != "FORMAL_PHASE9_A"
        or result.get("case_id") != case_id
        or result.get("status") != "PASS"
        or result.get("command_record") != command_reference
        or result.get("raw_log") != raw_reference
        or result_counts != parsed_counts
        or result_exit_code != 0
    ):
        raise Phase9ForensicReplaySafetyError("acceptance test result differs")
    _receipt_coordinate(result, request, path=f"acceptance.{case_id}.test_result")
    result_output_sha256 = canonical_sha256(
        {
            "schema": "authority-phase9-pytest-case-outcome-v1",
            "case_id": case_id,
            **parsed_counts,
            "exit_code": 0,
        }
    )
    if result.get("output_sha256") != result_output_sha256:
        raise Phase9ForensicReplaySafetyError(
            "acceptance test result semantic output differs"
        )

    _mapping(
        command,
        f"acceptance.{case_id}.command_record.body",
        _ACCEPTANCE_COMMAND_KEYS,
    )
    aggregate_binding = {
        name: _sha(
            body.get(name), f"acceptance.{case_id}.{name}"
        )
        for name in _ACCEPTANCE_AGGREGATE_FIELDS
    }
    if any(
        command.get(name) != value or result.get(name) != value
        for name, value in aggregate_binding.items()
    ):
        raise Phase9ForensicReplaySafetyError(
            "acceptance aggregate runner binding differs"
        )
    if (
        command.get("schema") != PHASE9_ACCEPTANCE_COMMAND_SCHEMA
        or command.get("execution_domain") != "FORMAL_PHASE9_A"
    ):
        raise Phase9ForensicReplaySafetyError("acceptance command schema/domain differs")
    _receipt_coordinate(command, request, path=f"acceptance.{case_id}.command_record")
    argv = command.get("command_argv")
    python_executable = _text(
        command.get("python_executable"),
        f"acceptance.{case_id}.python_executable",
    )
    working_directory = _text(
        command.get("working_directory"),
        f"acceptance.{case_id}.working_directory",
    )
    basetemp = None
    if type(argv) is list:
        basetemps = [
            item.split("=", 1)[1]
            for item in argv
            if type(item) is str and item.startswith("--basetemp=")
        ]
        if len(basetemps) == 1:
            basetemp = basetemps[0]
    expected_prefix = [
        python_executable,
        "-I",
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-vv",
        "--tb=short",
        "--color=no",
    ]
    one_case_command = (
        type(argv) is list
        and argv[: len(expected_prefix)] == expected_prefix
        and len(argv) == len(expected_prefix) + 2
        and argv[-1] == expected_node
    )
    aggregate_command = (
        type(argv) is list
        and argv[:4] == [python_executable, "-I", "-S", "-B"]
        and argv[-len(PHASE9_ACCEPTANCE_TEST_NODES):]
        == [
            PHASE9_ACCEPTANCE_TEST_NODES[value]
            for value in PHASE9_ACCEPTANCE_CASES
        ]
    )
    if (
        type(argv) is not list
        or any(type(item) is not str or not item for item in argv)
        or not (one_case_command or aggregate_command)
        or basetemp is None
        or not Path(basetemp).is_absolute()
        or command.get("case_id") != case_id
        or command.get("acceptance_spec_sha256")
        != PHASE9_ACCEPTANCE_SPEC_SHA256
        or command.get("test_node") != expected_node
        or command.get("source_inventory_sha256")
        != request.source_inventory_sha256
        or command.get("raw_log") != raw_reference
    ):
        raise Phase9ForensicReplaySafetyError("acceptance command binding differs")
    for name, path_value in (
        ("working_directory", working_directory),
        ("python_executable", python_executable),
    ):
        if not Path(path_value).is_absolute():
            raise Phase9ForensicReplaySafetyError(
                f"acceptance.{case_id}.{name} must be absolute"
            )
    python_descriptor = _validate_acceptance_python_descriptor(
        command.get("python_executable_descriptor"),
        python_executable=python_executable,
        path=f"acceptance.{case_id}.python_executable_descriptor",
    )
    environment = _validate_acceptance_environment(
        command.get("environment"),
        working_directory=working_directory,
        path=f"acceptance.{case_id}.environment",
    )
    started = _integer(command.get("started_at"), f"acceptance.{case_id}.started_at")
    completed = _integer(command.get("completed_at"), f"acceptance.{case_id}.completed_at")
    exit_code = _integer(command.get("exit_code"), f"acceptance.{case_id}.exit_code")
    if (
        completed < started
        or completed > occurred_at + PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS
        or exit_code != 0
    ):
        raise Phase9ForensicReplaySafetyError("acceptance command time range differs")
    command_input_sha256 = _acceptance_command_input_sha256(
        request, case_id=case_id
    )
    command_dependency, command_event_id = _validate_provenance(
        command,
        request=request,
        receipt_kind="ACCEPTANCE_COMMAND",
        dependency_kind="ACCEPTANCE_CASE",
        logical_id=case_id,
        component="acceptance-command-runner",
        input_sha256=command_input_sha256,
        event_sequence=1,
        predecessor_event_id=None,
        predecessor_receipt_sha256=None,
        path=f"acceptance.{case_id}.command_record",
    )
    if command.get("output_sha256") != raw_sha256:
        raise Phase9ForensicReplaySafetyError(
            "acceptance command provenance differs"
        )
    result_dependency, result_event_id = _validate_provenance(
        result,
        request=request,
        receipt_kind="ACCEPTANCE_RESULT",
        dependency_kind="ACCEPTANCE_CASE",
        logical_id=case_id,
        component="acceptance-result-parser",
        input_sha256=raw_sha256,
        dependency_input_sha256=command_input_sha256,
        event_sequence=2,
        predecessor_event_id=command_event_id,
        predecessor_receipt_sha256=str(command_reference["receipt_sha256"]),
        path=f"acceptance.{case_id}.test_result",
    )
    if result_dependency != command_dependency:
        raise Phase9ForensicReplaySafetyError(
            "acceptance result provenance differs"
        )
    receipt_dependency, _receipt_event_id = _validate_provenance(
        body,
        request=request,
        receipt_kind="ACCEPTANCE_CASE",
        logical_id=case_id,
        component="acceptance-case-finalizer",
        input_sha256=str(result_reference["receipt_sha256"]),
        dependency_input_sha256=command_input_sha256,
        event_sequence=3,
        predecessor_event_id=result_event_id,
        predecessor_receipt_sha256=str(result_reference["receipt_sha256"]),
        path=f"acceptance.{case_id}.receipt",
    )
    if (
        receipt_dependency != result_dependency
        or body.get("output_sha256") != result_reference["receipt_sha256"]
        or body.get("raw_log") != raw_reference
        or body.get("test_result") != result_reference
    ):
        raise Phase9ForensicReplaySafetyError(
            "acceptance receipt provenance differs"
        )
    if command_reference != body.get("command_record"):
        raise Phase9ForensicReplaySafetyError("acceptance command reference differs")
    return (
        _validated_receipt(
            receipt_kind="ACCEPTANCE_CASE",
            logical_id=case_id,
            reference=reference,
            body=body,
            occurred_at=occurred_at,
        ),
        {
            "case_id": case_id,
            "test_node": expected_node,
            "working_directory": working_directory,
            "python_executable": python_executable,
            "python_executable_descriptor": python_descriptor,
            "environment": environment,
            "basetemp": basetemp,
        },
    )


def _typed_receipt_set_sha256(
    receipts: list[ValidatedEvidenceReceiptV1],
) -> str:
    items = sorted(
        (receipt.set_item() for receipt in receipts),
        key=lambda item: (str(item["receipt_kind"]), str(item["logical_id"])),
    )
    if len({(item["receipt_kind"], item["logical_id"]) for item in items}) != len(items):
        raise Phase9ForensicReplaySafetyError("typed receipt identity is duplicated")
    if len({item["logical_path"] for item in items}) != len(items):
        raise Phase9ForensicReplaySafetyError("typed receipt path is duplicated")
    if len({item["raw_bytes_sha256"] for item in items}) != len(items):
        raise Phase9ForensicReplaySafetyError("typed receipt bytes are reused")
    receipt_ids = [
        _text(
            _strict_json(
                receipt.receipt_json.encode("utf-8"),
                f"typed receipt {receipt.receipt_kind}/{receipt.logical_id}",
            ).get("receipt_id"),
            f"typed receipt {receipt.receipt_kind}/{receipt.logical_id}.receipt_id",
            identifier=True,
        )
        for receipt in receipts
    ]
    if len(receipt_ids) != len(set(receipt_ids)):
        raise Phase9ForensicReplaySafetyError("typed receipt ID is reused")
    return canonical_sha256({"schema": PHASE9_TYPED_RECEIPT_SET_SCHEMA, "receipts": items})


def _verify_entry_gate(
    body: dict[str, object],
    request: Phase9ForensicReplayRequestV1,
    *,
    trusted_now: int,
) -> tuple[str, int]:
    expected_keys = {
        "schema", "status", "evaluated_at", "request_evaluated_at",
        "clock_skew_seconds", "candidate", "project_id", "workflow_id",
        "run_generation", "state_receipt_sha256", "source_verification_sha256",
        "creation_receipt_sha256", "operator_authorization_receipt_sha256",
        "official_input_manifest_sha256",
        "official_input_raw_bytes_set_sha256",
        "execution_context_receipt_sha256", "source_inventory_sha256",
        "p0_evidence_root_sha256",
        "p0_receipt_sha256s", "blockers", "authorization_scope",
        "gate_result_sha256",
    }
    _mapping(body, "entry_gate", expected_keys)
    if body.get("schema") != PHASE9_ENTRY_GATE_SCHEMA:
        raise Phase9ForensicReplaySafetyError("entry gate schema is unsupported")
    if body.get("status") != "READY" or body.get("blockers") != []:
        raise Phase9ForensicReplaySafetyError("entry gate is not READY")
    digest = _self_hash(body, "gate_result_sha256", "entry_gate")
    if digest != request.entry_gate_result_sha256:
        raise Phase9ForensicReplaySafetyError("entry gate result binding differs")
    evaluated_at = _integer(body.get("evaluated_at"), "entry_gate.evaluated_at")
    request_evaluated_at = body.get("request_evaluated_at")
    clock_skew_seconds = body.get("clock_skew_seconds")
    if (
        type(request_evaluated_at) is not int
        or type(clock_skew_seconds) is not int
        or request_evaluated_at - evaluated_at != clock_skew_seconds
        or abs(clock_skew_seconds) > PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS
    ):
        raise Phase9ForensicReplaySafetyError("entry gate clock metadata differs")
    if abs(trusted_now - evaluated_at) > PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS:
        raise Phase9ForensicReplaySafetyError("entry gate is stale at trusted current time")
    candidate = _mapping(
        body.get("candidate"),
        "entry_gate.candidate",
        {"commit", "tree", "parent"},
    )
    if (
        candidate.get("commit") != request.source_commit
        or candidate.get("tree") != request.source_tree
        or candidate.get("parent") != request.source_parent
        or body.get("project_id") != request.project_id
        or body.get("workflow_id") != request.workflow_id
        or body.get("run_generation") != request.run_generation
        or body.get("creation_receipt_sha256")
        != request.run_generation_creation_receipt_sha256
        or body.get("source_inventory_sha256")
        != request.source_inventory_sha256
    ):
        raise Phase9ForensicReplaySafetyError("entry gate coordinate differs")
    scope = _mapping(body.get("authorization_scope"), "entry_gate.authorization_scope")
    expected_scope = {
        "phase9_a_forensic_replay": False,
        "provider_or_network": False,
        "production_outbox_or_delivery": False,
        "release": False,
        "deployment": False,
        "migration": False,
        "cutover": False,
    }
    if scope != expected_scope:
        raise Phase9ForensicReplaySafetyError("entry gate exceeded its entry-only scope")
    p0 = _mapping(body.get("p0_receipt_sha256s"), "entry_gate.p0_receipts")
    if set(p0) != set(P0_REQUIREMENTS):
        raise Phase9ForensicReplaySafetyError("entry gate P0 inventory differs")
    for name, digest_value in p0.items():
        _sha(digest_value, f"entry_gate.p0_receipt_sha256s.{name}")
    state_receipt_sha256 = ""
    for field in (
        "state_receipt_sha256", "source_verification_sha256",
        "operator_authorization_receipt_sha256",
        "official_input_manifest_sha256", "official_input_raw_bytes_set_sha256",
        "execution_context_receipt_sha256", "p0_evidence_root_sha256",
    ):
        digest_value = _sha(body.get(field), f"entry_gate.{field}")
        if field == "state_receipt_sha256":
            state_receipt_sha256 = digest_value
    return state_receipt_sha256, evaluated_at


def _evidence_payload_descriptors(
    request: Phase9ForensicReplayRequestV1,
) -> tuple[ReplayEvidenceFileV1, ...]:
    start_count = sum(
        item.logical_path == "start_authorization.json"
        for item in request.evidence_files
    )
    if start_count > 1:
        raise Phase9ForensicReplaySafetyError(
            "evidence inventory contains duplicate start authorization"
        )
    values = tuple(
        item
        for item in request.evidence_files
        if item.logical_path != "start_authorization.json"
    )
    return values


def phase9_replay_evidence_payload_set_sha256(
    request: Phase9ForensicReplayRequestV1,
) -> str:
    """Hash the exact replay payload without the later-issued authorization file."""

    return canonical_sha256(
        {
            "schema": "authority-phase9-replay-evidence-payload-set-v1",
            "files": [
                item.as_dict() for item in _evidence_payload_descriptors(request)
            ],
        }
    )


def phase9_start_authorization_target_sha256(
    request: Phase9ForensicReplayRequestV1,
    *,
    evidence_attestation_sha256: str,
    entry_state_receipt_sha256: str,
) -> str:
    body = request.as_dict()
    body["evidence_files"] = [
        item.as_dict() for item in _evidence_payload_descriptors(request)
    ]
    return canonical_sha256(
        {
            "schema": "authority-phase9-start-authorization-target-v1",
            "replay_request_without_start_authorization": body,
            "evidence_payload_set_sha256": (
                phase9_replay_evidence_payload_set_sha256(request)
            ),
            "evidence_attestation_sha256": _sha(
                evidence_attestation_sha256,
                "evidence_attestation_sha256",
            ),
            "entry_state_receipt_sha256": _sha(
                entry_state_receipt_sha256,
                "entry_state_receipt_sha256",
            ),
        }
    )


def _verify_start_authorization(
    body: dict[str, object],
    request: Phase9ForensicReplayRequestV1,
    *,
    trusted_now: int,
    require_current_operator: bool = True,
) -> tuple[str, str, str, str, str, str]:
    if sum(
        item.logical_path == "start_authorization.json"
        for item in request.evidence_files
    ) != 1:
        raise Phase9ForensicReplaySafetyError(
            "evidence inventory must contain exactly one start authorization"
        )
    expected_keys = {
        "schema", "authorization_id", "authorization_mechanism", "authorized",
        "operator_uid", "operator_account", "operation", "project_id",
        "workflow_id", "run_generation", "source_commit", "source_tree",
        "source_parent", "source_inventory_sha256", "issued_at",
        "expires_at", "entry_gate_result_sha256", "entry_state_receipt_sha256",
        "replay_coordinate_sha256", "nonce_sha256",
        "authorization_target_sha256", "evidence_attestation_sha256",
        "evidence_payload_set_sha256", "authorization_scope",
        "authorization_receipt_sha256",
    }
    if set(body) != expected_keys:
        raise Phase9ForensicReplaySafetyError("start authorization keys differ")
    if body.get("schema") != PHASE9_START_AUTHORIZATION_SCHEMA:
        raise Phase9ForensicReplaySafetyError("start authorization schema differs")
    digest = _self_hash(body, "authorization_receipt_sha256", "start_authorization")
    if (
        body.get("authorized") is not True
        or body.get("authorization_mechanism") != "CONTROLLED_OS_ACCOUNT"
        or body.get("operation") != "PHASE9_A_FORENSIC_REPLAY"
        or body.get("project_id") != request.project_id
        or body.get("workflow_id") != request.workflow_id
        or body.get("run_generation") != request.run_generation
        or body.get("source_commit") != request.source_commit
        or body.get("source_tree") != request.source_tree
        or body.get("source_parent") != request.source_parent
        or body.get("source_inventory_sha256")
        != request.source_inventory_sha256
        or body.get("entry_gate_result_sha256") != request.entry_gate_result_sha256
        or body.get("replay_coordinate_sha256")
        != _replay_coordinate_sha256(request)
        or body.get("evidence_payload_set_sha256")
        != phase9_replay_evidence_payload_set_sha256(request)
    ):
        raise Phase9ForensicReplaySafetyError("start authorization coordinate differs")
    evidence_attestation_sha256 = _sha(
        body.get("evidence_attestation_sha256"),
        "start_authorization.evidence_attestation_sha256",
    )
    entry_state_receipt_sha256 = _sha(
        body.get("entry_state_receipt_sha256"),
        "start_authorization.entry_state_receipt_sha256",
    )
    nonce_sha256 = _sha(
        body.get("nonce_sha256"), "start_authorization.nonce_sha256"
    )
    expected_target = phase9_start_authorization_target_sha256(
        request,
        evidence_attestation_sha256=evidence_attestation_sha256,
        entry_state_receipt_sha256=entry_state_receipt_sha256,
    )
    if body.get("authorization_target_sha256") != expected_target:
        raise Phase9ForensicReplaySafetyError(
            "start authorization target differs"
        )
    issued = _integer(
        body.get("issued_at"), "start_authorization.issued_at", minimum=1
    )
    expires = _integer(
        body.get("expires_at"), "start_authorization.expires_at", minimum=1
    )
    if expires < issued or not issued <= trusted_now <= expires:
        raise Phase9ForensicReplaySafetyError(
            "start authorization is not valid at trusted current time"
        )
    if trusted_now - issued > PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS:
        raise Phase9ForensicReplaySafetyError(
            "start authorization issue time exceeds trusted clock skew"
        )
    operator_uid = _integer(body.get("operator_uid"), "start_authorization.operator_uid")
    operator_account = _text(
        body.get("operator_account"),
        "start_authorization.operator_account",
        identifier=True,
    )
    if require_current_operator:
        try:
            uid = os.geteuid()
            account = pwd.getpwuid(uid).pw_name
        except (AttributeError, KeyError) as exc:
            raise Phase9ForensicReplaySafetyError(
                "OS account cannot be verified"
            ) from exc
        if operator_uid != uid or operator_account != account:
            raise Phase9ForensicReplaySafetyError(
                "start authorization OS account differs"
            )
    scope = _mapping(body.get("authorization_scope"), "start_authorization.scope")
    expected_scope = {
        "phase9_a_forensic_replay": True,
        "provider_or_network": False,
        "production_outbox_or_delivery": False,
        "release": False,
        "deployment": False,
        "migration": False,
        "cutover": False,
    }
    if scope != expected_scope:
        raise Phase9ForensicReplaySafetyError("start authorization scope differs")
    authorization_id = _text(
        body.get("authorization_id"), "start_authorization.authorization_id",
        identifier=True,
    )
    return (
        digest,
        authorization_id,
        nonce_sha256,
        evidence_attestation_sha256,
        entry_state_receipt_sha256,
        expected_target,
    )


def _control(
    values: Mapping[str, bytes], path: str, schema: str
) -> dict[str, object]:
    body = _strict_json(values[path], path)
    if body.get("schema") != schema:
        raise Phase9ForensicReplaySafetyError(f"{path} schema differs")
    return body


def _effective(values: list[str]) -> str:
    if any(value == "FAIL" for value in values):
        return "FAIL"
    if values and all(value == "PASS" for value in values):
        return "PASS"
    return "INDETERMINATE"


def _evaluate_evidence(
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    *,
    trusted_now: int,
    require_start_authorization: bool = True,
    require_component_receipts: bool = True,
) -> dict[str, object]:
    if abs(request.occurred_at - trusted_now) > PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS:
        raise Phase9ForensicReplaySafetyError(
            "request occurrence metadata exceeds trusted clock skew"
        )
    entry = _control(values, "entry_gate.json", PHASE9_ENTRY_GATE_SCHEMA)
    entry_state_sha256, entry_evaluated_at = _verify_entry_gate(
        entry, request, trusted_now=trusted_now
    )
    if require_start_authorization:
        authorization = _control(
            values, "start_authorization.json", PHASE9_START_AUTHORIZATION_SCHEMA
        )
        (
            authorization_sha,
            authorization_id,
            authorization_nonce_sha256,
            evidence_attestation_sha256,
            authorization_entry_state_sha256,
            authorization_target_sha256,
        ) = _verify_start_authorization(
            authorization, request, trusted_now=trusted_now
        )
        if authorization_entry_state_sha256 != entry_state_sha256:
            raise Phase9ForensicReplaySafetyError(
                "start authorization entry state differs"
            )
        authorization_json = canonical_bytes(authorization).decode("utf-8")
    else:
        authorization_sha = ""
        authorization_id = ""
        authorization_nonce_sha256 = ""
        evidence_attestation_sha256 = ""
        authorization_target_sha256 = ""
        authorization_json = ""
    packet = _control(values, "packet.json", "authority-phase9-packet-evidence-v1")
    roles = _control(values, "roles.json", PHASE9_ROLE_EVIDENCE_SCHEMA)
    verdict = _control(values, "verdict.json", "authority-phase9-verdict-evidence-v1")
    snapshot = _control(values, "snapshot.json", "authority-phase9-snapshot-evidence-v1")
    outbox = _control(
        values, "outbox_supervisor.json", PHASE9_RUNTIME_EVIDENCE_SCHEMA
    )
    acceptance = _control(
        values, "acceptance.json", PHASE9_ACCEPTANCE_EVIDENCE_SCHEMA
    )
    _mapping(
        packet,
        "packet",
        {
            "schema", "required_claims", "present_claims", "packet_path",
            "packet_sha256", "dispatch_count",
        },
    )
    _mapping(roles, "roles", {"schema", "roles"})
    _mapping(
        verdict,
        "verdict",
        {"schema", "roles", "effective_verdict", "exit_code"},
    )
    _mapping(snapshot, "snapshot", {"schema", "coordinate", "sections"})
    _mapping(
        outbox,
        "outbox_supervisor",
        {
            "schema", "precommit_external_launch_count",
            "committed_reclaim_count", "pending_outbox_count",
            "uncertain_automatic_resend_count", "active_descendant_count",
            "process_scope_receipts",
        },
    )
    _mapping(
        acceptance,
        "acceptance",
        {"schema", "cases", "delivery", "terminal"},
    )

    blockers: list[dict[str, str]] = []
    typed_receipts: list[ValidatedEvidenceReceiptV1] = []
    process_identities: set[tuple[object, object, object]] = set()
    required = packet.get("required_claims")
    present = packet.get("present_claims")
    if (
        type(required) is not list or type(present) is not list
        or any(type(item) is not str for item in required + present)
        or required != sorted(set(required)) or present != sorted(set(present))
    ):
        raise Phase9ForensicReplaySafetyError("packet claim inventories are malformed")
    packet_path = _text(packet.get("packet_path"), "packet.packet_path")
    packet_sha256 = _sha(packet.get("packet_sha256"), "packet.packet_sha256")
    packet_raw = values.get(packet_path)
    if packet_raw is None or hashlib.sha256(packet_raw).hexdigest() != packet_sha256:
        raise Phase9ForensicReplaySafetyError("packet raw bytes binding differs")
    raw_required, raw_present = _extract_packet_claims(packet_raw)
    if required != raw_required or present != raw_present:
        raise Phase9ForensicReplaySafetyError(
            "packet claim inventory differs from exact packet-v2 bytes"
        )
    missing = sorted(set(raw_required) - set(raw_present))
    dispatch_count = _integer(packet.get("dispatch_count"), "packet.dispatch_count")
    if missing:
        if dispatch_count != 0:
            blockers.append({"code": "DISPATCH_WITH_MISSING_CLAIMS", "detail": ",".join(missing)})
        blockers.append({"code": "MISSING_PACKET_CLAIMS", "detail": ",".join(missing)})

    role_values = roles.get("roles")
    if type(role_values) is not list:
        raise Phase9ForensicReplaySafetyError("roles.roles must be a list")
    if request.replay_mode == TECHNICAL:
        if [item.get("role") for item in role_values if type(item) is dict] != [
            "execution", "math", "paper"
        ]:
            raise Phase9ForensicReplaySafetyError("exact three sorted roles are required")
        generations: set[str] = set()
        for index, raw_role in enumerate(role_values):
            role = _mapping(
                raw_role,
                f"roles[{index}]",
                {
                    "role", "role_generation", "inherited", "packet_sha256",
                    "output_path", "output_sha256", "process_receipt",
                },
            )
            generation = _text(
                role.get("role_generation"),
                f"roles[{index}].generation",
                identifier=True,
            )
            if role.get("inherited") is not False or generation in generations:
                raise Phase9ForensicReplaySafetyError("role generations must be new and unique")
            generations.add(generation)
            if role.get("packet_sha256") != packet.get("packet_sha256"):
                raise Phase9ForensicReplaySafetyError("role packet binding differs")
            output_path = _text(role.get("output_path"), f"roles[{index}].output_path")
            output = values.get(output_path)
            output_sha256 = _sha(
                role.get("output_sha256"), f"roles[{index}].output_sha256"
            )
            if output is None or hashlib.sha256(output).hexdigest() != output_sha256:
                raise Phase9ForensicReplaySafetyError("role output bytes binding differs")
            process_receipt, provider_receipt = _validate_role_process_receipt(
                role.get("process_receipt"),
                request=request,
                values=values,
                role=str(role["role"]),
                role_generation=generation,
                packet_sha256=str(packet["packet_sha256"]),
                output_path=output_path,
                output=output,
            )
            process_body = _strict_json(
                process_receipt.receipt_json.encode("utf-8"),
                f"roles[{index}].stored_process_receipt",
            )
            identity = tuple(
                process_body[name]
                for name in ("invocation_id", "attempt_id", "process_scope_id")
            )
            if identity in process_identities:
                raise Phase9ForensicReplaySafetyError("role process identity is reused")
            process_identities.add(identity)
            typed_receipts.extend((process_receipt, provider_receipt))
    elif role_values:
        raise Phase9ForensicReplaySafetyError("ablation cannot contain judge role output")

    if request.replay_mode == TECHNICAL:
        layers = verdict.get("roles")
        if type(layers) is not dict or sorted(layers) != ["execution", "math", "paper"]:
            raise Phase9ForensicReplaySafetyError("verdict role layers differ")
        role_effective: list[str] = []
        for role in sorted(layers):
            layer = _mapping(
                layers[role],
                f"verdict.roles.{role}",
                {"raw", "protocol", "grounding", "effective"},
            )
            values_ = [layer.get(name) for name in ("raw", "protocol", "grounding")]
            if any(value not in {"PASS", "FAIL", "INDETERMINATE"} for value in values_):
                raise Phase9ForensicReplaySafetyError("verdict layer is unsupported")
            computed = _effective(values_)
            if layer.get("effective") != computed:
                raise Phase9ForensicReplaySafetyError("contradictory effective role verdict")
            role_effective.append(computed)
        effective = _effective(role_effective)
        if (
            verdict.get("effective_verdict") != effective
            or _integer(verdict.get("exit_code"), "verdict.exit_code") != 0
        ):
            raise Phase9ForensicReplaySafetyError("contradictory aggregate verdict")
        terminal_reason = "FORENSIC_REPLAY_COMPLETED"
        exit_code = 0
    else:
        if verdict.get("roles") != {} or verdict.get("effective_verdict") != "NOT_APPLICABLE":
            raise Phase9ForensicReplaySafetyError("ablation verdict must be NOT_APPLICABLE")
        effective = "NOT_APPLICABLE"
        terminal_reason = "PERMANENT_ABLATION_NO_DELIVERY"
        exit_code = _integer(verdict.get("exit_code"), "verdict.exit_code", minimum=1)

    coordinate = _mapping(
        snapshot.get("coordinate"),
        "snapshot.coordinate",
        {"project_id", "project_revision", "run_generation"},
    )
    _integer(coordinate.get("project_revision"), "snapshot.coordinate.project_revision")
    expected_coordinate = {
        "project_id": request.project_id,
        "project_revision": request.project_revision,
        "run_generation": request.run_generation,
    }
    if coordinate != expected_coordinate:
        raise Phase9ForensicReplaySafetyError("snapshot coordinate differs")
    sections = snapshot.get("sections")
    if type(sections) is not list or not sections:
        raise Phase9ForensicReplaySafetyError("snapshot sections are missing")
    section_names: list[str] = []
    for index, raw_section in enumerate(sections):
        section = _mapping(
            raw_section,
            f"snapshot.sections[{index}]",
            {"section", "coordinate", "read_failed", "read_status"},
        )
        section_names.append(
            _text(
                section.get("section"),
                f"snapshot.sections[{index}].section",
                identifier=True,
            )
        )
        if section.get("coordinate") != expected_coordinate:
            raise Phase9ForensicReplaySafetyError("snapshot section coordinate differs")
        read_status = section.get("read_status")
        read_failed = section.get("read_failed")
        if read_status not in {"AVAILABLE", "GAP", "ERROR"}:
            raise Phase9ForensicReplaySafetyError("snapshot read status is unsupported")
        if type(read_failed) is not bool or read_failed != (read_status == "ERROR"):
            raise Phase9ForensicReplaySafetyError(
                "snapshot read failure and ERROR status must agree"
            )
    if section_names != sorted(set(section_names)):
        raise Phase9ForensicReplaySafetyError(
            "snapshot section inventory must be sorted and unique"
        )

    expected_runtime = {
        "precommit_external_launch_count": 0,
        "pending_outbox_count": 0,
        "uncertain_automatic_resend_count": 0,
        "active_descendant_count": 0,
    }
    for key, expected_value in expected_runtime.items():
        if _integer(outbox.get(key), f"outbox_supervisor.{key}") != expected_value:
            blockers.append({"code": key.upper(), "detail": f"expected {expected_value}"})
    committed_reclaim_count = _integer(
        outbox.get("committed_reclaim_count"),
        "outbox_supervisor.committed_reclaim_count",
    )
    # A contract probe proves reclaim behavior in the isolated acceptance run.
    # The replay's live runtime evidence must describe this exact generation,
    # where dispatch is disabled and therefore no production reclaim occurred.
    if committed_reclaim_count != 0:
        blockers.append({"code": "RECLAIM_COUNT", "detail": "must be zero"})
    receipts = outbox.get("process_scope_receipts")
    if type(receipts) is not dict or sorted(receipts) != ["failed", "kill", "pause"]:
        raise Phase9ForensicReplaySafetyError("process-scope receipts differ")
    for name, receipt_reference in receipts.items():
        process_receipt = _validate_process_scope_receipt(
            receipt_reference,
            request=request,
            values=values,
            action_name=name,
        )
        process_body = _strict_json(
            process_receipt.receipt_json.encode("utf-8"),
            f"process_scope_receipts.{name}.stored",
        )
        identity = tuple(
            process_body[field]
            for field in ("invocation_id", "attempt_id", "process_scope_id")
        )
        if identity in process_identities:
            raise Phase9ForensicReplaySafetyError("process-scope identity is reused")
        process_identities.add(identity)
        typed_receipts.append(process_receipt)

    cases = acceptance.get("cases")
    if type(cases) is not list:
        raise Phase9ForensicReplaySafetyError("acceptance cases are missing")
    case_ids: list[str] = []
    acceptance_runner_bindings: list[dict[str, object]] = []
    for index, raw_case in enumerate(cases):
        case = _mapping(
            raw_case,
            f"acceptance.cases[{index}]",
            {"case_id", "result", "receipt"},
        )
        case_id = _text(case.get("case_id"), f"acceptance.cases[{index}].case_id", identifier=True)
        case_ids.append(case_id)
        if case.get("result") != "PASS":
            blockers.append({"code": "ACCEPTANCE_CASE_NONPASS", "detail": case_id})
        acceptance_receipt, runner_binding = _validate_acceptance_case_receipt(
            case.get("receipt"),
            request=request,
            values=values,
            case_id=case_id,
        )
        typed_receipts.append(acceptance_receipt)
        acceptance_runner_bindings.append(runner_binding)
    if tuple(case_ids) != PHASE9_ACCEPTANCE_CASES:
        raise Phase9ForensicReplaySafetyError("acceptance case inventory differs")
    delivery = _mapping(acceptance.get("delivery"), "acceptance.delivery")
    required_delivery = {
        "delivery_capability": DELIVERY_DISABLED,
        "release_created": False,
        "final_acceptance_created": False,
        "final_submission_created": False,
        "reusable": False,
        "delivery_override_applied": False,
    }
    delivery_booleans = (
        "release_created", "final_acceptance_created",
        "final_submission_created", "reusable", "delivery_override_applied",
    )
    if delivery != required_delivery or any(
        type(delivery.get(name)) is not bool for name in delivery_booleans
    ):
        blockers.append({"code": "DELIVERY_FENCE", "detail": "delivery evidence differs"})
    terminal = _mapping(
        acceptance.get("terminal"),
        "acceptance.terminal",
        {
            "terminal_reason", "requested_resume_target", "effective_verdict",
            "exit_code",
        },
    )
    terminal_exit_code = _integer(terminal.get("exit_code"), "acceptance.terminal.exit_code")
    if terminal != {
        "terminal_reason": terminal_reason,
        "requested_resume_target": RESUME_TARGET,
        "effective_verdict": effective,
        "exit_code": terminal_exit_code,
    } or terminal_exit_code != exit_code:
        raise Phase9ForensicReplaySafetyError("terminal evidence differs")
    runtime_counts = {
        **expected_runtime,
        "committed_reclaim_count": committed_reclaim_count,
    }
    if require_component_receipts:
        typed_receipts.extend(
            _validate_component_receipts(
                request,
                values,
                entry_state_receipt_sha256=entry_state_sha256,
                runtime_counts=runtime_counts,
            )
        )
    blockers.sort(key=lambda item: (item["code"], item["detail"]))
    typed_receipt_set_sha256 = _typed_receipt_set_sha256(typed_receipts)
    return {
        "blockers": blockers,
        "authorization_receipt_sha256": authorization_sha,
        "authorization_id": authorization_id,
        "authorization_json": authorization_json,
        "authorization_nonce_sha256": authorization_nonce_sha256,
        "authorization_target_sha256": authorization_target_sha256,
        "evidence_attestation_sha256": evidence_attestation_sha256,
        "entry_state_receipt_sha256": entry_state_sha256,
        "entry_evaluated_at": entry_evaluated_at,
        "typed_receipts": tuple(typed_receipts),
        "acceptance_runner_bindings": tuple(acceptance_runner_bindings),
        "typed_receipt_set_sha256": typed_receipt_set_sha256,
        "packet_sha256": packet["packet_sha256"],
        "roles_sha256": hashlib.sha256(values["roles.json"]).hexdigest(),
        "verdict_sha256": hashlib.sha256(values["verdict.json"]).hexdigest(),
        "snapshot_sha256": hashlib.sha256(values["snapshot.json"]).hexdigest(),
        "runtime_safety_sha256": hashlib.sha256(values["outbox_supervisor.json"]).hexdigest(),
        "runtime_counts": runtime_counts,
        "acceptance_sha256": hashlib.sha256(values["acceptance.json"]).hexdigest(),
        "terminal_reason": terminal_reason,
        "effective_verdict": effective,
        "exit_code": exit_code,
    }


def _preflight_body(
    request: Phase9ForensicReplayRequestV1,
    evaluation: Mapping[str, object] | None,
    blockers: list[dict[str, str]],
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": PHASE9_REPLAY_PREFLIGHT_SCHEMA,
        "status": "READY" if not blockers else "BLOCKED",
        "replay_id": request.replay_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "request_sha256": request.request_sha256,
        "evidence_set_sha256": request.evidence_set_sha256,
        "terminal_reason": None if evaluation is None else evaluation["terminal_reason"],
        "effective_verdict": None if evaluation is None else evaluation["effective_verdict"],
        "blockers": blockers,
        "authorization_scope": {
            "phase9_a_forensic_replay": not blockers,
            "provider_or_network": False,
            "production_outbox_or_delivery": False,
            "release": False,
            "deployment": False,
            "migration": False,
            "cutover": False,
        },
    }
    body["preflight_sha256"] = canonical_sha256(body)
    return body


def preflight_phase9_forensic_replay(
    request: Phase9ForensicReplayRequestV1,
    *,
    evidence_root: str | Path,
    trusted_now: int | None = None,
) -> dict[str, object]:
    value = validate_phase9_forensic_replay_request(request)
    now = int(time.time()) if trusted_now is None else _integer(
        trusted_now, "trusted_now"
    )
    try:
        evidence, _inventory = _read_evidence_set(evidence_root, value)
        evaluation = _evaluate_evidence(value, evidence, trusted_now=now)
        blockers = list(evaluation["blockers"])
        return _preflight_body(value, evaluation, blockers)
    except Phase9ForensicReplayError as exc:
        return _preflight_body(
            value,
            None,
            [{"code": "INVALID_EVIDENCE", "detail": f"{type(exc).__name__}: {exc}"}],
        )


def _event(
    request: Phase9ForensicReplayRequestV1,
    sequence: int,
    kind: str,
    state: str,
    predecessor: str | None,
    evidence: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    body = {
        "schema": "authority-phase9-forensic-replay-event-v1",
        "replay_id": request.replay_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "sequence": sequence,
        "event_kind": kind,
        "state": state,
        "predecessor_event_sha256": predecessor,
        "evidence": dict(evidence),
        "occurred_at": request.occurred_at,
    }
    return body, canonical_sha256(body)


def _result_from_receipt(
    request: Phase9ForensicReplayRequestV1,
    receipt: sqlite3.Row,
    *,
    replayed: bool,
) -> Phase9ForensicReplayResult:
    return Phase9ForensicReplayResult(
        request.replay_id, request.workflow_id, request.run_generation,
        request.request_sha256, str(receipt["terminal_reason"]),
        str(receipt["effective_verdict"]), str(receipt["receipt_id"]),
        str(receipt["receipt_sha256"]), int(receipt["occurred_at"]), replayed,
    )


def _source_snapshot_tuple(snapshot: object) -> tuple[str, str, str, str]:
    try:
        source = getattr(snapshot, "source")
        inventory = getattr(snapshot, "tracked_inventory")
        values = (
            getattr(source, "source_commit"),
            getattr(source, "source_tree"),
            getattr(source, "source_parent"),
            getattr(inventory, "inventory_sha256"),
        )
    except AttributeError as exc:
        raise Phase9ForensicReplaySafetyError(
            "source snapshot does not expose the required identity"
        ) from exc
    return (
        _git_oid(values[0], "source_snapshot.source_commit"),
        _git_oid(values[1], "source_snapshot.source_tree"),
        _git_oid(values[2], "source_snapshot.source_parent"),
        _sha(values[3], "source_snapshot.source_inventory_sha256"),
    )


def _external_tree_identity(
    root: Path, *, label: str
) -> tuple[tuple[object, ...], ...]:
    """Capture lstat identities so same-byte replacements are still changes."""

    def identity(path: Path, logical_path: str) -> tuple[object, ...]:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise Phase9ForensicReplayConflict(f"{label} identity is unavailable") from exc
        return (
            logical_path,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
        )

    records = [identity(root, ".")]
    try:
        for directory, directory_names, file_names in os.walk(
            root, followlinks=False
        ):
            directory_names.sort()
            file_names.sort()
            parent = Path(directory)
            for name in directory_names + file_names:
                path = parent / name
                records.append(identity(path, path.relative_to(root).as_posix()))
    except OSError as exc:
        raise Phase9ForensicReplayConflict(f"{label} inventory is unavailable") from exc
    return tuple(sorted(records, key=lambda item: str(item[0]).encode("utf-8")))


def _external_file_identity(path: Path, *, label: str) -> tuple[object, ...]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise Phase9ForensicReplayConflict(f"{label} identity is unavailable") from exc
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _current_executable_descriptor(
    path: Path,
) -> tuple[dict[str, object], tuple[object, ...]]:
    """Read one executable without following a link or losing pathname identity."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise Phase9ForensicReplayConflict(
            "acceptance Python executable is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not stat.S_IMODE(before.st_mode) & 0o111
        or before.st_size <= 0
        or before.st_size > 128 * 1024 * 1024
    ):
        raise Phase9ForensicReplayConflict(
            "acceptance Python must be one bounded executable regular file"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            byte_length = 0
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                byte_length += len(chunk)
                if byte_length > 128 * 1024 * 1024:
                    raise Phase9ForensicReplayConflict(
                        "acceptance Python executable exceeds the size limit"
                    )
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        final = path.lstat()
    except OSError as exc:
        raise Phase9ForensicReplayConflict(
            "acceptance Python executable cannot be read safely"
        ) from exc

    def identity(metadata: os.stat_result) -> tuple[object, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    current_identity = identity(after)
    if (
        identity(before) != identity(opened)
        or identity(opened) != current_identity
        or identity(final) != current_identity
        or byte_length != after.st_size
    ):
        raise Phase9ForensicReplayConflict(
            "acceptance Python executable changed while being read"
        )
    body = {
        "schema": PHASE9_ACCEPTANCE_PYTHON_SCHEMA,
        "requested_path": str(path),
        "resolved_path": str(path),
        "byte_length": byte_length,
        "raw_bytes_sha256": digest.hexdigest(),
        "mode": stat.S_IMODE(after.st_mode),
    }
    return body, current_identity


def _authority_runtime_source_sha256(
    connection: sqlite3.Connection,
    *,
    request: Phase9ForensicReplayRequestV1,
    receipt_kind: str,
    logical_id: str,
    logical_path: str,
    raw_bytes_sha256: str,
    byte_length: int,
    receipt_sha256: str,
    dependency_fingerprint_sha256: str,
    input_sha256: str,
    output_sha256: str,
    packet_sha256: str | None,
    invocation_id: str,
    attempt_id: str,
    process_scope_id: str,
) -> str:
    rows: dict[str, dict[str, object]] = {}
    for table, key, value in (
        ("authority_invocations", "invocation_id", invocation_id),
        ("authority_attempts", "attempt_id", attempt_id),
        ("authority_process_scopes", "process_scope_id", process_scope_id),
    ):
        row = connection.execute(
            f'SELECT * FROM "{table}" WHERE "{key}"=?', (value,)
        ).fetchone()
        if row is None:
            raise Phase9ForensicReplayConflict(
                "Authority runtime source graph is incomplete"
            )
        rows[table] = dict(row)
    invocation = rows["authority_invocations"]
    attempt = rows["authority_attempts"]
    scope = rows["authority_process_scopes"]
    command_row = connection.execute(
        "SELECT * FROM authority_commands WHERE command_id=?",
        (invocation.get("command_id"),),
    ).fetchone()
    if (
        command_row is None
        or attempt.get("invocation_id") != invocation_id
        or scope.get("attempt_id") != attempt_id
    ):
        raise Phase9ForensicReplayConflict(
            "Authority runtime source graph coordinate differs"
        )
    command = dict(command_row)
    try:
        completion = _strict_json(
            str(command["envelope_json"]).encode("utf-8"),
            "Authority Phase9 runtime completion",
        )
        _mapping(
            completion,
            "Authority Phase9 runtime completion",
            {
                "schema", "execution_domain", "candidate", "project_id",
                "workflow_id", "run_generation", "replay_coordinate_sha256",
                "source_inventory_sha256", "logical_id", "receipt_kinds",
                "invocation_id", "attempt_id", "process_scope_id",
                "dependency_fingerprint_sha256", "input_sha256",
                "output_sha256", "packet_sha256", "receipts", "status",
                "delivery_capability",
            },
        )
    except Phase9ForensicReplayError as exc:
        raise Phase9ForensicReplayConflict(
            "Authority runtime completion is malformed"
        ) from exc
    expected_kinds = (
        ["ROLE_PROCESS", "ROLE_PROVIDER"]
        if receipt_kind in {"ROLE_PROCESS", "ROLE_PROVIDER"}
        else ["PROCESS_SCOPE"]
    )
    receipt_rows = completion.get("receipts")
    if type(receipt_rows) is not list:
        raise Phase9ForensicReplayConflict(
            "Authority runtime completion receipt inventory is malformed"
        )
    normalized_receipts: list[dict[str, object]] = []
    for index, value in enumerate(receipt_rows):
        normalized_receipts.append(
            dict(
                _mapping(
                    value,
                    f"Authority runtime completion.receipts[{index}]",
                    {
                        "receipt_kind", "logical_path", "byte_length",
                        "raw_bytes_sha256", "receipt_sha256",
                    },
                )
            )
        )
    normalized_receipts.sort(key=lambda value: str(value["receipt_kind"]))
    expected_binding = {
        "receipt_kind": receipt_kind,
        "logical_path": logical_path,
        "byte_length": byte_length,
        "raw_bytes_sha256": raw_bytes_sha256,
        "receipt_sha256": receipt_sha256,
    }
    if (
        completion.get("schema") != "authority-phase9-runtime-completion-v1"
        or completion.get("execution_domain") != "FORMAL_PHASE9_A"
        or completion.get("candidate")
        != {
            "commit": request.source_commit,
            "tree": request.source_tree,
            "parent": request.source_parent,
        }
        or completion.get("project_id") != request.project_id
        or completion.get("workflow_id") != request.workflow_id
        or completion.get("run_generation") != request.run_generation
        or completion.get("replay_coordinate_sha256")
        != _replay_coordinate_sha256(request)
        or completion.get("source_inventory_sha256")
        != request.source_inventory_sha256
        or completion.get("logical_id") != logical_id
        or completion.get("receipt_kinds") != expected_kinds
        or completion.get("invocation_id") != invocation_id
        or completion.get("attempt_id") != attempt_id
        or completion.get("process_scope_id") != process_scope_id
        or completion.get("dependency_fingerprint_sha256")
        != dependency_fingerprint_sha256
        or completion.get("input_sha256") != input_sha256
        or completion.get("output_sha256") != output_sha256
        or completion.get("packet_sha256") != packet_sha256
        or [str(value["receipt_kind"]) for value in normalized_receipts]
        != expected_kinds
        or expected_binding not in normalized_receipts
        or completion.get("status") != "COMPLETED"
        or completion.get("delivery_capability") != DELIVERY_DISABLED
        or command.get("workflow_id") != request.workflow_id
        or command.get("project_id") != request.project_id
        or command.get("requested_revision") != request.project_revision
        or command.get("command_type") != "PHASE9_A_RUNTIME_COMPLETION"
        or command.get("envelope_schema")
        != "authority-phase9-runtime-completion-v1"
        or command.get("envelope_sha256")
        != hashlib.sha256(canonical_bytes(completion)).hexdigest()
        or canonical_bytes(completion).decode("utf-8")
        != command.get("envelope_json")
        or invocation.get("workflow_id") != request.workflow_id
        or invocation.get("invocation_kind")
        != "PHASE9_A_RUNTIME_COMPLETION"
        or invocation.get("scope_schema")
        != "authority-phase9-runtime-completion-v1"
        or invocation.get("scope_json") != command.get("envelope_json")
        or invocation.get("metadata_json") != command.get("envelope_json")
        or attempt.get("scope_schema")
        != "authority-phase9-runtime-completion-v1"
        or attempt.get("scope_json") != command.get("envelope_json")
        or attempt.get("metadata_json") != command.get("envelope_json")
        or scope.get("process_kind") != "PHASE9_A_RUNTIME_COMPLETION"
        or scope.get("scope_schema")
        != "authority-phase9-runtime-completion-v1"
        or scope.get("scope_json") != command.get("envelope_json")
        or scope.get("metadata_json") != command.get("envelope_json")
    ):
        raise Phase9ForensicReplayConflict(
            "Authority runtime completion semantic binding differs"
        )
    return canonical_sha256(
        {
            "schema": "authority-phase9-runtime-source-graph-v1",
            "command": command,
            "invocation": invocation,
            "attempt": attempt,
            "process_scope": scope,
        }
    )


def _validate_authority_runtime_completion(
    connection: sqlite3.Connection,
    *,
    request: Phase9ForensicReplayRequestV1,
    runtime: sqlite3.Row,
    receipt_body: Mapping[str, object],
    require_current_operator: bool = True,
) -> None:
    """Require the one-use runtime authorization and immutable completion fact."""

    completion = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_runtime_completions "
        "WHERE completion_sha256=?",
        (runtime["runtime_completion_sha256"],),
    ).fetchone()
    if completion is None:
        raise Phase9ForensicReplayConflict(
            "Authority runtime completion fact is unavailable"
        )
    authorization = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_runtime_authorizations "
        "WHERE authorization_id=?",
        (completion["authorization_id"],),
    ).fetchone()
    if authorization is None:
        raise Phase9ForensicReplayConflict(
            "Authority runtime authorization is unavailable"
        )
    authorization_body = _strict_json(
        str(authorization["authorization_json"]).encode("utf-8"),
        "Authority runtime authorization",
    )
    authorization_keys = {
        "schema", "authorization_id", "nonce_sha256",
        "authorization_mechanism", "authorized", "operation", "candidate",
        "project_id", "workflow_id", "run_generation",
        "source_inventory_sha256", "replay_coordinate_sha256",
        "receipt_kind", "logical_id", "logical_path", "invocation_id",
        "attempt_id", "process_scope_id", "packet_sha256",
        "dependency_fingerprint_sha256", "input_sha256", "operator_uid",
        "operator_account", "issued_at", "expires_at",
        "authorization_scope", "authorization_receipt_sha256",
    }
    _mapping(
        authorization_body,
        "Authority runtime authorization",
        authorization_keys,
    )
    expected_scope = {
        "runtime_completion_record": True,
        "provider_or_network": False,
        "production_outbox_or_delivery": False,
        "release": False,
        "deployment": False,
        "migration": False,
        "cutover": False,
    }
    expected_authorization_scalars = {
        "authorization_id": authorization["authorization_id"],
        "nonce_sha256": authorization["nonce_sha256"],
        "project_id": request.project_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "source_commit": request.source_commit,
        "source_tree": request.source_tree,
        "source_parent": request.source_parent,
        "source_inventory_sha256": request.source_inventory_sha256,
        "replay_coordinate_sha256": _replay_coordinate_sha256(request),
        "receipt_kind": runtime["receipt_kind"],
        "logical_id": runtime["logical_id"],
        "logical_path": runtime["logical_path"],
        "invocation_id": runtime["invocation_id"],
        "attempt_id": runtime["attempt_id"],
        "process_scope_id": runtime["process_scope_id"],
        "packet_sha256": runtime["packet_sha256"],
        "dependency_fingerprint_sha256": runtime[
            "dependency_fingerprint_sha256"
        ],
        "input_sha256": runtime["input_sha256"],
        "operator_uid": authorization["operator_uid"],
        "operator_account": authorization["operator_account"],
        "issued_at": authorization["issued_at"],
        "expires_at": authorization["expires_at"],
    }
    expected_authorization = {
        "schema": "authority-phase9-runtime-authorization-v1",
        "authorization_id": expected_authorization_scalars["authorization_id"],
        "nonce_sha256": expected_authorization_scalars["nonce_sha256"],
        "authorization_mechanism": "CONTROLLED_OS_ACCOUNT",
        "authorized": True,
        "operation": "RECORD_PHASE9_A_RUNTIME_COMPLETION",
        "candidate": {
            "commit": request.source_commit,
            "tree": request.source_tree,
            "parent": request.source_parent,
        },
        **{
            key: value
            for key, value in expected_authorization_scalars.items()
            if key not in {"source_commit", "source_tree", "source_parent"}
        },
        "authorization_scope": expected_scope,
    }
    expected_authorization["authorization_receipt_sha256"] = canonical_sha256(
        expected_authorization
    )
    if (
        authorization_body != expected_authorization
        or authorization["authorization_receipt_sha256"]
        != expected_authorization["authorization_receipt_sha256"]
        or any(
            authorization[name] != value
            for name, value in expected_authorization_scalars.items()
            if name not in {"source_commit", "source_tree", "source_parent"}
        )
        or authorization["source_commit"] != request.source_commit
        or authorization["source_tree"] != request.source_tree
        or authorization["source_parent"] != request.source_parent
    ):
        raise Phase9ForensicReplayConflict(
            "Authority runtime authorization semantics differ"
        )
    if require_current_operator:
        try:
            current_uid = os.geteuid()
            current_account = pwd.getpwuid(current_uid).pw_name
        except (AttributeError, KeyError) as exc:
            raise Phase9ForensicReplayConflict(
                "Authority runtime OS account cannot be verified"
            ) from exc
        if (
            authorization["operator_uid"] != current_uid
            or authorization["operator_account"] != current_account
        ):
            raise Phase9ForensicReplayConflict(
                "Authority runtime authorization OS account differs"
            )
    source_sha256 = _authority_runtime_source_sha256(
        connection,
        request=request,
        receipt_kind=str(runtime["receipt_kind"]),
        logical_id=str(runtime["logical_id"]),
        logical_path=str(runtime["logical_path"]),
        raw_bytes_sha256=str(runtime["raw_bytes_sha256"]),
        byte_length=int(runtime["byte_length"]),
        receipt_sha256=str(runtime["receipt_sha256"]),
        dependency_fingerprint_sha256=str(
            runtime["dependency_fingerprint_sha256"]
        ),
        input_sha256=str(runtime["input_sha256"]),
        output_sha256=str(runtime["output_sha256"]),
        packet_sha256=runtime["packet_sha256"],
        invocation_id=str(runtime["invocation_id"]),
        attempt_id=str(runtime["attempt_id"]),
        process_scope_id=str(runtime["process_scope_id"]),
    )
    completion_body = {
        "schema": "authority-phase9-runtime-completion-attestation-v1",
        "authorization_id": authorization["authorization_id"],
        "nonce_sha256": authorization["nonce_sha256"],
        "authorization_receipt_sha256": authorization[
            "authorization_receipt_sha256"
        ],
        "execution_domain": "FORMAL_PHASE9_A",
        **{
            name: runtime[name]
            for name in (
                "workflow_id", "run_generation", "receipt_kind", "logical_id",
                "invocation_id", "attempt_id", "process_scope_id",
                "packet_sha256", "dependency_fingerprint_sha256",
                "input_sha256", "output_sha256", "logical_path",
                "byte_length", "raw_bytes_sha256", "receipt_sha256",
                "authority_source_sha256",
            )
        },
        "completed_at": completion["completed_at"],
    }
    expected_completion = {
        **completion_body,
        "completion_sha256": canonical_sha256(completion_body),
    }
    if (
        completion["completion_sha256"]
        != expected_completion["completion_sha256"]
        or _strict_json(
            str(completion["completion_json"]).encode("utf-8"),
            "Authority runtime completion",
        )
        != expected_completion
        or any(
            completion[name] != value
            for name, value in completion_body.items()
            if name != "schema"
        )
        or completion["completed_at"] < authorization["issued_at"]
        or completion["completed_at"] > authorization["expires_at"]
        or runtime["authority_source_sha256"] != source_sha256
        or receipt_body.get("dependency_fingerprint_sha256")
        != runtime["dependency_fingerprint_sha256"]
        or receipt_body.get("input_sha256") != runtime["input_sha256"]
        or receipt_body.get("output_sha256") != runtime["output_sha256"]
    ):
        raise Phase9ForensicReplayConflict(
            "Authority runtime completion fact differs"
        )


def _acceptance_runner_case_source_sha256(
    case_id: str,
    test_node: str,
    *,
    command_sha256: str,
    raw_log_sha256: str,
    junit_sha256: str,
    event_log_sha256: str,
    outcome_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "schema": "authority-phase9-acceptance-runner-case-source-v1",
            "case_id": case_id,
            "test_node": test_node,
            "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
            "command_sha256": command_sha256,
            "raw_log_sha256": raw_log_sha256,
            "junit_sha256": junit_sha256,
            "event_log_sha256": event_log_sha256,
            "outcome_sha256": outcome_sha256,
            "status": "PASS",
        }
    )


def _validate_attested_acceptance_events(row: Mapping[str, object]) -> str:
    raw = bytes(row["acceptance_event_log"])
    nonce = row["acceptance_event_nonce"]
    if (
        type(nonce) is not str
        or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        or not raw
        or not raw.endswith(b"\n")
        or hashlib.sha256(raw).hexdigest()
        != row["acceptance_event_log_sha256"]
    ):
        raise Phase9ForensicReplayConflict(
            "Authority acceptance trusted event stream identity differs"
        )
    events: list[dict[str, object]] = []
    for sequence, line in enumerate(raw.splitlines()):
        value = _strict_json(line, f"Authority acceptance event {sequence}")
        if (
            canonical_bytes(value) != line
            or value.get("schema") != "paper-factory-trusted-pytest-events-v2"
            or value.get("nonce") != nonce
            or value.get("sequence") != sequence
        ):
            raise Phase9ForensicReplayConflict(
                "Authority acceptance trusted event sequence differs"
            )
        events.append(value)
    if len(events) < 3:
        raise Phase9ForensicReplayConflict(
            "Authority acceptance trusted event stream is incomplete"
        )
    expected_nodes = [
        PHASE9_ACCEPTANCE_TEST_NODES[case_id]
        for case_id in PHASE9_ACCEPTANCE_CASES
    ]
    start, finish = events[0], events[-1]
    collections = [value for value in events if value.get("event") == "collection"]
    if (
        set(start) != {"schema", "nonce", "sequence", "event", "rootdir"}
        or
        start.get("event") != "session_start"
        or type(start.get("rootdir")) is not str
        or set(finish)
        != {"schema", "nonce", "sequence", "event", "exitstatus"}
        or finish.get("event") != "session_finish"
        or finish.get("exitstatus") != 0
        or len(collections) != 1
        or set(collections[0])
        != {"schema", "nonce", "sequence", "event", "nodeids"}
        or collections[0].get("nodeids") != expected_nodes
    ):
        raise Phase9ForensicReplayConflict(
            "Authority acceptance trusted session boundary differs"
        )
    phase_facts: dict[tuple[str, str], dict[str, object]] = {}
    for value in events[1:-1]:
        kind = value.get("event")
        if kind == "collection":
            continue
        if kind == "warning":
            raise Phase9ForensicReplayConflict(
                "Authority acceptance trusted event stream contains a warning"
            )
        if kind != "phase_fact" or set(value) != {
            "schema", "nonce", "sequence", "event", "nodeid", "when",
            "outcome", "xfail_declared", "source",
        }:
            raise Phase9ForensicReplayConflict(
                "Authority acceptance trusted phase event differs"
            )
        nodeid = value.get("nodeid")
        when = value.get("when")
        key = (str(nodeid), str(when))
        if (
            nodeid not in expected_nodes
            or when not in {"setup", "call", "teardown"}
            or value.get("outcome") != "passed"
            or value.get("xfail_declared") is not False
            or value.get("source") != "runtest_call_excinfo"
            or key in phase_facts
        ):
            raise Phase9ForensicReplayConflict(
                "Authority acceptance trusted phase result differs"
            )
        phase_facts[key] = value
    if set(phase_facts) != {
        (node, phase)
        for node in expected_nodes
        for phase in ("setup", "call", "teardown")
    }:
        raise Phase9ForensicReplayConflict(
            "Authority acceptance trusted phase inventory differs"
        )
    return str(start["rootdir"])


def _validate_attested_acceptance_run(row: sqlite3.Row) -> dict[str, object]:
    trusted_rootdir = _validate_attested_acceptance_events(row)
    raw_log = bytes(row["acceptance_raw_log"])
    junit_xml = bytes(row["acceptance_junit_xml"])
    command_json = str(row["acceptance_command_json"])
    outcome_json = str(row["acceptance_outcome_json"])
    if (
        hashlib.sha256(raw_log).hexdigest()
        != row["acceptance_raw_log_sha256"]
        or hashlib.sha256(junit_xml).hexdigest()
        != row["acceptance_junit_sha256"]
        or canonical_sha256(_strict_json(command_json.encode(), "attested command"))
        != row["acceptance_command_sha256"]
    ):
        raise Phase9ForensicReplayConflict(
            "Authority acceptance runner bytes differ"
        )
    command = _strict_json(command_json.encode(), "attested command")
    expected_command_keys = {
        "schema", "execution_domain", "acceptance_spec_sha256", "cwd",
        "python", "python_runtime", "sandbox", "trusted_reporter_sha256",
        "trusted_event_sha256", "argv", "sandbox_argv", "environment",
        "started_at", "finished_at", "exit_code", "raw_log_sha256",
        "junit_sha256", "outcome_sha256",
    }
    _mapping(command, "attested command", expected_command_keys)
    argv = command.get("argv")
    expected_nodes = [
        PHASE9_ACCEPTANCE_TEST_NODES[case_id]
        for case_id in PHASE9_ACCEPTANCE_CASES
    ]
    if (
        command.get("schema")
        != "authority-phase9-replay-acceptance-command-v1"
        or command.get("execution_domain") != "FORMAL_PHASE9_A"
        or command.get("acceptance_spec_sha256")
        != PHASE9_ACCEPTANCE_SPEC_SHA256
        or command.get("cwd") != trusted_rootdir
        or command.get("trusted_event_sha256")
        != row["acceptance_event_log_sha256"]
        or command.get("raw_log_sha256") != row["acceptance_raw_log_sha256"]
        or command.get("junit_sha256") != row["acceptance_junit_sha256"]
        or command.get("outcome_sha256") != row["acceptance_outcome_sha256"]
        or command.get("exit_code") != 0
        or type(argv) is not list
        or argv[-len(expected_nodes):] != expected_nodes
        or command.get("started_at") != row["started_at"]
        or command.get("finished_at") != row["finished_at"]
    ):
        raise Phase9ForensicReplayConflict(
            "Authority acceptance command/event binding differs"
        )
    outcome = _strict_json(outcome_json.encode(), "attested outcome")
    if _self_hash(
        outcome, "outcome_sha256", "attested outcome"
    ) != row["acceptance_outcome_sha256"]:
        raise Phase9ForensicReplayConflict(
            "Authority acceptance outcome hash differs"
        )
    lines = raw_log.decode("utf-8", errors="strict").splitlines()
    summaries = [
        line
        for line in lines
        if re.fullmatch(r"=+ [0-9]+ passed in [0-9]+(?:\.[0-9]+)?s =+", line)
    ]
    if len(summaries) != 1 or re.fullmatch(
        r"=+ 17 passed in [0-9]+(?:\.[0-9]+)?s =+", summaries[0]
    ) is None:
        raise Phase9ForensicReplayConflict(
            "Authority acceptance log lacks one exact terminal summary"
        )
    if any(token in raw_log for token in (b" FAILED ", b" ERROR ", b"Traceback")):
        raise Phase9ForensicReplayConflict(
            "Authority acceptance log contains a non-pass outcome"
        )
    observed_nodes = []
    for node in expected_nodes:
        matches = [line for line in lines if line.startswith(node + " ")]
        if len(matches) != 1 or " PASSED " not in matches[0]:
            raise Phase9ForensicReplayConflict(
                "Authority acceptance log node inventory differs"
            )
        observed_nodes.append(node)
    try:
        xml_root = ET.fromstring(junit_xml)
    except ET.ParseError as exc:
        raise Phase9ForensicReplayConflict(
            "Authority acceptance JUnit is malformed"
        ) from exc
    suites = [xml_root] if xml_root.tag == "testsuite" else list(
        xml_root.findall(".//testsuite")
    )
    if not suites:
        raise Phase9ForensicReplayConflict(
            "Authority acceptance JUnit lacks a suite"
        )
    totals = {
        name: sum(int(suite.attrib.get(name, "0")) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }
    testcases = list(xml_root.iter("testcase"))
    observed_names = {
        f"{case.attrib.get('classname', '').replace('.', '/')}.py::"
        f"{case.attrib.get('name', '')}"
        for case in testcases
    }
    if (
        totals != {"tests": 17, "failures": 0, "errors": 0, "skipped": 0}
        or len(testcases) != 17
        or observed_names != set(expected_nodes)
    ):
        raise Phase9ForensicReplayConflict(
            "Authority acceptance JUnit outcome differs"
        )
    expected_outcome = {
        "schema": "authority-phase9-replay-acceptance-outcome-v1",
        "execution_domain": "FORMAL_PHASE9_A",
        "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
        "cases": [
            {
                "case_id": case_id,
                "test_node": PHASE9_ACCEPTANCE_TEST_NODES[case_id],
                "status": "PASS",
            }
            for case_id in PHASE9_ACCEPTANCE_CASES
        ],
        "collected": 17,
        "passed": 17,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "exit_code": 0,
    }
    expected_outcome["outcome_sha256"] = canonical_sha256(expected_outcome)
    if outcome != expected_outcome:
        raise Phase9ForensicReplayConflict(
            "Authority acceptance parsed outcome differs"
        )
    return outcome


def _verify_authority_replay_attestation(
    connection: sqlite3.Connection,
    request: Phase9ForensicReplayRequestV1,
    evaluation: Mapping[str, object],
    *,
    trusted_now: int,
    evidence_root: Path | None,
    require_current_operator: bool = True,
) -> sqlite3.Row:
    attestation_sha256 = _sha(
        evaluation.get("evidence_attestation_sha256"),
        "evaluation.evidence_attestation_sha256",
    )
    row = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_evidence_attestations "
        "WHERE attestation_sha256=?",
        (attestation_sha256,),
    ).fetchone()
    if row is None or row["execution_domain"] != "FORMAL_PHASE9_A":
        raise Phase9ForensicReplayConflict(
            "formal Authority replay evidence attestation is unavailable"
        )
    attestation = _strict_json(
        str(row["attestation_json"]).encode(), "Authority replay attestation"
    )
    if _self_hash(
        attestation, "attestation_sha256", "Authority replay attestation"
    ) != attestation_sha256:
        raise Phase9ForensicReplayConflict(
            "Authority replay attestation self-hash differs"
        )
    expected_scalars = {
        "schema": PHASE9_REPLAY_EVIDENCE_ATTESTATION_SCHEMA,
        "execution_domain": "FORMAL_PHASE9_A",
        "project_id": request.project_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "replay_mode": request.replay_mode,
        "replay_coordinate_sha256": _replay_coordinate_sha256(request),
        "source_inventory_sha256": request.source_inventory_sha256,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "entry_state_receipt_sha256": evaluation["entry_state_receipt_sha256"],
        "evidence_payload_set_sha256": (
            phase9_replay_evidence_payload_set_sha256(request)
        ),
        "typed_receipt_set_sha256": evaluation["typed_receipt_set_sha256"],
        "packet_sha256": evaluation["packet_sha256"],
        "roles_sha256": evaluation["roles_sha256"],
        "verdict_sha256": evaluation["verdict_sha256"],
        "snapshot_sha256": evaluation["snapshot_sha256"],
        "runtime_safety_sha256": evaluation["runtime_safety_sha256"],
        "acceptance_sha256": evaluation["acceptance_sha256"],
        "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
    }
    if any(row[key] != value for key, value in expected_scalars.items() if key != "schema"):
        raise Phase9ForensicReplayConflict(
            "Authority replay attestation coordinate or evidence differs"
        )
    if any(attestation.get(key) != value for key, value in expected_scalars.items()):
        raise Phase9ForensicReplayConflict(
            "Authority replay attestation JSON differs"
        )
    if int(row["attested_at"]) > trusted_now:
        raise Phase9ForensicReplayConflict(
            "Authority replay attestation is from the future"
        )
    outcome = _validate_attested_acceptance_run(row)
    expected_attestation_body = {
        **expected_scalars,
        "authorization_id": row["authorization_id"],
        "authorization_receipt_sha256": row[
            "authorization_receipt_sha256"
        ],
        "consumption_receipt_sha256": row["consumption_receipt_sha256"],
        "invocation_id": row["invocation_id"],
        "runtime_record_set_sha256": row["runtime_record_set_sha256"],
        "acceptance_command_sha256": row["acceptance_command_sha256"],
        "acceptance_event_log_sha256": row[
            "acceptance_event_log_sha256"
        ],
        "acceptance_event_nonce": row["acceptance_event_nonce"],
        "acceptance_raw_log_sha256": row["acceptance_raw_log_sha256"],
        "acceptance_junit_sha256": row["acceptance_junit_sha256"],
        "acceptance_outcome_sha256": row["acceptance_outcome_sha256"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "attested_at": row["attested_at"],
    }
    expected_attestation = {
        **expected_attestation_body,
        "attestation_sha256": canonical_sha256(expected_attestation_body),
    }
    if attestation != expected_attestation or attestation_sha256 != expected_attestation[
        "attestation_sha256"
    ]:
        raise Phase9ForensicReplayConflict(
            "Authority replay attestation fields differ"
        )

    evidence_authorization = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_evidence_authorizations "
        "WHERE authorization_id=?",
        (row["authorization_id"],),
    ).fetchone()
    evidence_consumption = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_evidence_consumptions "
        "WHERE authorization_id=?",
        (row["authorization_id"],),
    ).fetchone()
    if evidence_authorization is None or evidence_consumption is None:
        raise Phase9ForensicReplayConflict(
            "Authority replay producer authorization graph is incomplete"
        )
    evidence_authorization_json = _strict_json(
        str(evidence_authorization["authorization_json"]).encode("utf-8"),
        "Authority replay evidence authorization",
    )
    if _self_hash(
        evidence_authorization_json,
        "authorization_receipt_sha256",
        "Authority replay evidence authorization",
    ) != evidence_authorization["authorization_receipt_sha256"]:
        raise Phase9ForensicReplayConflict(
            "Authority replay evidence authorization hash differs"
        )
    expected_evidence_authorization = {
        "schema": "authority-phase9-replay-evidence-authorization-v1",
        "authorization_id": evidence_authorization["authorization_id"],
        "nonce_sha256": evidence_authorization["nonce_sha256"],
        "authorization_mechanism": "CONTROLLED_OS_ACCOUNT",
        "authorized": True,
        "operation": "PRODUCE_PHASE9_A_REPLAY_EVIDENCE",
        "project_id": request.project_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "replay_mode": request.replay_mode,
        "source_commit": request.source_commit,
        "source_tree": request.source_tree,
        "source_parent": request.source_parent,
        "source_inventory_sha256": request.source_inventory_sha256,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "entry_state_receipt_sha256": evaluation[
            "entry_state_receipt_sha256"
        ],
        "replay_coordinate_sha256": _replay_coordinate_sha256(request),
        "intended_evidence_root": (
            str(evidence_root.resolve(strict=True))
            if evidence_root is not None
            else evidence_authorization["intended_evidence_root"]
        ),
        "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
        "operator_uid": evidence_authorization["operator_uid"],
        "operator_account": evidence_authorization["operator_account"],
        "issued_at": evidence_authorization["issued_at"],
        "expires_at": evidence_authorization["expires_at"],
        "authorization_scope": {
            "replay_evidence_producer": True,
            "provider_or_network": False,
            "production_outbox_or_delivery": False,
            "release": False,
            "deployment": False,
            "migration": False,
            "cutover": False,
        },
    }
    expected_evidence_authorization["authorization_receipt_sha256"] = (
        canonical_sha256(expected_evidence_authorization)
    )
    if evidence_authorization_json != expected_evidence_authorization:
        raise Phase9ForensicReplayConflict(
            "Authority replay evidence authorization fields differ"
        )
    if (
        evidence_authorization["authorization_receipt_sha256"]
        != row["authorization_receipt_sha256"]
        or evidence_authorization["acceptance_spec_sha256"]
        != PHASE9_ACCEPTANCE_SPEC_SHA256
        or int(evidence_authorization["expires_at"])
        - int(evidence_authorization["issued_at"])
        > PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS
        or int(row["attested_at"]) > int(evidence_authorization["expires_at"])
    ):
        raise Phase9ForensicReplayConflict(
            "Authority replay evidence authorization validity differs"
        )
    evidence_consumption_json = _strict_json(
        str(evidence_consumption["consumption_json"]).encode("utf-8"),
        "Authority replay evidence consumption",
    )
    expected_evidence_consumption_body = {
        "schema": "authority-phase9-replay-evidence-consumption-v1",
        "authorization_id": evidence_authorization["authorization_id"],
        "nonce_sha256": evidence_authorization["nonce_sha256"],
        "authorization_receipt_sha256": evidence_authorization[
            "authorization_receipt_sha256"
        ],
        "invocation_id": evidence_consumption["invocation_id"],
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "consumed_at": evidence_consumption["consumed_at"],
    }
    expected_evidence_consumption = {
        **expected_evidence_consumption_body,
        "consumption_receipt_sha256": canonical_sha256(
            expected_evidence_consumption_body
        ),
    }
    if (
        evidence_consumption_json != expected_evidence_consumption
        or evidence_consumption["consumption_receipt_sha256"]
        != row["consumption_receipt_sha256"]
        or evidence_consumption["invocation_id"] != row["invocation_id"]
    ):
        raise Phase9ForensicReplayConflict(
            "Authority replay evidence consumption differs"
        )
    items = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_evidence_attestation_items "
        "WHERE attestation_sha256=? ORDER BY receipt_kind, logical_id",
        (attestation_sha256,),
    ).fetchall()
    receipts = sorted(
        evaluation["typed_receipts"],
        key=lambda value: (value.receipt_kind, value.logical_id),
    )
    if len(items) != len(receipts):
        raise Phase9ForensicReplayConflict(
            "Authority replay attestation item inventory differs"
        )
    runtime_record_hashes: list[str] = []
    for item, receipt in zip(items, receipts):
        body = _strict_json(
            receipt.receipt_json.encode(),
            f"attested receipt {receipt.receipt_kind}/{receipt.logical_id}",
        )
        expected = {
            "receipt_kind": receipt.receipt_kind,
            "logical_id": receipt.logical_id,
            "logical_path": receipt.logical_path,
            "byte_length": receipt.byte_length,
            "raw_bytes_sha256": receipt.raw_bytes_sha256,
            "receipt_sha256": receipt.receipt_sha256,
            "dependency_fingerprint_sha256": body["dependency_fingerprint_sha256"],
            "input_sha256": body["input_sha256"],
            "output_sha256": body["output_sha256"],
        }
        if any(item[key] != value for key, value in expected.items()):
            raise Phase9ForensicReplayConflict(
                "Authority replay attestation item differs"
            )
        item_json = _strict_json(
            str(item["item_json"]).encode(), "Authority replay attestation item"
        )
        if _self_hash(
            item_json, "item_sha256", "Authority replay attestation item"
        ) != item["item_sha256"]:
            raise Phase9ForensicReplayConflict(
                "Authority replay attestation item hash differs"
            )
        expected_item_body = {
            key: item[key]
            for key in (
                "attestation_sha256", "receipt_kind", "logical_id",
                "logical_path", "byte_length", "raw_bytes_sha256",
                "receipt_sha256", "source_kind", "source_record_sha256",
                "invocation_id", "attempt_id", "process_scope_id",
                "packet_sha256", "dependency_fingerprint_sha256",
                "input_sha256", "output_sha256",
            )
        }
        expected_item = {
            **expected_item_body,
            "item_sha256": canonical_sha256(expected_item_body),
        }
        if item_json != expected_item or item["item_sha256"] != expected_item[
            "item_sha256"
        ]:
            raise Phase9ForensicReplayConflict(
                "Authority replay attestation item fields differ"
            )
        if receipt.receipt_kind == "ACCEPTANCE_CASE":
            case_id = receipt.logical_id
            aggregate_binding = {
                "aggregate_command_sha256": row["acceptance_command_sha256"],
                "aggregate_raw_log_sha256": row["acceptance_raw_log_sha256"],
                "aggregate_junit_sha256": row["acceptance_junit_sha256"],
                "aggregate_event_log_sha256": row[
                    "acceptance_event_log_sha256"
                ],
                "aggregate_outcome_sha256": row[
                    "acceptance_outcome_sha256"
                ],
            }
            if (
                item["source_kind"] != "ACCEPTANCE_RUNNER"
                or any(
                    body.get(key) != value
                    for key, value in aggregate_binding.items()
                )
                or item["source_record_sha256"]
                != _acceptance_runner_case_source_sha256(
                    case_id,
                    PHASE9_ACCEPTANCE_TEST_NODES[case_id],
                    command_sha256=str(row["acceptance_command_sha256"]),
                    raw_log_sha256=str(row["acceptance_raw_log_sha256"]),
                    junit_sha256=str(row["acceptance_junit_sha256"]),
                    event_log_sha256=str(row["acceptance_event_log_sha256"]),
                    outcome_sha256=str(row["acceptance_outcome_sha256"]),
                )
                or outcome["cases"][PHASE9_ACCEPTANCE_CASES.index(case_id)][
                    "status"
                ]
                != "PASS"
            ):
                raise Phase9ForensicReplayConflict(
                    "acceptance item lacks its trusted runner result"
                )
        elif receipt.receipt_kind in _COMPONENT_RECEIPTS:
            if (
                item["source_kind"] != "EVIDENCE_PRODUCER"
                or item["source_record_sha256"]
                != evidence_consumption["consumption_receipt_sha256"]
                or any(
                    item[field] is not None
                    for field in (
                        "invocation_id", "attempt_id", "process_scope_id",
                        "packet_sha256",
                    )
                )
            ):
                raise Phase9ForensicReplayConflict(
                    "component item lacks its Authority producer consumption"
                )
        else:
            if item["source_kind"] != "RUNTIME_RECORD":
                raise Phase9ForensicReplayConflict(
                    "runtime item lacks an Authority runtime record"
                )
            runtime = connection.execute(
                "SELECT * FROM authority_production_phase9_replay_runtime_records "
                "WHERE record_sha256=?",
                (item["source_record_sha256"],),
            ).fetchone()
            if runtime is None or runtime["execution_domain"] != "FORMAL_PHASE9_A":
                raise Phase9ForensicReplayConflict(
                    "formal Authority runtime record is unavailable"
                )
            runtime_body = {
                "schema": "authority-phase9-runtime-record-v1",
                **{
                    key: runtime[key]
                    for key in (
                        "execution_domain", "workflow_id", "run_generation",
                        "receipt_kind", "logical_id", "invocation_id",
                        "attempt_id", "process_scope_id", "packet_sha256",
                        "dependency_fingerprint_sha256", "input_sha256",
                        "output_sha256", "logical_path", "byte_length",
                        "raw_bytes_sha256", "receipt_sha256",
                        "authority_source_sha256", "runtime_completion_sha256",
                        "recorded_at",
                    )
                },
            }
            expected_runtime_json = {
                **runtime_body,
                "record_sha256": canonical_sha256(runtime_body),
            }
            if (
                runtime["record_sha256"] != expected_runtime_json["record_sha256"]
                or _strict_json(
                    str(runtime["record_json"]).encode("utf-8"),
                    "Authority runtime record",
                )
                != expected_runtime_json
            ):
                raise Phase9ForensicReplayConflict(
                    "Authority runtime record JSON differs"
                )
            for key in (
                "receipt_kind", "logical_id", "logical_path", "byte_length",
                "raw_bytes_sha256", "receipt_sha256", "invocation_id",
                "attempt_id", "process_scope_id", "packet_sha256",
                "dependency_fingerprint_sha256", "input_sha256", "output_sha256",
            ):
                if runtime[key] != item[key]:
                    raise Phase9ForensicReplayConflict(
                        "Authority runtime record differs from attested receipt"
                    )
            if (
                runtime["workflow_id"] != request.workflow_id
                or runtime["run_generation"] != request.run_generation
                or runtime["authority_source_sha256"]
                != _authority_runtime_source_sha256(
                    connection,
                    request=request,
                    receipt_kind=receipt.receipt_kind,
                    logical_id=receipt.logical_id,
                    logical_path=receipt.logical_path,
                    raw_bytes_sha256=receipt.raw_bytes_sha256,
                    byte_length=receipt.byte_length,
                    receipt_sha256=receipt.receipt_sha256,
                    dependency_fingerprint_sha256=str(
                        body["dependency_fingerprint_sha256"]
                    ),
                    input_sha256=str(body["input_sha256"]),
                    output_sha256=str(body["output_sha256"]),
                    packet_sha256=body.get("packet_sha256"),
                    invocation_id=str(item["invocation_id"]),
                    attempt_id=str(item["attempt_id"]),
                    process_scope_id=str(item["process_scope_id"]),
                )
            ):
                raise Phase9ForensicReplayConflict(
                    "Authority runtime source binding differs"
                )
            _validate_authority_runtime_completion(
                connection,
                request=request,
                runtime=runtime,
                receipt_body=body,
                require_current_operator=require_current_operator,
            )
            runtime_record_hashes.append(str(runtime["record_sha256"]))
    expected_runtime_set = canonical_sha256(
        {
            "schema": "authority-phase9-runtime-record-set-v1",
            "record_sha256s": sorted(runtime_record_hashes),
        }
    )
    if row["runtime_record_set_sha256"] != expected_runtime_set:
        raise Phase9ForensicReplayConflict(
            "Authority runtime record set differs"
        )
    authorization = connection.execute(
        "SELECT * FROM authority_production_phase9_start_authorizations "
        "WHERE authorization_id=?",
        (evaluation["authorization_id"],),
    ).fetchone()
    if authorization is None:
        raise Phase9ForensicReplayConflict(
            "Authority-issued start authorization is unavailable"
        )
    expected_authorization = {
        "authorization_id": evaluation["authorization_id"],
        "nonce_sha256": evaluation["authorization_nonce_sha256"],
        "authorization_target_sha256": evaluation["authorization_target_sha256"],
        "evidence_attestation_sha256": attestation_sha256,
        "evidence_payload_set_sha256": expected_scalars[
            "evidence_payload_set_sha256"
        ],
        "project_id": request.project_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "source_commit": request.source_commit,
        "source_tree": request.source_tree,
        "source_parent": request.source_parent,
        "source_inventory_sha256": request.source_inventory_sha256,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "entry_state_receipt_sha256": evaluation["entry_state_receipt_sha256"],
        "authorization_receipt_sha256": evaluation[
            "authorization_receipt_sha256"
        ],
        "authorization_json": evaluation["authorization_json"],
    }
    external_start = _strict_json(
        str(evaluation["authorization_json"]).encode("utf-8"),
        "validated start authorization",
    )
    expected_authorization.update(
        {
            "issued_at": external_start["issued_at"],
            "expires_at": external_start["expires_at"],
        }
    )
    start_descriptor = next(
        (
            item for item in request.evidence_files
            if item.logical_path == "start_authorization.json"
        ),
        None,
    )
    if start_descriptor is None:
        raise Phase9ForensicReplayConflict(
            "start authorization evidence descriptor is unavailable"
        )
    expected_authorization.update(
        {
            "start_authorization_byte_length": start_descriptor.byte_length,
            "start_authorization_raw_bytes_sha256": (
                start_descriptor.raw_bytes_sha256
            ),
            "final_evidence_set_sha256": request.evidence_set_sha256,
                "operator_uid": (
                    os.geteuid()
                    if require_current_operator
                    else authorization["operator_uid"]
                ),
                "operator_account": (
                    pwd.getpwuid(os.geteuid()).pw_name
                    if require_current_operator
                    else authorization["operator_account"]
                ),
        }
    )
    if any(authorization[key] != value for key, value in expected_authorization.items()):
        raise Phase9ForensicReplayConflict(
            "Authority start authorization binding differs"
        )
    if not int(authorization["issued_at"]) <= trusted_now <= int(
        authorization["expires_at"]
    ):
        raise Phase9ForensicReplayConflict(
            "Authority start authorization is expired"
        )
    return row


class Phase9ForensicReplayService:
    """Finalize one already-authorized local replay in one transaction."""

    def __init__(
        self,
        database: str | Path,
        *,
        expected_source_fence_sha256: str,
        source_repository: str | Path,
        evidence_root: str | Path,
        official_input_root: str | Path,
        execution_context_receipt_path: str | Path,
        execution_root: str | Path | None = None,
        fault_hook: Callable[[str], None] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.path = authority_database_path(database)
        self.expected_source_fence_sha256 = _sha(
            expected_source_fence_sha256, "expected_source_fence_sha256"
        )
        self.source_repository = Path(source_repository)
        self.evidence_root = Path(evidence_root)
        self.official_input_root = Path(official_input_root)
        self.execution_context_receipt_path = Path(execution_context_receipt_path)
        self.execution_root = (
            None if execution_root is None else Path(execution_root)
        )
        configured_paths = [
            ("source_repository", self.source_repository),
            ("evidence_root", self.evidence_root),
            ("official_input_root", self.official_input_root),
            (
                "execution_context_receipt_path",
                self.execution_context_receipt_path,
            ),
        ]
        if self.execution_root is not None:
            configured_paths.append(("execution_root", self.execution_root))
        for name, path in configured_paths:
            if not path.is_absolute():
                raise Phase9ForensicReplaySafetyError(
                    f"{name} must be an absolute path"
                )
        if fault_hook is not None and not callable(fault_hook):
            raise Phase9ForensicReplaySafetyError("fault_hook must be callable")
        if clock is not None and not callable(clock):
            raise Phase9ForensicReplaySafetyError("clock must be callable")
        self.fault_hook = fault_hook
        self._clock = (lambda: int(time.time())) if clock is None else clock

    def _trusted_now(self) -> int:
        return _integer(self._clock(), "trusted_now")

    def _current_source_snapshot(self):
        try:
            return read_verified_execution_source_snapshot(
                self.source_repository,
                execution_root=self.execution_root,
            )
        except Phase9RunGenerationError as exc:
            raise Phase9ForensicReplayConflict(
                "current Git or executing source differs"
            ) from exc

    def _verify_acceptance_runner_bindings(
        self, evaluation: Mapping[str, object]
    ) -> tuple[tuple[object, ...], ...]:
        """Bind claimed acceptance runs to this live, immutable local runner."""

        bindings = evaluation.get("acceptance_runner_bindings")
        if type(bindings) is not tuple or tuple(
            item.get("case_id") if type(item) is dict else None for item in bindings
        ) != PHASE9_ACCEPTANCE_CASES:
            raise Phase9ForensicReplayConflict(
                "acceptance runner inventory differs"
            )
        configured_source = Path(os.path.abspath(self.source_repository))
        try:
            source_metadata = configured_source.lstat()
            resolved_source = configured_source.resolve(strict=True)
        except OSError as exc:
            raise Phase9ForensicReplayConflict(
                "acceptance working directory is unavailable"
            ) from exc
        if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISDIR(
            source_metadata.st_mode
        ):
            raise Phase9ForensicReplayConflict(
                "acceptance working directory must be a real directory"
            )
        trusted_python = Path(sys.executable).resolve(strict=True)
        actual_python, python_identity = _current_executable_descriptor(
            trusted_python
        )
        identities: list[tuple[object, ...]] = [
            (
                "WORKING_DIRECTORY",
                str(configured_source),
                source_metadata.st_dev,
                source_metadata.st_ino,
                source_metadata.st_mode,
                source_metadata.st_nlink,
                source_metadata.st_size,
                source_metadata.st_mtime_ns,
                source_metadata.st_ctime_ns,
            ),
            ("PYTHON_EXECUTABLE", str(trusted_python), *python_identity),
        ]
        for item in bindings:
            assert type(item) is dict
            case_id = str(item["case_id"])
            working_directory = Path(str(item["working_directory"]))
            python_executable = Path(str(item["python_executable"]))
            if (
                Path(os.path.abspath(working_directory)) != configured_source
                or working_directory.resolve(strict=True) != resolved_source
            ):
                raise Phase9ForensicReplayConflict(
                    "acceptance working directory differs from the configured source"
                )
            if (
                python_executable != trusted_python
                or item["python_executable_descriptor"] != actual_python
            ):
                raise Phase9ForensicReplayConflict(
                    "acceptance Python executable bytes differ"
                )
            environment = item["environment"]
            if (
                type(environment) is not dict
                or type(environment.get("variables")) is not dict
                or environment["variables"].get("PYTHONPATH")
                != str(configured_source)
            ):
                raise Phase9ForensicReplayConflict(
                    "acceptance environment source binding differs"
                )
            expected_node = PHASE9_ACCEPTANCE_TEST_NODES[case_id]
            relative = PurePosixPath(expected_node.split("::", 1)[0])
            target = configured_source.joinpath(*relative.parts)
            try:
                target_metadata = target.lstat()
            except OSError as exc:
                raise Phase9ForensicReplayConflict(
                    "acceptance fixed test target is unavailable"
                ) from exc
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not stat.S_ISREG(target_metadata.st_mode)
                or target_metadata.st_nlink != 1
            ):
                raise Phase9ForensicReplayConflict(
                    "acceptance fixed test target is not one source file"
                )
            basetemp = Path(str(item["basetemp"]))
            normalized_basetemp = basetemp.resolve(strict=False)
            if (
                not basetemp.is_absolute()
                or normalized_basetemp == resolved_source
                or normalized_basetemp.is_relative_to(resolved_source)
                or normalized_basetemp == self.evidence_root.resolve(strict=True)
                or normalized_basetemp.is_relative_to(
                    self.evidence_root.resolve(strict=True)
                )
                or normalized_basetemp == self.official_input_root.resolve(strict=True)
                or normalized_basetemp.is_relative_to(
                    self.official_input_root.resolve(strict=True)
                )
            ):
                raise Phase9ForensicReplayConflict(
                    "acceptance basetemp is not isolated from bound inputs"
                )
            identities.append(
                (
                    "TEST_TARGET",
                    case_id,
                    expected_node,
                    target_metadata.st_dev,
                    target_metadata.st_ino,
                    target_metadata.st_mode,
                    target_metadata.st_nlink,
                    target_metadata.st_size,
                    target_metadata.st_mtime_ns,
                    target_metadata.st_ctime_ns,
                )
            )
        return tuple(identities)

    def _recover_committed(
        self,
        request: Phase9ForensicReplayRequestV1,
    ) -> Phase9ForensicReplayResult | None:
        """Recover one exact committed terminal graph using database facts only."""

        with isolated_authority_snapshot_ro(self.path) as connection:
            try:
                connection.execute("BEGIN")
                self._verify_installation(connection)
                replay = self._replay(connection, request)
                if replay is not None:
                    try:
                        completed = _validate_phase9_completed_replay_in_transaction(
                            connection,
                            workflow_id=request.workflow_id,
                            expected_run_generation=request.run_generation,
                            expected_terminal_receipt_sha256=replay.receipt_sha256,
                            expected_replay_id=request.replay_id,
                            require_current=False,
                        )
                    except Phase9ForensicReplaySafetyError as exc:
                        raise Phase9ForensicReplayConflict(
                            "committed replay graph is invalid"
                        ) from exc
                    if (
                        completed.get("replay_id") != request.replay_id
                        or completed.get("request_sha256") != request.request_sha256
                    ):
                        raise Phase9ForensicReplayConflict(
                            "idempotent replay is not the exact committed terminal graph"
                        )
                connection.commit()
                return replay
            except Exception:
                connection.rollback()
                raise

    def _fault(self, checkpoint: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(checkpoint)

    def _live_entry_state(
        self,
        connection: sqlite3.Connection,
        request: Phase9ForensicReplayRequestV1,
    ) -> object:
        try:
            return collect_phase9_entry_state_in_transaction(
                connection,
                expected_source_fence_sha256=self.expected_source_fence_sha256,
                workflow_id=request.workflow_id,
                candidate=CandidateIdentity(
                    request.source_commit, request.source_tree, request.source_parent
                ),
            )
        except Phase9EntryError as exc:
            raise Phase9ForensicReplayConflict(
                "live Phase9 entry state differs from the READY gate"
            ) from exc

    @staticmethod
    def _verify_live_entry_state(
        state: object,
        *,
        expected_state_receipt_sha256: str,
        runtime_counts: Mapping[str, object],
    ) -> None:
        actual = getattr(state, "state_receipt_sha256", None)
        if actual != expected_state_receipt_sha256:
            raise Phase9ForensicReplayConflict(
                "live Phase9 entry state differs from the READY gate"
            )
        if (
            getattr(state, "active_process_count", None)
            != runtime_counts.get("active_descendant_count")
            or getattr(state, "pending_outbox_count", None)
            != runtime_counts.get("pending_outbox_count")
        ):
            raise Phase9ForensicReplayConflict(
                "runtime evidence differs from live Authority state"
            )

    @staticmethod
    def _control_fence(connection: sqlite3.Connection) -> None:
        writer = connection.execute(
            "SELECT switch_mode, writer_enabled FROM "
            "authority_production_writer_state WHERE singleton=1"
        ).fetchone()
        consumer = connection.execute(
            "SELECT consumer_enabled FROM authority_production_consumer_state "
            "WHERE singleton=1"
        ).fetchone()
        if (
            writer is None or consumer is None or writer["switch_mode"] != "V1_ONLY"
            or bool(writer["writer_enabled"]) or bool(consumer["consumer_enabled"])
        ):
            raise Phase9ForensicReplaySafetyError(
                "Phase9 finalization requires V1_ONLY and disabled writer/consumer"
            )

    def _verify_installation(self, connection: sqlite3.Connection) -> None:
        verify_production_installation(connection, require_ready=True)
        if legacy_source_identity_sha256(connection) != self.expected_source_fence_sha256:
            raise Phase9ForensicReplayConflict("Authority source fence differs")

    @staticmethod
    def _generation_request(
        connection: sqlite3.Connection,
        request: Phase9ForensicReplayRequestV1,
    ) -> RunGenerationRequestV1:
        row = connection.execute(
            "SELECT * FROM authority_production_run_generation_creation_receipts "
            "WHERE run_generation=? AND receipt_sha256=?",
            (
                request.run_generation,
                request.run_generation_creation_receipt_sha256,
            ),
        ).fetchone()
        if row is None:
            raise Phase9ForensicReplayConflict(
                "run-generation creation receipt is unavailable"
            )
        body = _strict_json(
            str(row["receipt_json"]).encode("utf-8"),
            "run-generation creation receipt",
        )
        expected_keys = {
            "schema", "run_generation", "workflow_id", "operation_kind",
            "request_sha256", "request", "official_input_manifest_sha256",
            "official_input_raw_bytes_set_sha256",
            "execution_context_receipt_sha256",
            "operator_authorization_receipt_sha256",
            "operator_authorization_consumption_sha256",
            "authorization_target_sha256", "source_inventory_sha256",
            "occurred_at",
        }
        _mapping(body, "run-generation creation receipt", expected_keys)
        occurred_at = _integer(
            body.get("occurred_at"), "run-generation creation receipt.occurred_at"
        )
        try:
            generation_request = _run_generation_request_from_dict_binding(
                body.get("request")
            )
        except Phase9RunGenerationError as exc:
            raise Phase9ForensicReplayConflict(
                "stored run-generation request is invalid"
            ) from exc
        expected = {
            "schema": RUN_GENERATION_RECEIPT_SCHEMA,
            "run_generation": request.run_generation,
            "workflow_id": request.workflow_id,
            "operation_kind": generation_request.operation_kind,
            "request_sha256": generation_request.request_sha256,
            "official_input_manifest_sha256": (
                generation_request.official_inputs.manifest_sha256
            ),
            "official_input_raw_bytes_set_sha256": (
                generation_request.official_inputs.raw_bytes_set_sha256
            ),
            "execution_context_receipt_sha256": (
                generation_request.execution_context.receipt_sha256
            ),
            "operator_authorization_receipt_sha256": (
                generation_request.operator_authorization.receipt_sha256
            ),
            "operator_authorization_consumption_sha256": canonical_sha256(
                {
                    "schema": "authority-phase9-run-generation-authorization-consumption-v1",
                    "authorization_id": (
                        generation_request.operator_authorization.authorization_id
                    ),
                    "authorization_receipt_sha256": (
                        generation_request.operator_authorization.receipt_sha256
                    ),
                    "authorization_target_sha256": (
                        generation_request.authorization_target_sha256
                    ),
                    "request_sha256": generation_request.request_sha256,
                    "run_generation": generation_request.derived_run_generation,
                    "workflow_id": generation_request.workflow_id,
                    "consumed_at": generation_request.occurred_at,
                }
            ),
            "authorization_target_sha256": (
                generation_request.authorization_target_sha256
            ),
            "source_inventory_sha256": request.source_inventory_sha256,
            "occurred_at": generation_request.occurred_at,
        }
        if (
            canonical_sha256(body) != row["receipt_sha256"]
            or row["receipt_id"]
            != f"run-generation-receipt:{row['receipt_sha256'][:32]}"
            or row["run_generation"] != request.run_generation
            or row["workflow_id"] != request.workflow_id
            or row["operation_kind"] != generation_request.operation_kind
            or row["occurred_at"] != generation_request.occurred_at
            or row["request_sha256"] != generation_request.request_sha256
            or any(body.get(name) != value for name, value in expected.items())
            or body.get("request") != generation_request.as_dict()
            or generation_request.derived_run_generation != request.run_generation
            or generation_request.project_id != request.project_id
            or generation_request.workflow_id != request.workflow_id
            or generation_request.project_revision != request.project_revision
            or generation_request.project_generation != request.project_generation
            or generation_request.source.source_commit != request.source_commit
            or generation_request.source.source_tree != request.source_tree
            or generation_request.source.source_parent != request.source_parent
            or generation_request.source_inventory_sha256
            != request.source_inventory_sha256
        ):
            raise Phase9ForensicReplayConflict(
                "run-generation creation receipt binding differs"
            )
        return generation_request

    def _verify_external_generation_inputs(
        self,
        connection: sqlite3.Connection,
        request: Phase9ForensicReplayRequestV1,
    ) -> tuple[
        RunGenerationRequestV1,
        tuple[tuple[str, int, str], ...],
        bytes,
        tuple[tuple[object, ...], ...],
        tuple[object, ...],
    ]:
        generation_request = self._generation_request(connection, request)
        try:
            official_identity_before = _external_tree_identity(
                self.official_input_root, label="official input root"
            )
            context_identity_before = _external_file_identity(
                self.execution_context_receipt_path,
                label="execution context receipt",
            )
            official_snapshot = verify_official_input_snapshot(
                self.official_input_root,
                generation_request.official_inputs,
            )
            context_bytes = verify_execution_context_receipt(
                self.execution_context_receipt_path,
                generation_request.execution_context,
            )
            official_identity_after = _external_tree_identity(
                self.official_input_root, label="official input root"
            )
            context_identity_after = _external_file_identity(
                self.execution_context_receipt_path,
                label="execution context receipt",
            )
        except (Phase9RunGenerationError, OSError) as exc:
            raise Phase9ForensicReplayConflict(
                "current official input or execution context differs"
            ) from exc
        if (
            official_identity_before != official_identity_after
            or context_identity_before != context_identity_after
        ):
            raise Phase9ForensicReplayConflict(
                "official input or execution context changed while read"
            )
        return (
            generation_request,
            official_snapshot,
            context_bytes,
            official_identity_after,
            context_identity_after,
        )

    def _revalidate_live_inputs(
        self,
        connection: sqlite3.Connection,
        request: Phase9ForensicReplayRequestV1,
        *,
        baseline: Mapping[str, object],
        minimum_trusted_now: int,
    ) -> int:
        """Repeat every mutable gate/input check at a transaction boundary."""

        values, evidence_inventory = _read_evidence_set(
            self.evidence_root, request
        )
        trusted_now = self._trusted_now()
        if trusted_now < minimum_trusted_now:
            raise Phase9ForensicReplayConflict(
                "trusted current time moved backwards during transaction"
            )
        evaluation = _evaluate_evidence(
            request, values, trusted_now=trusted_now
        )
        if (
            values != baseline["evidence_values"]
            or evidence_inventory != baseline["evidence_inventory"]
            or evaluation != baseline["evaluation"]
        ):
            raise Phase9ForensicReplayConflict(
                "evidence changed during transaction"
            )
        live_state = self._live_entry_state(connection, request)
        self._verify_live_entry_state(
            live_state,
            expected_state_receipt_sha256=str(
                evaluation["entry_state_receipt_sha256"]
            ),
            runtime_counts=_mapping(
                evaluation["runtime_counts"], "runtime_counts"
            ),
        )
        if live_state != baseline["live_state"]:
            raise Phase9ForensicReplayConflict(
                "live Phase9 entry state changed during transaction"
            )
        _verify_authority_replay_attestation(
            connection, request, evaluation, trusted_now=trusted_now,
            evidence_root=self.evidence_root,
        )
        external = self._verify_external_generation_inputs(connection, request)
        if external != baseline["external_generation_inputs"]:
            raise Phase9ForensicReplayConflict(
                "official input or execution context changed during transaction"
            )
        current_source = self._current_source_snapshot()
        if _source_snapshot_tuple(current_source) != baseline["source"]:
            raise Phase9ForensicReplayConflict(
                "source changed during transaction"
            )
        runner_identity = self._verify_acceptance_runner_bindings(evaluation)
        if runner_identity != baseline["acceptance_runner_identity"]:
            raise Phase9ForensicReplayConflict(
                "acceptance runner inputs changed during transaction"
            )
        self._control_fence(connection)
        return trusted_now

    @staticmethod
    def _verify_coordinate(
        connection: sqlite3.Connection, request: Phase9ForensicReplayRequestV1
    ) -> None:
        row = connection.execute(
            """
            SELECT w.project_id, w.current_revision, w.project_generation,
                   w.run_generation, c.creation_receipt_sha256,
                   g.source_commit, g.source_tree, g.source_parent,
                   g.source_inventory_sha256, g.run_mode,
                   g.modeling_consultation_contract,
                   g.delivery_capability
            FROM authority_workflows w
            JOIN authority_production_run_generation_current c
              ON c.workflow_id=w.workflow_id AND c.run_generation=w.run_generation
            JOIN authority_production_run_generations g
              ON g.workflow_id=w.workflow_id AND g.run_generation=w.run_generation
            WHERE w.workflow_id=?
            """,
            (request.workflow_id,),
        ).fetchone()
        if row is None or (
            row["project_id"] != request.project_id
            or row["current_revision"] != request.project_revision
            or row["project_generation"] != request.project_generation
            or row["run_generation"] != request.run_generation
            or row["creation_receipt_sha256"]
            != request.run_generation_creation_receipt_sha256
            or row["source_commit"] != request.source_commit
            or row["source_tree"] != request.source_tree
            or row["source_parent"] != request.source_parent
            or row["source_inventory_sha256"] != request.source_inventory_sha256
            or row["run_mode"] != "FORENSIC_REPLAY"
            or row["modeling_consultation_contract"]
            != "LEGACY_NOT_APPLICABLE"
            or row["delivery_capability"] != DELIVERY_DISABLED
        ):
            raise Phase9ForensicReplayConflict("current generation coordinate differs")

    @staticmethod
    def _verify_predecessor(
        connection: sqlite3.Connection, request: Phase9ForensicReplayRequestV1
    ) -> None:
        current = connection.execute(
            "SELECT * FROM authority_production_phase9_replay_current "
            "WHERE workflow_id=?",
            (request.workflow_id,),
        ).fetchone()
        if request.operation_kind == CREATE:
            if current is not None:
                raise Phase9ForensicReplayConflict("CREATE requires no current Phase9 replay")
            return
        if current is None or (
            current["replay_id"] != request.predecessor_replay_id
            or current["terminal_receipt_sha256"]
            != request.predecessor_terminal_receipt_sha256
        ):
            raise Phase9ForensicReplayConflict("ROTATE predecessor pointer differs")

    @staticmethod
    def _replay(
        connection: sqlite3.Connection, request: Phase9ForensicReplayRequestV1
    ) -> Phase9ForensicReplayResult | None:
        rows = connection.execute(
            "SELECT * FROM authority_production_phase9_replay_idempotency "
            "WHERE idempotency_key=? ORDER BY workflow_id",
            (request.idempotency_key,),
        ).fetchall()
        if rows and (
            len(rows) != 1 or rows[0]["workflow_id"] != request.workflow_id
        ):
            raise Phase9ForensicReplayConflict(
                "idempotency key has a different workflow binding"
            )
        if not rows:
            # ``request_json`` is the immutable reverse binding for the legacy
            # composite-key table.  A removed/moved idempotency row must not
            # make its key reusable in another workflow.
            for stored_replay in connection.execute(
                "SELECT replay_id, request_json, request_sha256 FROM "
                "authority_production_phase9_replays"
            ).fetchall():
                stored_body = _strict_json(
                    str(stored_replay["request_json"]).encode("utf-8"),
                    "stored replay request",
                )
                try:
                    stored_request = phase9_forensic_replay_request_from_dict(
                        stored_body
                    )
                except Phase9ForensicReplayError as exc:
                    raise Phase9ForensicReplayConflict(
                        "stored replay request is invalid"
                    ) from exc
                if (
                    stored_request.request_sha256
                    != stored_replay["request_sha256"]
                    or stored_request.replay_id != stored_replay["replay_id"]
                ):
                    raise Phase9ForensicReplayConflict(
                        "stored replay request identity differs"
                    )
                if stored_request.idempotency_key == request.idempotency_key:
                    raise Phase9ForensicReplayConflict(
                        "committed replay trace lacks its exact global "
                        "idempotency key binding"
                    )
            identity = (request.request_sha256, request.replay_id)
            trace_queries = (
                (
                    "SELECT 1 FROM "
                    "authority_production_phase9_replay_idempotency "
                    "WHERE request_sha256=? OR replay_id=? LIMIT 1",
                    identity,
                ),
                (
                    "SELECT 1 FROM authority_production_phase9_replays "
                    "WHERE request_sha256=? OR replay_id=? LIMIT 1",
                    identity,
                ),
                (
                    "SELECT 1 FROM authority_production_phase9_replay_events "
                    "WHERE replay_id=? LIMIT 1",
                    (request.replay_id,),
                ),
                (
                    "SELECT 1 FROM "
                    "authority_production_phase9_terminal_receipts "
                    "WHERE replay_id=? LIMIT 1",
                    (request.replay_id,),
                ),
                (
                    "SELECT 1 FROM authority_production_phase9_replay_current "
                    "WHERE replay_id=? LIMIT 1",
                    (request.replay_id,),
                ),
                (
                    "SELECT 1 FROM "
                    "authority_production_phase9_evidence_receipts "
                    "WHERE replay_id=? LIMIT 1",
                    (request.replay_id,),
                ),
                (
                    "SELECT 1 FROM "
                    "authority_production_phase9_gate_consumptions "
                    "WHERE request_sha256=? OR replay_id=? LIMIT 1",
                    identity,
                ),
                (
                    "SELECT 1 FROM "
                    "authority_production_phase9_start_authorization_consumptions "
                    "WHERE request_sha256=? OR replay_id=? LIMIT 1",
                    identity,
                ),
            )
            for query, parameters in trace_queries:
                if connection.execute(query, parameters).fetchone() is not None:
                    raise Phase9ForensicReplayConflict(
                        "committed replay trace lacks its exact idempotency binding"
                    )
            return None
        row = rows[0]
        if row["request_sha256"] != request.request_sha256 or row["replay_id"] != request.replay_id:
            raise Phase9ForensicReplayConflict("idempotency key has different request bytes")
        validate_phase9_forensic_replay_request(request)
        reverse_bindings = connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_replay_idempotency "
            "WHERE request_sha256=? OR replay_id=? OR terminal_receipt_sha256=?",
            (
                request.request_sha256,
                request.replay_id,
                row["terminal_receipt_sha256"],
            ),
        ).fetchone()[0]
        if reverse_bindings != 1:
            raise Phase9ForensicReplayConflict(
                "idempotency reverse binding is not unique"
            )
        receipt = connection.execute(
            "SELECT * FROM authority_production_phase9_terminal_receipts "
            "WHERE replay_id=? AND receipt_sha256=?",
            (request.replay_id, row["terminal_receipt_sha256"]),
        ).fetchone()
        if receipt is None:
            raise Phase9ForensicReplayConflict("idempotent terminal receipt differs")
        return _result_from_receipt(request, receipt, replayed=False)

    @staticmethod
    def _consume_entry_gate(
        connection: sqlite3.Connection,
        request: Phase9ForensicReplayRequestV1,
        evaluation: Mapping[str, object],
        *,
        consumed_at: int,
    ) -> str:
        body = {
            "schema": PHASE9_GATE_CONSUMPTION_SCHEMA,
            "gate_result_sha256": request.entry_gate_result_sha256,
            "entry_state_receipt_sha256": evaluation["entry_state_receipt_sha256"],
            "start_authorization_id": evaluation["authorization_id"],
            "start_authorization_receipt_sha256": evaluation[
                "authorization_receipt_sha256"
            ],
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            "replay_id": request.replay_id,
            "request_sha256": request.request_sha256,
            "consumed_at": consumed_at,
        }
        receipt_sha256 = canonical_sha256(body)
        receipt = dict(body)
        receipt["receipt_sha256"] = receipt_sha256
        try:
            connection.execute(
                """
                INSERT INTO authority_production_phase9_gate_consumptions(
                    gate_result_sha256, entry_state_receipt_sha256,
                    start_authorization_id, start_authorization_receipt_sha256,
                    workflow_id, run_generation, replay_id, request_sha256,
                    consumed_at, receipt_json, receipt_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.entry_gate_result_sha256,
                    evaluation["entry_state_receipt_sha256"],
                    evaluation["authorization_id"],
                    evaluation["authorization_receipt_sha256"],
                    request.workflow_id,
                    request.run_generation,
                    request.replay_id,
                    request.request_sha256,
                    consumed_at,
                    canonical_bytes(receipt).decode("utf-8"),
                    receipt_sha256,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise Phase9ForensicReplayConflict(
                "entry gate or start authorization was already consumed"
            ) from exc
        return receipt_sha256

    @staticmethod
    def _consume_start_authorization(
        connection: sqlite3.Connection,
        request: Phase9ForensicReplayRequestV1,
        evaluation: Mapping[str, object],
        *,
        consumed_at: int,
    ) -> str:
        body = {
            "schema": PHASE9_REPLAY_START_CONSUMPTION_SCHEMA,
            "authorization_id": evaluation["authorization_id"],
            "nonce_sha256": evaluation["authorization_nonce_sha256"],
            "authorization_receipt_sha256": evaluation[
                "authorization_receipt_sha256"
            ],
            "authorization_target_sha256": evaluation[
                "authorization_target_sha256"
            ],
            "evidence_attestation_sha256": evaluation[
                "evidence_attestation_sha256"
            ],
            "request_sha256": request.request_sha256,
            "replay_id": request.replay_id,
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            "consumed_at": consumed_at,
        }
        receipt_sha256 = canonical_sha256(body)
        receipt = {**body, "consumption_receipt_sha256": receipt_sha256}
        connection.create_function(
            "phase9_replay_start_write_capability", 0, lambda: 1
        )
        try:
            connection.execute(
                """
                INSERT INTO authority_production_phase9_start_authorization_consumptions(
                    authorization_id, nonce_sha256, request_sha256, replay_id,
                    workflow_id, run_generation, consumed_at, consumption_json,
                    consumption_receipt_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    evaluation["authorization_id"],
                    evaluation["authorization_nonce_sha256"],
                    request.request_sha256,
                    request.replay_id,
                    request.workflow_id,
                    request.run_generation,
                    consumed_at,
                    canonical_bytes(receipt).decode("utf-8"),
                    receipt_sha256,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise Phase9ForensicReplayConflict(
                "start authorization nonce was already consumed"
            ) from exc
        finally:
            connection.create_function(
                "phase9_replay_start_write_capability", 0, lambda: 0
            )
        return receipt_sha256

    @staticmethod
    def _insert_typed_receipts(
        connection: sqlite3.Connection,
        request: Phase9ForensicReplayRequestV1,
        evaluation: Mapping[str, object],
    ) -> None:
        receipts = evaluation.get("typed_receipts")
        if type(receipts) is not tuple or any(
            type(receipt) is not ValidatedEvidenceReceiptV1 for receipt in receipts
        ):
            raise Phase9ForensicReplaySafetyError("typed receipt collection is malformed")
        for receipt in receipts:
            connection.execute(
                """
                INSERT INTO authority_production_phase9_evidence_receipts(
                    replay_id, workflow_id, run_generation, receipt_kind,
                    logical_id, logical_path, byte_length, raw_bytes_sha256,
                    receipt_json, receipt_sha256, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.replay_id,
                    request.workflow_id,
                    request.run_generation,
                    receipt.receipt_kind,
                    receipt.logical_id,
                    receipt.logical_path,
                    receipt.byte_length,
                    receipt.raw_bytes_sha256,
                    receipt.receipt_json,
                    receipt.receipt_sha256,
                    receipt.occurred_at,
                ),
            )

    @staticmethod
    def _verify_committed_bindings(
        connection: sqlite3.Connection,
        request: Phase9ForensicReplayRequestV1,
        evaluation: Mapping[str, object],
    ) -> None:
        start_consumption = connection.execute(
            "SELECT * FROM authority_production_phase9_start_authorization_consumptions "
            "WHERE authorization_id=?",
            (evaluation["authorization_id"],),
        ).fetchone()
        if start_consumption is None or any(
            start_consumption[field] != expected
            for field, expected in (
                ("nonce_sha256", evaluation["authorization_nonce_sha256"]),
                ("request_sha256", request.request_sha256),
                ("replay_id", request.replay_id),
                ("workflow_id", request.workflow_id),
                ("run_generation", request.run_generation),
            )
        ):
            raise Phase9ForensicReplayConflict(
                "stored start authorization consumption differs"
            )
        consumption_body = _strict_json(
            str(start_consumption["consumption_json"]).encode(),
            "stored start authorization consumption",
        )
        if _self_hash(
            consumption_body,
            "consumption_receipt_sha256",
            "stored start authorization consumption",
        ) != start_consumption["consumption_receipt_sha256"]:
            raise Phase9ForensicReplayConflict(
                "stored start authorization consumption hash differs"
            )
        consumption = connection.execute(
            "SELECT * FROM authority_production_phase9_gate_consumptions "
            "WHERE gate_result_sha256=?",
            (request.entry_gate_result_sha256,),
        ).fetchone()
        if consumption is None or any(
            consumption[field] != expected
            for field, expected in (
                ("entry_state_receipt_sha256", evaluation["entry_state_receipt_sha256"]),
                ("start_authorization_id", evaluation["authorization_id"]),
                (
                    "start_authorization_receipt_sha256",
                    evaluation["authorization_receipt_sha256"],
                ),
                ("workflow_id", request.workflow_id),
                ("run_generation", request.run_generation),
                ("replay_id", request.replay_id),
                ("request_sha256", request.request_sha256),
            )
        ):
            raise Phase9ForensicReplayConflict("stored gate consumption differs")
        consumption_body = _strict_json(
            str(consumption["receipt_json"]).encode("utf-8"),
            "stored gate consumption",
        )
        if (
            _self_hash(consumption_body, "receipt_sha256", "stored gate consumption")
            != consumption["receipt_sha256"]
        ):
            raise Phase9ForensicReplayConflict("stored gate consumption hash differs")
        rows = connection.execute(
            "SELECT * FROM authority_production_phase9_evidence_receipts "
            "WHERE replay_id=? ORDER BY receipt_kind, logical_id",
            (request.replay_id,),
        ).fetchall()
        expected_receipts = sorted(
            evaluation["typed_receipts"],
            key=lambda item: (item.receipt_kind, item.logical_id),
        )
        if len(rows) != len(expected_receipts):
            raise Phase9ForensicReplayConflict("stored typed receipt inventory differs")
        for row, expected in zip(rows, expected_receipts):
            if any(
                row[field] != value
                for field, value in (
                    ("workflow_id", request.workflow_id),
                    ("run_generation", request.run_generation),
                    ("receipt_kind", expected.receipt_kind),
                    ("logical_id", expected.logical_id),
                    ("logical_path", expected.logical_path),
                    ("byte_length", expected.byte_length),
                    ("raw_bytes_sha256", expected.raw_bytes_sha256),
                    ("receipt_json", expected.receipt_json),
                    ("receipt_sha256", expected.receipt_sha256),
                    ("occurred_at", expected.occurred_at),
                )
            ):
                raise Phase9ForensicReplayConflict("stored typed receipt differs")

    def execute(
        self, request: Phase9ForensicReplayRequestV1
    ) -> Phase9ForensicReplayResult:
        lookup = _validate_phase9_forensic_recovery_identity(request)
        commit_lease = authority_state_commit_lease(self.path.parent.parent)
        try:
            commit_lease.__enter__()
        except AuthorityStateLeaseError as exc:
            raise Phase9ForensicReplayConflict(
                "Authority state commit lease cannot be acquired"
            ) from exc
        connection: sqlite3.Connection | None = None
        try:
            committed = self._recover_committed(lookup)
            if committed is not None:
                return committed
            value = validate_phase9_forensic_replay_request(lookup)
            # Resolve every deterministic rejection through a query-only
            # connection while holding the shared writer lease.  A rejected
            # request must not create or remove WAL/SHM state merely because a
            # write-capable SQLite connection was opened.
            with isolated_authority_snapshot_ro(self.path) as preflight:
                try:
                    preflight.execute("BEGIN")
                    self._verify_installation(preflight)
                    replay = self._replay(preflight, value)
                    if replay is not None:
                        completed = _validate_phase9_completed_replay_in_transaction(
                            preflight,
                            workflow_id=value.workflow_id,
                            expected_run_generation=value.run_generation,
                            expected_terminal_receipt_sha256=replay.receipt_sha256,
                            expected_replay_id=value.replay_id,
                            require_current=False,
                        )
                        if (
                            completed.get("replay_id") != value.replay_id
                            or completed.get("request_sha256")
                            != value.request_sha256
                        ):
                            raise Phase9ForensicReplayConflict(
                                "idempotent replay is not the exact committed "
                                "terminal graph"
                            )
                        preflight.commit()
                        return replay
                    first_now = self._trusted_now()
                    preflight_values, _preflight_inventory = _read_evidence_set(
                        self.evidence_root, value
                    )
                    preflight_evaluation = _evaluate_evidence(
                        value, preflight_values, trusted_now=first_now
                    )
                    if preflight_evaluation["blockers"]:
                        raise Phase9ForensicReplaySafetyError(
                            "Phase9 replay preflight is BLOCKED: "
                            + canonical_bytes(
                                preflight_evaluation["blockers"]
                            ).decode("utf-8")
                        )
                    expected_source = (
                        value.source_commit,
                        value.source_tree,
                        value.source_parent,
                        value.source_inventory_sha256,
                    )
                    self._verify_acceptance_runner_bindings(preflight_evaluation)
                    self._control_fence(preflight)
                    self._verify_coordinate(preflight, value)
                    source = self._current_source_snapshot()
                    if _source_snapshot_tuple(source) != expected_source:
                        raise Phase9ForensicReplayConflict(
                            "current source identity differs"
                        )
                    self._verify_external_generation_inputs(preflight, value)
                    _verify_authority_replay_attestation(
                        preflight,
                        value,
                        preflight_evaluation,
                        trusted_now=first_now,
                        evidence_root=self.evidence_root,
                    )
                    self._verify_predecessor(preflight, value)
                    preflight_live_state = self._live_entry_state(preflight, value)
                    self._verify_live_entry_state(
                        preflight_live_state,
                        expected_state_receipt_sha256=str(
                            preflight_evaluation["entry_state_receipt_sha256"]
                        ),
                        runtime_counts=_mapping(
                            preflight_evaluation["runtime_counts"],
                            "runtime_counts",
                        ),
                    )
                    _verify_authority_replay_attestation(
                        preflight,
                        value,
                        preflight_evaluation,
                        trusted_now=first_now,
                        evidence_root=self.evidence_root,
                    )
                    preflight.commit()
                except Exception:
                    preflight.rollback()
                    raise

            connection = connect_authority_rw(self.path)
            connection.execute("BEGIN IMMEDIATE")
            self._verify_installation(connection)
            replay = self._replay(connection, value)
            if replay is not None:
                try:
                    completed = _validate_phase9_completed_replay_in_transaction(
                        connection,
                        workflow_id=value.workflow_id,
                        expected_run_generation=value.run_generation,
                        expected_terminal_receipt_sha256=replay.receipt_sha256,
                        expected_replay_id=value.replay_id,
                        require_current=False,
                    )
                except Phase9ForensicReplaySafetyError as exc:
                    raise Phase9ForensicReplayConflict(
                        "committed replay graph is invalid"
                    ) from exc
                if (
                    completed.get("replay_id") != value.replay_id
                    or completed.get("request_sha256") != value.request_sha256
                ):
                    raise Phase9ForensicReplayConflict(
                        "idempotent replay is not the exact committed terminal graph"
                    )
                connection.commit()
                return replay
            first_values, first_evidence_inventory = _read_evidence_set(
                self.evidence_root, value
            )
            evaluation = _evaluate_evidence(
                value, first_values, trusted_now=first_now
            )
            if evaluation["blockers"]:
                raise Phase9ForensicReplaySafetyError(
                    "Phase9 replay preflight is BLOCKED: "
                    + canonical_bytes(evaluation["blockers"]).decode("utf-8")
                )
            expected_source = (
                value.source_commit,
                value.source_tree,
                value.source_parent,
                value.source_inventory_sha256,
            )
            first_runner_identity = self._verify_acceptance_runner_bindings(
                evaluation
            )
            self._control_fence(connection)
            self._verify_coordinate(connection, value)
            source = self._current_source_snapshot()
            if _source_snapshot_tuple(source) != expected_source:
                raise Phase9ForensicReplayConflict("current source identity differs")
            (
                first_generation_request,
                first_official_snapshot,
                first_context_bytes,
                first_official_identity,
                first_context_identity,
            ) = self._verify_external_generation_inputs(connection, value)
            _verify_authority_replay_attestation(
                connection, value, evaluation, trusted_now=first_now,
                evidence_root=self.evidence_root,
            )
            self._verify_predecessor(connection, value)
            first_live_state = self._live_entry_state(connection, value)
            self._verify_live_entry_state(
                first_live_state,
                expected_state_receipt_sha256=str(
                    evaluation["entry_state_receipt_sha256"]
                ),
                runtime_counts=_mapping(
                    evaluation["runtime_counts"], "runtime_counts"
                ),
            )
            _verify_authority_replay_attestation(
                connection, value, evaluation, trusted_now=first_now,
                evidence_root=self.evidence_root,
            )
            baseline = {
                "evidence_values": first_values,
                "evidence_inventory": first_evidence_inventory,
                "evaluation": evaluation,
                "live_state": first_live_state,
                "external_generation_inputs": (
                    first_generation_request,
                    first_official_snapshot,
                    first_context_bytes,
                    first_official_identity,
                    first_context_identity,
                ),
                "source": expected_source,
                "acceptance_runner_identity": first_runner_identity,
            }
            self._fault("after_live_gate_start")
            connection.execute(
                """
                INSERT INTO authority_production_phase9_replays(
                    replay_id, workflow_id, project_id, project_revision,
                    project_generation, run_generation,
                    run_generation_creation_receipt_sha256, operation_kind,
                    predecessor_replay_id, predecessor_terminal_receipt_sha256,
                    replay_mode, requested_resume_target, delivery_capability,
                    source_commit, source_tree, source_parent,
                    entry_gate_result_sha256, evidence_set_sha256,
                    request_json, request_sha256, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DISABLED',
                          ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.replay_id, value.workflow_id, value.project_id,
                    value.project_revision, value.project_generation,
                    value.run_generation, value.run_generation_creation_receipt_sha256,
                    value.operation_kind, value.predecessor_replay_id,
                    value.predecessor_terminal_receipt_sha256, value.replay_mode,
                    value.requested_resume_target, value.source_commit,
                    value.source_tree, value.source_parent,
                    value.entry_gate_result_sha256, value.evidence_set_sha256,
                    canonical_bytes(value.as_dict()).decode("utf-8"),
                    value.request_sha256, value.occurred_at,
                ),
            )
            self._fault("after_replay")
            start_consumption_receipt_sha256 = (
                self._consume_start_authorization(
                    connection,
                    value,
                    evaluation,
                    consumed_at=first_now,
                )
            )
            self._fault("after_start_authorization_consumption")
            gate_consumption_receipt_sha256 = self._consume_entry_gate(
                connection,
                value,
                evaluation,
                consumed_at=first_now,
            )
            self._fault("after_gate_consumption")
            self._insert_typed_receipts(connection, value, evaluation)
            self._fault("after_typed_receipts")
            event_specs = (
                ("ENTRY_READY", "READY", {
                    "entry_gate_result_sha256": value.entry_gate_result_sha256,
                    "entry_state_receipt_sha256": evaluation[
                        "entry_state_receipt_sha256"
                    ],
                    "authorization_receipt_sha256": evaluation["authorization_receipt_sha256"],
                    "start_consumption_receipt_sha256": (
                        start_consumption_receipt_sha256
                    ),
                    "evidence_attestation_sha256": evaluation[
                        "evidence_attestation_sha256"
                    ],
                    "gate_consumption_receipt_sha256": (
                        gate_consumption_receipt_sha256
                    ),
                }),
                ("PACKET_REBUILT", "PACKET_REBUILT", {
                    "packet_sha256": evaluation["packet_sha256"],
                }),
                ("ROLES_COLLECTED", "ROLES_COLLECTED", {
                    "roles_sha256": evaluation["roles_sha256"],
                    "replay_mode": value.replay_mode,
                }),
                ("VERDICT_COMPUTED", "VERDICT_COMPUTED", {
                    "verdict_sha256": evaluation["verdict_sha256"],
                    "effective_verdict": evaluation["effective_verdict"],
                }),
                ("SNAPSHOT_CAPTURED", "SNAPSHOT_CAPTURED", {
                    "snapshot_sha256": evaluation["snapshot_sha256"],
                }),
                ("TERMINAL", "COMPLETED", {
                    "runtime_safety_sha256": evaluation["runtime_safety_sha256"],
                    "acceptance_sha256": evaluation["acceptance_sha256"],
                    "typed_receipt_set_sha256": evaluation[
                        "typed_receipt_set_sha256"
                    ],
                    "terminal_reason": evaluation["terminal_reason"],
                    "delivery_capability": DELIVERY_DISABLED,
                }),
            )
            predecessor: str | None = None
            final_event_sha256 = ""
            for sequence, (kind, state, evidence) in enumerate(event_specs, start=1):
                event_body, event_sha256 = _event(
                    value, sequence, kind, state, predecessor, evidence
                )
                connection.execute(
                    "INSERT INTO authority_production_phase9_replay_events "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        value.replay_id, sequence, kind, state, predecessor,
                        canonical_bytes(event_body).decode("utf-8"), event_sha256,
                        value.occurred_at,
                    ),
                )
                predecessor = event_sha256
                final_event_sha256 = event_sha256
                self._fault(f"after_event_{sequence}")
            self._fault("before_terminal_revalidation")
            preterminal_now = self._revalidate_live_inputs(
                connection,
                value,
                baseline=baseline,
                minimum_trusted_now=first_now,
            )
            self._fault("after_live_gate_preterminal")
            receipt_body = {
                "schema": PHASE9_TERMINAL_RECEIPT_SCHEMA,
                "replay_id": value.replay_id,
                "workflow_id": value.workflow_id,
                "run_generation": value.run_generation,
                "request_sha256": value.request_sha256,
                "evidence_set_sha256": value.evidence_set_sha256,
                "source_inventory_sha256": value.source_inventory_sha256,
                "entry_gate_result_sha256": value.entry_gate_result_sha256,
                "entry_state_receipt_sha256": evaluation[
                    "entry_state_receipt_sha256"
                ],
                "start_authorization_receipt_sha256": evaluation[
                    "authorization_receipt_sha256"
                ],
                "start_authorization_consumption_receipt_sha256": (
                    start_consumption_receipt_sha256
                ),
                "evidence_attestation_sha256": evaluation[
                    "evidence_attestation_sha256"
                ],
                "gate_consumption_receipt_sha256": (
                    gate_consumption_receipt_sha256
                ),
                "typed_receipt_set_sha256": evaluation[
                    "typed_receipt_set_sha256"
                ],
                "packet_sha256": evaluation["packet_sha256"],
                "roles_sha256": evaluation["roles_sha256"],
                "verdict_sha256": evaluation["verdict_sha256"],
                "snapshot_sha256": evaluation["snapshot_sha256"],
                "runtime_safety_sha256": evaluation["runtime_safety_sha256"],
                "acceptance_sha256": evaluation["acceptance_sha256"],
                "terminal_reason": evaluation["terminal_reason"],
                "requested_resume_target": value.requested_resume_target,
                "effective_verdict": evaluation["effective_verdict"],
                "exit_code": evaluation["exit_code"],
                "delivery_capability": DELIVERY_DISABLED,
                "final_event_sha256": final_event_sha256,
                "occurred_at": value.occurred_at,
            }
            receipt_sha256 = canonical_sha256(receipt_body)
            receipt_id = f"phase9-terminal:{receipt_sha256[:32]}"
            connection.execute(
                """
                INSERT INTO authority_production_phase9_terminal_receipts(
                    receipt_id, replay_id, workflow_id, run_generation,
                    terminal_reason, exit_code, effective_verdict,
                    final_event_sha256, receipt_json, receipt_sha256, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id, value.replay_id, value.workflow_id,
                    value.run_generation, evaluation["terminal_reason"],
                    evaluation["exit_code"], evaluation["effective_verdict"],
                    final_event_sha256, canonical_bytes(receipt_body).decode("utf-8"),
                    receipt_sha256, value.occurred_at,
                ),
            )
            connection.execute(
                "INSERT INTO authority_production_phase9_replay_idempotency "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    value.workflow_id, value.idempotency_key, value.request_sha256,
                    value.replay_id, receipt_sha256,
                ),
            )
            self._fault("after_receipt")
            if value.operation_kind == CREATE:
                connection.execute(
                    "INSERT INTO authority_production_phase9_replay_current "
                    "VALUES (?, ?, ?, ?, ?, 'COMPLETED', ?)",
                    (
                        value.workflow_id, value.replay_id, value.run_generation,
                        receipt_sha256, final_event_sha256, value.occurred_at,
                    ),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE authority_production_phase9_replay_current
                    SET replay_id=?, run_generation=?, terminal_receipt_sha256=?,
                        final_event_sha256=?, state='COMPLETED', updated_at=?
                    WHERE workflow_id=? AND replay_id=?
                      AND terminal_receipt_sha256=?
                    """,
                    (
                        value.replay_id, value.run_generation, receipt_sha256,
                        final_event_sha256, value.occurred_at, value.workflow_id,
                        value.predecessor_replay_id,
                        value.predecessor_terminal_receipt_sha256,
                    ),
                )
                if updated.rowcount != 1:
                    raise Phase9ForensicReplayConflict("Phase9 current pointer CAS is stale")
            self._fault("after_current_pointer")
            self._revalidate_live_inputs(
                connection,
                value,
                baseline=baseline,
                minimum_trusted_now=preterminal_now,
            )
            completed = validate_current_phase9_completed_replay_in_transaction(
                connection,
                workflow_id=value.workflow_id,
                expected_run_generation=value.run_generation,
                expected_terminal_receipt_sha256=receipt_sha256,
            )
            if completed.get("replay_id") != value.replay_id:
                raise Phase9ForensicReplayConflict(
                    "precommit replay graph is not the exact current terminal"
                )
            self._fault("before_commit")
            connection.commit()
            receipt = connection.execute(
                "SELECT * FROM authority_production_phase9_terminal_receipts "
                "WHERE receipt_sha256=?", (receipt_sha256,),
            ).fetchone()
            assert receipt is not None
            return _result_from_receipt(value, receipt, replayed=False)
        except AuthorityStateLeaseError as exc:
            if connection is not None:
                connection.rollback()
            raise Phase9ForensicReplayConflict(
                "Authority state snapshot cannot be read safely"
            ) from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
            commit_lease.__exit__(*sys.exc_info())


def _request_file_reference_matches(
    value: object,
    request: Phase9ForensicReplayRequestV1,
    *,
    path: str,
    semantic: bool,
) -> dict[str, object]:
    keys = _FILE_REFERENCE_KEYS if semantic else _RAW_FILE_REFERENCE_KEYS
    reference = _mapping(value, path, keys)
    logical_path = _text(reference["logical_path"], f"{path}.logical_path")
    descriptor = {
        item.logical_path: item for item in request.evidence_files
    }.get(logical_path)
    if (
        descriptor is None
        or descriptor.byte_length != _integer(
            reference["byte_length"], f"{path}.byte_length", minimum=1
        )
        or descriptor.raw_bytes_sha256
        != _sha(reference["raw_bytes_sha256"], f"{path}.raw_bytes_sha256")
    ):
        raise Phase9ForensicReplayConflict(f"{path} request inventory binding differs")
    if semantic:
        _sha(reference["receipt_sha256"], f"{path}.receipt_sha256")
    return reference


def _stored_typed_receipt_set_sha256(
    rows: list[sqlite3.Row],
    request: Phase9ForensicReplayRequestV1,
    *,
    entry_state_receipt_sha256: str,
) -> str:
    expected: dict[str, set[str]] = {
        "ROLE_PROCESS": set() if request.replay_mode == ABLATE_NO_JUDGE else {
            "execution", "math", "paper"
        },
        "ROLE_PROVIDER": set() if request.replay_mode == ABLATE_NO_JUDGE else {
            "execution", "math", "paper"
        },
        "PROCESS_SCOPE": {"failed", "kill", "pause"},
        "ACCEPTANCE_CASE": set(PHASE9_ACCEPTANCE_CASES),
        "PACKET": {"packet"},
        "OUTBOX": {"outbox"},
        "SNAPSHOT": {"snapshot"},
        "VERDICT": {"verdict"},
    }
    actual: dict[str, set[str]] = {kind: set() for kind in expected}
    stored: dict[tuple[str, str], tuple[sqlite3.Row, dict[str, object]]] = {}
    inventory_paths: set[str] = set()
    raw_hashes: set[str] = set()
    receipt_ids: set[str] = set()
    process_identities: set[tuple[object, object, object]] = set()
    items: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        kind = str(row["receipt_kind"])
        logical_id = str(row["logical_id"])
        if kind not in expected or logical_id in actual[kind]:
            raise Phase9ForensicReplayConflict("stored typed receipt identity differs")
        actual[kind].add(logical_id)
        if (
            row["replay_id"] != request.replay_id
            or row["workflow_id"] != request.workflow_id
            or row["run_generation"] != request.run_generation
        ):
            raise Phase9ForensicReplayConflict("stored typed receipt coordinate differs")
        logical_path = _text(
            row["logical_path"], f"stored_receipts[{index}].logical_path"
        )
        byte_length = _integer(
            row["byte_length"], f"stored_receipts[{index}].byte_length", minimum=1
        )
        raw_sha256 = _sha(
            row["raw_bytes_sha256"], f"stored_receipts[{index}].raw_bytes_sha256"
        )
        receipt_sha256 = _sha(
            row["receipt_sha256"], f"stored_receipts[{index}].receipt_sha256"
        )
        if logical_path in inventory_paths or raw_sha256 in raw_hashes:
            raise Phase9ForensicReplayConflict("stored typed receipt bytes are reused")
        inventory_paths.add(logical_path)
        raw_hashes.add(raw_sha256)
        descriptor = {
            item.logical_path: item for item in request.evidence_files
        }.get(logical_path)
        raw = str(row["receipt_json"]).encode("utf-8")
        if (
            descriptor is None
            or descriptor.byte_length != byte_length
            or descriptor.raw_bytes_sha256 != raw_sha256
            or len(raw) != byte_length
            or hashlib.sha256(raw).hexdigest() != raw_sha256
        ):
            raise Phase9ForensicReplayConflict("stored typed receipt file binding differs")
        receipt = _strict_json(raw, f"stored_receipts[{index}]")
        receipt_keys = {
            "ROLE_PROCESS": _ROLE_PROCESS_RECEIPT_KEYS,
            "ROLE_PROVIDER": _ROLE_PROVIDER_RECEIPT_KEYS,
            "PROCESS_SCOPE": _PROCESS_SCOPE_RECEIPT_KEYS,
            "ACCEPTANCE_CASE": _ACCEPTANCE_CASE_RECEIPT_KEYS,
            "PACKET": _COMPONENT_RECEIPT_KEYS,
            "OUTBOX": _COMPONENT_RECEIPT_KEYS,
            "SNAPSHOT": _COMPONENT_RECEIPT_KEYS,
            "VERDICT": _COMPONENT_RECEIPT_KEYS,
        }[kind]
        _mapping(receipt, f"stored_receipts[{index}]", receipt_keys)
        occurred_at = _integer(
            receipt.get("occurred_at"),
            f"stored_receipts[{index}].occurred_at",
        )
        if (
            _self_hash(receipt, "receipt_sha256", f"stored_receipts[{index}]")
            != receipt_sha256
            or row["occurred_at"] != occurred_at
            or occurred_at > request.occurred_at
        ):
            raise Phase9ForensicReplayConflict("stored typed receipt hash/time differs")
        _receipt_coordinate(receipt, request, path=f"stored_receipts[{index}]")
        receipt_id = _text(
            receipt.get("receipt_id"),
            f"stored_receipts[{index}].receipt_id",
            identifier=True,
        )
        if receipt_id in receipt_ids:
            raise Phase9ForensicReplayConflict("stored typed receipt ID is reused")
        receipt_ids.add(receipt_id)
        schema = {
            "ROLE_PROCESS": PHASE9_ROLE_PROCESS_RECEIPT_SCHEMA,
            "ROLE_PROVIDER": PHASE9_ROLE_PROVIDER_RECEIPT_SCHEMA,
            "PROCESS_SCOPE": PHASE9_PROCESS_SCOPE_RECEIPT_SCHEMA,
            "ACCEPTANCE_CASE": PHASE9_ACCEPTANCE_CASE_RECEIPT_SCHEMA,
            "PACKET": PHASE9_PACKET_COMPONENT_RECEIPT_SCHEMA,
            "OUTBOX": PHASE9_OUTBOX_COMPONENT_RECEIPT_SCHEMA,
            "SNAPSHOT": PHASE9_SNAPSHOT_COMPONENT_RECEIPT_SCHEMA,
            "VERDICT": PHASE9_VERDICT_COMPONENT_RECEIPT_SCHEMA,
        }[kind]
        if receipt.get("schema") != schema:
            raise Phase9ForensicReplayConflict("stored typed receipt schema differs")
        if kind in {"ROLE_PROCESS", "ROLE_PROVIDER"}:
            if receipt.get("role") != logical_id:
                raise Phase9ForensicReplayConflict("stored role receipt identity differs")
            for field in (
                "role_generation", "invocation_id", "attempt_id", "process_scope_id",
            ):
                _text(
                    receipt.get(field),
                    f"stored_receipts[{index}].{field}",
                    identifier=True,
                )
            _sha(
                receipt.get("packet_sha256"),
                f"stored_receipts[{index}].packet_sha256",
            )
            _text(
                receipt.get("output_path"),
                f"stored_receipts[{index}].output_path",
            )
            _integer(
                receipt.get("output_byte_length"),
                f"stored_receipts[{index}].output_byte_length",
                minimum=1,
            )
            _sha(
                receipt.get("output_sha256"),
                f"stored_receipts[{index}].output_sha256",
            )
            if kind == "ROLE_PROVIDER":
                _text(
                    receipt.get("provider_call_id"),
                    f"stored_receipts[{index}].provider_call_id",
                    identifier=True,
                )
            else:
                _integer(
                    receipt.get("exit_code"),
                    f"stored_receipts[{index}].exit_code",
                )
                process_identity = tuple(
                    receipt[field]
                    for field in (
                        "invocation_id", "attempt_id", "process_scope_id",
                    )
                )
                if process_identity in process_identities:
                    raise Phase9ForensicReplayConflict(
                        "stored process identity is reused"
                    )
                process_identities.add(process_identity)
            if (
                receipt.get("inherited") is not False
                or receipt.get("predecessor_role_generation") is not None
            ):
                raise Phase9ForensicReplayConflict(
                    "stored role receipt inherits an earlier generation"
                )
        elif kind == "PROCESS_SCOPE":
            for field in (
                "invocation_id", "attempt_id", "process_scope_id", "scope_kind",
            ):
                _text(
                    receipt.get(field),
                    f"stored_receipts[{index}].{field}",
                    identifier=True,
                )
            _sha(
                receipt.get("process_identity_sha256"),
                f"stored_receipts[{index}].process_identity_sha256",
            )
            _integer(
                receipt.get("active_descendant_count"),
                f"stored_receipts[{index}].active_descendant_count",
            )
            process_identity = tuple(
                receipt[field]
                for field in ("invocation_id", "attempt_id", "process_scope_id")
            )
            if process_identity in process_identities:
                raise Phase9ForensicReplayConflict(
                    "stored process identity is reused"
                )
            process_identities.add(process_identity)
            if (
                receipt.get("action") != logical_id.upper()
                or receipt.get("result") != "PASS"
                or receipt.get("active_descendant_count") != 0
                or receipt.get("output_sha256")
                != canonical_sha256(
                    {
                        "schema": "authority-phase9-process-scope-result-v1",
                        "action": logical_id.upper(),
                        "process_identity_sha256": receipt.get(
                            "process_identity_sha256"
                        ),
                        "result": "PASS",
                        "active_descendant_count": 0,
                    }
                )
            ):
                raise Phase9ForensicReplayConflict("stored process-scope receipt differs")
            _validate_provenance(
                receipt,
                request=request,
                receipt_kind="PROCESS_SCOPE",
                logical_id=logical_id,
                component="process-scope-supervisor",
                input_sha256=str(receipt["process_identity_sha256"]),
                event_sequence=1,
                predecessor_event_id=None,
                predecessor_receipt_sha256=None,
                path=f"stored process-scope {logical_id}",
            )
        elif kind == "ACCEPTANCE_CASE":
            if (
                receipt.get("case_id") != logical_id
                or receipt.get("result") != "PASS"
            ):
                raise Phase9ForensicReplayConflict("stored acceptance receipt differs")
        else:
            _kind_path, _kind_schema, evidence_path, producer_component = (
                _COMPONENT_RECEIPTS[kind]
            )
            evidence_descriptor = {
                item.logical_path: item for item in request.evidence_files
            }.get(evidence_path)
            if evidence_descriptor is None:
                raise Phase9ForensicReplayConflict(
                    "stored component evidence is absent from the request"
                )
            runtime_counts = {
                "precommit_external_launch_count": 0,
                "pending_outbox_count": 0,
                "uncertain_automatic_resend_count": 0,
                "active_descendant_count": 0,
                "committed_reclaim_count": 0,
            }
            if kind == "PACKET":
                input_sha256 = evidence_descriptor.raw_bytes_sha256
            elif kind == "VERDICT":
                roles_descriptor = {
                    item.logical_path: item for item in request.evidence_files
                }.get("roles.json")
                if roles_descriptor is None:
                    raise Phase9ForensicReplayConflict(
                        "stored verdict lacks its role input descriptor"
                    )
                input_sha256 = roles_descriptor.raw_bytes_sha256
            elif kind == "OUTBOX":
                input_sha256 = canonical_sha256(
                    {
                        "schema": "authority-phase9-outbox-live-input-v1",
                        "entry_state_receipt_sha256": (
                            entry_state_receipt_sha256
                        ),
                        "runtime_counts": runtime_counts,
                    }
                )
            else:
                input_sha256 = canonical_sha256(
                    {
                        "schema": "authority-phase9-snapshot-live-input-v1",
                        "project_id": request.project_id,
                        "project_revision": request.project_revision,
                        "project_generation": request.project_generation,
                        "workflow_id": request.workflow_id,
                        "run_generation": request.run_generation,
                        "entry_state_receipt_sha256": (
                            entry_state_receipt_sha256
                        ),
                    }
                )
            dependency, _event_id = _validate_provenance(
                receipt,
                request=request,
                receipt_kind=kind,
                logical_id=logical_id,
                component=producer_component,
                input_sha256=input_sha256,
                event_sequence=1,
                predecessor_event_id=None,
                predecessor_receipt_sha256=None,
                path=f"stored {kind.lower()} component",
            )
            expected_source = _component_authority_source_sha256(
                kind,
                request,
                entry_state_receipt_sha256=entry_state_receipt_sha256,
                input_sha256=input_sha256,
                output_sha256=evidence_descriptor.raw_bytes_sha256,
            )
            if (
                receipt.get("execution_domain") != "FORMAL_PHASE9_A"
                or receipt.get("receipt_id")
                != f"phase9-{kind.lower()}-component:{request.run_generation}"
                or receipt.get("component") != kind
                or receipt.get("evidence_logical_path") != evidence_path
                or receipt.get("evidence_sha256")
                != evidence_descriptor.raw_bytes_sha256
                or receipt.get("output_sha256")
                != evidence_descriptor.raw_bytes_sha256
                or receipt.get("input_sha256") != input_sha256
                or receipt.get("dependency_fingerprint_sha256") != dependency
                or receipt.get("authority_source_sha256") != expected_source
            ):
                raise Phase9ForensicReplayConflict(
                    "stored component receipt semantics differ"
                )
        stored[(kind, logical_id)] = (row, receipt)
        items.append(
            {
                "receipt_kind": kind,
                "logical_id": logical_id,
                "logical_path": logical_path,
                "byte_length": byte_length,
                "raw_bytes_sha256": raw_sha256,
                "receipt_sha256": receipt_sha256,
            }
        )
    if actual != expected:
        raise Phase9ForensicReplayConflict("stored typed receipt inventory differs")

    for role in expected["ROLE_PROCESS"]:
        process = stored[("ROLE_PROCESS", role)][1]
        provider_row, provider = stored[("ROLE_PROVIDER", role)]
        provider_reference = _request_file_reference_matches(
            process.get("provider_receipt"),
            request,
            path=f"stored role {role} provider_receipt",
            semantic=True,
        )
        if any(
            provider_reference[field] != provider_row[column]
            for field, column in (
                ("logical_path", "logical_path"),
                ("byte_length", "byte_length"),
                ("raw_bytes_sha256", "raw_bytes_sha256"),
                ("receipt_sha256", "receipt_sha256"),
            )
        ):
            raise Phase9ForensicReplayConflict("stored provider receipt reference differs")
        for field in (
            "role", "role_generation", "invocation_id", "attempt_id",
            "process_scope_id", "packet_sha256", "output_path",
            "output_byte_length", "output_sha256",
        ):
            if process.get(field) != provider.get(field):
                raise Phase9ForensicReplayConflict(
                    f"stored role process/provider {field} differs"
                )
        if (
            process.get("process_kind") != "ROLE"
            or process.get("process_status") != "COMPLETED"
            or process.get("exit_code") != 0
            or provider.get("provider_status") != "SUCCEEDED"
            or provider.get("occurred_at") > process.get("occurred_at")
        ):
            raise Phase9ForensicReplayConflict("stored role execution status differs")
        provider_dependency, provider_event_id = _validate_provenance(
            provider,
            request=request,
            receipt_kind="ROLE_PROVIDER",
            dependency_kind="ROLE",
            logical_id=role,
            component="provider-runtime",
            input_sha256=str(provider["packet_sha256"]),
            event_sequence=1,
            predecessor_event_id=None,
            predecessor_receipt_sha256=None,
            path=f"stored role {role} provider",
        )
        expected_role_generation = _role_generation_id(
            request,
            role=role,
            dependency_fingerprint_sha256=provider_dependency,
        )
        if (
            provider.get("role_generation") != expected_role_generation
            or process.get("role_generation") != expected_role_generation
        ):
            raise Phase9ForensicReplayConflict(
                "stored role generation is not derived from current replay"
            )
        process_dependency, _process_event_id = _validate_provenance(
            process,
            request=request,
            receipt_kind="ROLE_PROCESS",
            dependency_kind="ROLE",
            logical_id=role,
            component="role-process-supervisor",
            input_sha256=str(process["packet_sha256"]),
            event_sequence=2,
            predecessor_event_id=provider_event_id,
            predecessor_receipt_sha256=str(provider_row["receipt_sha256"]),
            path=f"stored role {role} process",
        )
        if process_dependency != provider_dependency:
            raise Phase9ForensicReplayConflict(
                "stored role dependency fingerprint differs"
            )

    for case_id in expected["ACCEPTANCE_CASE"]:
        receipt = stored[("ACCEPTANCE_CASE", case_id)][1]
        command_reference = _request_file_reference_matches(
            receipt.get("command_record"), request,
            path=f"stored acceptance {case_id} command_record", semantic=True,
        )
        raw_reference = _request_file_reference_matches(
            receipt.get("raw_log"), request,
            path=f"stored acceptance {case_id} raw_log", semantic=False,
        )
        result_reference = _request_file_reference_matches(
            receipt.get("test_result"), request,
            path=f"stored acceptance {case_id} test_result", semantic=True,
        )
        raw_sha256 = str(raw_reference["raw_bytes_sha256"])
        command_input_sha256 = _acceptance_command_input_sha256(
            request, case_id=case_id
        )
        dependency = _dependency_fingerprint_sha256(
            request,
            receipt_kind="ACCEPTANCE_CASE",
            logical_id=case_id,
            input_sha256=command_input_sha256,
        )
        result_event_id = _evidence_event_id(
            receipt_kind="ACCEPTANCE_RESULT",
            logical_id=case_id,
            dependency_fingerprint_sha256=dependency,
        )
        receipt_dependency, _receipt_event_id = _validate_provenance(
            receipt,
            request=request,
            receipt_kind="ACCEPTANCE_CASE",
            logical_id=case_id,
            component="acceptance-case-finalizer",
            input_sha256=str(result_reference["receipt_sha256"]),
            dependency_kind="ACCEPTANCE_CASE",
            dependency_input_sha256=command_input_sha256,
            event_sequence=3,
            predecessor_event_id=result_event_id,
            predecessor_receipt_sha256=str(result_reference["receipt_sha256"]),
            path=f"stored acceptance {case_id}",
        )
        if (
            receipt_dependency != dependency
            or receipt.get("output_sha256")
            != result_reference["receipt_sha256"]
        ):
            raise Phase9ForensicReplayConflict(
                "stored acceptance provenance differs"
            )

    items.sort(key=lambda item: (str(item["receipt_kind"]), str(item["logical_id"])))
    return canonical_sha256({"schema": PHASE9_TYPED_RECEIPT_SET_SCHEMA, "receipts": items})


def _validate_stored_generation_provenance(
    connection: sqlite3.Connection,
    *,
    request: Phase9ForensicReplayRequestV1,
    generation: sqlite3.Row,
    generation_request: RunGenerationRequestV1,
) -> None:
    """Rebuild the immutable source and one-use creation authorization graph."""

    try:
        committed = Phase9RunGenerationService._replay(
            connection, generation_request
        )
    except Phase9RunGenerationError as exc:
        raise Phase9ForensicReplayConflict(
            "current completed replay run-generation graph differs"
        ) from exc
    if (
        committed is None
        or committed.run_generation != request.run_generation
        or committed.receipt_sha256
        != request.run_generation_creation_receipt_sha256
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay run-generation graph is incomplete"
        )

    consumption_body = _authorization_consumption_body(generation_request)
    consumption_sha256 = canonical_sha256(consumption_body)
    consumption_json = canonical_bytes(consumption_body).decode("utf-8")
    consumption = connection.execute(
        "SELECT * FROM "
        "authority_production_run_generation_authorization_consumptions "
        "WHERE run_generation=? AND request_sha256=?",
        (request.run_generation, generation_request.request_sha256),
    ).fetchone()
    expected_consumption = {
        "authorization_id": (
            generation_request.operator_authorization.authorization_id
        ),
        "authorization_receipt_sha256": (
            generation_request.operator_authorization.receipt_sha256
        ),
        "authorization_target_sha256": (
            generation_request.authorization_target_sha256
        ),
        "request_sha256": generation_request.request_sha256,
        "run_generation": request.run_generation,
        "workflow_id": request.workflow_id,
        "consumed_at": generation_request.occurred_at,
        "receipt_json": consumption_json,
        "receipt_sha256": consumption_sha256,
    }
    if (
        consumption is None
        or generation["authorization_id"]
        != generation_request.operator_authorization.authorization_id
        or any(
            consumption[name] != value
            for name, value in expected_consumption.items()
        )
        or _strict_json(
            str(consumption["receipt_json"]).encode("utf-8"),
            "run-generation authorization consumption",
        )
        != consumption_body
        or consumption_body.get("schema")
        != RUN_GENERATION_AUTHORIZATION_CONSUMPTION_SCHEMA
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay run-generation authorization graph differs"
        )

    inventory = connection.execute(
        "SELECT * FROM authority_production_run_generation_source_inventories "
        "WHERE inventory_sha256=?",
        (request.source_inventory_sha256,),
    ).fetchone()
    if inventory is None:
        raise Phase9ForensicReplayConflict(
            "current completed replay source inventory is unavailable"
        )
    try:
        _verify_stored_source_inventory(
            inventory,
            generation_request,
            connection,
        )
    except Phase9RunGenerationError as exc:
        raise Phase9ForensicReplayConflict(
            "current completed replay source inventory identity differs"
        ) from exc


def _validate_replay_current_chain(
    connection: sqlite3.Connection,
    *,
    request: Phase9ForensicReplayRequestV1,
    terminal_receipt_sha256: str,
    final_event_sha256: str,
) -> None:
    """Prove an immutable replay is current or on its unique successor chain."""

    current = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_current "
        "WHERE workflow_id=?",
        (request.workflow_id,),
    ).fetchone()
    if current is None:
        raise Phase9ForensicReplayConflict(
            "completed replay current pointer is missing"
        )
    current_successor_count = connection.execute(
        "SELECT COUNT(*) FROM authority_production_phase9_replays "
        "WHERE predecessor_replay_id=?",
        (current["replay_id"],),
    ).fetchone()[0]
    if current_successor_count != 0:
        raise Phase9ForensicReplayConflict(
            "completed replay current has a dangling successor"
        )
    seen: set[str] = set()
    replay_id = str(current["replay_id"])
    current_terminal_sha256 = str(current["terminal_receipt_sha256"])
    while True:
        if replay_id in seen:
            raise Phase9ForensicReplayConflict(
                "completed replay successor chain cycles"
            )
        seen.add(replay_id)
        replay = connection.execute(
            "SELECT * FROM authority_production_phase9_replays "
            "WHERE replay_id=? AND workflow_id=?",
            (replay_id, request.workflow_id),
        ).fetchone()
        terminal = connection.execute(
            "SELECT * FROM authority_production_phase9_terminal_receipts "
            "WHERE replay_id=? AND receipt_sha256=?",
            (replay_id, current_terminal_sha256),
        ).fetchone()
        if replay is None or terminal is None:
            raise Phase9ForensicReplayConflict(
                "completed replay successor chain is incomplete"
            )
        if replay_id == str(current["replay_id"]):
            if (
                current["workflow_id"] != request.workflow_id
                or current["run_generation"] != replay["run_generation"]
                or current["terminal_receipt_sha256"]
                != terminal["receipt_sha256"]
                or current["final_event_sha256"]
                != terminal["final_event_sha256"]
                or current["state"] != "COMPLETED"
                or current["updated_at"] != terminal["occurred_at"]
            ):
                raise Phase9ForensicReplayConflict(
                    "completed replay current pointer differs"
                )
        if replay_id == request.replay_id:
            if (
                current_terminal_sha256 != terminal_receipt_sha256
                or terminal["final_event_sha256"] != final_event_sha256
            ):
                raise Phase9ForensicReplayConflict(
                    "completed replay successor target differs"
                )
            return

        successor_request_body = _strict_json(
            str(replay["request_json"]).encode("utf-8"),
            "completed replay successor request",
        )
        successor_request = phase9_forensic_replay_request_from_dict(
            successor_request_body
        )
        if (
            successor_request.replay_id != replay_id
            or successor_request.operation_kind != ROTATE
            or successor_request.request_sha256 != replay["request_sha256"]
            or successor_request.predecessor_replay_id is None
            or successor_request.predecessor_terminal_receipt_sha256 is None
        ):
            raise Phase9ForensicReplayConflict(
                "completed replay successor request differs"
            )
        completed = _validate_phase9_completed_replay_in_transaction(
            connection,
            workflow_id=request.workflow_id,
            expected_run_generation=successor_request.run_generation,
            expected_terminal_receipt_sha256=current_terminal_sha256,
            expected_replay_id=replay_id,
            require_current=False,
            _validate_generation_provenance=False,
            _validate_current_chain=False,
        )
        if completed.get("replay_id") != replay_id:
            raise Phase9ForensicReplayConflict(
                "completed replay successor graph differs"
            )
        predecessor_id = successor_request.predecessor_replay_id
        branch_count = connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_replays "
            "WHERE predecessor_replay_id=?",
            (predecessor_id,),
        ).fetchone()[0]
        if branch_count != 1:
            raise Phase9ForensicReplayConflict(
                "completed replay successor chain is not unique"
            )
        replay_id = predecessor_id
        current_terminal_sha256 = (
            successor_request.predecessor_terminal_receipt_sha256
        )


def _validate_phase9_completed_replay_in_transaction(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    expected_run_generation: str,
    expected_terminal_receipt_sha256: str,
    expected_replay_id: str | None,
    require_current: bool,
    _validate_generation_provenance: bool = True,
    _validate_current_chain: bool = True,
) -> dict[str, object]:
    """Reconstruct an immutable terminal graph without owning its transaction."""

    workflow = _text(workflow_id, "workflow_id", identifier=True)
    run_generation = _text(
        expected_run_generation, "expected_run_generation", identifier=True
    )
    expected_terminal = _sha(
        expected_terminal_receipt_sha256,
        "expected_terminal_receipt_sha256",
    )
    current = None
    replay_id = None
    if require_current:
        current = connection.execute(
            "SELECT * FROM authority_production_phase9_replay_current "
            "WHERE workflow_id=?",
            (workflow,),
        ).fetchone()
        if current is None or any(
            current[name] != value
            for name, value in (
                ("workflow_id", workflow),
                ("run_generation", run_generation),
                ("terminal_receipt_sha256", expected_terminal),
                ("state", "COMPLETED"),
            )
        ):
            raise Phase9ForensicReplayConflict(
                "current completed replay pointer differs"
            )
        replay_id = str(current["replay_id"])
        if expected_replay_id is not None and replay_id != expected_replay_id:
            raise Phase9ForensicReplayConflict(
                "current completed replay identity differs"
            )
    else:
        replay_id = _text(
            expected_replay_id,
            "expected_replay_id",
            identifier=True,
        )
    replay = connection.execute(
        "SELECT * FROM authority_production_phase9_replays WHERE replay_id=?",
        (replay_id,),
    ).fetchone()
    terminal = connection.execute(
        "SELECT * FROM authority_production_phase9_terminal_receipts "
        "WHERE replay_id=? AND receipt_sha256=?",
        (replay_id, expected_terminal),
    ).fetchone()
    if replay is None or terminal is None:
        raise Phase9ForensicReplayConflict(
            "current completed replay companion rows are missing"
        )
    request_body = _strict_json(
        str(replay["request_json"]).encode("utf-8"),
        "current completed replay request",
    )
    request = phase9_forensic_replay_request_from_dict(request_body)
    if (
        canonical_sha256(request_body) != replay["request_sha256"]
        or request.request_sha256 != replay["request_sha256"]
        or request.replay_id != replay["replay_id"]
        or request.workflow_id != workflow
        or request.run_generation != run_generation
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay request identity differs"
        )
    replay_expected = {
        "workflow_id": request.workflow_id,
        "project_id": request.project_id,
        "project_revision": request.project_revision,
        "project_generation": request.project_generation,
        "run_generation": request.run_generation,
        "run_generation_creation_receipt_sha256": (
            request.run_generation_creation_receipt_sha256
        ),
        "operation_kind": request.operation_kind,
        "predecessor_replay_id": request.predecessor_replay_id,
        "predecessor_terminal_receipt_sha256": (
            request.predecessor_terminal_receipt_sha256
        ),
        "replay_mode": request.replay_mode,
        "requested_resume_target": request.requested_resume_target,
        "delivery_capability": DELIVERY_DISABLED,
        "source_commit": request.source_commit,
        "source_tree": request.source_tree,
        "source_parent": request.source_parent,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "evidence_set_sha256": request.evidence_set_sha256,
        "started_at": request.occurred_at,
    }
    if any(replay[name] != value for name, value in replay_expected.items()):
        raise Phase9ForensicReplayConflict(
            "current completed replay/request fields differ"
        )
    generation = connection.execute(
        "SELECT * FROM authority_production_run_generations "
        "WHERE workflow_id=? AND run_generation=?",
        (workflow, run_generation),
    ).fetchone()
    generation_expected = {
        "project_id": request.project_id,
        "workflow_id": workflow,
        "project_revision": request.project_revision,
        "project_generation": request.project_generation,
        "run_generation": run_generation,
        "run_mode": "FORENSIC_REPLAY",
        "modeling_consultation_contract": "LEGACY_NOT_APPLICABLE",
        "delivery_capability": DELIVERY_DISABLED,
        "source_commit": request.source_commit,
        "source_tree": request.source_tree,
        "source_parent": request.source_parent,
        "source_inventory_sha256": request.source_inventory_sha256,
    }
    if generation is None or any(
        generation[name] != value for name, value in generation_expected.items()
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay/run-generation coordinate differs"
        )
    generation_request = Phase9ForensicReplayService._generation_request(
        connection, request
    )
    if (
        generation_request.derived_run_generation != run_generation
        or generation_request.request_sha256 != generation["request_sha256"]
        or generation_request.operator_authorization.receipt_sha256
        != generation["operator_authorization_receipt_sha256"]
        or generation_request.authorization_target_sha256
        != generation["authorization_target_sha256"]
        or canonical_sha256(generation_request.contract_pins)
        != generation["contract_pin_set_sha256"]
        or generation_request.official_inputs.manifest_sha256
        != generation["official_input_manifest_sha256"]
        or generation_request.official_inputs.raw_bytes_set_sha256
        != generation["official_input_raw_bytes_set_sha256"]
        or generation_request.execution_context.receipt_sha256
        != generation["execution_context_receipt_sha256"]
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay/run-generation semantic graph differs"
        )
    if _validate_generation_provenance:
        _validate_stored_generation_provenance(
            connection,
            request=request,
            generation=generation,
            generation_request=generation_request,
        )
    if require_current:
        workflow_row = connection.execute(
            "SELECT * FROM authority_workflows WHERE workflow_id=?",
            (workflow,),
        ).fetchone()
        if workflow_row is None or any(
            workflow_row[name] != value
            for name, value in (
                ("project_id", request.project_id),
                ("project_generation", request.project_generation),
                ("run_generation", request.run_generation),
                ("current_revision", request.project_revision),
                ("current_revision_availability", "RECORDED"),
                (
                    "contract_pin_set_sha256",
                    canonical_sha256(generation_request.contract_pins),
                ),
                ("contract_pin_availability", "RECORDED"),
            )
        ):
            raise Phase9ForensicReplayConflict(
                "current completed replay workflow coordinate differs"
            )
    consumption = connection.execute(
        "SELECT * FROM authority_production_phase9_gate_consumptions "
        "WHERE replay_id=?",
        (request.replay_id,),
    ).fetchone()
    if consumption is None:
        raise Phase9ForensicReplayConflict(
            "current completed replay lacks gate consumption"
        )
    consumption_body = _strict_json(
        str(consumption["receipt_json"]).encode("utf-8"),
        "current completed replay gate consumption",
    )
    consumption_keys = {
        "schema", "gate_result_sha256", "entry_state_receipt_sha256",
        "start_authorization_id", "start_authorization_receipt_sha256",
        "workflow_id", "run_generation", "replay_id", "request_sha256",
        "consumed_at", "receipt_sha256",
    }
    _mapping(
        consumption_body,
        "current completed replay gate consumption",
        consumption_keys,
    )
    consumption_expected = {
        "gate_result_sha256": request.entry_gate_result_sha256,
        "workflow_id": workflow,
        "run_generation": run_generation,
        "replay_id": request.replay_id,
        "request_sha256": request.request_sha256,
    }
    if (
        consumption_body.get("schema") != PHASE9_GATE_CONSUMPTION_SCHEMA
        or any(
            consumption_body.get(name) != consumption[name]
            for name in consumption_keys - {"schema", "receipt_sha256"}
        )
        or any(
            consumption[name] != value
            for name, value in consumption_expected.items()
        )
        or _self_hash(
            consumption_body,
            "receipt_sha256",
            "current completed replay gate consumption",
        )
        != consumption["receipt_sha256"]
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay gate consumption differs"
        )
    typed_rows = connection.execute(
        "SELECT * FROM authority_production_phase9_evidence_receipts "
        "WHERE replay_id=? ORDER BY receipt_kind, logical_id",
        (request.replay_id,),
    ).fetchall()
    typed_set_sha256 = _stored_typed_receipt_set_sha256(
        typed_rows,
        request,
        entry_state_receipt_sha256=str(
            consumption["entry_state_receipt_sha256"]
        ),
    )

    receipt_body = _strict_json(
        str(terminal["receipt_json"]).encode("utf-8"),
        "current completed replay terminal receipt",
    )
    terminal_keys = {
        "schema", "replay_id", "workflow_id", "run_generation",
        "request_sha256", "evidence_set_sha256", "source_inventory_sha256",
        "entry_gate_result_sha256", "entry_state_receipt_sha256",
        "start_authorization_receipt_sha256",
        "start_authorization_consumption_receipt_sha256",
        "evidence_attestation_sha256",
        "gate_consumption_receipt_sha256", "typed_receipt_set_sha256",
        "packet_sha256", "roles_sha256", "verdict_sha256",
        "snapshot_sha256", "runtime_safety_sha256", "acceptance_sha256",
        "terminal_reason", "requested_resume_target", "effective_verdict",
        "exit_code", "delivery_capability", "final_event_sha256",
        "occurred_at",
    }
    _mapping(
        receipt_body,
        "current completed replay terminal receipt",
        terminal_keys,
    )
    terminal_sha256 = canonical_sha256(receipt_body)
    descriptors = {
        item.logical_path: item for item in request.evidence_files
    }
    component_paths = {
        "packet_sha256": "payload/packet.bin",
        "roles_sha256": "roles.json",
        "verdict_sha256": "verdict.json",
        "snapshot_sha256": "snapshot.json",
        "runtime_safety_sha256": "outbox_supervisor.json",
        "acceptance_sha256": "acceptance.json",
    }
    try:
        component_hashes = {
            name: descriptors[path].raw_bytes_sha256
            for name, path in component_paths.items()
        }
    except KeyError as exc:
        raise Phase9ForensicReplayConflict(
            "current completed replay request omits terminal component evidence"
        ) from exc
    start_consumption = connection.execute(
        "SELECT * FROM authority_production_phase9_start_authorization_consumptions "
        "WHERE replay_id=?",
        (request.replay_id,),
    ).fetchone()
    if start_consumption is None:
        raise Phase9ForensicReplayConflict(
            "current completed replay start consumption is unavailable"
        )
    consumed_at = _integer(
        start_consumption["consumed_at"],
        "current completed replay start consumption consumed_at",
    )
    start_consumption_body = _strict_json(
        str(start_consumption["consumption_json"]).encode(),
        "current completed replay start consumption",
    )
    start_authorization = connection.execute(
        "SELECT * FROM authority_production_phase9_start_authorizations "
        "WHERE authorization_id=?",
        (start_consumption["authorization_id"],),
    ).fetchone()
    if start_authorization is None:
        raise Phase9ForensicReplayConflict(
            "current completed replay start authorization is unavailable"
        )
    authorization_body = _strict_json(
        str(start_authorization["authorization_json"]).encode(),
        "current completed replay start authorization",
    )
    try:
        (
            authorization_sha256,
            authorization_id,
            authorization_nonce_sha256,
            evidence_attestation_sha256,
            authorization_entry_state_sha256,
            authorization_target_sha256,
        ) = _verify_start_authorization(
            authorization_body,
            request,
            trusted_now=consumed_at,
            require_current_operator=False,
        )
    except Phase9ForensicReplaySafetyError as exc:
        raise Phase9ForensicReplayConflict(
            "current completed replay start authorization is invalid"
        ) from exc
    start_descriptor = descriptors.get("start_authorization.json")
    authorization_raw = str(start_authorization["authorization_json"]).encode(
        "utf-8"
    )
    expected_authorization_row = {
        "authorization_id": authorization_id,
        "nonce_sha256": authorization_nonce_sha256,
        "authorization_target_sha256": authorization_target_sha256,
        "evidence_attestation_sha256": evidence_attestation_sha256,
        "evidence_payload_set_sha256": (
            phase9_replay_evidence_payload_set_sha256(request)
        ),
        "project_id": request.project_id,
        "workflow_id": workflow,
        "run_generation": run_generation,
        "source_commit": request.source_commit,
        "source_tree": request.source_tree,
        "source_parent": request.source_parent,
        "source_inventory_sha256": request.source_inventory_sha256,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "entry_state_receipt_sha256": authorization_entry_state_sha256,
        "start_authorization_byte_length": len(authorization_raw),
        "start_authorization_raw_bytes_sha256": hashlib.sha256(
            authorization_raw
        ).hexdigest(),
        "final_evidence_set_sha256": request.evidence_set_sha256,
        "operator_uid": authorization_body["operator_uid"],
        "operator_account": authorization_body["operator_account"],
        "issued_at": authorization_body["issued_at"],
        "expires_at": authorization_body["expires_at"],
        "authorization_json": authorization_raw.decode("utf-8"),
        "authorization_receipt_sha256": authorization_sha256,
    }
    if (
        start_descriptor is None
        or start_descriptor.byte_length != len(authorization_raw)
        or start_descriptor.raw_bytes_sha256
        != hashlib.sha256(authorization_raw).hexdigest()
        or any(
            start_authorization[name] != value
            for name, value in expected_authorization_row.items()
        )
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay start authorization differs"
        )
    expected_start_consumption_body = {
        "schema": PHASE9_REPLAY_START_CONSUMPTION_SCHEMA,
        "authorization_id": authorization_id,
        "nonce_sha256": authorization_nonce_sha256,
        "authorization_receipt_sha256": authorization_sha256,
        "authorization_target_sha256": authorization_target_sha256,
        "evidence_attestation_sha256": evidence_attestation_sha256,
        "request_sha256": request.request_sha256,
        "replay_id": request.replay_id,
        "workflow_id": workflow,
        "run_generation": run_generation,
        "consumed_at": consumed_at,
    }
    expected_start_consumption = {
        **expected_start_consumption_body,
        "consumption_receipt_sha256": canonical_sha256(
            expected_start_consumption_body
        ),
    }
    expected_start_consumption_row = {
        "authorization_id": authorization_id,
        "nonce_sha256": authorization_nonce_sha256,
        "request_sha256": request.request_sha256,
        "replay_id": request.replay_id,
        "workflow_id": workflow,
        "run_generation": run_generation,
        "consumed_at": consumed_at,
        "consumption_json": canonical_bytes(
            expected_start_consumption
        ).decode("utf-8"),
        "consumption_receipt_sha256": expected_start_consumption[
            "consumption_receipt_sha256"
        ],
    }
    if (
        start_consumption_body != expected_start_consumption
        or any(
            start_consumption[name] != value
            for name, value in expected_start_consumption_row.items()
        )
        or consumption["start_authorization_id"] != authorization_id
        or consumption["start_authorization_receipt_sha256"]
        != authorization_sha256
        or consumption["entry_state_receipt_sha256"]
        != authorization_entry_state_sha256
        or consumption["consumed_at"] != consumed_at
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay start consumption differs"
        )
    attestation = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_evidence_attestations "
        "WHERE attestation_sha256=?",
        (start_authorization["evidence_attestation_sha256"],),
    ).fetchone()
    expected_attestation = {
        "execution_domain": "FORMAL_PHASE9_A",
        "project_id": request.project_id,
        "workflow_id": workflow,
        "run_generation": run_generation,
        "replay_mode": request.replay_mode,
        "replay_coordinate_sha256": _replay_coordinate_sha256(request),
        "source_inventory_sha256": request.source_inventory_sha256,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "entry_state_receipt_sha256": consumption[
            "entry_state_receipt_sha256"
        ],
        "evidence_payload_set_sha256": (
            phase9_replay_evidence_payload_set_sha256(request)
        ),
        "typed_receipt_set_sha256": typed_set_sha256,
        "packet_sha256": component_hashes["packet_sha256"],
        "roles_sha256": component_hashes["roles_sha256"],
        "verdict_sha256": component_hashes["verdict_sha256"],
        "snapshot_sha256": component_hashes["snapshot_sha256"],
        "runtime_safety_sha256": component_hashes[
            "runtime_safety_sha256"
        ],
        "acceptance_sha256": component_hashes["acceptance_sha256"],
        "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
    }
    if attestation is None or any(
        attestation[name] != value
        for name, value in expected_attestation.items()
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay Authority attestation differs"
        )
    stored_evaluation = {
        "evidence_attestation_sha256": attestation["attestation_sha256"],
        "entry_state_receipt_sha256": consumption[
            "entry_state_receipt_sha256"
        ],
        "typed_receipts": tuple(
            ValidatedEvidenceReceiptV1(
                receipt_kind=str(item["receipt_kind"]),
                logical_id=str(item["logical_id"]),
                logical_path=str(item["logical_path"]),
                byte_length=int(item["byte_length"]),
                raw_bytes_sha256=str(item["raw_bytes_sha256"]),
                receipt_json=str(item["receipt_json"]),
                receipt_sha256=str(item["receipt_sha256"]),
                occurred_at=int(item["occurred_at"]),
            )
            for item in typed_rows
        ),
        "typed_receipt_set_sha256": typed_set_sha256,
        **component_hashes,
        "authorization_id": start_authorization["authorization_id"],
        "authorization_nonce_sha256": start_authorization["nonce_sha256"],
        "authorization_target_sha256": start_authorization[
            "authorization_target_sha256"
        ],
        "authorization_receipt_sha256": start_authorization[
            "authorization_receipt_sha256"
        ],
        "authorization_json": start_authorization["authorization_json"],
    }
    _verify_authority_replay_attestation(
        connection,
        request,
        stored_evaluation,
        trusted_now=int(start_consumption["consumed_at"]),
        evidence_root=None,
        require_current_operator=False,
    )
    attestation_body = _strict_json(
        str(attestation["attestation_json"]).encode(),
        "current completed replay Authority attestation",
    )
    if _self_hash(
        attestation_body,
        "attestation_sha256",
        "current completed replay Authority attestation",
    ) != attestation["attestation_sha256"]:
        raise Phase9ForensicReplayConflict(
            "current completed replay Authority attestation hash differs"
        )
    _validate_attested_acceptance_run(attestation)
    attested_items = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_evidence_attestation_items "
        "WHERE attestation_sha256=? ORDER BY receipt_kind, logical_id",
        (attestation["attestation_sha256"],),
    ).fetchall()
    if len(attested_items) != len(typed_rows):
        raise Phase9ForensicReplayConflict(
            "current completed replay attested item inventory differs"
        )
    runtime_hashes: list[str] = []
    for item, typed in zip(attested_items, typed_rows):
        for item_name, typed_name in (
            ("receipt_kind", "receipt_kind"),
            ("logical_id", "logical_id"),
            ("logical_path", "logical_path"),
            ("byte_length", "byte_length"),
            ("raw_bytes_sha256", "raw_bytes_sha256"),
            ("receipt_sha256", "receipt_sha256"),
        ):
            if item[item_name] != typed[typed_name]:
                raise Phase9ForensicReplayConflict(
                    "current completed replay attested item differs"
                )
        if item["source_kind"] == "RUNTIME_RECORD":
            runtime = connection.execute(
                "SELECT * FROM authority_production_phase9_replay_runtime_records "
                "WHERE record_sha256=? AND execution_domain='FORMAL_PHASE9_A'",
                (item["source_record_sha256"],),
            ).fetchone()
            typed_body = _strict_json(
                str(typed["receipt_json"]).encode("utf-8"),
                "current completed replay typed runtime receipt",
            )
            if runtime is None or runtime["authority_source_sha256"] != (
                _authority_runtime_source_sha256(
                    connection,
                    request=request,
                    receipt_kind=str(typed["receipt_kind"]),
                    logical_id=str(typed["logical_id"]),
                    logical_path=str(typed["logical_path"]),
                    raw_bytes_sha256=str(typed["raw_bytes_sha256"]),
                    byte_length=int(typed["byte_length"]),
                    receipt_sha256=str(typed["receipt_sha256"]),
                    dependency_fingerprint_sha256=str(
                        typed_body["dependency_fingerprint_sha256"]
                    ),
                    input_sha256=str(typed_body["input_sha256"]),
                    output_sha256=str(typed_body["output_sha256"]),
                    packet_sha256=typed_body.get("packet_sha256"),
                    invocation_id=str(runtime["invocation_id"]),
                    attempt_id=str(runtime["attempt_id"]),
                    process_scope_id=str(runtime["process_scope_id"]),
                )
            ):
                raise Phase9ForensicReplayConflict(
                    "current completed replay runtime source differs"
                )
            _validate_authority_runtime_completion(
                connection,
                request=request,
                runtime=runtime,
                receipt_body=typed_body,
                require_current_operator=False,
            )
            runtime_hashes.append(str(runtime["record_sha256"]))
        elif item["source_kind"] == "ACCEPTANCE_RUNNER":
            if item["source_record_sha256"] != (
                _acceptance_runner_case_source_sha256(
                    str(item["logical_id"]),
                    PHASE9_ACCEPTANCE_TEST_NODES[str(item["logical_id"])],
                    command_sha256=str(attestation["acceptance_command_sha256"]),
                    raw_log_sha256=str(attestation["acceptance_raw_log_sha256"]),
                    junit_sha256=str(attestation["acceptance_junit_sha256"]),
                    event_log_sha256=str(attestation["acceptance_event_log_sha256"]),
                    outcome_sha256=str(attestation["acceptance_outcome_sha256"]),
                )
            ):
                raise Phase9ForensicReplayConflict(
                    "current completed replay acceptance source differs"
                )
        elif item["source_kind"] == "EVIDENCE_PRODUCER":
            if (
                item["receipt_kind"] not in _COMPONENT_RECEIPTS
                or item["source_record_sha256"]
                != attestation["consumption_receipt_sha256"]
                or any(
                    item[field] is not None
                    for field in (
                        "invocation_id", "attempt_id", "process_scope_id",
                        "packet_sha256",
                    )
                )
            ):
                raise Phase9ForensicReplayConflict(
                    "current completed replay component source differs"
                )
        else:
            raise Phase9ForensicReplayConflict(
                "current completed replay item source kind differs"
            )
    if attestation["runtime_record_set_sha256"] != canonical_sha256(
        {
            "schema": "authority-phase9-runtime-record-set-v1",
            "record_sha256s": sorted(runtime_hashes),
        }
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay runtime record set differs"
        )
    terminal_expected = {
        "schema": PHASE9_TERMINAL_RECEIPT_SCHEMA,
        "replay_id": request.replay_id,
        "workflow_id": workflow,
        "run_generation": run_generation,
        "request_sha256": request.request_sha256,
        "evidence_set_sha256": request.evidence_set_sha256,
        "source_inventory_sha256": request.source_inventory_sha256,
        "entry_gate_result_sha256": request.entry_gate_result_sha256,
        "entry_state_receipt_sha256": consumption[
            "entry_state_receipt_sha256"
        ],
        "start_authorization_receipt_sha256": consumption[
            "start_authorization_receipt_sha256"
        ],
        "start_authorization_consumption_receipt_sha256": start_consumption[
            "consumption_receipt_sha256"
        ],
        "evidence_attestation_sha256": attestation["attestation_sha256"],
        "gate_consumption_receipt_sha256": consumption["receipt_sha256"],
        "typed_receipt_set_sha256": typed_set_sha256,
        **component_hashes,
        "requested_resume_target": RESUME_TARGET,
        "delivery_capability": DELIVERY_DISABLED,
        "occurred_at": request.occurred_at,
    }
    for name in (
        "packet_sha256", "roles_sha256", "verdict_sha256", "snapshot_sha256",
        "runtime_safety_sha256", "acceptance_sha256", "final_event_sha256",
    ):
        _sha(receipt_body.get(name), f"terminal.{name}")
    if (
        terminal_sha256 != expected_terminal
        or any(
            receipt_body.get(name) != value
            for name, value in terminal_expected.items()
        )
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay terminal binding differs"
        )
    if request.replay_mode == TECHNICAL:
        terminal_semantics_valid = (
            receipt_body.get("terminal_reason") == "FORENSIC_REPLAY_COMPLETED"
            and receipt_body.get("effective_verdict")
            in {"PASS", "FAIL", "INDETERMINATE"}
            and type(receipt_body.get("exit_code")) is int
            and receipt_body.get("exit_code") == 0
        )
    else:
        terminal_semantics_valid = (
            receipt_body.get("terminal_reason")
            == "PERMANENT_ABLATION_NO_DELIVERY"
            and receipt_body.get("effective_verdict") == "NOT_APPLICABLE"
            and type(receipt_body.get("exit_code")) is int
            and int(receipt_body["exit_code"]) > 0
        )
    terminal_row_expected = {
        "receipt_id": f"phase9-terminal:{terminal_sha256[:32]}",
        "replay_id": request.replay_id,
        "workflow_id": workflow,
        "run_generation": run_generation,
        "terminal_reason": receipt_body["terminal_reason"],
        "exit_code": receipt_body["exit_code"],
        "effective_verdict": receipt_body["effective_verdict"],
        "final_event_sha256": receipt_body["final_event_sha256"],
        "receipt_sha256": terminal_sha256,
        "occurred_at": request.occurred_at,
    }
    if not terminal_semantics_valid or any(
        terminal[name] != value for name, value in terminal_row_expected.items()
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay terminal semantics differ"
        )

    expected_events = (
        ("ENTRY_READY", "READY", {
            "entry_gate_result_sha256": request.entry_gate_result_sha256,
            "entry_state_receipt_sha256": consumption[
                "entry_state_receipt_sha256"
            ],
            "authorization_receipt_sha256": consumption[
                "start_authorization_receipt_sha256"
            ],
            "start_consumption_receipt_sha256": start_consumption[
                "consumption_receipt_sha256"
            ],
            "evidence_attestation_sha256": attestation[
                "attestation_sha256"
            ],
            "gate_consumption_receipt_sha256": consumption["receipt_sha256"],
        }),
        ("PACKET_REBUILT", "PACKET_REBUILT", {
            "packet_sha256": component_hashes["packet_sha256"],
        }),
        ("ROLES_COLLECTED", "ROLES_COLLECTED", {
            "roles_sha256": component_hashes["roles_sha256"],
            "replay_mode": request.replay_mode,
        }),
        ("VERDICT_COMPUTED", "VERDICT_COMPUTED", {
            "verdict_sha256": component_hashes["verdict_sha256"],
            "effective_verdict": receipt_body["effective_verdict"],
        }),
        ("SNAPSHOT_CAPTURED", "SNAPSHOT_CAPTURED", {
            "snapshot_sha256": component_hashes["snapshot_sha256"],
        }),
        ("TERMINAL", "COMPLETED", {
            "runtime_safety_sha256": component_hashes[
                "runtime_safety_sha256"
            ],
            "acceptance_sha256": component_hashes["acceptance_sha256"],
            "typed_receipt_set_sha256": typed_set_sha256,
            "terminal_reason": receipt_body["terminal_reason"],
            "delivery_capability": DELIVERY_DISABLED,
        }),
    )
    event_rows = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_events "
        "WHERE replay_id=? ORDER BY sequence",
        (request.replay_id,),
    ).fetchall()
    if len(event_rows) != len(expected_events):
        raise Phase9ForensicReplayConflict(
            "current completed replay event inventory differs"
        )
    predecessor: str | None = None
    for sequence, (row, spec) in enumerate(
        zip(event_rows, expected_events), start=1
    ):
        kind, state, evidence = spec
        expected_body, expected_sha256 = _event(
            request, sequence, kind, state, predecessor, evidence
        )
        actual_body = _strict_json(
            str(row["event_json"]).encode("utf-8"),
            f"current completed replay event {sequence}",
        )
        if (
            actual_body != expected_body
            or row["sequence"] != sequence
            or row["event_kind"] != kind
            or row["state"] != state
            or row["predecessor_event_sha256"] != predecessor
            or row["event_sha256"] != expected_sha256
            or row["occurred_at"] != request.occurred_at
        ):
            raise Phase9ForensicReplayConflict(
                "current completed replay event graph differs"
            )
        predecessor = expected_sha256
    idempotency = connection.execute(
        "SELECT * FROM authority_production_phase9_replay_idempotency "
        "WHERE workflow_id=? AND idempotency_key=?",
        (workflow, request.idempotency_key),
    ).fetchone()
    if (
        predecessor != receipt_body["final_event_sha256"]
        or idempotency is None
        or any(
            idempotency[name] != value
            for name, value in (
                ("request_sha256", request.request_sha256),
                ("replay_id", request.replay_id),
                ("terminal_receipt_sha256", terminal_sha256),
            )
        )
    ):
        raise Phase9ForensicReplayConflict(
            "current completed replay terminal graph differs"
        )
    if _validate_current_chain:
        _validate_replay_current_chain(
            connection,
            request=request,
            terminal_receipt_sha256=terminal_sha256,
            final_event_sha256=predecessor,
        )
    if require_current:
        assert current is not None
        current_expected = {
            "workflow_id": workflow,
            "replay_id": request.replay_id,
            "run_generation": run_generation,
            "terminal_receipt_sha256": terminal_sha256,
            "final_event_sha256": predecessor,
            "state": "COMPLETED",
            "updated_at": request.occurred_at,
        }
        if any(current[name] != value for name, value in current_expected.items()):
            raise Phase9ForensicReplayConflict(
                "current completed replay final pointer differs"
            )
    return {
        "workflow_id": workflow,
        "run_generation": run_generation,
        "replay_id": request.replay_id,
        "terminal_receipt_sha256": terminal_sha256,
        "final_event_sha256": predecessor,
        "request_sha256": request.request_sha256,
        "typed_receipt_set_sha256": typed_set_sha256,
    }


def validate_current_phase9_completed_replay_in_transaction(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    expected_run_generation: str,
    expected_terminal_receipt_sha256: str,
) -> dict[str, object]:
    """Reconstruct the exact current terminal graph without writing."""

    return _validate_phase9_completed_replay_in_transaction(
        connection,
        workflow_id=workflow_id,
        expected_run_generation=expected_run_generation,
        expected_terminal_receipt_sha256=expected_terminal_receipt_sha256,
        expected_replay_id=None,
        require_current=True,
    )


def collect_phase9_forensic_replay_state(
    database: str | Path,
    *,
    expected_source_fence_sha256: str,
    workflow_id: str,
) -> dict[str, object]:
    """Collect and fully reconstruct current Phase9 state in one RO snapshot."""

    path = authority_database_path(database)
    expected_fence = _sha(expected_source_fence_sha256, "expected_source_fence_sha256")
    workflow = _text(workflow_id, "workflow_id", identifier=True)
    connection = connect_authority_ro(path)
    try:
        connection.execute("BEGIN")
        verify_production_installation(connection, require_ready=True)
        if legacy_source_identity_sha256(connection) != expected_fence:
            raise Phase9ForensicReplayConflict("Authority source fence differs")
        current = connection.execute(
            "SELECT * FROM authority_production_phase9_replay_current WHERE workflow_id=?",
            (workflow,),
        ).fetchone()
        if current is None:
            body: dict[str, object] = {
                "schema": PHASE9_REPLAY_STATE_SCHEMA,
                "status": "BLOCKED",
                "workflow_id": workflow,
                "blockers": [{"code": "NO_CURRENT_REPLAY", "detail": "no terminal replay"}],
            }
        else:
            validate_current_phase9_completed_replay_in_transaction(
                connection,
                workflow_id=workflow,
                expected_run_generation=str(current["run_generation"]),
                expected_terminal_receipt_sha256=str(
                    current["terminal_receipt_sha256"]
                ),
            )
            replay = connection.execute(
                "SELECT * FROM authority_production_phase9_replays WHERE replay_id=?",
                (current["replay_id"],),
            ).fetchone()
            terminal = connection.execute(
                "SELECT * FROM authority_production_phase9_terminal_receipts "
                "WHERE replay_id=?",
                (current["replay_id"],),
            ).fetchone()
            if replay is None or terminal is None:
                raise Phase9ForensicReplayConflict("current replay companion rows are missing")
            request_body = _strict_json(
                str(replay["request_json"]).encode("utf-8"), "stored request"
            )
            request = phase9_forensic_replay_request_from_dict(request_body)
            if (
                canonical_sha256(request_body) != replay["request_sha256"]
                or request.request_sha256 != replay["request_sha256"]
                or request.replay_id != replay["replay_id"]
            ):
                raise Phase9ForensicReplayConflict("stored request identity differs")
            replay_fields = {
                "workflow_id": request.workflow_id,
                "project_id": request.project_id,
                "project_revision": request.project_revision,
                "project_generation": request.project_generation,
                "run_generation": request.run_generation,
                "run_generation_creation_receipt_sha256": (
                    request.run_generation_creation_receipt_sha256
                ),
                "operation_kind": request.operation_kind,
                "predecessor_replay_id": request.predecessor_replay_id,
                "predecessor_terminal_receipt_sha256": (
                    request.predecessor_terminal_receipt_sha256
                ),
                "replay_mode": request.replay_mode,
                "requested_resume_target": request.requested_resume_target,
                "delivery_capability": request.delivery_capability,
                "source_commit": request.source_commit,
                "source_tree": request.source_tree,
                "source_parent": request.source_parent,
                "entry_gate_result_sha256": request.entry_gate_result_sha256,
                "evidence_set_sha256": request.evidence_set_sha256,
                "started_at": request.occurred_at,
            }
            if any(replay[name] != value for name, value in replay_fields.items()):
                raise Phase9ForensicReplayConflict("stored replay/request fields differ")

            generation = connection.execute(
                """
                SELECT g.*, c.creation_receipt_sha256
                FROM authority_production_run_generations g
                JOIN authority_production_run_generation_current c
                  ON c.workflow_id=g.workflow_id
                 AND c.run_generation=g.run_generation
                WHERE g.workflow_id=? AND g.run_generation=?
                """,
                (request.workflow_id, request.run_generation),
            ).fetchone()
            generation_expected = {
                "project_id": request.project_id,
                "workflow_id": request.workflow_id,
                "project_revision": request.project_revision,
                "project_generation": request.project_generation,
                "run_generation": request.run_generation,
                "creation_receipt_sha256": (
                    request.run_generation_creation_receipt_sha256
                ),
                "run_mode": "FORENSIC_REPLAY",
                "modeling_consultation_contract": "LEGACY_NOT_APPLICABLE",
                "delivery_capability": DELIVERY_DISABLED,
                "source_commit": request.source_commit,
                "source_tree": request.source_tree,
                "source_parent": request.source_parent,
                "source_inventory_sha256": request.source_inventory_sha256,
            }
            if generation is None or any(
                generation[name] != value
                for name, value in generation_expected.items()
            ):
                raise Phase9ForensicReplayConflict(
                    "stored replay/run-generation coordinate differs"
                )

            consumption = connection.execute(
                "SELECT * FROM authority_production_phase9_gate_consumptions "
                "WHERE replay_id=?",
                (request.replay_id,),
            ).fetchone()
            if consumption is None:
                raise Phase9ForensicReplayConflict("stored replay lacks gate consumption")
            consumption_body = _strict_json(
                str(consumption["receipt_json"]).encode("utf-8"),
                "stored gate consumption",
            )
            consumption_keys = {
                "schema", "gate_result_sha256", "entry_state_receipt_sha256",
                "start_authorization_id", "start_authorization_receipt_sha256",
                "workflow_id", "run_generation", "replay_id", "request_sha256",
                "consumed_at", "receipt_sha256",
            }
            _mapping(consumption_body, "stored gate consumption", consumption_keys)
            if consumption_body.get("schema") != PHASE9_GATE_CONSUMPTION_SCHEMA:
                raise Phase9ForensicReplayConflict("gate consumption schema differs")
            for name in consumption_keys - {"schema", "receipt_sha256"}:
                if consumption_body.get(name) != consumption[name]:
                    raise Phase9ForensicReplayConflict(
                        f"stored gate consumption {name} differs"
                    )
            if (
                consumption["gate_result_sha256"] != request.entry_gate_result_sha256
                or consumption["workflow_id"] != request.workflow_id
                or consumption["run_generation"] != request.run_generation
                or consumption["request_sha256"] != request.request_sha256
                or _self_hash(
                    consumption_body, "receipt_sha256", "stored gate consumption"
                ) != consumption["receipt_sha256"]
            ):
                raise Phase9ForensicReplayConflict("stored gate consumption binding differs")

            live_entry_state = collect_phase9_entry_state_in_transaction(
                connection,
                expected_source_fence_sha256=expected_fence,
                workflow_id=request.workflow_id,
                candidate=CandidateIdentity(
                    request.source_commit,
                    request.source_tree,
                    request.source_parent,
                ),
            )
            if (
                live_entry_state.state_receipt_sha256
                != consumption["entry_state_receipt_sha256"]
                or live_entry_state.source_inventory_sha256
                != request.source_inventory_sha256
                or live_entry_state.active_process_count != 0
                or live_entry_state.pending_outbox_count != 0
                or live_entry_state.unresolved_migration_count != 0
            ):
                raise Phase9ForensicReplayConflict(
                    "completed replay live entry state is no longer the consumed READY state"
                )

            typed_rows = connection.execute(
                "SELECT * FROM authority_production_phase9_evidence_receipts "
                "WHERE replay_id=? ORDER BY receipt_kind, logical_id",
                (request.replay_id,),
            ).fetchall()
            typed_set_sha256 = _stored_typed_receipt_set_sha256(
                typed_rows,
                request,
                entry_state_receipt_sha256=str(
                    consumption["entry_state_receipt_sha256"]
                ),
            )
            start_consumption = connection.execute(
                "SELECT * FROM authority_production_phase9_start_authorization_consumptions "
                "WHERE replay_id=?",
                (request.replay_id,),
            ).fetchone()
            if start_consumption is None:
                raise Phase9ForensicReplayConflict(
                    "stored replay lacks start authorization consumption"
                )
            start_authorization = connection.execute(
                "SELECT * FROM authority_production_phase9_start_authorizations "
                "WHERE authorization_id=?",
                (start_consumption["authorization_id"],),
            ).fetchone()
            if start_authorization is None:
                raise Phase9ForensicReplayConflict(
                    "stored replay lacks start authorization"
                )
            evidence_attestation_sha256 = start_authorization[
                "evidence_attestation_sha256"
            ]

            receipt_body = _strict_json(
                str(terminal["receipt_json"]).encode("utf-8"), "stored terminal receipt"
            )
            terminal_keys = {
                "schema", "replay_id", "workflow_id", "run_generation",
                "request_sha256", "evidence_set_sha256", "source_inventory_sha256",
                "entry_gate_result_sha256", "entry_state_receipt_sha256",
                "start_authorization_receipt_sha256",
                "start_authorization_consumption_receipt_sha256",
                "evidence_attestation_sha256",
                "gate_consumption_receipt_sha256", "typed_receipt_set_sha256",
                "packet_sha256", "roles_sha256", "verdict_sha256",
                "snapshot_sha256", "runtime_safety_sha256", "acceptance_sha256",
                "terminal_reason", "requested_resume_target", "effective_verdict",
                "exit_code", "delivery_capability", "final_event_sha256",
                "occurred_at",
            }
            _mapping(receipt_body, "stored terminal receipt", terminal_keys)
            if receipt_body.get("schema") != PHASE9_TERMINAL_RECEIPT_SCHEMA:
                raise Phase9ForensicReplayConflict("stored terminal receipt schema differs")
            terminal_sha256 = canonical_sha256(receipt_body)
            terminal_expected = {
                "replay_id": request.replay_id,
                "workflow_id": request.workflow_id,
                "run_generation": request.run_generation,
                "request_sha256": request.request_sha256,
                "evidence_set_sha256": request.evidence_set_sha256,
                "source_inventory_sha256": request.source_inventory_sha256,
                "entry_gate_result_sha256": request.entry_gate_result_sha256,
                "entry_state_receipt_sha256": consumption["entry_state_receipt_sha256"],
                "start_authorization_receipt_sha256": consumption[
                    "start_authorization_receipt_sha256"
                ],
                "start_authorization_consumption_receipt_sha256": (
                    start_consumption["consumption_receipt_sha256"]
                ),
                "evidence_attestation_sha256": evidence_attestation_sha256,
                "gate_consumption_receipt_sha256": consumption["receipt_sha256"],
                "typed_receipt_set_sha256": typed_set_sha256,
                "requested_resume_target": request.requested_resume_target,
                "delivery_capability": DELIVERY_DISABLED,
                "occurred_at": request.occurred_at,
            }
            if any(receipt_body.get(name) != value for name, value in terminal_expected.items()):
                raise Phase9ForensicReplayConflict("stored terminal receipt binding differs")
            if request.replay_mode == TECHNICAL:
                if (
                    receipt_body.get("terminal_reason")
                    != "FORENSIC_REPLAY_COMPLETED"
                    or receipt_body.get("effective_verdict")
                    not in {"PASS", "FAIL", "INDETERMINATE"}
                    or type(receipt_body.get("exit_code")) is not int
                    or receipt_body.get("exit_code") != 0
                ):
                    raise Phase9ForensicReplayConflict(
                        "stored technical terminal semantics differ"
                    )
            elif (
                receipt_body.get("terminal_reason")
                != "PERMANENT_ABLATION_NO_DELIVERY"
                or receipt_body.get("effective_verdict") != "NOT_APPLICABLE"
                or type(receipt_body.get("exit_code")) is not int
                or int(receipt_body["exit_code"]) <= 0
            ):
                raise Phase9ForensicReplayConflict(
                    "stored ablation terminal semantics differ"
                )
            terminal_row_fields = {
                "receipt_id": f"phase9-terminal:{terminal_sha256[:32]}",
                "replay_id": request.replay_id,
                "workflow_id": request.workflow_id,
                "run_generation": request.run_generation,
                "terminal_reason": receipt_body["terminal_reason"],
                "exit_code": receipt_body["exit_code"],
                "effective_verdict": receipt_body["effective_verdict"],
                "final_event_sha256": receipt_body["final_event_sha256"],
                "receipt_sha256": terminal_sha256,
                "occurred_at": request.occurred_at,
            }
            if any(terminal[name] != value for name, value in terminal_row_fields.items()):
                raise Phase9ForensicReplayConflict("stored terminal receipt row differs")

            expected_events = (
                ("ENTRY_READY", "READY", {
                    "entry_gate_result_sha256": request.entry_gate_result_sha256,
                    "entry_state_receipt_sha256": consumption["entry_state_receipt_sha256"],
                    "authorization_receipt_sha256": consumption[
                        "start_authorization_receipt_sha256"
                    ],
                    "start_consumption_receipt_sha256": start_consumption[
                        "consumption_receipt_sha256"
                    ],
                    "evidence_attestation_sha256": evidence_attestation_sha256,
                    "gate_consumption_receipt_sha256": consumption["receipt_sha256"],
                }),
                ("PACKET_REBUILT", "PACKET_REBUILT", {
                    "packet_sha256": receipt_body["packet_sha256"],
                }),
                ("ROLES_COLLECTED", "ROLES_COLLECTED", {
                    "roles_sha256": receipt_body["roles_sha256"],
                    "replay_mode": request.replay_mode,
                }),
                ("VERDICT_COMPUTED", "VERDICT_COMPUTED", {
                    "verdict_sha256": receipt_body["verdict_sha256"],
                    "effective_verdict": receipt_body["effective_verdict"],
                }),
                ("SNAPSHOT_CAPTURED", "SNAPSHOT_CAPTURED", {
                    "snapshot_sha256": receipt_body["snapshot_sha256"],
                }),
                ("TERMINAL", "COMPLETED", {
                    "runtime_safety_sha256": receipt_body["runtime_safety_sha256"],
                    "acceptance_sha256": receipt_body["acceptance_sha256"],
                    "typed_receipt_set_sha256": typed_set_sha256,
                    "terminal_reason": receipt_body["terminal_reason"],
                    "delivery_capability": DELIVERY_DISABLED,
                }),
            )
            event_rows = connection.execute(
                "SELECT * FROM authority_production_phase9_replay_events "
                "WHERE replay_id=? ORDER BY sequence",
                (request.replay_id,),
            ).fetchall()
            if len(event_rows) != len(expected_events):
                raise Phase9ForensicReplayConflict("event inventory differs")
            predecessor: str | None = None
            for sequence, (row, spec) in enumerate(
                zip(event_rows, expected_events), start=1
            ):
                kind, state, evidence = spec
                expected_body, expected_sha256 = _event(
                    request, sequence, kind, state, predecessor, evidence
                )
                actual_body = _strict_json(
                    str(row["event_json"]).encode("utf-8"),
                    f"stored event {sequence}",
                )
                if (
                    actual_body != expected_body
                    or row["sequence"] != sequence
                    or row["event_kind"] != kind
                    or row["state"] != state
                    or row["predecessor_event_sha256"] != predecessor
                    or row["event_sha256"] != expected_sha256
                    or row["occurred_at"] != request.occurred_at
                ):
                    raise Phase9ForensicReplayConflict("stored typed event graph differs")
                predecessor = expected_sha256
            if (
                predecessor != receipt_body["final_event_sha256"]
                or predecessor != current["final_event_sha256"]
            ):
                raise Phase9ForensicReplayConflict("final event pointer differs")

            idempotency = connection.execute(
                "SELECT * FROM authority_production_phase9_replay_idempotency "
                "WHERE workflow_id=? AND idempotency_key=?",
                (request.workflow_id, request.idempotency_key),
            ).fetchone()
            if idempotency is None or any(
                idempotency[name] != value
                for name, value in (
                    ("request_sha256", request.request_sha256),
                    ("replay_id", request.replay_id),
                    ("terminal_receipt_sha256", terminal_sha256),
                )
            ):
                raise Phase9ForensicReplayConflict("stored idempotency binding differs")
            current_expected = {
                "workflow_id": request.workflow_id,
                "replay_id": request.replay_id,
                "run_generation": request.run_generation,
                "terminal_receipt_sha256": terminal_sha256,
                "final_event_sha256": predecessor,
                "state": "COMPLETED",
                "updated_at": request.occurred_at,
            }
            if any(current[name] != value for name, value in current_expected.items()):
                raise Phase9ForensicReplayConflict("stored current replay pointer differs")
            body = {
                "schema": PHASE9_REPLAY_STATE_SCHEMA,
                "status": "COMPLETED",
                "workflow_id": workflow,
                "replay_id": request.replay_id,
                "run_generation": request.run_generation,
                "request_sha256": request.request_sha256,
                "evidence_set_sha256": request.evidence_set_sha256,
                "source_inventory_sha256": request.source_inventory_sha256,
                "terminal_receipt_sha256": terminal_sha256,
                "terminal_reason": terminal["terminal_reason"],
                "effective_verdict": terminal["effective_verdict"],
                "exit_code": terminal["exit_code"],
                "event_count": len(event_rows),
                "typed_receipt_count": len(typed_rows),
                "typed_receipt_set_sha256": typed_set_sha256,
                "gate_consumption_receipt_sha256": consumption["receipt_sha256"],
                "delivery_capability": replay["delivery_capability"],
                "blockers": [],
            }
        body["state_receipt_sha256"] = canonical_sha256(body)
        connection.commit()
        return body
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
