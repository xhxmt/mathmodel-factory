"""Default-off, durable Phase-7 evidence-grounding shadow runtime.

This module is deliberately a shadow-only evidence boundary.  Callers supply
already-authorized Phase-3/Phase-6 values and the exact bytes for all three
judge roles.  The runtime never reads those source paths, never calls a judge,
provider, network service, scheduler, worker, outbox, or Authority writer, and
never transfers production authority.

Every durable fact is rebuilt from exact inputs.  The SQLite store is an
explicit, caller-owned, standalone database opened through descriptor anchors
and rollback-journal mode.  Current projection changes are append/history
preserving and idempotent.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
from typing import Callable, Mapping

from .canonical import CanonicalizationError, canonical_bytes, canonical_sha256
from .fd_ownership import (
    OwnedDescriptor,
    RetryableCleanup,
    close_raw_descriptor_if_unowned,
    resilient_unlink_at,
    run_cleanup,
)
from .phase3_artifacts import (
    ArtifactLedgerOccurrence,
    ArtifactOccurrenceKind,
    Phase3ContractError,
    artifact_occurrence_from_dict,
    validate_artifact_occurrence,
)
from . import authority_read_repository as authority_read
from . import phase6_snapshot_grants as phase6
from scripts.evidence_grounding import ROLES, validate_grounding_bytes


PHASE7_GROUNDING_DEFAULT_ENABLED = False
PHASE7_INPUT_SCHEMA = "phase7-grounding-bundle-input-v1"
PHASE7_EXACT_BYTES_SCHEMA = "phase7-exact-bytes-v1"
PHASE7_UNAVAILABLE_BYTES_SCHEMA = "phase7-unavailable-bytes-v1"
PHASE7_PATH_FREE_REPORT_SCHEMA = "phase7-path-free-grounding-report-v1"
PHASE7_ROLE_RECEIPT_SCHEMA = "phase7-durable-role-grounding-receipt-v1"
PHASE7_RECEIPT_SCHEMA = "phase7-durable-grounding-bundle-receipt-v1"
PHASE7_AGGREGATE_POLICY_SCHEMA = "phase7-effective-aggregate-policy-v1"
PHASE7_EFFECTIVE_VERDICT_SCHEMA = "phase7-effective-aggregate-verdict-v1"
PHASE7_REQUEST_SCHEMA = "phase7-grounding-bundle-request-v1"
PHASE7_COMMIT_SCHEMA = "phase7-grounding-bundle-commit-v1"
PHASE7_RESULT_SCHEMA = "phase7-grounding-bundle-result-v1"
PHASE7_STORE_SCHEMA = "phase7-grounding-runtime-sqlite-v1"
PHASE7_RUN_SCHEMA = "phase7-grounding-full-shadow-run-v1"

ROLE_ORDER = ("math", "execution", "paper")
if set(ROLE_ORDER) != set(ROLES):  # fail closed if the validator contract drifts
    raise RuntimeError("Phase-7 role set differs from evidence grounding")

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}\Z")
_ROLE_SCHEMAS = {
    "math": "judge-hard-role-v2",
    "execution": "judge-hard-role-v2",
    "paper": "judge-paper-role-v3",
}
_ROLE_VERDICTS = {
    "math": frozenset({"PASS", "FAIL", "INDETERMINATE"}),
    "execution": frozenset({"PASS", "FAIL", "INDETERMINATE"}),
    "paper": frozenset({"PASS", "REVISE", "INDETERMINATE"}),
}
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SQLITE_HEADER = b"SQLite format 3\x00"
_CONNECTION_OWNER = "phase7-sqlite-connection"
_MAX_BUSY_TIMEOUT_MS = 300_000

CurrentHeadVerifier = Callable[[dict[str, object], dict[str, object], dict[str, object]], bool]
AdapterFence = Callable[[str], None]


def _deadline_check(deadline: object | None, stage: str) -> None:
    if deadline is None:
        return
    check = getattr(deadline, "check", None)
    if not callable(check):
        raise Phase7GroundingContractError("deadline must expose check()")
    del stage
    check()


def _fence(fence_hook: AdapterFence | None, stage: str) -> None:
    if fence_hook is not None:
        if not callable(fence_hook):
            raise Phase7GroundingContractError("fence_hook must be callable")
        fence_hook(stage)


def _remaining_seconds(deadline: object | None, maximum: float, stage: str) -> float:
    _deadline_check(deadline, stage)
    if deadline is None:
        return maximum
    remaining = getattr(deadline, "remaining_seconds", None)
    if not callable(remaining):
        raise Phase7GroundingContractError(
            "deadline must expose remaining_seconds()"
        )
    value = remaining()
    if type(value) not in {int, float} or value <= 0:
        _deadline_check(deadline, stage)
        raise Phase7GroundingBusy("Phase-7 total deadline has no remaining budget")
    return min(maximum, float(value))


class Phase7GroundingError(RuntimeError):
    """Base error with a stable machine-readable code."""

    code = "PHASE7_GROUNDING_ERROR"


class Phase7GroundingDisabled(Phase7GroundingError):
    code = "PHASE7_GROUNDING_DISABLED"


class Phase7GroundingContractError(Phase7GroundingError):
    code = "PHASE7_GROUNDING_CONTRACT_INVALID"


class Phase7GroundingStoreError(Phase7GroundingError):
    code = "PHASE7_GROUNDING_STORE_INVALID"


class _Phase7InitializationRace(Phase7GroundingStoreError):
    """Another normal initializer won the exclusive-create boundary."""


class Phase7GroundingReplayConflict(Phase7GroundingError):
    code = "PHASE7_GROUNDING_IDEMPOTENCY_CONFLICT"


class Phase7GroundingCurrentConflict(Phase7GroundingError):
    code = "PHASE7_GROUNDING_SOURCE_STALE"


class Phase7GroundingBusy(Phase7GroundingError):
    code = "PHASE7_GROUNDING_STORE_BUSY"


class Phase7GroundingNotFound(Phase7GroundingError):
    code = "PHASE7_GROUNDING_NOT_FOUND"


def _plain_text(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise Phase7GroundingContractError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise Phase7GroundingContractError(f"{field} must contain valid UTF-8") from exc
    return value


def _identifier(value: object, field: str) -> str:
    result = _plain_text(value, field)
    if _IDENTIFIER.fullmatch(result) is None:
        raise Phase7GroundingContractError(f"{field} must be a bounded identifier")
    return result


def _sha(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise Phase7GroundingContractError(f"{field} must be lowercase SHA-256")
    return value


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1 or value > 2**63 - 1:
        raise Phase7GroundingContractError(f"{field} must be a positive integer")
    return value


def _exact_mapping(value: object, keys: set[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise Phase7GroundingContractError(f"{field} must be an object")
    if set(value) != keys:
        raise Phase7GroundingContractError(
            f"{field} fields must be exactly {sorted(keys)!r}"
        )
    return value


def _canonical_dict(value: Mapping[str, object], field: str) -> dict[str, object]:
    try:
        return json.loads(canonical_bytes(value).decode("utf-8"))
    except (CanonicalizationError, UnicodeError, TypeError, ValueError) as exc:
        raise Phase7GroundingContractError(f"{field} is outside canonical JSON") from exc


def _false_safety(value: Mapping[str, object], field: str) -> None:
    for name in ("authoritative", "authority_transferred", "dispatch_performed"):
        if value.get(name) is not False:
            raise Phase7GroundingContractError(f"{field}.{name} must be false")


def _artifact_occurrence(value: object) -> ArtifactLedgerOccurrence:
    try:
        occurrence = (
            validate_artifact_occurrence(value)
            if type(value) is ArtifactLedgerOccurrence
            else artifact_occurrence_from_dict(value)
        )
    except (Phase3ContractError, TypeError, ValueError) as exc:
        raise Phase7GroundingContractError(
            "phase3_artifact_occurrence does not revalidate"
        ) from exc
    logical = PurePosixPath(occurrence.normalized_path)
    if logical.is_absolute() or ".." in logical.parts or "\\" in occurrence.normalized_path:
        raise Phase7GroundingContractError(
            "phase3 artifact occurrence must use a relative logical path"
        )
    return occurrence


def _artifact_state(value: object):
    """Use the public Authority aggregate-state boundary without duplicating it."""

    typed = getattr(authority_read, "AuthorityPhase3ArtifactState", None)
    validator = getattr(authority_read, "validate_authority_phase3_artifact_state", None)
    parser = getattr(authority_read, "authority_phase3_artifact_state_from_dict", None)
    if typed is None or validator is None or parser is None:
        raise Phase7GroundingContractError(
            "Authority Phase-3 artifact-state verifier is unavailable"
        )
    try:
        state = validator(value) if type(value) is typed else parser(value)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise Phase7GroundingContractError(
            "phase3_artifact_state does not revalidate"
        ) from exc
    wire = state.as_dict()
    if not isinstance(wire, Mapping):
        raise Phase7GroundingContractError("Phase-3 artifact state has no wire value")
    return state, _canonical_dict(wire, "phase3_artifact_state")


def _access_proof(value: object) -> dict[str, object]:
    try:
        checked = phase6.verify_shadow_access_proof(value)
    except (phase6.Phase6Error, TypeError, ValueError, KeyError) as exc:
        raise Phase7GroundingContractError(
            "phase6_access_proof does not revalidate"
        ) from exc
    wire = checked.as_dict() if hasattr(checked, "as_dict") else checked
    if not isinstance(wire, Mapping):
        raise Phase7GroundingContractError("Phase-6 proof verifier returned no wire value")
    result = _canonical_dict(wire, "phase6_access_proof")
    if result.get("shadow_allowed") is not True:
        raise Phase7GroundingContractError("Phase-6 access proof is not shadow-allowed")
    _false_safety(result, "phase6_access_proof")
    _sha(result.get("proof_sha256"), "phase6_access_proof.proof_sha256")
    return result


def _cross_check_sources(
    state: object,
    state_wire: Mapping[str, object],
    occurrence: ArtifactLedgerOccurrence,
    proof: Mapping[str, object],
) -> None:
    source = proof["source_binding"]
    authority = source["authority_coordinate"]
    coordinate = source["source_snapshot_coordinate"]
    state_sha = getattr(state, "state_sha256", None)
    if type(state_sha) is not str:
        state_sha = state_wire.get("state_sha256")
    _sha(state_sha, "phase3_artifact_state.state_sha256")
    if (
        state_sha != source["phase3_artifact_state_sha256"]
        or getattr(state, "workflow_id", None) != occurrence.workflow_id
        or getattr(state, "workflow_id", None) != authority["workflow_id"]
        or getattr(state, "through_revision", None) != authority["current_revision"]
        or getattr(state, "through_revision", None) != coordinate["project_revision"]
    ):
        raise Phase7GroundingCurrentConflict(
            "Phase-3 aggregate state, occurrence, and Phase-6 source differ"
        )
    members = tuple(getattr(state, "occurrences", ()))
    selected = [item for item in members if item.occurrence_id == occurrence.occurrence_id]
    if len(selected) != 1 or selected[0] != occurrence:
        raise Phase7GroundingCurrentConflict(
            "selected Phase-3 occurrence is not an exact aggregate-state member"
        )


def _role_mapping(value: object, field: str) -> Mapping[str, object]:
    return _exact_mapping(value, set(ROLE_ORDER), field)


def _encode_bytes(value: object, field: str) -> dict[str, object]:
    if value is None:
        return {
            "schema_version": PHASE7_UNAVAILABLE_BYTES_SCHEMA,
            "reason_code": f"{field.upper()}_UNAVAILABLE",
        }
    if type(value) is not bytes:
        raise Phase7GroundingContractError(f"{field} must be exact bytes or None")
    return {
        "schema_version": PHASE7_EXACT_BYTES_SCHEMA,
        "sha256": hashlib.sha256(value).hexdigest(),
        "byte_length": len(value),
        "base64": base64.b64encode(value).decode("ascii"),
    }


def _decode_bytes(value: object, field: str) -> bytes | None:
    if not isinstance(value, Mapping):
        raise Phase7GroundingStoreError(f"{field} byte value is malformed")
    if value.get("schema_version") == PHASE7_UNAVAILABLE_BYTES_SCHEMA:
        wire = _exact_mapping(value, {"schema_version", "reason_code"}, field)
        _identifier(wire["reason_code"], f"{field}.reason_code")
        return None
    wire = _exact_mapping(
        value, {"schema_version", "sha256", "byte_length", "base64"}, field
    )
    if wire["schema_version"] != PHASE7_EXACT_BYTES_SCHEMA:
        raise Phase7GroundingStoreError(f"{field} byte schema differs")
    digest = _sha(wire["sha256"], f"{field}.sha256")
    if type(wire["byte_length"]) is not int or wire["byte_length"] < 0:
        raise Phase7GroundingStoreError(f"{field}.byte_length is invalid")
    if type(wire["base64"]) is not str:
        raise Phase7GroundingStoreError(f"{field}.base64 is invalid")
    try:
        raw = base64.b64decode(wire["base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise Phase7GroundingStoreError(f"{field}.base64 is invalid") from exc
    if len(raw) != wire["byte_length"] or hashlib.sha256(raw).hexdigest() != digest:
        raise Phase7GroundingStoreError(f"{field} exact-byte identity differs")
    return raw


def _raw_verdict(role_output: bytes | None, role: str) -> str | None:
    if role_output is None:
        return None
    try:
        lines = role_output.decode("utf-8", errors="strict").splitlines()
        match = re.fullmatch(r"VERDICT: ([A-Z_]+)", lines[0]) if len(lines) >= 2 else None
        if match is None:
            return None
        payload = json.loads("\n".join(lines[1:]))
        verdict = match.group(1)
        if (
            type(payload) is not dict
            or payload.get("schema_version") != _ROLE_SCHEMAS[role]
            or payload.get("role") != role
            or payload.get("verdict") != verdict
            or verdict not in _ROLE_VERDICTS[role]
        ):
            return None
        return verdict
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, IndexError):
        return None


def _normalized_report(
    role: str,
    raw_report: object,
    manifest_raw: bytes | None,
    context_raw: bytes | None,
) -> dict[str, object]:
    """Remove host labels while retaining every stable validation result."""

    report = _exact_mapping(
        raw_report,
        {"schema_version", "role", "valid", "manifest", "context", "refs", "errors"},
        f"{role} grounding report",
    )
    if report["schema_version"] != "evidence-grounding-v1" or report["role"] != role:
        raise Phase7GroundingContractError("grounding report schema or role differs")
    if type(report["valid"]) is not bool:
        raise Phase7GroundingContractError("grounding report valid flag differs")
    identities: dict[str, dict[str, object]] = {}
    for name, supplied in (("manifest", manifest_raw), ("context", context_raw)):
        observed = _exact_mapping(report[name], {"path", "sha256", "size"}, name)
        expected_sha = None if supplied is None else hashlib.sha256(supplied).hexdigest()
        expected_size = None if supplied is None else len(supplied)
        if observed["sha256"] is not None and observed["sha256"] != expected_sha:
            raise Phase7GroundingContractError(f"{role} {name} validator identity differs")
        if observed["size"] is not None and observed["size"] != expected_size:
            raise Phase7GroundingContractError(f"{role} {name} validator size differs")
        identities[name] = {"sha256": expected_sha, "byte_length": expected_size}
    if type(report["refs"]) is not list or type(report["errors"]) is not list:
        raise Phase7GroundingContractError("grounding report refs/errors must be arrays")
    refs: list[dict[str, object]] = []
    ref_keys = {
        "ref_id", "chunk_id", "quote_sha256", "resolved_path", "line_start",
        "line_end", "source_line_start", "source_line_end", "context_line_start",
        "context_line_end",
    }
    seen: set[str] = set()
    for index, raw in enumerate(report["refs"]):
        ref = _exact_mapping(raw, ref_keys, f"{role}.refs[{index}]")
        ref_id = _identifier(ref["ref_id"], f"{role}.refs[{index}].ref_id")
        if ref_id in seen:
            raise Phase7GroundingContractError("grounding ref_id is duplicated")
        seen.add(ref_id)
        path = _plain_text(ref["resolved_path"], "grounding resolved_path")
        logical = PurePosixPath(path)
        if logical.is_absolute() or ".." in logical.parts or "\\" in path:
            raise Phase7GroundingContractError("grounding report has a non-logical path")
        item: dict[str, object] = {
            "ref_id": ref_id,
            "chunk_id": _sha(ref["chunk_id"], "grounding chunk_id"),
            "quote_sha256": _sha(ref["quote_sha256"], "grounding quote_sha256"),
            "resolved_path": path,
        }
        for name in (
            "line_start", "line_end", "source_line_start", "source_line_end",
            "context_line_start", "context_line_end",
        ):
            item[name] = _positive(ref[name], f"grounding {name}")
        if (
            item["line_start"] != item["source_line_start"]
            or item["line_end"] != item["source_line_end"]
            or item["line_end"] < item["line_start"]
            or item["context_line_end"] < item["context_line_start"]
        ):
            raise Phase7GroundingContractError("grounding line range differs")
        refs.append(item)
    errors: list[dict[str, str]] = []
    for index, raw in enumerate(report["errors"]):
        error = _exact_mapping(raw, {"ref_id", "code", "message"}, f"{role}.errors[{index}]")
        errors.append({
            "ref_id": _plain_text(error["ref_id"], "grounding error ref_id"),
            "code": _identifier(error["code"], "grounding error code"),
        })
    if report["valid"] is True and errors:
        raise Phase7GroundingContractError("valid grounding report contains errors")
    if report["valid"] is False and not errors:
        raise Phase7GroundingContractError("invalid grounding report lacks errors")
    return {
        "schema_version": PHASE7_PATH_FREE_REPORT_SCHEMA,
        "role": role,
        "valid": report["valid"],
        "manifest": identities["manifest"],
        "context": identities["context"],
        "refs": refs,
        "errors": errors,
    }


def _unavailable_report(
    role: str,
    role_raw: bytes | None,
    manifest_raw: bytes | None,
    context_raw: bytes | None,
) -> dict[str, object]:
    missing = [
        name
        for name, value in (
            ("ROLE_OUTPUT", role_raw), ("MANIFEST", manifest_raw), ("CONTEXT", context_raw)
        )
        if value is None
    ]
    return {
        "schema_version": PHASE7_PATH_FREE_REPORT_SCHEMA,
        "role": role,
        "valid": False,
        "manifest": {
            "sha256": None if manifest_raw is None else hashlib.sha256(manifest_raw).hexdigest(),
            "byte_length": None if manifest_raw is None else len(manifest_raw),
        },
        "context": {
            "sha256": None if context_raw is None else hashlib.sha256(context_raw).hexdigest(),
            "byte_length": None if context_raw is None else len(context_raw),
        },
        "refs": [],
        "errors": [
            {"ref_id": "__packet__", "code": f"{name}_UNAVAILABLE"}
            for name in missing
        ],
    }


def _aggregate_policy() -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": PHASE7_AGGREGATE_POLICY_SCHEMA,
        "upstream_aggregate_schema_version": "judge-aggregate-v3",
        "role_order": list(ROLE_ORDER),
        "role_schema_versions": dict(_ROLE_SCHEMAS),
        "hard_fail_roles": ["math", "execution"],
        "hard_fail_action": "REOPEN_REVISION_MODEL",
        "indeterminate_action": "INDETERMINATE_REVIEW",
        "paper_revise_action": "REOPEN_REVISION_TEXT",
        "pass_action": "PASS",
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    return {**body, "policy_sha256": canonical_sha256(body)}


def _aggregate(role_effective: Mapping[str, str], occurrence_kind: str) -> tuple[str, str, str]:
    if occurrence_kind != ArtifactOccurrenceKind.RECORD.value:
        return "INDETERMINATE", "INDETERMINATE_REVIEW", "PHASE3_ARTIFACT_NOT_RECORDED"
    if role_effective["math"] == "FAIL" or role_effective["execution"] == "FAIL":
        return "FAIL", "REOPEN_REVISION_MODEL", "HARD_ROLE_FAILED"
    if any(role_effective[role] == "INDETERMINATE" for role in ROLE_ORDER):
        return "INDETERMINATE", "INDETERMINATE_REVIEW", "ROLE_INDETERMINATE"
    if role_effective["paper"] == "REVISE":
        return "REVISE", "REOPEN_REVISION_TEXT", "PAPER_REVISE"
    return "PASS", "PASS", "ALL_ROLES_GROUNDED_PASS"


@dataclass(frozen=True)
class PreparedGroundingBundle:
    request: dict[str, object]
    request_sha256: str
    receipt: dict[str, object]
    receipt_sha256: str
    effective_verdict: dict[str, object]
    effective_verdict_sha256: str
    scope_key: str
    occurrence_id: str
    authority_revision: int
    aggregate_verdict: str
    aggregate_action: str
    grounding_valid: bool


@dataclass(frozen=True)
class GroundingCommitResult:
    idempotency_key: str
    scope_key: str
    sequence: int
    previous_commit_sha256: str | None
    request_sha256: str
    receipt_sha256: str
    effective_verdict_sha256: str
    commit_sha256: str
    occurrence_id: str
    authority_revision: int
    aggregate_verdict: str
    aggregate_action: str
    grounding_valid: bool
    replayed: bool
    current: bool
    authoritative: bool = False
    authority_transferred: bool = False
    dispatch_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE7_RESULT_SCHEMA,
            "idempotency_key": self.idempotency_key,
            "scope_key": self.scope_key,
            "sequence": self.sequence,
            "previous_commit_sha256": self.previous_commit_sha256,
            "request_sha256": self.request_sha256,
            "receipt_sha256": self.receipt_sha256,
            "effective_verdict_sha256": self.effective_verdict_sha256,
            "commit_sha256": self.commit_sha256,
            "occurrence_id": self.occurrence_id,
            "authority_revision": self.authority_revision,
            "aggregate_verdict": self.aggregate_verdict,
            "aggregate_action": self.aggregate_action,
            "grounding_valid": self.grounding_valid,
            "replayed": self.replayed,
            "current": self.current,
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
        }


def prepare_grounding_bundle(
    *,
    idempotency_key: str,
    phase3_artifact_state: object,
    phase3_artifact_occurrence: object,
    phase6_access_proof: object,
    role_output_bytes: Mapping[str, bytes | None],
    manifest_bytes: Mapping[str, bytes | None],
    context_bytes: Mapping[str, bytes | None],
    deadline: object | None = None,
    fence_hook: AdapterFence | None = None,
) -> PreparedGroundingBundle:
    """Deeply rebuild one atomic three-role grounding bundle.

    ``None`` is an explicit stable unavailable outcome.  It is persisted as
    INDETERMINATE so a previous PASS cannot remain current after a normal
    missing/write-failed judge output.
    """

    _deadline_check(deadline, "phase7_precompute_before")
    _fence(fence_hook, "phase7_precompute_before")
    key = _identifier(idempotency_key, "idempotency_key")
    state, state_wire = _artifact_state(phase3_artifact_state)
    occurrence = _artifact_occurrence(phase3_artifact_occurrence)
    proof = _access_proof(phase6_access_proof)
    _cross_check_sources(state, state_wire, occurrence, proof)
    role_values = _role_mapping(role_output_bytes, "role_output_bytes")
    manifest_values = _role_mapping(manifest_bytes, "manifest_bytes")
    context_values = _role_mapping(context_bytes, "context_bytes")

    proof_sha = _sha(proof["proof_sha256"], "phase6_access_proof.proof_sha256")
    state_sha = _sha(
        getattr(state, "state_sha256", state_wire.get("state_sha256")),
        "phase3_artifact_state.state_sha256",
    )
    source = proof["source_binding"]
    authority = source["authority_coordinate"]
    authority_revision = int(authority["current_revision"])
    scope_body: dict[str, object] = {
        "schema_version": "phase7-grounding-scope-v1",
        "workflow_id": occurrence.workflow_id,
        "project_id": authority["project_id"],
        "normalized_path": occurrence.normalized_path,
    }
    scope_key = canonical_sha256(scope_body)

    role_receipts: dict[str, dict[str, object]] = {}
    role_effective: dict[str, str] = {}
    all_valid = occurrence.kind is ArtifactOccurrenceKind.RECORD
    for role in ROLE_ORDER:
        encoded_role = _encode_bytes(role_values[role], f"{role}_role_output")
        encoded_manifest = _encode_bytes(manifest_values[role], f"{role}_manifest")
        encoded_context = _encode_bytes(context_values[role], f"{role}_context")
        role_raw = _decode_bytes(encoded_role, f"{role}.role_output")
        manifest_raw = _decode_bytes(encoded_manifest, f"{role}.manifest")
        context_raw = _decode_bytes(encoded_context, f"{role}.context")
        if role_raw is None or manifest_raw is None or context_raw is None:
            report = _unavailable_report(role, role_raw, manifest_raw, context_raw)
        else:
            report = _normalized_report(
                role,
                validate_grounding_bytes(
                    role_raw, manifest_raw, context_raw, role=role
                ),
                manifest_raw,
                context_raw,
            )
        raw_verdict = _raw_verdict(role_raw, role)
        valid = bool(report["valid"] and raw_verdict is not None)
        effective = raw_verdict if valid else "INDETERMINATE"
        role_effective[role] = effective
        all_valid = all_valid and valid
        report_sha = canonical_sha256(report)
        role_body: dict[str, object] = {
            "schema_version": PHASE7_ROLE_RECEIPT_SCHEMA,
            "role": role,
            "role_schema_version": _ROLE_SCHEMAS[role],
            "role_output_bytes": encoded_role,
            "manifest_bytes": encoded_manifest,
            "context_bytes": encoded_context,
            "grounding_report": report,
            "grounding_report_sha256": report_sha,
            "raw_verdict": raw_verdict,
            "effective_verdict": effective,
            "grounding_valid": valid,
            "reason_code": "GROUNDING_VALID" if valid else "GROUNDING_INDETERMINATE",
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
        role_receipts[role] = {
            **role_body,
            "role_receipt_sha256": canonical_sha256(role_body),
        }

    occurrence_wire = _canonical_dict(
        occurrence.as_dict(), "phase3_artifact_occurrence"
    )
    input_body: dict[str, object] = {
        "schema_version": PHASE7_INPUT_SCHEMA,
        "phase3_artifact_state": state_wire,
        "phase3_artifact_state_sha256": state_sha,
        "phase3_artifact_occurrence": occurrence_wire,
        "phase3_artifact_occurrence_id": occurrence.occurrence_id,
        "phase3_occurrence_semantic_sha256": occurrence.semantic_sha256,
        "phase6_access_proof": proof,
        "phase6_access_proof_sha256": proof_sha,
        "phase6_source_binding_sha256": source["binding_sha256"],
        "authority_coordinate_sha256": source["authority_coordinate_sha256"],
        "source_snapshot_coordinate_sha256": source["source_snapshot_coordinate_sha256"],
        "scope": scope_body,
        "scope_key": scope_key,
        "role_receipt_sha256s": {
            role: role_receipts[role]["role_receipt_sha256"] for role in ROLE_ORDER
        },
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    input_sha = canonical_sha256(input_body)
    input_identity = {**input_body, "input_identity_sha256": input_sha}
    receipt_body: dict[str, object] = {
        "schema_version": PHASE7_RECEIPT_SCHEMA,
        "input_identity": input_identity,
        "input_identity_sha256": input_sha,
        "role_receipts": role_receipts,
        "grounding_valid": all_valid,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    receipt_sha = canonical_sha256(receipt_body)
    receipt = {**receipt_body, "receipt_sha256": receipt_sha}
    aggregate_verdict, aggregate_action, reason = _aggregate(
        role_effective, occurrence.kind.value
    )
    policy = _aggregate_policy()
    effective_body: dict[str, object] = {
        "schema_version": PHASE7_EFFECTIVE_VERDICT_SCHEMA,
        "scope_key": scope_key,
        "occurrence_id": occurrence.occurrence_id,
        "authority_revision": authority_revision,
        "role_effective_verdicts": role_effective,
        "aggregate_verdict": aggregate_verdict,
        "aggregate_action": aggregate_action,
        "reason_code": reason,
        "aggregate_policy": policy,
        "aggregate_policy_sha256": policy["policy_sha256"],
        "input_identity_sha256": input_sha,
        "grounding_receipt_sha256": receipt_sha,
        "phase3_artifact_state_sha256": state_sha,
        "phase3_artifact_occurrence_id": occurrence.occurrence_id,
        "phase6_access_proof_sha256": proof_sha,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    effective_sha = canonical_sha256(effective_body)
    effective_receipt = {
        **effective_body,
        "effective_verdict_sha256": effective_sha,
    }
    request: dict[str, object] = {
        "schema_version": PHASE7_REQUEST_SCHEMA,
        "idempotency_key": key,
        "scope_key": scope_key,
        "input_identity_sha256": input_sha,
        "grounding_receipt_sha256": receipt_sha,
        "effective_verdict_sha256": effective_sha,
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    request_sha = canonical_sha256(request)
    prepared = PreparedGroundingBundle(
        request=request,
        request_sha256=request_sha,
        receipt=receipt,
        receipt_sha256=receipt_sha,
        effective_verdict=effective_receipt,
        effective_verdict_sha256=effective_sha,
        scope_key=scope_key,
        occurrence_id=occurrence.occurrence_id,
        authority_revision=authority_revision,
        aggregate_verdict=aggregate_verdict,
        aggregate_action=aggregate_action,
        grounding_valid=all_valid,
    )
    _deadline_check(deadline, "phase7_precompute_after")
    _fence(fence_hook, "phase7_precompute_after")
    return prepared


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE phase7_shadow_schema_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version TEXT NOT NULL,
        store_instance_id TEXT NOT NULL,
        creation_binding_json TEXT NOT NULL,
        schema_digest_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE phase7_shadow_grounding_receipts (
        receipt_sha256 TEXT PRIMARY KEY,
        scope_key TEXT NOT NULL,
        occurrence_id TEXT NOT NULL,
        authority_revision INTEGER NOT NULL,
        receipt_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE phase7_shadow_effective_verdicts (
        effective_verdict_sha256 TEXT PRIMARY KEY,
        receipt_sha256 TEXT NOT NULL UNIQUE,
        verdict_json TEXT NOT NULL,
        FOREIGN KEY(receipt_sha256)
            REFERENCES phase7_shadow_grounding_receipts(receipt_sha256)
    )
    """,
    """
    CREATE TABLE phase7_shadow_commits (
        commit_sha256 TEXT PRIMARY KEY,
        scope_key TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        previous_commit_sha256 TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL UNIQUE,
        receipt_sha256 TEXT NOT NULL,
        effective_verdict_sha256 TEXT NOT NULL,
        occurrence_id TEXT NOT NULL,
        authority_revision INTEGER NOT NULL,
        commit_json TEXT NOT NULL,
        UNIQUE(scope_key, sequence),
        FOREIGN KEY(previous_commit_sha256)
            REFERENCES phase7_shadow_commits(commit_sha256),
        FOREIGN KEY(receipt_sha256)
            REFERENCES phase7_shadow_grounding_receipts(receipt_sha256),
        FOREIGN KEY(effective_verdict_sha256)
            REFERENCES phase7_shadow_effective_verdicts(effective_verdict_sha256)
    )
    """,
    """
    CREATE TABLE phase7_shadow_current (
        scope_key TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        commit_sha256 TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL,
        effective_verdict_sha256 TEXT NOT NULL,
        occurrence_id TEXT NOT NULL,
        authority_revision INTEGER NOT NULL,
        FOREIGN KEY(commit_sha256)
            REFERENCES phase7_shadow_commits(commit_sha256),
        FOREIGN KEY(receipt_sha256)
            REFERENCES phase7_shadow_grounding_receipts(receipt_sha256),
        FOREIGN KEY(effective_verdict_sha256)
            REFERENCES phase7_shadow_effective_verdicts(effective_verdict_sha256)
    )
    """,
    """
    CREATE TABLE phase7_shadow_idempotency (
        idempotency_key TEXT PRIMARY KEY,
        request_json TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        commit_sha256 TEXT NOT NULL UNIQUE,
        FOREIGN KEY(commit_sha256)
            REFERENCES phase7_shadow_commits(commit_sha256)
    )
    """,
)

