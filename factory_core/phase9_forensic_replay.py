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
import time
from typing import Callable, Mapping

from .authority_production_schema import (
    authority_database_path,
    connect_authority_ro,
    connect_authority_rw,
    legacy_source_identity_sha256,
    verify_production_installation,
)
from .canonical import canonical_bytes, canonical_sha256
from .phase9_entry import PHASE9_ENTRY_GATE_SCHEMA, P0_REQUIREMENTS
from .phase9_run_generation import (
    PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS,
    read_current_git_source_identity,
)


PHASE9_REPLAY_REQUEST_SCHEMA = "authority-phase9-forensic-replay-request-v1"
PHASE9_REPLAY_RESULT_SCHEMA = "authority-phase9-forensic-replay-result-v1"
PHASE9_REPLAY_PREFLIGHT_SCHEMA = "authority-phase9-forensic-preflight-v1"
PHASE9_REPLAY_STATE_SCHEMA = "authority-phase9-forensic-state-v1"
PHASE9_START_AUTHORIZATION_SCHEMA = "authority-phase9-start-authorization-v2"

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


def phase9_forensic_replay_request_from_dict(
    value: object,
) -> Phase9ForensicReplayRequestV1:
    keys = {
        "schema_version", "idempotency_key", "operation_kind", "project_id",
        "workflow_id", "project_revision", "project_generation",
        "run_generation", "run_generation_creation_receipt_sha256",
        "predecessor_replay_id", "predecessor_terminal_receipt_sha256",
        "replay_mode", "requested_resume_target", "delivery_capability",
        "source_commit", "source_tree", "source_parent",
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
                _text(item["logical_path"], f"evidence_files[{index}].logical_path"),
                _integer(item["byte_length"], f"evidence_files[{index}].byte_length"),
                _sha(item["raw_bytes_sha256"], f"evidence_files[{index}].raw_bytes_sha256"),
            )
        )
    request = Phase9ForensicReplayRequestV1(
        _text(body["schema_version"], "request.schema_version"),
        _text(body["idempotency_key"], "request.idempotency_key", identifier=True),
        _text(body["operation_kind"], "request.operation_kind", identifier=True),
        _text(body["project_id"], "request.project_id", identifier=True),
        _text(body["workflow_id"], "request.workflow_id", identifier=True),
        _integer(body["project_revision"], "request.project_revision"),
        _text(body["project_generation"], "request.project_generation", identifier=True),
        _text(body["run_generation"], "request.run_generation", identifier=True),
        _sha(
            body["run_generation_creation_receipt_sha256"],
            "request.run_generation_creation_receipt_sha256",
        ),
        body["predecessor_replay_id"],
        body["predecessor_terminal_receipt_sha256"],
        _text(body["replay_mode"], "request.replay_mode", identifier=True),
        _text(body["requested_resume_target"], "request.requested_resume_target", identifier=True),
        _text(body["delivery_capability"], "request.delivery_capability", identifier=True),
        _git_oid(body["source_commit"], "request.source_commit"),
        _git_oid(body["source_tree"], "request.source_tree"),
        _git_oid(body["source_parent"], "request.source_parent"),
        _sha(body["entry_gate_result_sha256"], "request.entry_gate_result_sha256"),
        tuple(files), _integer(body["occurred_at"], "request.occurred_at"),
    )
    return validate_phase9_forensic_replay_request(request)


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
    paths: list[str] = []
    for index, item in enumerate(request.evidence_files):
        pure = PurePosixPath(item.logical_path)
        if (
            pure.is_absolute()
            or item.logical_path != pure.as_posix()
            or ".." in pure.parts
        ):
            raise Phase9ForensicReplaySafetyError(
                f"evidence_files[{index}] path is not normalized relative POSIX"
            )
        if item.byte_length > 16 * 1024 * 1024:
            raise Phase9ForensicReplaySafetyError("one evidence file is too large")
        paths.append(item.logical_path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise Phase9ForensicReplaySafetyError("evidence paths must be sorted and unique")
    if not _CONTROL_FILES.issubset(paths):
        raise Phase9ForensicReplaySafetyError("required control evidence files are missing")
    if sum(item.byte_length for item in request.evidence_files) > 64 * 1024 * 1024:
        raise Phase9ForensicReplaySafetyError("evidence set is too large")
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
    except OSError as exc:
        raise Phase9ForensicReplaySafetyError(f"{label} cannot be read safely") from exc
    identity = lambda item: (
        item.st_dev, item.st_ino, item.st_mode, item.st_nlink, item.st_size,
        item.st_mtime_ns,
    )
    if (
        len(raw) > maximum
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or identity(before) != identity(opened)
        or identity(opened) != identity(after)
    ):
        raise Phase9ForensicReplaySafetyError(f"{label} changed while being read")
    return raw


def _read_evidence_set(
    root_value: str | Path, request: Phase9ForensicReplayRequestV1
) -> dict[str, bytes]:
    root = Path(root_value)
    try:
        before = root.lstat()
    except OSError as exc:
        raise Phase9ForensicReplaySafetyError("evidence root is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise Phase9ForensicReplaySafetyError("evidence root must be a directory")
    root = root.resolve()
    actual: list[str] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in directory_names:
            item = parent / name
            metadata = item.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise Phase9ForensicReplaySafetyError(
                    "evidence tree contains a symlink or special directory"
                )
        for name in file_names:
            item = parent / name
            metadata = item.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise Phase9ForensicReplaySafetyError(
                    "evidence tree contains a symlink, hardlink, or special file"
                )
            actual.append(item.relative_to(root).as_posix())
    expected = [item.logical_path for item in request.evidence_files]
    if sorted(actual) != expected:
        raise Phase9ForensicReplaySafetyError("evidence inventory differs")
    values: dict[str, bytes] = {}
    for item in request.evidence_files:
        raw = _regular_file_bytes(
            root.joinpath(*PurePosixPath(item.logical_path).parts),
            maximum=item.byte_length,
            label=f"evidence {item.logical_path}",
        )
        if (
            len(raw) != item.byte_length
            or hashlib.sha256(raw).hexdigest() != item.raw_bytes_sha256
        ):
            raise Phase9ForensicReplaySafetyError(
                f"evidence bytes differ: {item.logical_path}"
            )
        values[item.logical_path] = raw
    after = root.lstat()
    if (before.st_dev, before.st_ino, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_mtime_ns
    ):
        raise Phase9ForensicReplaySafetyError("evidence root changed while read")
    return values


def _self_hash(body: Mapping[str, object], field: str, path: str) -> str:
    value = _mapping(body, path)
    recorded = _sha(value.get(field), f"{path}.{field}")
    unsigned = dict(value)
    unsigned.pop(field)
    if canonical_sha256(unsigned) != recorded:
        raise Phase9ForensicReplaySafetyError(f"{path} self-hash differs")
    return recorded


def _verify_entry_gate(
    body: dict[str, object], request: Phase9ForensicReplayRequestV1
) -> None:
    if body.get("schema") != PHASE9_ENTRY_GATE_SCHEMA:
        raise Phase9ForensicReplaySafetyError("entry gate schema is unsupported")
    if body.get("status") != "READY" or body.get("blockers") != []:
        raise Phase9ForensicReplaySafetyError("entry gate is not READY")
    digest = _self_hash(body, "gate_result_sha256", "entry_gate")
    if digest != request.entry_gate_result_sha256:
        raise Phase9ForensicReplaySafetyError("entry gate result binding differs")
    candidate = _mapping(body.get("candidate"), "entry_gate.candidate")
    if (
        candidate.get("commit") != request.source_commit
        or candidate.get("tree") != request.source_tree
        or candidate.get("parent") != request.source_parent
        or body.get("project_id") != request.project_id
        or body.get("workflow_id") != request.workflow_id
        or body.get("run_generation") != request.run_generation
        or body.get("creation_receipt_sha256")
        != request.run_generation_creation_receipt_sha256
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
    for field in (
        "state_receipt_sha256", "source_verification_sha256",
        "operator_authorization_receipt_sha256",
        "official_input_manifest_sha256", "official_input_raw_bytes_set_sha256",
        "execution_context_receipt_sha256", "p0_evidence_root_sha256",
    ):
        _sha(body.get(field), f"entry_gate.{field}")


def _verify_start_authorization(
    body: dict[str, object],
    request: Phase9ForensicReplayRequestV1,
    *,
    trusted_now: int,
) -> str:
    expected_keys = {
        "schema", "authorization_id", "authorization_mechanism", "authorized",
        "operator_uid", "operator_account", "operation", "project_id",
        "workflow_id", "run_generation", "source_commit", "issued_at",
        "expires_at", "entry_gate_result_sha256", "authorization_scope",
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
        or body.get("entry_gate_result_sha256") != request.entry_gate_result_sha256
    ):
        raise Phase9ForensicReplaySafetyError("start authorization coordinate differs")
    issued = _integer(body.get("issued_at"), "start_authorization.issued_at")
    expires = _integer(body.get("expires_at"), "start_authorization.expires_at")
    if expires < issued or not issued <= trusted_now <= expires:
        raise Phase9ForensicReplaySafetyError(
            "start authorization is not valid at trusted current time"
        )
    try:
        uid = os.geteuid()
        account = pwd.getpwuid(uid).pw_name
    except (AttributeError, KeyError) as exc:
        raise Phase9ForensicReplaySafetyError("OS account cannot be verified") from exc
    if body.get("operator_uid") != uid or body.get("operator_account") != account:
        raise Phase9ForensicReplaySafetyError("start authorization OS account differs")
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
    return digest


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
) -> dict[str, object]:
    if abs(request.occurred_at - trusted_now) > PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS:
        raise Phase9ForensicReplaySafetyError(
            "request occurrence metadata exceeds trusted clock skew"
        )
    entry = _control(values, "entry_gate.json", PHASE9_ENTRY_GATE_SCHEMA)
    _verify_entry_gate(entry, request)
    authorization = _control(
        values, "start_authorization.json", PHASE9_START_AUTHORIZATION_SCHEMA
    )
    authorization_sha = _verify_start_authorization(
        authorization, request, trusted_now=trusted_now
    )
    packet = _control(values, "packet.json", "authority-phase9-packet-evidence-v1")
    roles = _control(values, "roles.json", "authority-phase9-role-evidence-v1")
    verdict = _control(values, "verdict.json", "authority-phase9-verdict-evidence-v1")
    snapshot = _control(values, "snapshot.json", "authority-phase9-snapshot-evidence-v1")
    outbox = _control(
        values, "outbox_supervisor.json", "authority-phase9-runtime-safety-evidence-v1"
    )
    acceptance = _control(
        values, "acceptance.json", "authority-phase9-acceptance-evidence-v1"
    )

    blockers: list[dict[str, str]] = []
    required = packet.get("required_claims")
    present = packet.get("present_claims")
    if (
        type(required) is not list or type(present) is not list
        or any(type(item) is not str for item in required + present)
        or required != sorted(set(required)) or present != sorted(set(present))
    ):
        raise Phase9ForensicReplaySafetyError("packet claim inventories are malformed")
    missing = sorted(set(required) - set(present))
    dispatch_count = _integer(packet.get("dispatch_count"), "packet.dispatch_count")
    if missing:
        if dispatch_count != 0:
            blockers.append({"code": "DISPATCH_WITH_MISSING_CLAIMS", "detail": ",".join(missing)})
        blockers.append({"code": "MISSING_PACKET_CLAIMS", "detail": ",".join(missing)})
    packet_path = _text(packet.get("packet_path"), "packet.packet_path")
    packet_raw = values.get(packet_path)
    if packet_raw is None or hashlib.sha256(packet_raw).hexdigest() != packet.get("packet_sha256"):
        raise Phase9ForensicReplaySafetyError("packet raw bytes binding differs")

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
            role = _mapping(raw_role, f"roles[{index}]")
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
            if output is None or hashlib.sha256(output).hexdigest() != role.get("output_sha256"):
                raise Phase9ForensicReplaySafetyError("role output bytes binding differs")
            _sha(role.get("process_receipt_sha256"), f"roles[{index}].process_receipt_sha256")
    elif role_values:
        raise Phase9ForensicReplaySafetyError("ablation cannot contain judge role output")

    if request.replay_mode == TECHNICAL:
        layers = verdict.get("roles")
        if type(layers) is not dict or sorted(layers) != ["execution", "math", "paper"]:
            raise Phase9ForensicReplaySafetyError("verdict role layers differ")
        role_effective: list[str] = []
        for role in sorted(layers):
            layer = _mapping(layers[role], f"verdict.roles.{role}")
            values_ = [layer.get(name) for name in ("raw", "protocol", "grounding")]
            if any(value not in {"PASS", "FAIL", "INDETERMINATE"} for value in values_):
                raise Phase9ForensicReplaySafetyError("verdict layer is unsupported")
            computed = _effective(values_)
            if layer.get("effective") != computed:
                raise Phase9ForensicReplaySafetyError("contradictory effective role verdict")
            role_effective.append(computed)
        effective = _effective(role_effective)
        if verdict.get("effective_verdict") != effective:
            raise Phase9ForensicReplaySafetyError("contradictory aggregate verdict")
        terminal_reason = "FORENSIC_REPLAY_COMPLETED"
        exit_code = 0
    else:
        if verdict.get("roles") != {} or verdict.get("effective_verdict") != "NOT_APPLICABLE":
            raise Phase9ForensicReplaySafetyError("ablation verdict must be NOT_APPLICABLE")
        effective = "NOT_APPLICABLE"
        terminal_reason = "PERMANENT_ABLATION_NO_DELIVERY"
        exit_code = _integer(verdict.get("exit_code"), "verdict.exit_code", minimum=1)

    coordinate = _mapping(snapshot.get("coordinate"), "snapshot.coordinate")
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
    for index, raw_section in enumerate(sections):
        section = _mapping(raw_section, f"snapshot.sections[{index}]")
        if section.get("coordinate") != expected_coordinate:
            raise Phase9ForensicReplaySafetyError("snapshot section coordinate differs")
        if section.get("read_status") not in {"AVAILABLE", "GAP", "ERROR"}:
            raise Phase9ForensicReplaySafetyError("snapshot read status is unsupported")
        if section.get("read_failed") is True and section.get("read_status") != "ERROR":
            raise Phase9ForensicReplaySafetyError("snapshot read failure must be ERROR")

    expected_runtime = {
        "precommit_external_launch_count": 0,
        "pending_outbox_count": 0,
        "uncertain_automatic_resend_count": 0,
        "active_descendant_count": 0,
    }
    for key, expected_value in expected_runtime.items():
        if outbox.get(key) != expected_value:
            blockers.append({"code": key.upper(), "detail": f"expected {expected_value}"})
    if outbox.get("committed_reclaim_count") not in {0, 1}:
        blockers.append({"code": "RECLAIM_COUNT", "detail": "must be zero or one"})
    receipts = outbox.get("process_scope_receipts")
    if type(receipts) is not dict or sorted(receipts) != ["failed", "kill", "pause"]:
        raise Phase9ForensicReplaySafetyError("process-scope receipts differ")
    for name, digest in receipts.items():
        _sha(digest, f"process_scope_receipts.{name}")

    cases = acceptance.get("cases")
    if type(cases) is not list:
        raise Phase9ForensicReplaySafetyError("acceptance cases are missing")
    case_ids: list[str] = []
    for index, raw_case in enumerate(cases):
        case = _mapping(raw_case, f"acceptance.cases[{index}]")
        case_id = _text(case.get("case_id"), f"acceptance.cases[{index}].case_id", identifier=True)
        case_ids.append(case_id)
        if case.get("result") != "PASS":
            blockers.append({"code": "ACCEPTANCE_CASE_NONPASS", "detail": case_id})
        _sha(case.get("receipt_sha256"), f"acceptance.cases[{index}].receipt_sha256")
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
    if delivery != required_delivery:
        blockers.append({"code": "DELIVERY_FENCE", "detail": "delivery evidence differs"})
    terminal = _mapping(acceptance.get("terminal"), "acceptance.terminal")
    if terminal != {
        "terminal_reason": terminal_reason,
        "requested_resume_target": RESUME_TARGET,
        "effective_verdict": effective,
        "exit_code": exit_code,
    }:
        raise Phase9ForensicReplaySafetyError("terminal evidence differs")
    blockers.sort(key=lambda item: (item["code"], item["detail"]))
    return {
        "blockers": blockers,
        "authorization_receipt_sha256": authorization_sha,
        "packet_sha256": packet["packet_sha256"],
        "roles_sha256": hashlib.sha256(values["roles.json"]).hexdigest(),
        "verdict_sha256": hashlib.sha256(values["verdict.json"]).hexdigest(),
        "snapshot_sha256": hashlib.sha256(values["snapshot.json"]).hexdigest(),
        "runtime_safety_sha256": hashlib.sha256(values["outbox_supervisor.json"]).hexdigest(),
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
        evidence = _read_evidence_set(evidence_root, value)
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


class Phase9ForensicReplayService:
    """Finalize one already-authorized local replay in one transaction."""

    def __init__(
        self,
        database: str | Path,
        *,
        expected_source_fence_sha256: str,
        source_repository: str | Path,
        evidence_root: str | Path,
        fault_hook: Callable[[str], None] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.path = authority_database_path(database)
        self.expected_source_fence_sha256 = _sha(
            expected_source_fence_sha256, "expected_source_fence_sha256"
        )
        self.source_repository = Path(source_repository)
        self.evidence_root = Path(evidence_root)
        if fault_hook is not None and not callable(fault_hook):
            raise Phase9ForensicReplaySafetyError("fault_hook must be callable")
        if clock is not None and not callable(clock):
            raise Phase9ForensicReplaySafetyError("clock must be callable")
        self.fault_hook = fault_hook
        self._clock = (lambda: int(time.time())) if clock is None else clock

    def _trusted_now(self) -> int:
        return _integer(self._clock(), "trusted_now")

    def _fault(self, checkpoint: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(checkpoint)

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
    def _verify_coordinate(
        connection: sqlite3.Connection, request: Phase9ForensicReplayRequestV1
    ) -> None:
        row = connection.execute(
            """
            SELECT w.project_id, w.current_revision, w.project_generation,
                   w.run_generation, c.creation_receipt_sha256
            FROM authority_workflows w
            JOIN authority_production_run_generation_current c
              ON c.workflow_id=w.workflow_id AND c.run_generation=w.run_generation
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
        row = connection.execute(
            "SELECT * FROM authority_production_phase9_replay_idempotency "
            "WHERE workflow_id=? AND idempotency_key=?",
            (request.workflow_id, request.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request.request_sha256 or row["replay_id"] != request.replay_id:
            raise Phase9ForensicReplayConflict("idempotency key has different request bytes")
        receipt = connection.execute(
            "SELECT * FROM authority_production_phase9_terminal_receipts "
            "WHERE replay_id=? AND receipt_sha256=?",
            (request.replay_id, row["terminal_receipt_sha256"]),
        ).fetchone()
        if receipt is None:
            raise Phase9ForensicReplayConflict("idempotent terminal receipt differs")
        return _result_from_receipt(request, receipt, replayed=True)

    def execute(
        self, request: Phase9ForensicReplayRequestV1
    ) -> Phase9ForensicReplayResult:
        value = validate_phase9_forensic_replay_request(request)
        first_values = _read_evidence_set(self.evidence_root, value)
        evaluation = _evaluate_evidence(
            value, first_values, trusted_now=self._trusted_now()
        )
        if evaluation["blockers"]:
            raise Phase9ForensicReplaySafetyError(
                "Phase9 replay preflight is BLOCKED: "
                + canonical_bytes(evaluation["blockers"]).decode("utf-8")
            )
        source = read_current_git_source_identity(self.source_repository)
        if (
            source.source_commit != value.source_commit
            or source.source_tree != value.source_tree
            or source.source_parent != value.source_parent
        ):
            raise Phase9ForensicReplayConflict("current source identity differs")
        connection = connect_authority_rw(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_installation(connection)
            self._control_fence(connection)
            self._verify_coordinate(connection, value)
            replay = self._replay(connection, value)
            if replay is not None:
                connection.commit()
                return replay
            self._verify_predecessor(connection, value)
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
            event_specs = (
                ("ENTRY_READY", "READY", {
                    "entry_gate_result_sha256": value.entry_gate_result_sha256,
                    "authorization_receipt_sha256": evaluation["authorization_receipt_sha256"],
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
            receipt_body = {
                "schema": "authority-phase9-forensic-terminal-receipt-v1",
                "replay_id": value.replay_id,
                "workflow_id": value.workflow_id,
                "run_generation": value.run_generation,
                "request_sha256": value.request_sha256,
                "evidence_set_sha256": value.evidence_set_sha256,
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
            second_values = _read_evidence_set(self.evidence_root, value)
            second_evaluation = _evaluate_evidence(
                value, second_values, trusted_now=self._trusted_now()
            )
            if second_values != first_values or second_evaluation != evaluation:
                raise Phase9ForensicReplayConflict("evidence changed during transaction")
            current_source = read_current_git_source_identity(self.source_repository)
            if current_source != source:
                raise Phase9ForensicReplayConflict("source changed during transaction")
            self._control_fence(connection)
            self._fault("before_commit")
            connection.commit()
            receipt = connection.execute(
                "SELECT * FROM authority_production_phase9_terminal_receipts "
                "WHERE receipt_sha256=?", (receipt_sha256,),
            ).fetchone()
            assert receipt is not None
            return _result_from_receipt(value, receipt, replayed=False)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def collect_phase9_forensic_replay_state(
    database: str | Path,
    *,
    expected_source_fence_sha256: str,
    workflow_id: str,
) -> dict[str, object]:
    """Collect and verify current Phase9 state in one query-only snapshot."""

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
            """
            SELECT c.*, p.request_json, p.request_sha256, p.evidence_set_sha256,
                   r.receipt_json, r.terminal_reason, r.effective_verdict,
                   r.exit_code, r.occurred_at
            FROM authority_production_phase9_replay_current c
            JOIN authority_production_phase9_replays p ON p.replay_id=c.replay_id
            JOIN authority_production_phase9_terminal_receipts r
              ON r.replay_id=c.replay_id
             AND r.receipt_sha256=c.terminal_receipt_sha256
             AND r.final_event_sha256=c.final_event_sha256
            WHERE c.workflow_id=?
            """,
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
            request_body = _strict_json(str(current["request_json"]).encode(), "stored request")
            receipt_body = _strict_json(str(current["receipt_json"]).encode(), "stored receipt")
            if canonical_sha256(request_body) != current["request_sha256"]:
                raise Phase9ForensicReplayConflict("stored request hash differs")
            if canonical_sha256(receipt_body) != current["terminal_receipt_sha256"]:
                raise Phase9ForensicReplayConflict("stored terminal receipt hash differs")
            rows = connection.execute(
                "SELECT * FROM authority_production_phase9_replay_events "
                "WHERE replay_id=? ORDER BY sequence", (current["replay_id"],),
            ).fetchall()
            if [row["sequence"] for row in rows] != list(range(1, 7)):
                raise Phase9ForensicReplayConflict("event sequence differs")
            predecessor: str | None = None
            for row in rows:
                event_body = _strict_json(str(row["event_json"]).encode(), "stored event")
                if (
                    row["predecessor_event_sha256"] != predecessor
                    or event_body.get("predecessor_event_sha256") != predecessor
                    or canonical_sha256(event_body) != row["event_sha256"]
                ):
                    raise Phase9ForensicReplayConflict("event chain differs")
                predecessor = str(row["event_sha256"])
            if predecessor != current["final_event_sha256"]:
                raise Phase9ForensicReplayConflict("final event pointer differs")
            body = {
                "schema": PHASE9_REPLAY_STATE_SCHEMA,
                "status": "COMPLETED",
                "workflow_id": workflow,
                "replay_id": current["replay_id"],
                "run_generation": current["run_generation"],
                "request_sha256": current["request_sha256"],
                "evidence_set_sha256": current["evidence_set_sha256"],
                "terminal_receipt_sha256": current["terminal_receipt_sha256"],
                "terminal_reason": current["terminal_reason"],
                "effective_verdict": current["effective_verdict"],
                "exit_code": current["exit_code"],
                "event_count": len(rows),
                "delivery_capability": DELIVERY_DISABLED,
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