_IMMUTABLE_TABLES = (
    "phase7_shadow_schema_state",
    "phase7_shadow_grounding_receipts",
    "phase7_shadow_effective_verdicts",
    "phase7_shadow_commits",
    "phase7_shadow_idempotency",
)


def _schema_inventory(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    return tuple(
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table_name": str(row[2]),
            "sql": None if row[3] is None else str(row[3]),
        }
        for row in rows
    )


def _create_schema(connection: sqlite3.Connection) -> str:
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)
    for table in _IMMUTABLE_TABLES:
        for action in ("UPDATE", "DELETE"):
            connection.execute(
                f"""
                CREATE TRIGGER {table}_immutable_{action.lower()}
                BEFORE {action} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END
                """
            )
    connection.execute(
        """
        CREATE TRIGGER phase7_shadow_current_no_delete
        BEFORE DELETE ON phase7_shadow_current
        BEGIN
            SELECT RAISE(ABORT, 'phase7 current projection cannot be deleted');
        END
        """
    )
    return canonical_sha256(_schema_inventory(connection))


def _expected_schema_digest() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        return _create_schema(connection)
    finally:
        connection.close()


_EXPECTED_SCHEMA_DIGEST = _expected_schema_digest()


def _canonical_json(value: Mapping[str, object]) -> str:
    return canonical_bytes(value).decode("utf-8")


def _decode_canonical_json(value: object, field: str) -> dict[str, object]:
    if type(value) is not str:
        raise Phase7GroundingStoreError(f"{field} is not stored JSON text")
    try:
        decoded = json.loads(value)
        if type(decoded) is not dict or _canonical_json(decoded) != value:
            raise Phase7GroundingStoreError(f"{field} is not canonical JSON")
        return decoded
    except (json.JSONDecodeError, CanonicalizationError, UnicodeError, TypeError) as exc:
        if isinstance(exc, Phase7GroundingStoreError):
            raise
        raise Phase7GroundingStoreError(f"{field} is invalid") from exc


def _verify_hashed_wire(value: Mapping[str, object], hash_field: str, field: str) -> str:
    digest = _sha(value.get(hash_field), f"{field}.{hash_field}")
    body = {name: item for name, item in value.items() if name != hash_field}
    if canonical_sha256(body) != digest:
        raise Phase7GroundingStoreError(f"{field} hash differs")
    return digest


def _rebuild_stored_receipt(value: object) -> tuple[dict[str, object], PreparedGroundingBundle]:
    receipt = _exact_mapping(
        value,
        {
            "schema_version", "input_identity", "input_identity_sha256",
            "role_receipts", "grounding_valid", "authoritative",
            "authority_transferred", "dispatch_performed", "receipt_sha256",
        },
        "grounding bundle receipt",
    )
    if receipt["schema_version"] != PHASE7_RECEIPT_SCHEMA:
        raise Phase7GroundingStoreError("grounding bundle receipt schema differs")
    _false_safety(receipt, "grounding bundle receipt")
    _verify_hashed_wire(receipt, "receipt_sha256", "grounding bundle receipt")
    identity = _exact_mapping(
        receipt["input_identity"],
        {
            "schema_version", "phase3_artifact_state",
            "phase3_artifact_state_sha256", "phase3_artifact_occurrence",
            "phase3_artifact_occurrence_id", "phase3_occurrence_semantic_sha256",
            "phase6_access_proof", "phase6_access_proof_sha256",
            "phase6_source_binding_sha256", "authority_coordinate_sha256",
            "source_snapshot_coordinate_sha256", "scope", "scope_key",
            "role_receipt_sha256s", "authoritative", "authority_transferred",
            "dispatch_performed", "input_identity_sha256",
        },
        "grounding input identity",
    )
    if identity["schema_version"] != PHASE7_INPUT_SCHEMA:
        raise Phase7GroundingStoreError("grounding input schema differs")
    _false_safety(identity, "grounding input identity")
    input_sha = _verify_hashed_wire(
        identity, "input_identity_sha256", "grounding input identity"
    )
    if receipt["input_identity_sha256"] != input_sha:
        raise Phase7GroundingStoreError("grounding receipt input binding differs")
    roles = _role_mapping(receipt["role_receipts"], "stored role_receipts")
    supplied_role_hashes = _role_mapping(
        identity["role_receipt_sha256s"], "stored role receipt hashes"
    )
    role_bytes: dict[str, bytes | None] = {}
    manifests: dict[str, bytes | None] = {}
    contexts: dict[str, bytes | None] = {}
    role_keys = {
        "schema_version", "role", "role_schema_version", "role_output_bytes",
        "manifest_bytes", "context_bytes", "grounding_report",
        "grounding_report_sha256", "raw_verdict", "effective_verdict",
        "grounding_valid", "reason_code", "authoritative",
        "authority_transferred", "dispatch_performed", "role_receipt_sha256",
    }
    for role in ROLE_ORDER:
        role_receipt = _exact_mapping(roles[role], role_keys, f"stored {role} receipt")
        if role_receipt["schema_version"] != PHASE7_ROLE_RECEIPT_SCHEMA:
            raise Phase7GroundingStoreError(f"stored {role} receipt schema differs")
        _false_safety(role_receipt, f"stored {role} receipt")
        role_sha = _verify_hashed_wire(
            role_receipt, "role_receipt_sha256", f"stored {role} receipt"
        )
        if supplied_role_hashes[role] != role_sha:
            raise Phase7GroundingStoreError(f"stored {role} receipt binding differs")
        role_bytes[role] = _decode_bytes(
            role_receipt["role_output_bytes"], f"stored {role}.role_output"
        )
        manifests[role] = _decode_bytes(
            role_receipt["manifest_bytes"], f"stored {role}.manifest"
        )
        contexts[role] = _decode_bytes(
            role_receipt["context_bytes"], f"stored {role}.context"
        )
    try:
        rebuilt = prepare_grounding_bundle(
            idempotency_key="phase7-storage-revalidation",
            phase3_artifact_state=identity["phase3_artifact_state"],
            phase3_artifact_occurrence=identity["phase3_artifact_occurrence"],
            phase6_access_proof=identity["phase6_access_proof"],
            role_output_bytes=role_bytes,
            manifest_bytes=manifests,
            context_bytes=contexts,
        )
    except Phase7GroundingError as exc:
        raise Phase7GroundingStoreError(
            "stored grounding bundle does not independently revalidate"
        ) from exc
    canonical = _canonical_dict(receipt, "grounding bundle receipt")
    if rebuilt.receipt != canonical:
        raise Phase7GroundingStoreError("stored grounding bundle reconstruction differs")
    return canonical, rebuilt


def _verify_effective(
    value: object, rebuilt: PreparedGroundingBundle
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise Phase7GroundingStoreError("effective aggregate verdict is malformed")
    canonical = _canonical_dict(value, "effective aggregate verdict")
    _verify_hashed_wire(
        canonical, "effective_verdict_sha256", "effective aggregate verdict"
    )
    if canonical != rebuilt.effective_verdict:
        raise Phase7GroundingStoreError("effective aggregate verdict reconstruction differs")
    return canonical


def _verify_request(
    value: object,
    *,
    rebuilt: PreparedGroundingBundle,
) -> dict[str, object]:
    request = _exact_mapping(
        value,
        {
            "schema_version", "idempotency_key", "scope_key",
            "input_identity_sha256", "grounding_receipt_sha256",
            "effective_verdict_sha256", "authoritative", "authority_transferred",
            "dispatch_performed",
        },
        "grounding bundle request",
    )
    if request["schema_version"] != PHASE7_REQUEST_SCHEMA:
        raise Phase7GroundingStoreError("grounding bundle request schema differs")
    _false_safety(request, "grounding bundle request")
    _identifier(request["idempotency_key"], "grounding bundle idempotency_key")
    if (
        request["scope_key"] != rebuilt.scope_key
        or request["input_identity_sha256"]
        != rebuilt.receipt["input_identity_sha256"]
        or request["grounding_receipt_sha256"] != rebuilt.receipt_sha256
        or request["effective_verdict_sha256"]
        != rebuilt.effective_verdict_sha256
    ):
        raise Phase7GroundingStoreError("grounding bundle request binding differs")
    return _canonical_dict(request, "grounding bundle request")


def _verify_commit(
    value: object,
    *,
    request: Mapping[str, object],
    rebuilt: PreparedGroundingBundle,
) -> dict[str, object]:
    commit = _exact_mapping(
        value,
        {
            "schema_version", "scope_key", "sequence",
            "previous_commit_sha256", "idempotency_key", "request_sha256",
            "grounding_receipt_sha256", "effective_verdict_sha256",
            "occurrence_id", "authority_revision", "authoritative",
            "authority_transferred", "dispatch_performed", "commit_sha256",
        },
        "grounding bundle commit",
    )
    if commit["schema_version"] != PHASE7_COMMIT_SCHEMA:
        raise Phase7GroundingStoreError("grounding bundle commit schema differs")
    _false_safety(commit, "grounding bundle commit")
    _verify_hashed_wire(commit, "commit_sha256", "grounding bundle commit")
    sequence = _positive(commit["sequence"], "grounding bundle sequence")
    previous = commit["previous_commit_sha256"]
    if previous is not None:
        _sha(previous, "grounding bundle previous_commit_sha256")
    if (
        commit["scope_key"] != rebuilt.scope_key
        or commit["idempotency_key"] != request["idempotency_key"]
        or commit["request_sha256"] != canonical_sha256(request)
        or commit["grounding_receipt_sha256"] != rebuilt.receipt_sha256
        or commit["effective_verdict_sha256"]
        != rebuilt.effective_verdict_sha256
        or commit["occurrence_id"] != rebuilt.occurrence_id
        or commit["authority_revision"] != rebuilt.authority_revision
        or sequence != commit["sequence"]
    ):
        raise Phase7GroundingStoreError("grounding bundle commit binding differs")
    return _canonical_dict(commit, "grounding bundle commit")


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "_FileIdentity":
        return cls(
            int(value.st_dev), int(value.st_ino), int(value.st_size),
            int(value.st_mtime_ns), int(value.st_ctime_ns),
        )


class _AnchoredConnection(sqlite3.Connection):
    _database_anchor: OwnedDescriptor | None = None
    _parent_anchor: OwnedDescriptor | None = None
    _identity: _FileIdentity | None = None
    _parent_identity: tuple[int, int] | None = None
    _commit_completed: bool = False
    _sqlite_closed: bool = False

    def _adopt_anchors(
        self,
        *,
        database: OwnedDescriptor,
        database_owner: str,
        parent: OwnedDescriptor,
        parent_owner: str,
    ) -> None:
        if self._database_anchor is not None or self._parent_anchor is not None:
            raise Phase7GroundingStoreError("connection already owns anchors")
        self._database_anchor = database
        self._parent_anchor = parent
        database.transfer(owner=database_owner, new_owner=_CONNECTION_OWNER)
        parent.transfer(owner=parent_owner, new_owner=_CONNECTION_OWNER)

    def _close_anchor(self, attribute: str) -> None:
        anchor = getattr(self, attribute)
        if anchor is None:
            return
        try:
            run_cleanup([(f"close {attribute}", anchor.cleanup(_CONNECTION_OWNER))])
        finally:
            if anchor.closed:
                setattr(self, attribute, None)

    def _anchor_callbacks(self):
        return [
            (
                f"close Phase-7 {attribute}",
                RetryableCleanup(lambda attribute=attribute: self._close_anchor(attribute)),
            )
            for attribute in ("_parent_anchor", "_database_anchor")
            if getattr(self, attribute) is not None
        ]

    def close(self) -> None:
        try:
            callbacks = self._anchor_callbacks()
            if not self._sqlite_closed:
                sqlite3.Connection.close(self)
                self._sqlite_closed = True
            run_cleanup(callbacks)
        except BaseException as primary:
            def reconcile() -> None:
                if not self._sqlite_closed:
                    sqlite3.Connection.close(self)
                    self._sqlite_closed = True

            run_cleanup(
                [("reconcile interrupted Phase-7 SQLite close", RetryableCleanup(reconcile))],
                primary=primary,
            )
            run_cleanup(self._anchor_callbacks(), primary=primary)
            raise

    @property
    def resources_closed(self) -> bool:
        return self._sqlite_closed and self._database_anchor is None and self._parent_anchor is None

    def __del__(self) -> None:
        if self.resources_closed:
            return
        try:
            self.close()
        except BaseException:
            pass


def _cleanup_failed_connection(
    connection: _AnchoredConnection, *, primary: BaseException, label: str
) -> None:
    def rollback() -> None:
        if connection.in_transaction:
            connection.rollback()

    run_cleanup(
        [
            (f"rollback {label}", rollback),
            (f"close {label}", RetryableCleanup(connection.close)),
        ],
        primary=primary,
    )


def _phase7_failure_point(stage: str) -> None:
    """Deterministic seam for transaction/creation rollback tests."""


class Phase7GroundingStore:
    """Descriptor-anchored standalone SQLite store for Phase-7 facts."""

    __slots__ = ("_path", "_busy_timeout_ms")

    def __init__(self, database: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if not isinstance(database, (str, Path)) or not str(database):
            raise Phase7GroundingStoreError("an explicit Phase-7 SQLite path is required")
        path = Path(database)
        if not path.is_absolute():
            raise Phase7GroundingStoreError("Phase-7 SQLite path must be absolute")
        if (
            type(busy_timeout_ms) is not int
            or busy_timeout_ms < 1
            or busy_timeout_ms > _MAX_BUSY_TIMEOUT_MS
        ):
            raise Phase7GroundingContractError(
                f"busy_timeout_ms must be within 1..{_MAX_BUSY_TIMEOUT_MS}"
            )
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms

    @property
    def path(self) -> Path:
        return self._path

    @property
    def busy_timeout_ms(self) -> int:
        return self._busy_timeout_ms

    def _validate_parent(self) -> None:
        parent = self._path.parent
        if not parent.is_dir():
            raise Phase7GroundingStoreError("Phase-7 SQLite parent must exist")
        try:
            resolved = parent.resolve(strict=True)
        except OSError as exc:
            raise Phase7GroundingStoreError("Phase-7 SQLite parent is unavailable") from exc
        if resolved != parent:
            raise Phase7GroundingStoreError("Phase-7 SQLite parent cannot contain symlinks")
        if not Path("/proc/self/fd").is_dir():
            raise Phase7GroundingStoreError("descriptor-anchored SQLite is unavailable")

    def _open_parent(self, owner: str) -> tuple[OwnedDescriptor, tuple[int, int]]:
        self._validate_parent()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent: OwnedDescriptor | None = None
        try:
            parent = OwnedDescriptor.from_opener(
                lambda: os.open(self._path.parent, flags),
                owner=owner,
                label="Phase-7 parent",
            )
            anchored = os.fstat(parent.fileno(owner))
            named = os.stat(self._path.parent, follow_symlinks=False)
            identity = (int(anchored.st_dev), int(anchored.st_ino))
            if (
                not stat.S_ISDIR(anchored.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or identity != (int(named.st_dev), int(named.st_ino))
            ):
                raise Phase7GroundingStoreError("Phase-7 parent identity differs")
            return parent, identity
        except BaseException as primary:
            if parent is not None:
                run_cleanup(
                    [("close failed Phase-7 parent", parent.cleanup(owner))],
                    primary=primary,
                )
            raise

    def _assert_parent(
        self, parent: OwnedDescriptor, owner: str, expected: tuple[int, int]
    ) -> None:
        try:
            anchored = os.fstat(parent.fileno(owner))
            named = os.stat(self._path.parent, follow_symlinks=False)
        except OSError as exc:
            raise Phase7GroundingStoreError("Phase-7 parent fence failed") from exc
        if (
            not stat.S_ISDIR(anchored.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (int(anchored.st_dev), int(anchored.st_ino)) != expected
            or (int(named.st_dev), int(named.st_ino)) != expected
        ):
            raise Phase7GroundingStoreError("Phase-7 parent fence changed")

    def _assert_no_sidecars(self, parent_fd: int | None = None) -> None:
        for suffix in _SIDECAR_SUFFIXES:
            name = f"{self._path.name}{suffix}"
            try:
                if parent_fd is None:
                    os.lstat(f"{self._path}{suffix}")
                else:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise Phase7GroundingStoreError(
                    f"Phase-7 {suffix} sidecar preflight failed"
                ) from exc
            raise Phase7GroundingStoreError(
                f"Phase-7 preflight rejects existing {suffix} sidecar"
            )

    @staticmethod
    def _fd_uri(descriptor: int, query: str) -> str:
        return f"file:/proc/self/fd/{descriptor}?{query}"

    @staticmethod
    def _identity_from_fd(descriptor: int) -> _FileIdentity:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or int(value.st_nlink) != 1:
            raise Phase7GroundingStoreError(
                "Phase-7 database must be a single-link regular file"
            )
        if stat.S_IMODE(value.st_mode) != 0o600:
            raise Phase7GroundingStoreError("Phase-7 database mode must be exactly 0600")
        return _FileIdentity.from_stat(value)

    @staticmethod
    def _entry_identity(parent_fd: int, name: str) -> _FileIdentity:
        try:
            value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise Phase7GroundingStoreError("Phase-7 database entry is unavailable") from exc
        if not stat.S_ISREG(value.st_mode) or int(value.st_nlink) != 1:
            raise Phase7GroundingStoreError(
                "Phase-7 database must be a single-link regular file"
            )
        if stat.S_IMODE(value.st_mode) != 0o600:
            raise Phase7GroundingStoreError("Phase-7 database mode must be exactly 0600")
        return _FileIdentity.from_stat(value)

    def _path_identity(self) -> _FileIdentity:
        try:
            value = os.lstat(self._path)
        except OSError as exc:
            raise Phase7GroundingStoreError("Phase-7 database path is unavailable") from exc
        if not stat.S_ISREG(value.st_mode) or int(value.st_nlink) != 1:
            raise Phase7GroundingStoreError(
                "Phase-7 database must be a single-link regular file"
            )
        if stat.S_IMODE(value.st_mode) != 0o600:
            raise Phase7GroundingStoreError("Phase-7 database mode must be exactly 0600")
        return _FileIdentity.from_stat(value)

    def _assert_path(
        self,
        database: OwnedDescriptor,
        owner: str,
        *,
        expected: _FileIdentity | None = None,
        exact: bool,
    ) -> _FileIdentity:
        anchored = self._identity_from_fd(database.fileno(owner))
        named = self._path_identity()
        if (anchored.device, anchored.inode) != (named.device, named.inode):
            raise Phase7GroundingStoreError(
                "Phase-7 path no longer names the anchored inode"
            )
        if exact and anchored != named:
            raise Phase7GroundingStoreError("Phase-7 path metadata changed")
        if expected is not None:
            if exact and anchored != expected:
                raise Phase7GroundingStoreError("Phase-7 anchored identity changed")
            if not exact and (anchored.device, anchored.inode) != (
                expected.device,
                expected.inode,
            ):
                raise Phase7GroundingStoreError("Phase-7 anchored inode changed")
        return anchored

    def _acquire_lock(self, descriptor: int, total_deadline: object | None = None) -> None:
        lock_deadline = time.monotonic() + _remaining_seconds(
            total_deadline,
            self._busy_timeout_ms / 1000.0,
            "phase7_store_lock_before",
        )
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError as exc:
                remaining = min(
                    lock_deadline - time.monotonic(),
                    _remaining_seconds(
                        total_deadline,
                        self._busy_timeout_ms / 1000.0,
                        "phase7_store_lock_wait",
                    ),
                )
                if remaining <= 0:
                    raise Phase7GroundingBusy("Phase-7 store lock deadline expired") from exc
                time.sleep(min(0.01, remaining))

    def _creation_binding(
        self, identity: _FileIdentity, parent_identity: tuple[int, int]
    ) -> dict[str, object]:
        return {
            "schema_version": "phase7-store-ownership-binding-v1",
            "absolute_path_sha256": canonical_sha256(
                {
                    "schema_version": "phase7-store-absolute-path-v1",
                    "absolute_path": str(self._path),
                }
            ),
            "created_device": identity.device,
            "created_inode": identity.inode,
            "created_size": identity.size,
            "parent_device": parent_identity[0],
            "parent_inode": parent_identity[1],
        }

    def _verify_schema(
        self,
        connection: sqlite3.Connection,
        identity: _FileIdentity,
        parent_identity: tuple[int, int],
    ) -> None:
        digest = canonical_sha256(_schema_inventory(connection))
        if digest != _EXPECTED_SCHEMA_DIGEST:
            raise Phase7GroundingStoreError("Phase-7 exact schema profile differs")
        rows = connection.execute(
            "SELECT * FROM phase7_shadow_schema_state ORDER BY singleton"
        ).fetchall()
        if len(rows) != 1:
            raise Phase7GroundingStoreError("Phase-7 ownership marker is unavailable")
        row = rows[0]
        if (
            row["singleton"] != 1
            or row["schema_version"] != PHASE7_STORE_SCHEMA
            or row["schema_digest_sha256"] != _EXPECTED_SCHEMA_DIGEST
        ):
            raise Phase7GroundingStoreError("Phase-7 ownership marker differs")
        binding = _decode_canonical_json(row["creation_binding_json"], "creation binding")
        expected = self._creation_binding(
            _FileIdentity(identity.device, identity.inode, 0, 0, 0),
            parent_identity,
        )
        if set(binding) != set(expected):
            raise Phase7GroundingStoreError("Phase-7 ownership binding is malformed")
        for field in expected:
            if field != "created_size" and binding[field] != expected[field]:
                raise Phase7GroundingStoreError("Phase-7 ownership binding differs")
        if binding["created_size"] != 0:
            raise Phase7GroundingStoreError(
                "Phase-7 store was not bound at exclusive creation"
            )

    def _open_verified(
        self,
        deadline: object | None = None,
    ) -> tuple[OwnedDescriptor, _FileIdentity, OwnedDescriptor, tuple[int, int]]:
        parent_owner = "phase7-preflight-parent"
        database_owner = "phase7-preflight-database"
        parent, parent_identity = self._open_parent(parent_owner)
        database: OwnedDescriptor | None = None
        readonly: sqlite3.Connection | None = None
        try:
            parent_fd = parent.fileno(parent_owner)
            database = OwnedDescriptor.from_opener(
                lambda: os.open(
                    self._path.name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                ),
                owner=database_owner,
                label="Phase-7 database",
            )
            self._acquire_lock(database.fileno(database_owner), deadline)
            self._assert_no_sidecars(parent_fd)
            before = self._assert_path(database, database_owner, exact=True)
            header = os.pread(database.fileno(database_owner), 100, 0)
            if (
                len(header) != 100
                or header[:16] != _SQLITE_HEADER
                or header[18] != 1
                or header[19] != 1
            ):
                raise Phase7GroundingStoreError(
                    "Phase-7 rollback-journal SQLite header is invalid"
                )
            readonly = sqlite3.connect(
                self._fd_uri(database.fileno(database_owner), "mode=ro&immutable=1"),
                uri=True,
                timeout=0,
            )
            readonly.row_factory = sqlite3.Row
            readonly.execute("PRAGMA query_only=ON")
            self._verify_schema(readonly, before, parent_identity)
            self._verify_integrity(readonly)
            readonly.close()
            readonly = None
            self._assert_path(database, database_owner, expected=before, exact=True)
            self._assert_parent(parent, parent_owner, parent_identity)
            self._assert_no_sidecars(parent_fd)
            return database, before, parent, parent_identity
        except BaseException as primary:
            callbacks = []
            if readonly is not None:
                callbacks.append(("close Phase-7 readonly preflight", RetryableCleanup(readonly.close)))
            if database is not None:
                callbacks.append(("close Phase-7 preflight database", database.cleanup(database_owner)))
            callbacks.append(("close Phase-7 preflight parent", parent.cleanup(parent_owner)))
            run_cleanup(callbacks, primary=primary)
            raise

    def _connect(self, deadline: object | None = None) -> _AnchoredConnection:
        database, identity, parent, parent_identity = self._open_verified(deadline)
        database_owner = database.owner
        parent_owner = parent.owner
        connection: _AnchoredConnection | None = None
        try:
            sqlite_timeout = _remaining_seconds(
                deadline,
                self._busy_timeout_ms / 1000.0,
                "phase7_sqlite_connect_before",
            )
            connection = sqlite3.connect(
                self._fd_uri(database.fileno(database_owner), "mode=rw"),
                uri=True,
                timeout=sqlite_timeout,
                factory=_AnchoredConnection,
            )
            connection._adopt_anchors(
                database=database,
                database_owner=database_owner,
                parent=parent,
                parent_owner=parent_owner,
            )
            connection._identity = identity
            connection._parent_identity = parent_identity
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={max(1, int(sqlite_timeout * 1000))}")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise Phase7GroundingStoreError("Phase-7 journal mode differs")
            connection.execute("PRAGMA synchronous=FULL")
            self._assert_connection(connection, exact=True)
            self._verify_schema(connection, identity, parent_identity)
            self._verify_integrity(connection)
            return connection
        except BaseException as primary:
            callbacks = []
            if connection is not None:
                callbacks.append(("close failed Phase-7 connection", RetryableCleanup(connection.close)))
            callbacks.extend(
                [
                    ("close untransferred Phase-7 database", database.cleanup(database_owner)),
                    ("close untransferred Phase-7 parent", parent.cleanup(parent_owner)),
                ]
            )
            run_cleanup(callbacks, primary=primary)
            raise

    def _assert_connection(
        self,
        connection: _AnchoredConnection,
        *,
        exact: bool,
        require_no_sidecars: bool = True,
    ) -> None:
        if (
            type(connection) is not _AnchoredConnection
            or connection._database_anchor is None
            or connection._parent_anchor is None
            or connection._identity is None
            or connection._parent_identity is None
        ):
            raise Phase7GroundingStoreError("Phase-7 connection ownership is incomplete")
        self._assert_path(
            connection._database_anchor,
            _CONNECTION_OWNER,
            expected=connection._identity,
            exact=exact,
        )
        self._assert_parent(
            connection._parent_anchor,
            _CONNECTION_OWNER,
            connection._parent_identity,
        )
        if require_no_sidecars:
            self._assert_no_sidecars(
                connection._parent_anchor.fileno(_CONNECTION_OWNER)
            )

    def _begin(
        self, connection: _AnchoredConnection, deadline: object | None = None
    ) -> None:
        _deadline_check(deadline, "phase7_sqlite_begin_before")
        self._assert_connection(connection, exact=True)
        self._verify_integrity(connection)
        connection.execute("BEGIN IMMEDIATE")
        self._assert_connection(connection, exact=True, require_no_sidecars=False)

    def _commit(
        self, connection: _AnchoredConnection, deadline: object | None = None
    ) -> None:
        _deadline_check(deadline, "phase7_sqlite_commit_before")
        self._assert_connection(connection, exact=False, require_no_sidecars=False)
        try:
            connection.commit()
            connection._commit_completed = True
        except BaseException:
            if not connection.in_transaction:
                connection._commit_completed = True
            raise
        self._assert_connection(connection, exact=False)

    def _verify_integrity(self, connection: sqlite3.Connection) -> None:
        check = connection.execute("PRAGMA integrity_check").fetchall()
        if [tuple(row) for row in check] != [("ok",)]:
            raise Phase7GroundingStoreError("Phase-7 SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise Phase7GroundingStoreError("Phase-7 foreign-key integrity differs")

        receipts: dict[str, tuple[dict[str, object], PreparedGroundingBundle]] = {}
        for row in connection.execute(
            "SELECT * FROM phase7_shadow_grounding_receipts ORDER BY receipt_sha256"
        ).fetchall():
            wire = _decode_canonical_json(row["receipt_json"], "grounding receipt")
            canonical, rebuilt = _rebuild_stored_receipt(wire)
            if (
                row["receipt_sha256"] != rebuilt.receipt_sha256
                or row["scope_key"] != rebuilt.scope_key
                or row["occurrence_id"] != rebuilt.occurrence_id
                or row["authority_revision"] != rebuilt.authority_revision
            ):
                raise Phase7GroundingStoreError("grounding receipt row differs")
            receipts[rebuilt.receipt_sha256] = (canonical, rebuilt)

        effectives: dict[str, dict[str, object]] = {}
        for row in connection.execute(
            "SELECT * FROM phase7_shadow_effective_verdicts "
            "ORDER BY effective_verdict_sha256"
        ).fetchall():
            receipt_pair = receipts.get(str(row["receipt_sha256"]))
            if receipt_pair is None:
                raise Phase7GroundingStoreError("effective verdict has no receipt")
            effective = _verify_effective(
                _decode_canonical_json(row["verdict_json"], "effective verdict"),
                receipt_pair[1],
            )
            digest = str(effective["effective_verdict_sha256"])
            if row["effective_verdict_sha256"] != digest:
                raise Phase7GroundingStoreError("effective verdict row differs")
            effectives[digest] = effective

        requests: dict[str, tuple[dict[str, object], str]] = {}
        for row in connection.execute(
            "SELECT * FROM phase7_shadow_idempotency ORDER BY idempotency_key"
        ).fetchall():
            raw = _decode_canonical_json(row["request_json"], "idempotency request")
            receipt_sha = raw.get("grounding_receipt_sha256")
            pair = receipts.get(str(receipt_sha))
            if pair is None:
                raise Phase7GroundingStoreError("idempotency request has no receipt")
            request = _verify_request(raw, rebuilt=pair[1])
            request_sha = canonical_sha256(request)
            if (
                row["idempotency_key"] != request["idempotency_key"]
                or row["request_sha256"] != request_sha
            ):
                raise Phase7GroundingStoreError("idempotency request row differs")
            requests[str(request["idempotency_key"])] = (
                request,
                str(row["commit_sha256"]),
            )

        commits: dict[str, tuple[dict[str, object], PreparedGroundingBundle]] = {}
        chains: dict[str, list[dict[str, object]]] = {}
        for row in connection.execute(
            "SELECT * FROM phase7_shadow_commits ORDER BY scope_key,sequence"
        ).fetchall():
            pair = receipts.get(str(row["receipt_sha256"]))
            if pair is None:
                raise Phase7GroundingStoreError("commit has no receipt")
            request_pair = requests.get(str(row["idempotency_key"]))
            if request_pair is None:
                raise Phase7GroundingStoreError("commit has no idempotency request")
            request, request_commit = request_pair
            commit = _verify_commit(
                _decode_canonical_json(row["commit_json"], "grounding commit"),
                request=request,
                rebuilt=pair[1],
            )
            digest = str(commit["commit_sha256"])
            if (
                row["commit_sha256"] != digest
                or row["scope_key"] != commit["scope_key"]
                or row["sequence"] != commit["sequence"]
                or row["previous_commit_sha256"] != commit["previous_commit_sha256"]
                or row["request_sha256"] != commit["request_sha256"]
                or row["effective_verdict_sha256"]
                != commit["effective_verdict_sha256"]
                or row["occurrence_id"] != commit["occurrence_id"]
                or row["authority_revision"] != commit["authority_revision"]
                or request_commit != digest
                or commit["effective_verdict_sha256"] not in effectives
            ):
                raise Phase7GroundingStoreError("grounding commit row differs")
            commits[digest] = (commit, pair[1])
            chains.setdefault(str(commit["scope_key"]), []).append(commit)

        if len(commits) != len(requests):
            raise Phase7GroundingStoreError("commit/idempotency cardinality differs")
        for chain in chains.values():
            for index, commit in enumerate(chain):
                expected_previous = None if index == 0 else chain[index - 1]["commit_sha256"]
                if (
                    commit["sequence"] != index + 1
                    or commit["previous_commit_sha256"] != expected_previous
                ):
                    raise Phase7GroundingStoreError("grounding commit chain differs")
                if index:
                    previous = chain[index - 1]
                    if commit["authority_revision"] < previous["authority_revision"]:
                        raise Phase7GroundingStoreError("grounding revision chain regressed")
                    if (
                        commit["authority_revision"] == previous["authority_revision"]
                        and commit["occurrence_id"] != previous["occurrence_id"]
                    ):
                        raise Phase7GroundingStoreError(
                            "same grounding revision changed occurrence"
                        )

        current_rows = connection.execute(
            "SELECT * FROM phase7_shadow_current ORDER BY scope_key"
        ).fetchall()
        if len(current_rows) != len(chains):
            raise Phase7GroundingStoreError("grounding current cardinality differs")
        for row in current_rows:
            chain = chains.get(str(row["scope_key"]))
            if not chain:
                raise Phase7GroundingStoreError("grounding current has no chain")
            head = chain[-1]
            if any(
                row[field] != head[source]
                for field, source in (
                    ("scope_key", "scope_key"),
                    ("sequence", "sequence"),
                    ("commit_sha256", "commit_sha256"),
                    ("receipt_sha256", "grounding_receipt_sha256"),
                    ("effective_verdict_sha256", "effective_verdict_sha256"),
                    ("occurrence_id", "occurrence_id"),
                    ("authority_revision", "authority_revision"),
                )
            ):
                raise Phase7GroundingStoreError("grounding current projection differs")

    def _cleanup_uncommitted(self, cleanup_fd: int, expected: _FileIdentity) -> None:
        parent_owner = "phase7-cleanup-parent"
        parent, parent_identity = self._open_parent(parent_owner)
        try:
            parent_fd = parent.fileno(parent_owner)
            self._assert_parent(parent, parent_owner, parent_identity)
            anchored = self._identity_from_fd(cleanup_fd)
            named = self._entry_identity(parent_fd, self._path.name)
            if (anchored.device, anchored.inode) != (expected.device, expected.inode) or (
                named.device,
                named.inode,
            ) != (expected.device, expected.inode):
                raise Phase7GroundingStoreError(
                    "refusing to clean a replaced Phase-7 database"
                )
            for suffix in _SIDECAR_SUFFIXES:
                name = f"{self._path.name}{suffix}"
                try:
                    sidecar = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(sidecar.st_mode):
                    raise Phase7GroundingStoreError(
                        "refusing to clean a non-regular Phase-7 sidecar"
                    )
                resilient_unlink_at(parent_fd, name)
            resilient_unlink_at(parent_fd, self._path.name)
            os.fsync(parent_fd)
        finally:
            run_cleanup([("close Phase-7 cleanup parent", parent.cleanup(parent_owner))])

    def _initialize_new(
        self,
        deadline: object | None = None,
        fence_hook: AdapterFence | None = None,
    ) -> None:
        _deadline_check(deadline, "phase7_initialize_before")
        _fence(fence_hook, "phase7_initialize_before")
        parent_owner = "phase7-initialize-parent"
        creator_owner = "phase7-initialize-creator"
        cleanup_owner = "phase7-initialize-cleanup"
        parent, parent_identity = self._open_parent(parent_owner)
        creator_raw = -1
        creator: OwnedDescriptor | None = None
        cleanup: OwnedDescriptor | None = None
        connection: _AnchoredConnection | None = None
        created: _FileIdentity | None = None
        committed = False
        try:
            parent_fd = parent.fileno(parent_owner)
            # Serialize the missing-path check, exclusive creation, SQLite
            # rollback-journal initialization and final parent fsync.  Locking
            # only the newly-created database inode leaves a normal race in
            # which another initializer can observe SQLite's transient
            # ``-journal`` sidecar before it can acquire that inode lock.
            self._acquire_lock(parent_fd, deadline)
            self._assert_parent(parent, parent_owner, parent_identity)
            self._assert_no_sidecars(parent_fd)
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                creator_raw = os.open(
                    self._path.name,
                    flags,
                    0o600,
                    dir_fd=parent_fd,
                )
                creator = OwnedDescriptor(
                    creator_raw,
                    owner=creator_owner,
                    label="Phase-7 creator",
                )
            except FileExistsError as exc:
                raise _Phase7InitializationRace(
                    "Phase-7 path appeared during exclusive creation"
                ) from exc
            self._acquire_lock(creator.fileno(creator_owner), deadline)
            created = self._assert_path(creator, creator_owner, exact=True)
            entry = self._entry_identity(parent_fd, self._path.name)
            if (created.device, created.inode) != (entry.device, entry.inode):
                raise Phase7GroundingStoreError(
                    "exclusive Phase-7 directory entry identity differs"
                )
            if created.size != 0:
                raise Phase7GroundingStoreError("new Phase-7 database is not empty")
            _phase7_failure_point("after_exclusive_create")
            cleanup = creator.duplicate(
                owner=creator_owner,
                new_owner=cleanup_owner,
                label="Phase-7 cleanup anchor",
            )
            _phase7_failure_point("after_cleanup_dup")
            connection = sqlite3.connect(
                self._fd_uri(creator.fileno(creator_owner), "mode=rw"),
                uri=True,
                timeout=_remaining_seconds(
                    deadline,
                    self._busy_timeout_ms / 1000.0,
                    "phase7_initialize_connect_before",
                ),
                factory=_AnchoredConnection,
            )
            _phase7_failure_point("after_sqlite_connect_before_transfer")
            connection._adopt_anchors(
                database=creator,
                database_owner=creator_owner,
                parent=parent,
                parent_owner=parent_owner,
            )
            connection._identity = created
            connection._parent_identity = parent_identity
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            digest = _create_schema(connection)
            if digest != _EXPECTED_SCHEMA_DIGEST:
                raise Phase7GroundingStoreError("new Phase-7 schema profile differs")
            binding = self._creation_binding(created, parent_identity)
            connection.execute(
                """
                INSERT INTO phase7_shadow_schema_state(
                    singleton,schema_version,store_instance_id,
                    creation_binding_json,schema_digest_sha256
                ) VALUES(1,?,?,?,?)
                """,
                (
                    PHASE7_STORE_SCHEMA,
                    canonical_sha256(binding),
                    _canonical_json(binding),
                    digest,
                ),
            )
            _phase7_failure_point("before_initialize_commit")
            self._commit(connection, deadline)
            committed = connection._commit_completed
            _phase7_failure_point("before_parent_fsync")
            os.fsync(connection._parent_anchor.fileno(_CONNECTION_OWNER))
            _phase7_failure_point("after_parent_fsync")
            connection.close()
            connection = None
            _fence(fence_hook, "phase7_initialize_after_commit")
        except BaseException as primary:
            if connection is not None:
                committed = connection._commit_completed
                _cleanup_failed_connection(
                    connection,
                    primary=primary,
                    label="Phase-7 initialization connection",
                )
            callbacks = []
            cleanup_fd: int | None = None
            if cleanup is not None and not cleanup.closed:
                cleanup_fd = cleanup.fileno(cleanup_owner)
            elif creator is not None and not creator.closed and creator.owner == creator_owner:
                cleanup_fd = creator.fileno(creator_owner)
            elif creator_raw >= 0:
                try:
                    self._identity_from_fd(creator_raw)
                    cleanup_fd = creator_raw
                except BaseException:
                    cleanup_fd = None
            if not committed and cleanup_fd is not None:
                if created is None:
                    try:
                        created = self._identity_from_fd(cleanup_fd)
                    except BaseException as probe_error:
                        def report_probe(error: BaseException = probe_error) -> None:
                            raise error

                        run_cleanup(
                            [("identify failed Phase-7 creation", report_probe)],
                            primary=primary,
                        )
                if created is not None:
                    callbacks.append(
                        (
                            "unlink failed Phase-7 exclusive creation",
                            lambda cleanup_fd=cleanup_fd, created=created: self._cleanup_uncommitted(
                                cleanup_fd, created
                            ),
                        )
                    )
            if cleanup is not None:
                callbacks.append(("close Phase-7 cleanup anchor", cleanup.cleanup(cleanup_owner)))
            if creator is not None:
                callbacks.append(("close Phase-7 creator", creator.cleanup(creator_owner)))
            if creator_raw >= 0:
                callbacks.append(
                    (
                        "close raw Phase-7 creator",
                        lambda: close_raw_descriptor_if_unowned(
                            creator_raw, creator, cleanup
                        ),
                    )
                )
            callbacks.append(("close Phase-7 initialize parent", parent.cleanup(parent_owner)))
            run_cleanup(callbacks, primary=primary)
            raise
        else:
            callbacks = []
            if cleanup is not None:
                callbacks.append(("close Phase-7 cleanup anchor", cleanup.cleanup(cleanup_owner)))
            if creator is not None:
                callbacks.append(("close Phase-7 creator", creator.cleanup(creator_owner)))
            if creator_raw >= 0:
                callbacks.append(
                    (
                        "close raw Phase-7 creator",
                        lambda: close_raw_descriptor_if_unowned(
                            creator_raw, creator, cleanup
                        ),
                    )
                )
            callbacks.append(("close Phase-7 initialize parent", parent.cleanup(parent_owner)))
            run_cleanup(callbacks)

    def initialize(
        self,
        *,
        deadline: object | None = None,
        fence_hook: AdapterFence | None = None,
    ) -> None:
        _deadline_check(deadline, "phase7_initialize_entry")
        _fence(fence_hook, "phase7_initialize_entry")
        self._validate_parent()
        if os.path.lexists(self._path):
            database, _, parent, _ = self._open_verified(deadline)
            run_cleanup(
                [
                    ("close verified Phase-7 database", database.cleanup(database.owner)),
                    ("close verified Phase-7 parent", parent.cleanup(parent.owner)),
                ]
            )
            _fence(fence_hook, "phase7_initialize_verified")
            return
        try:
            self._initialize_new(deadline, fence_hook)
        except _Phase7InitializationRace:
            if not os.path.lexists(self._path):
                raise
            # A normal concurrent initializer may have won O_EXCL.  Trust it
            # only after the full independent preflight below.
        database, _, parent, _ = self._open_verified(deadline)
        run_cleanup(
            [
                ("close initialized Phase-7 database", database.cleanup(database.owner)),
                ("close initialized Phase-7 parent", parent.cleanup(parent.owner)),
            ]
        )
        _fence(fence_hook, "phase7_initialize_verified")

    @staticmethod
    def _check_current_head(
        rebuilt: PreparedGroundingBundle,
        verifier: CurrentHeadVerifier | None,
    ) -> None:
        if not callable(verifier):
            raise Phase7GroundingContractError(
                "current_head_verifier is required for current Phase-7 state"
            )
        identity = rebuilt.receipt["input_identity"]
        try:
            current = verifier(
                identity["phase3_artifact_state"],
                identity["phase3_artifact_occurrence"],
                identity["phase6_access_proof"],
            )
        except Phase7GroundingError:
            raise
        except Exception as exc:
            if getattr(exc, "code", None) in {
                "PHASE78_DEADLINE_EXCEEDED",
                "PHASE78_REQUEST_CANCELLED",
            }:
                # The outer adapter owns timeout/cancellation classification.
                # Preserve that exact object so Web/CLI do not misreport a
                # shared-budget exhaustion as source-head drift.
                raise
            raise Phase7GroundingCurrentConflict(
                "current-head verification failed closed"
            ) from exc
        if current is not True:
            raise Phase7GroundingCurrentConflict(
                "captured Phase-3/Phase-6 heads are no longer current"
            )

    @staticmethod
    def _joined_row(
        connection: sqlite3.Connection,
        *,
        idempotency_key: str | None = None,
        scope_key: str | None = None,
    ) -> sqlite3.Row | None:
        if (idempotency_key is None) == (scope_key is None):
            raise Phase7GroundingStoreError("exactly one bundle lookup key is required")
        where = "i.idempotency_key=?" if idempotency_key is not None else "c.scope_key=? AND cur.commit_sha256=c.commit_sha256"
        key = idempotency_key if idempotency_key is not None else scope_key
        return connection.execute(
            f"""
            SELECT c.*,i.request_json,r.receipt_json,e.verdict_json,
                   CASE WHEN cur.commit_sha256=c.commit_sha256 THEN 1 ELSE 0 END AS is_current
            FROM phase7_shadow_commits AS c
            JOIN phase7_shadow_idempotency AS i
              ON i.commit_sha256=c.commit_sha256
            JOIN phase7_shadow_grounding_receipts AS r
              ON r.receipt_sha256=c.receipt_sha256
            JOIN phase7_shadow_effective_verdicts AS e
              ON e.effective_verdict_sha256=c.effective_verdict_sha256
            LEFT JOIN phase7_shadow_current AS cur
              ON cur.scope_key=c.scope_key
            WHERE {where}
            """,
            (key,),
        ).fetchone()

    @staticmethod
    def _loaded_bundle(row: sqlite3.Row, *, replayed: bool) -> dict[str, object]:
        receipt, rebuilt = _rebuild_stored_receipt(
            _decode_canonical_json(row["receipt_json"], "loaded grounding receipt")
        )
        effective = _verify_effective(
            _decode_canonical_json(row["verdict_json"], "loaded effective verdict"),
            rebuilt,
        )
        request = _verify_request(
            _decode_canonical_json(row["request_json"], "loaded request"),
            rebuilt=rebuilt,
        )
        commit = _verify_commit(
            _decode_canonical_json(row["commit_json"], "loaded commit"),
            request=request,
            rebuilt=rebuilt,
        )
        current = bool(row["is_current"])
        result = GroundingCommitResult(
            idempotency_key=str(commit["idempotency_key"]),
            scope_key=str(commit["scope_key"]),
            sequence=int(commit["sequence"]),
            previous_commit_sha256=commit["previous_commit_sha256"],
            request_sha256=str(commit["request_sha256"]),
            receipt_sha256=str(commit["grounding_receipt_sha256"]),
            effective_verdict_sha256=str(commit["effective_verdict_sha256"]),
            commit_sha256=str(commit["commit_sha256"]),
            occurrence_id=str(commit["occurrence_id"]),
            authority_revision=int(commit["authority_revision"]),
            aggregate_verdict=str(effective["aggregate_verdict"]),
            aggregate_action=str(effective["aggregate_action"]),
            grounding_valid=bool(receipt["grounding_valid"]),
            replayed=replayed,
            current=current,
        )
        return {
            "schema_version": "phase7-grounding-loaded-bundle-v1",
            "request": request,
            "receipt": receipt,
            "effective_verdict": effective,
            "commit": commit,
            "result": result.as_dict(),
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }

    def record_grounding_bundle(
        self,
        *,
        idempotency_key: str,
        phase3_artifact_state: object,
        phase3_artifact_occurrence: object,
        phase6_access_proof: object,
        role_output_bytes: Mapping[str, bytes | None],
        manifest_bytes: Mapping[str, bytes | None],
        context_bytes: Mapping[str, bytes | None],
        current_head_verifier: CurrentHeadVerifier | None,
        deadline: object | None = None,
        fence_hook: AdapterFence | None = None,
    ) -> GroundingCommitResult:
        prepared = prepare_grounding_bundle(
            idempotency_key=idempotency_key,
            phase3_artifact_state=phase3_artifact_state,
            phase3_artifact_occurrence=phase3_artifact_occurrence,
            phase6_access_proof=phase6_access_proof,
            role_output_bytes=role_output_bytes,
            manifest_bytes=manifest_bytes,
            context_bytes=context_bytes,
            deadline=deadline,
            fence_hook=fence_hook,
        )
        self.initialize(deadline=deadline, fence_hook=fence_hook)
        _deadline_check(deadline, "phase7_transaction_before")
        _fence(fence_hook, "phase7_transaction_before")
        connection = self._connect(deadline)
        loaded_after_commit: dict[str, object] | None = None
        try:
            self._begin(connection, deadline)
            existing = self._joined_row(
                connection, idempotency_key=idempotency_key
            )
            if existing is not None:
                stored_request = _decode_canonical_json(
                    existing["request_json"], "existing idempotency request"
                )
                if (
                    existing["request_sha256"] != prepared.request_sha256
                    or stored_request != prepared.request
                ):
                    raise Phase7GroundingReplayConflict(
                        "idempotency key is already bound to different exact bytes"
                    )
                loaded_after_commit = self._loaded_bundle(existing, replayed=True)
                connection.rollback()
                connection.close()
                connection = None  # type: ignore[assignment]
                _deadline_check(deadline, "phase7_replay_after")
                _fence(fence_hook, "phase7_replay_after")
                result_wire = loaded_after_commit["result"]
                return GroundingCommitResult(
                    **{
                        field: result_wire[field]
                        for field in GroundingCommitResult.__dataclass_fields__
                    }
                )

            self._check_current_head(prepared, current_head_verifier)
            current = connection.execute(
                "SELECT * FROM phase7_shadow_current WHERE scope_key=?",
                (prepared.scope_key,),
            ).fetchone()
            if current is None:
                sequence = 1
                previous = None
            else:
                prior_revision = int(current["authority_revision"])
                if prepared.authority_revision < prior_revision:
                    raise Phase7GroundingCurrentConflict(
                        "Phase-7 source revision would regress the current chain"
                    )
                if (
                    prepared.authority_revision == prior_revision
                    and prepared.occurrence_id != current["occurrence_id"]
                ):
                    raise Phase7GroundingCurrentConflict(
                        "same Phase-7 source revision changed occurrence identity"
                    )
                sequence = int(current["sequence"]) + 1
                previous = str(current["commit_sha256"])

            connection.execute(
                """
                INSERT OR IGNORE INTO phase7_shadow_grounding_receipts(
                    receipt_sha256,scope_key,occurrence_id,authority_revision,receipt_json
                ) VALUES(?,?,?,?,?)
                """,
                (
                    prepared.receipt_sha256,
                    prepared.scope_key,
                    prepared.occurrence_id,
                    prepared.authority_revision,
                    _canonical_json(prepared.receipt),
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO phase7_shadow_effective_verdicts(
                    effective_verdict_sha256,receipt_sha256,verdict_json
                ) VALUES(?,?,?)
                """,
                (
                    prepared.effective_verdict_sha256,
                    prepared.receipt_sha256,
                    _canonical_json(prepared.effective_verdict),
                ),
            )
            commit_body: dict[str, object] = {
                "schema_version": PHASE7_COMMIT_SCHEMA,
                "scope_key": prepared.scope_key,
                "sequence": sequence,
                "previous_commit_sha256": previous,
                "idempotency_key": idempotency_key,
                "request_sha256": prepared.request_sha256,
                "grounding_receipt_sha256": prepared.receipt_sha256,
                "effective_verdict_sha256": prepared.effective_verdict_sha256,
                "occurrence_id": prepared.occurrence_id,
                "authority_revision": prepared.authority_revision,
                "authoritative": False,
                "authority_transferred": False,
                "dispatch_performed": False,
            }
            commit_sha = canonical_sha256(commit_body)
            commit = {**commit_body, "commit_sha256": commit_sha}
            connection.execute(
                """
                INSERT INTO phase7_shadow_commits(
                    commit_sha256,scope_key,sequence,previous_commit_sha256,
                    idempotency_key,request_sha256,receipt_sha256,
                    effective_verdict_sha256,occurrence_id,authority_revision,commit_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    commit_sha,
                    prepared.scope_key,
                    sequence,
                    previous,
                    idempotency_key,
                    prepared.request_sha256,
                    prepared.receipt_sha256,
                    prepared.effective_verdict_sha256,
                    prepared.occurrence_id,
                    prepared.authority_revision,
                    _canonical_json(commit),
                ),
            )
            connection.execute(
                """
                INSERT INTO phase7_shadow_idempotency(
                    idempotency_key,request_json,request_sha256,commit_sha256
                ) VALUES(?,?,?,?)
                """,
                (
                    idempotency_key,
                    _canonical_json(prepared.request),
                    prepared.request_sha256,
                    commit_sha,
                ),
            )
            connection.execute(
                """
                INSERT INTO phase7_shadow_current(
                    scope_key,sequence,commit_sha256,receipt_sha256,
                    effective_verdict_sha256,occurrence_id,authority_revision
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    sequence=excluded.sequence,
                    commit_sha256=excluded.commit_sha256,
                    receipt_sha256=excluded.receipt_sha256,
                    effective_verdict_sha256=excluded.effective_verdict_sha256,
                    occurrence_id=excluded.occurrence_id,
                    authority_revision=excluded.authority_revision
                """,
                (
                    prepared.scope_key,
                    sequence,
                    commit_sha,
                    prepared.receipt_sha256,
                    prepared.effective_verdict_sha256,
                    prepared.occurrence_id,
                    prepared.authority_revision,
                ),
            )
            _phase7_failure_point("after_current_before_cas")
            self._verify_integrity(connection)
            self._check_current_head(prepared, current_head_verifier)
            _deadline_check(deadline, "phase7_commit_fence")
            _fence(fence_hook, "phase7_commit_before")
            _phase7_failure_point("before_commit")
            self._commit(connection, deadline)
            _phase7_failure_point("after_commit")
            row = self._joined_row(connection, idempotency_key=idempotency_key)
            if row is None:
                raise Phase7GroundingStoreError("committed Phase-7 bundle is unavailable")
            loaded_after_commit = self._loaded_bundle(row, replayed=False)
            connection.close()
            connection = None  # type: ignore[assignment]
        except BaseException as primary:
            if connection is not None:
                _cleanup_failed_connection(
                    connection,
                    primary=primary,
                    label="Phase-7 grounding transaction",
                )
            raise
        assert loaded_after_commit is not None
        # A timeout/generation fence here deliberately leaves the durable
        # idempotency fact replayable on the caller's next attempt.
        _deadline_check(deadline, "phase7_commit_after")
        _fence(fence_hook, "phase7_commit_after")
        result_wire = loaded_after_commit["result"]
        return GroundingCommitResult(
            **{
                field: result_wire[field]
                for field in GroundingCommitResult.__dataclass_fields__
            }
        )

    def load_bundle(
        self,
        idempotency_key: str,
        *,
        deadline: object | None = None,
        fence_hook: AdapterFence | None = None,
    ) -> dict[str, object]:
        key = _identifier(idempotency_key, "idempotency_key")
        _deadline_check(deadline, "phase7_load_before")
        _fence(fence_hook, "phase7_load_before")
        self.initialize(deadline=deadline, fence_hook=fence_hook)
        connection = self._connect(deadline)
        try:
            row = self._joined_row(connection, idempotency_key=key)
            if row is None:
                raise Phase7GroundingNotFound("Phase-7 idempotency fact is unavailable")
            bundle = self._loaded_bundle(row, replayed=True)
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection, primary=primary, label="Phase-7 bundle load"
            )
            raise
        _deadline_check(deadline, "phase7_load_after")
        _fence(fence_hook, "phase7_load_after")
        return bundle

    def load(
        self,
        idempotency_key: str,
        *,
        deadline: object | None = None,
        fence_hook: AdapterFence | None = None,
    ) -> GroundingCommitResult:
        wire = self.load_bundle(
            idempotency_key, deadline=deadline, fence_hook=fence_hook
        )["result"]
        return GroundingCommitResult(
            **{
                field: wire[field]
                for field in GroundingCommitResult.__dataclass_fields__
            }
        )

    def load_current_bundle(
        self,
        scope_key: str,
        *,
        current_head_verifier: CurrentHeadVerifier | None,
        deadline: object | None = None,
        fence_hook: AdapterFence | None = None,
    ) -> dict[str, object]:
        key = _sha(scope_key, "scope_key")
        _deadline_check(deadline, "phase7_current_load_before")
        _fence(fence_hook, "phase7_current_load_before")
        self.initialize(deadline=deadline, fence_hook=fence_hook)
        connection = self._connect(deadline)
        try:
            row = self._joined_row(connection, scope_key=key)
            if row is None:
                raise Phase7GroundingNotFound("Phase-7 current projection is unavailable")
            bundle = self._loaded_bundle(row, replayed=False)
            receipt = bundle["receipt"]
            _, rebuilt = _rebuild_stored_receipt(receipt)
            self._check_current_head(rebuilt, current_head_verifier)
            connection.close()
        except BaseException as primary:
            _cleanup_failed_connection(
                connection, primary=primary, label="Phase-7 current bundle load"
            )
            raise
        _deadline_check(deadline, "phase7_current_load_after")
        _fence(fence_hook, "phase7_current_load_after")
        return bundle

    def load_current(
        self,
        scope_key: str,
        *,
        current_head_verifier: CurrentHeadVerifier | None,
        deadline: object | None = None,
        fence_hook: AdapterFence | None = None,
    ) -> GroundingCommitResult:
        wire = self.load_current_bundle(
            scope_key,
            current_head_verifier=current_head_verifier,
            deadline=deadline,
            fence_hook=fence_hook,
        )["result"]
        return GroundingCommitResult(
            **{
                field: wire[field]
                for field in GroundingCommitResult.__dataclass_fields__
            }
        )


@dataclass(frozen=True)
class Phase7ShadowRun:
    schema_version: str
    enabled: bool
    store_verified: bool
    result: dict[str, object] | None
    authoritative: bool
    authority_transferred: bool
    dispatch_performed: bool
    run_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "store_verified": self.store_verified,
            "result": self.result,
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
            "run_sha256": self.run_sha256,
        }


def _run_result(
    *, enabled: bool, verified: bool, result: GroundingCommitResult | None
) -> Phase7ShadowRun:
    body: dict[str, object] = {
        "schema_version": PHASE7_RUN_SCHEMA,
        "enabled": enabled,
        "store_verified": verified,
        "result": None if result is None else result.as_dict(),
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    return Phase7ShadowRun(
        schema_version=PHASE7_RUN_SCHEMA,
        enabled=enabled,
        store_verified=verified,
        result=body["result"],
        authoritative=False,
        authority_transferred=False,
        dispatch_performed=False,
        run_sha256=canonical_sha256(body),
    )


def run_phase7_grounding_shadow(
    *,
    enabled: bool = PHASE7_GROUNDING_DEFAULT_ENABLED,
    database: str | Path | None = None,
    idempotency_key: str | None = None,
    phase3_artifact_state: object = None,
    phase3_artifact_occurrence: object = None,
    phase6_access_proof: object = None,
    role_output_bytes: Mapping[str, bytes | None] | None = None,
    manifest_bytes: Mapping[str, bytes | None] | None = None,
    context_bytes: Mapping[str, bytes | None] | None = None,
    current_head_verifier: CurrentHeadVerifier | None = None,
    busy_timeout_ms: int = 5_000,
    deadline: object | None = None,
    fence_hook: AdapterFence | None = None,
) -> Phase7ShadowRun:
    """Run the durable shadow only after explicit enablement.

    The disabled return occurs before path construction, Phase-3/6 parsing,
    byte validation, deadline callbacks, SQLite, or any source read.
    """

    if type(enabled) is not bool:
        raise Phase7GroundingContractError("enabled must be a boolean")
    if not enabled:
        return _run_result(enabled=False, verified=False, result=None)
    if database is None or idempotency_key is None:
        raise Phase7GroundingContractError(
            "enabled Phase-7 shadow requires database and idempotency_key"
        )
    if role_output_bytes is None or manifest_bytes is None or context_bytes is None:
        raise Phase7GroundingContractError(
            "enabled Phase-7 shadow requires all three role byte mappings"
        )
    store = Phase7GroundingStore(database, busy_timeout_ms=busy_timeout_ms)
    result = store.record_grounding_bundle(
        idempotency_key=idempotency_key,
        phase3_artifact_state=phase3_artifact_state,
        phase3_artifact_occurrence=phase3_artifact_occurrence,
        phase6_access_proof=phase6_access_proof,
        role_output_bytes=role_output_bytes,
        manifest_bytes=manifest_bytes,
        context_bytes=context_bytes,
        current_head_verifier=current_head_verifier,
        deadline=deadline,
        fence_hook=fence_hook,
    )
    return _run_result(enabled=True, verified=True, result=result)


__all__ = [
    "AdapterFence",
    "CurrentHeadVerifier",
    "GroundingCommitResult",
    "PHASE7_AGGREGATE_POLICY_SCHEMA",
    "PHASE7_EFFECTIVE_VERDICT_SCHEMA",
    "PHASE7_GROUNDING_DEFAULT_ENABLED",
    "PHASE7_RECEIPT_SCHEMA",
    "PHASE7_ROLE_RECEIPT_SCHEMA",
    "PHASE7_RUN_SCHEMA",
    "Phase7GroundingBusy",
    "Phase7GroundingContractError",
    "Phase7GroundingCurrentConflict",
    "Phase7GroundingDisabled",
    "Phase7GroundingError",
    "Phase7GroundingNotFound",
    "Phase7GroundingReplayConflict",
    "Phase7GroundingStore",
    "Phase7GroundingStoreError",
    "Phase7ShadowRun",
    "PreparedGroundingBundle",
    "prepare_grounding_bundle",
    "run_phase7_grounding_shadow",
]
