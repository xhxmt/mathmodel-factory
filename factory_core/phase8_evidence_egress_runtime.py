"""Default-off durable Phase-8 reference/egress shadow runtime.

Immutable reference, approval and decision facts are written and re-read from
an explicit CAS before one SQLite transaction publishes history/current rows.
The runtime validates complete Phase-3/6/7 identities but never grants
Authority authority and never dispatches a transfer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
from typing import Callable, Mapping, NoReturn

from factory_core import authority_read_repository as authority_read
from factory_core import phase6_snapshot_grants as phase6
from factory_core.canonical import CanonicalizationError, canonical_bytes, canonical_sha256
from factory_core.data_egress import (
    DATA_EGRESS_APPROVAL_SCHEMA,
    DataEgressError,
    data_egress_policy_sha256,
    evaluate_data_egress,
)
from factory_core.fd_ownership import OwnedDescriptor, RetryableCleanup, resilient_unlink_at, run_cleanup
from factory_core.phase78_deadline import Phase78OutcomeUncertain
from factory_core.phase3_artifacts import (
    ArtifactLedgerOccurrence,
    Phase3ContractError,
    artifact_occurrence_from_dict,
    validate_artifact_occurrence,
)
from factory_core.reference_materializer import (
    CasBlobFact,
    ReferenceCas,
    load_reference_package,
)


PHASE8_DEFAULT_ENABLED = False
PHASE8_STORE_SCHEMA = "phase8-evidence-egress-runtime-sqlite-v2"
PHASE8_BINDING_SCHEMA = "phase8-reference-binding-v1"
PHASE8_BINDING_RESULT_SCHEMA = "phase8-reference-binding-result-v1"
PHASE8_APPROVAL_PREFLIGHT_SCHEMA = "phase8-trusted-local-approval-preflight-v1"
PHASE8_APPROVAL_SCHEMA = "phase8-durable-egress-approval-v1"
PHASE8_APPROVAL_EVENT_SCHEMA = "phase8-durable-approval-event-v1"
PHASE8_DECISION_SCHEMA = "phase8-durable-egress-decision-v1"
PHASE8_CURRENT_VIEW_SCHEMA = "phase8-egress-current-view-v1"
PHASE8_PUBLICATION_SCHEMA = "phase8-current-publication-v1"
PHASE8_PUBLICATION_RECEIPT_SCHEMA = "phase8-publication-receipt-v1"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}\Z")
_SIDECARS = ("-wal", "-shm", "-journal")
_SQLITE_HEADER = b"SQLite format 3\x00"
_CONNECTION_OWNER = "phase8-sqlite-connection"

CurrentHeadVerifier = Callable[[str, str], bool]
AdapterFence = Callable[[str], None]
PublicationHeadVerifier = Callable[[Mapping[str, object]], bool]
PostCommitFence = Callable[[sqlite3.Connection], None]
PostCommitReconciler = Callable[[sqlite3.Connection], None]


class Phase8RuntimeError(RuntimeError):
    code = "PHASE8_RUNTIME_ERROR"


class Phase8Disabled(Phase8RuntimeError):
    code = "PHASE8_DISABLED"


class Phase8ContractError(Phase8RuntimeError):
    code = "PHASE8_CONTRACT_INVALID"


class Phase8StoreError(Phase8RuntimeError):
    code = "PHASE8_STORE_INVALID"


class Phase8SchemaIncompatible(Phase8StoreError):
    code = "PHASE8_SCHEMA_INCOMPATIBLE"


class Phase8ReplayConflict(Phase8RuntimeError):
    code = "PHASE8_IDEMPOTENCY_CONFLICT"


class Phase8CurrentConflict(Phase8RuntimeError):
    code = "PHASE8_SOURCE_STALE"


class Phase8NotFound(Phase8RuntimeError):
    code = "PHASE8_NOT_FOUND"


class Phase8DeadlineExceeded(Phase8RuntimeError):
    code = "PHASE8_DEADLINE_EXCEEDED"


def _fail(error: type[Phase8RuntimeError], detail: str) -> NoReturn:
    raise error(detail)


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(Phase8ContractError, f"{field} must be a bounded identifier")
    return value


def _sha(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(Phase8ContractError, f"{field} must be lowercase SHA-256")
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        _fail(Phase8ContractError, f"{field} must be an integer >= {minimum}")
    return value


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        _fail(Phase8ContractError, f"{field} must be an object")
    try:
        result = json.loads(canonical_bytes(value).decode("utf-8"))
    except (CanonicalizationError, UnicodeError, TypeError, ValueError) as exc:
        raise Phase8ContractError(f"{field} is outside canonical JSON") from exc
    if dict(value) != result:
        _fail(Phase8ContractError, f"{field} must use exact JSON-safe values")
    return result


def _json_safe(value: object, field: str) -> dict[str, object]:
    """Normalize a trusted typed object's wire projection to JSON values."""

    try:
        result = json.loads(canonical_bytes(value).decode("utf-8"))
    except (CanonicalizationError, UnicodeError, TypeError, ValueError) as exc:
        raise Phase8ContractError(f"{field} is outside canonical JSON") from exc
    if type(result) is not dict:
        _fail(Phase8ContractError, f"{field} must be an object")
    return result


def _exact(value: object, keys: set[str], field: str) -> dict[str, object]:
    result = _mapping(value, field)
    if set(result) != keys:
        _fail(Phase8ContractError, f"{field} fields differ")
    return result


def _false_safety(value: Mapping[str, object], field: str) -> None:
    for name in ("authoritative", "authority_transferred", "dispatch_performed"):
        if value.get(name) is not False:
            _fail(Phase8ContractError, f"{field}.{name} must be false")


def build_phase8_publication_identity(
    *,
    publication_kind: str,
    publication_key: str,
    generation: Mapping[str, object],
    phase7_scope_key: str,
    phase7_commit_sha256: str,
) -> dict[str, object]:
    """Build the identity every mutable current projection must carry."""

    kind = _identifier(publication_kind, "publication_kind")
    key = _identifier(publication_key, "publication_key")
    generation_wire = _mapping(generation, "publication generation")
    if not generation_wire:
        _fail(Phase8ContractError, "publication generation cannot be empty")
    body: dict[str, object] = {
        "schema_version": PHASE8_PUBLICATION_SCHEMA,
        "publication_kind": kind,
        "publication_key": key,
        "generation": generation_wire,
        "phase7_scope_key": _sha(phase7_scope_key, "phase7_scope_key"),
        "phase7_commit_sha256": _sha(
            phase7_commit_sha256, "phase7_commit_sha256"
        ),
        "authoritative": False,
        "authority_transferred": False,
        "dispatch_performed": False,
    }
    return {**body, "publication_sha256": canonical_sha256(body)}


def _publication_identity(
    value: object,
    *,
    publication_key: str,
    phase7_scope_key: str,
    phase7_commit_sha256: str,
) -> dict[str, object]:
    if value is None:
        value = build_phase8_publication_identity(
            publication_kind="standalone-shadow",
            publication_key=publication_key,
            generation={"mode": "store-local", "publication_key": publication_key},
            phase7_scope_key=phase7_scope_key,
            phase7_commit_sha256=phase7_commit_sha256,
        )
    publication = _mapping(value, "publication identity")
    if set(publication) != {
        "schema_version",
        "publication_kind",
        "publication_key",
        "generation",
        "phase7_scope_key",
        "phase7_commit_sha256",
        "authoritative",
        "authority_transferred",
        "dispatch_performed",
        "publication_sha256",
    }:
        _fail(Phase8ContractError, "publication identity fields differ")
    if publication.get("schema_version") != PHASE8_PUBLICATION_SCHEMA:
        _fail(Phase8ContractError, "publication identity schema differs")
    if publication.get("publication_key") != publication_key:
        _fail(Phase8ContractError, "publication identity key differs")
    if publication.get("phase7_scope_key") != phase7_scope_key or publication.get(
        "phase7_commit_sha256"
    ) != phase7_commit_sha256:
        _fail(Phase8CurrentConflict, "publication Phase-7 head differs")
    _identifier(publication.get("publication_kind"), "publication_kind")
    generation = _mapping(publication.get("generation"), "publication generation")
    if not generation:
        _fail(Phase8ContractError, "publication generation cannot be empty")
    _false_safety(publication, "publication identity")
    _hashed(publication, "publication_sha256", "publication identity")
    return publication


def _publication_is_current(
    publication: Mapping[str, object],
    verifier: PublicationHeadVerifier | None,
) -> bool:
    if verifier is None:
        return (
            publication.get("publication_kind") == "standalone-shadow"
            and publication.get("generation")
            == {
                "mode": "store-local",
                "publication_key": publication.get("publication_key"),
            }
        )
    try:
        return verifier(publication) is True
    except Phase8RuntimeError:
        raise
    except BaseException as exc:
        if getattr(exc, "code", None) in {
            "PHASE78_DEADLINE_EXCEEDED",
            "PHASE78_REQUEST_CANCELLED",
        }:
            raise
        raise Phase8CurrentConflict(
            "publication generation verification failed"
        ) from exc


def _decode_publication(
    sha256_value: object,
    json_value: object,
    *,
    field: str,
) -> dict[str, object]:
    publication = _decode_json(json_value, field)
    digest = _hashed(publication, "publication_sha256", field)
    if digest != sha256_value:
        _fail(Phase8StoreError, f"{field} row identity differs")
    return publication


def _check_deadline(deadline: object | None, phase_name: str) -> None:
    if deadline is None:
        return
    callback = getattr(deadline, "check", None)
    if not callable(callback):
        _fail(Phase8ContractError, "deadline must expose check()")
    try:
        try:
            callback(phase_name)
        except TypeError:
            callback()
    except Phase8RuntimeError:
        raise
    except BaseException as exc:
        if getattr(exc, "code", None) in {
            "PHASE78_DEADLINE_EXCEEDED",
            "PHASE78_REQUEST_CANCELLED",
        }:
            raise
        raise Phase8DeadlineExceeded(
            f"total deadline expired during {phase_name}"
        ) from exc


def _remaining_ms(deadline: object | None, configured: int, phase_name: str) -> int:
    _check_deadline(deadline, phase_name)
    if deadline is None:
        return configured
    callback = getattr(deadline, "remaining_seconds", None)
    if not callable(callback):
        _fail(Phase8ContractError, "deadline must expose remaining_seconds()")
    try:
        value = float(callback())
    except (TypeError, ValueError, OverflowError) as exc:
        raise Phase8ContractError("deadline returned an invalid budget") from exc
    if value <= 0:
        _fail(Phase8DeadlineExceeded, f"total deadline expired during {phase_name}")
    return max(1, min(configured, int(value * 1000)))


def _adapter_fence(callback: AdapterFence | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


def _canonical_json(value: Mapping[str, object]) -> str:
    return canonical_bytes(value).decode("utf-8")


def _decode_json(value: object, field: str) -> dict[str, object]:
    if type(value) is not str:
        _fail(Phase8StoreError, f"{field} is not JSON text")
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise Phase8StoreError(f"{field} is invalid JSON") from exc
    if type(result) is not dict or _canonical_json(result) != value:
        _fail(Phase8StoreError, f"{field} is not canonical JSON")
    return result


def _hashed(value: Mapping[str, object], hash_field: str, field: str) -> str:
    digest = _sha(value.get(hash_field), f"{field}.{hash_field}")
    body = {key: item for key, item in value.items() if key != hash_field}
    if canonical_sha256(body) != digest:
        _fail(Phase8ContractError, f"{field} hash differs")
    return digest


def _historical_staged_manifest(
    request_value: object,
    manifest_value: object,
    *,
    field: str,
) -> dict[str, object]:
    """Revalidate persisted wire bytes without consulting today's policy.

    Current policy identity belongs only at an effective-current boundary.
    Otherwise a policy upgrade would make immutable approval/decision history
    unreadable before that boundary can return a safe ``POLICY_DRIFT`` denial.
    """

    request = _mapping(request_value, f"{field} request")
    if set(request) != {
        "schema_version", "subject", "provider", "surface", "account_scope",
        "retention", "purpose", "artifacts",
    } or request.get("schema_version") != "data-egress-request-v1":
        _fail(Phase8StoreError, f"{field} request schema differs")
    raw_artifacts = request.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        _fail(Phase8StoreError, f"{field} request artifacts differ")
    artifacts: list[dict[str, object]] = []
    for ordinal, raw in enumerate(raw_artifacts):
        artifact = _mapping(raw, f"{field} artifact {ordinal}")
        if set(artifact) != {
            "artifact_id", "sha256", "byte_length", "transfer_form",
            "classification",
        }:
            _fail(Phase8StoreError, f"{field} artifact fields differ")
        artifact_id = artifact.get("artifact_id")
        transfer_form = artifact.get("transfer_form")
        classification = artifact.get("classification")
        length = artifact.get("byte_length")
        if (
            type(artifact_id) is not str
            or not artifact_id.strip()
            or type(transfer_form) is not str
            or transfer_form.strip() not in {"raw", "canonical-text", "excerpt", "rendered"}
            or type(classification) is not str
            or classification.strip()
            not in {"public", "internal", "confidential", "restricted"}
            or type(length) is not int
            or length < 0
        ):
            _fail(Phase8StoreError, f"{field} artifact value differs")
        artifacts.append(
            {
                "artifact_id": artifact_id.strip(),
                "sha256": _sha(artifact.get("sha256"), f"{field} artifact sha256"),
                "byte_length": length,
                "transfer_form": transfer_form.strip(),
                "classification": classification.strip(),
            }
        )
    artifacts.sort(key=lambda item: str(item["artifact_id"]))
    if len({str(item["artifact_id"]) for item in artifacts}) != len(artifacts):
        _fail(Phase8StoreError, f"{field} artifact identifiers are not unique")
    strings: dict[str, str] = {}
    for name in (
        "subject", "provider", "surface", "account_scope", "retention", "purpose"
    ):
        value = request.get(name)
        if type(value) is not str or not value.strip():
            _fail(Phase8StoreError, f"{field} request {name} differs")
        strings[name] = value.strip()
    request_identity: dict[str, object] = {
        "schema_version": "data-egress-request-v1",
        **strings,
        "artifacts": artifacts,
    }
    manifest = _mapping(manifest_value, f"{field} staged manifest")
    if set(manifest) != {
        "schema_version", "request_sha256", "policy_sha256", "subject",
        "provider", "surface", "account_scope", "retention", "purpose",
        "artifacts", "state", "staged_manifest_sha256",
    } or manifest.get("schema_version") != "data-egress-staged-manifest-v1":
        _fail(Phase8StoreError, f"{field} staged manifest schema differs")
    policy_sha = _sha(manifest.get("policy_sha256"), f"{field} historical policy")
    body: dict[str, object] = {
        "schema_version": "data-egress-staged-manifest-v1",
        "request_sha256": canonical_sha256(request_identity),
        "policy_sha256": policy_sha,
        **strings,
        "artifacts": artifacts,
        "state": "STAGED",
    }
    expected = {**body, "staged_manifest_sha256": canonical_sha256(body)}
    if manifest != expected:
        _fail(Phase8StoreError, f"{field} historical staging identity differs")
    return manifest


def _historical_pure_approval(
    value: object,
    manifest: Mapping[str, object],
    *,
    field: str,
) -> dict[str, object]:
    approval = _mapping(value, f"{field} approval")
    if set(approval) != {
        "schema_version", "approval_id", "approved", "staged_manifest_sha256",
        "subject", "policy_sha256", "purpose", "artifacts",
    } or approval.get("schema_version") != DATA_EGRESS_APPROVAL_SCHEMA:
        _fail(Phase8StoreError, f"{field} approval schema differs")
    approval_id = approval.get("approval_id")
    if type(approval_id) is not str or not approval_id.strip():
        _fail(Phase8StoreError, f"{field} approval id differs")
    artifacts = approval.get("artifacts")
    expected_artifacts = [
        {"artifact_id": item["artifact_id"], "sha256": item["sha256"]}
        for item in manifest["artifacts"]
    ]
    if (
        approval.get("approved") is not True
        or approval.get("staged_manifest_sha256")
        != manifest["staged_manifest_sha256"]
        or approval.get("subject") != manifest["subject"]
        or approval.get("policy_sha256") != manifest["policy_sha256"]
        or approval.get("purpose") != manifest["purpose"]
        or artifacts != expected_artifacts
    ):
        _fail(Phase8StoreError, f"{field} approval binding differs")
    return approval


def _verify_historical_pure_decision(value: object) -> bool:
    try:
        decision = _mapping(value, "historical pure decision")
        if set(decision) != {
            "schema_version", "status", "reason_code", "policy_sha256",
            "request_sha256", "staged_manifest", "approval", "approval_sha256",
            "dispatch_performed", "decision_sha256",
        } or decision.get("schema_version") != "data-egress-decision-v1":
            return False
        digest = _hashed(decision, "decision_sha256", "historical pure decision")
        manifest = _mapping(decision.get("staged_manifest"), "historical manifest")
        request_identity = {
            "schema_version": "data-egress-request-v1",
            "subject": manifest.get("subject"),
            "provider": manifest.get("provider"),
            "surface": manifest.get("surface"),
            "account_scope": manifest.get("account_scope"),
            "retention": manifest.get("retention"),
            "purpose": manifest.get("purpose"),
            "artifacts": manifest.get("artifacts"),
        }
        revalidated = _historical_staged_manifest(
            request_identity,
            manifest,
            field="historical pure decision",
        )
        approval_value = decision.get("approval")
        if approval_value is None:
            approval = None
            expected = ("DENIED", "APPROVAL_MISSING")
        else:
            approval = _historical_pure_approval(
                approval_value, revalidated, field="historical pure decision"
            )
            expected = ("AUTHORIZED", "EXACT_APPROVAL_MATCH")
        body = {key: item for key, item in decision.items() if key != "decision_sha256"}
        return (
            digest == canonical_sha256(body)
            and decision.get("dispatch_performed") is False
            and decision.get("policy_sha256") == revalidated["policy_sha256"]
            and decision.get("request_sha256") == revalidated["request_sha256"]
            and decision.get("approval_sha256")
            == (None if approval is None else canonical_sha256(approval))
            and (decision.get("status"), decision.get("reason_code")) == expected
        )
    except (Phase8RuntimeError, CanonicalizationError, TypeError, ValueError):
        return False


def _occurrence(value: object) -> ArtifactLedgerOccurrence:
    try:
        return (
            validate_artifact_occurrence(value)
            if type(value) is ArtifactLedgerOccurrence
            else artifact_occurrence_from_dict(value)
        )
    except (Phase3ContractError, TypeError, ValueError) as exc:
        raise Phase8ContractError("Phase-3 occurrence does not revalidate") from exc


def _artifact_state(value: object):
    typed = getattr(authority_read, "AuthorityPhase3ArtifactState", None)
    validator = getattr(authority_read, "validate_authority_phase3_artifact_state", None)
    parser = getattr(authority_read, "authority_phase3_artifact_state_from_dict", None)
    if typed is None or not callable(validator) or not callable(parser):
        _fail(Phase8ContractError, "Authority Phase-3 state verifier is unavailable")
    try:
        state = validator(value) if type(value) is typed else parser(value)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise Phase8ContractError("Phase-3 aggregate state does not revalidate") from exc
    return state, _json_safe(state.as_dict(), "phase3_artifact_state")


def _access_proof(value: object) -> dict[str, object]:
    try:
        proof = phase6.verify_shadow_access_proof(value)
    except (phase6.Phase6Error, TypeError, ValueError, KeyError) as exc:
        raise Phase8ContractError("Phase-6 access proof does not revalidate") from exc
    result = _json_safe(proof.as_dict(), "phase6_access_proof")
    _false_safety(result, "phase6_access_proof")
    if result.get("shadow_allowed") is not True:
        _fail(Phase8ContractError, "Phase-6 proof is not shadow-allowed")
    _sha(result.get("proof_sha256"), "phase6_access_proof.proof_sha256")
    return result


def _cross_check_phase3_6(
    state: object,
    state_wire: Mapping[str, object],
    occurrence: ArtifactLedgerOccurrence,
    proof: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    source = _mapping(proof.get("source_binding"), "source_binding")
    authority = _mapping(source.get("authority_coordinate"), "authority_coordinate")
    coordinate = _mapping(source.get("source_snapshot_coordinate"), "source_snapshot_coordinate")
    state_sha = getattr(state, "state_sha256", state_wire.get("state_sha256"))
    if (
        _sha(state_sha, "phase3_artifact_state.state_sha256")
        != source.get("phase3_artifact_state_sha256")
        or state_wire.get("workflow_id") != occurrence.workflow_id
        or state_wire.get("workflow_id") != authority.get("workflow_id")
        or state_wire.get("through_revision") != authority.get("current_revision")
        or state_wire.get("through_revision") != coordinate.get("project_revision")
        or occurrence.revision > int(state_wire.get("through_revision", -1))
        or authority.get("project_id") != coordinate.get("project_id")
    ):
        _fail(Phase8CurrentConflict, "Phase-3 state/occurrence and Phase-6 source differ")
    matches = [
        item for item in getattr(state, "occurrences", ())
        if item.normalized_path == occurrence.normalized_path
    ]
    if len(matches) != 1 or matches[0] != occurrence:
        _fail(
            Phase8CurrentConflict,
            "selected occurrence is not the unique current member for its path",
        )
    return authority, coordinate


_PHASE7_RESULT_KEYS = {
    "schema_version", "idempotency_key", "scope_key", "sequence",
    "previous_commit_sha256", "request_sha256", "receipt_sha256",
    "effective_verdict_sha256", "commit_sha256", "occurrence_id",
    "authority_revision", "aggregate_verdict", "aggregate_action",
    "grounding_valid", "replayed", "current", "authoritative",
    "authority_transferred", "dispatch_performed",
}


def _phase7_bundle(
    result_value: object,
    receipt_value: object,
    effective_value: object,
    *,
    state_sha256: str,
    authority_revision: int,
    occurrence: ArtifactLedgerOccurrence,
    proof_sha256: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    result = _exact(result_value, _PHASE7_RESULT_KEYS, "phase7_result")
    if result["schema_version"] != "phase7-grounding-bundle-result-v1":
        _fail(Phase8ContractError, "Phase-7 result schema differs")
    _false_safety(result, "phase7_result")
    for name in ("request_sha256", "receipt_sha256", "effective_verdict_sha256", "commit_sha256"):
        _sha(result[name], f"phase7_result.{name}")
    if (
        result["current"] is not True
        or result["occurrence_id"] != occurrence.occurrence_id
        or result["authority_revision"] != authority_revision
    ):
        _fail(Phase8CurrentConflict, "Phase-7 result is not current for the occurrence")
    receipt = _mapping(receipt_value, "phase7_receipt")
    if receipt.get("schema_version") != "phase7-durable-grounding-bundle-receipt-v1":
        _fail(Phase8ContractError, "Phase-7 receipt schema differs")
    _false_safety(receipt, "phase7_receipt")
    receipt_sha = _hashed(receipt, "receipt_sha256", "phase7_receipt")
    if receipt_sha != result["receipt_sha256"]:
        _fail(Phase8ContractError, "Phase-7 receipt/result binding differs")
    input_identity = _mapping(receipt.get("input_identity"), "phase7 input identity")
    _false_safety(input_identity, "phase7 input identity")
    input_sha = _hashed(
        input_identity,
        "input_identity_sha256",
        "phase7 input identity",
    )
    if (
        receipt.get("input_identity_sha256") != input_sha
        or input_identity.get("phase3_artifact_state_sha256") != state_sha256
        or input_identity.get("phase3_artifact_occurrence_id") != occurrence.occurrence_id
        or input_identity.get("phase6_access_proof_sha256") != proof_sha256
    ):
        _fail(Phase8ContractError, "Phase-7 upstream identity differs")
    role_receipts = receipt.get("role_receipts")
    if not isinstance(role_receipts, Mapping) or set(role_receipts) != {"math", "execution", "paper"}:
        _fail(Phase8ContractError, "Phase-7 role receipts are incomplete")
    for role, raw in role_receipts.items():
        role_receipt = _mapping(raw, f"Phase-7 {role} receipt")
        _false_safety(role_receipt, f"Phase-7 {role} receipt")
        _hashed(role_receipt, "role_receipt_sha256", f"Phase-7 {role} receipt")
    effective = _mapping(effective_value, "phase7_effective_verdict")
    if effective.get("schema_version") != "phase7-effective-aggregate-verdict-v1":
        _fail(Phase8ContractError, "Phase-7 effective verdict schema differs")
    _false_safety(effective, "phase7_effective_verdict")
    effective_sha = _hashed(
        effective, "effective_verdict_sha256", "phase7_effective_verdict"
    )
    if (
        effective_sha != result["effective_verdict_sha256"]
        or effective.get("grounding_receipt_sha256") != receipt_sha
        or effective.get("phase3_artifact_state_sha256") != state_sha256
        or effective.get("phase3_artifact_occurrence_id") != occurrence.occurrence_id
        or effective.get("phase6_access_proof_sha256") != proof_sha256
        or effective.get("aggregate_verdict") != result["aggregate_verdict"]
        or effective.get("aggregate_action") != result["aggregate_action"]
    ):
        _fail(Phase8ContractError, "Phase-7 effective/result identity differs")
    return result, receipt, effective


@dataclass(frozen=True)
class ReferenceBindingResult:
    binding_sha256: str
    scope_key: str
    sequence: int
    previous_binding_sha256: str | None
    idempotency_key: str
    replayed: bool
    current: bool
    binding: Mapping[str, object]
    authoritative: bool = False
    authority_transferred: bool = False
    dispatch_performed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PHASE8_BINDING_RESULT_SCHEMA,
            "binding_sha256": self.binding_sha256,
            "scope_key": self.scope_key,
            "sequence": self.sequence,
            "previous_binding_sha256": self.previous_binding_sha256,
            "idempotency_key": self.idempotency_key,
            "replayed": self.replayed,
            "current": self.current,
            "binding": dict(self.binding),
            "authoritative": self.authoritative,
            "authority_transferred": self.authority_transferred,
            "dispatch_performed": self.dispatch_performed,
        }


@dataclass(frozen=True)
class ApprovalResult:
    approval: Mapping[str, object]
    lifecycle_event: Mapping[str, object]
    replayed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "approval": dict(self.approval),
            "lifecycle_event": dict(self.lifecycle_event),
            "replayed": self.replayed,
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }


@dataclass(frozen=True)
class TrustedApprovalPreflightResult:
    preflight: Mapping[str, object]
    replayed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "preflight": dict(self.preflight),
            "replayed": self.replayed,
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }


@dataclass(frozen=True)
class DecisionResult:
    decision: Mapping[str, object]
    replayed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": dict(self.decision),
            "replayed": self.replayed,
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }


class _AnchoredConnection(sqlite3.Connection):
    _database_anchor: OwnedDescriptor | None = None
    _parent_anchor: OwnedDescriptor | None = None
    _sqlite_closed = False

    def adopt(
        self,
        database: OwnedDescriptor,
        database_owner: str,
        parent: OwnedDescriptor,
        parent_owner: str,
    ) -> None:
        self._database_anchor = database
        self._parent_anchor = parent
        database.transfer(owner=database_owner, new_owner=_CONNECTION_OWNER)
        parent.transfer(owner=parent_owner, new_owner=_CONNECTION_OWNER)

    def close(self) -> None:
        callbacks = []
        if self._database_anchor is not None:
            callbacks.append(("close Phase-8 database anchor", self._database_anchor.cleanup(_CONNECTION_OWNER)))
        if self._parent_anchor is not None:
            callbacks.append(("close Phase-8 parent anchor", self._parent_anchor.cleanup(_CONNECTION_OWNER)))
        try:
            if not self._sqlite_closed:
                sqlite3.Connection.close(self)
                self._sqlite_closed = True
            run_cleanup(callbacks)
        except BaseException as primary:
            if not self._sqlite_closed:
                def reconcile_sqlite_close() -> None:
                    if not self._sqlite_closed:
                        sqlite3.Connection.close(self)
                        self._sqlite_closed = True

                run_cleanup(
                    [
                        (
                            "reconcile interrupted Phase-8 SQLite close",
                            RetryableCleanup(reconcile_sqlite_close),
                        )
                    ],
                    primary=primary,
                )
            run_cleanup(callbacks, primary=primary)
            raise
        finally:
            if self._database_anchor is not None and self._database_anchor.closed:
                self._database_anchor = None
            if self._parent_anchor is not None and self._parent_anchor.closed:
                self._parent_anchor = None


_SCHEMA = (
    """CREATE TABLE phase8_schema_state(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version TEXT NOT NULL,
        absolute_path_sha256 TEXT NOT NULL
    )""",
    """CREATE TABLE phase8_reference_bindings(
        binding_sha256 TEXT PRIMARY KEY,
        logical_id TEXT NOT NULL UNIQUE,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        previous_binding_sha256 TEXT,
        binding_json TEXT NOT NULL,
        binding_blob_json TEXT NOT NULL,
        UNIQUE(scope_key,sequence)
    )""",
    """CREATE TABLE phase8_reference_current(
        scope_key TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        binding_sha256 TEXT NOT NULL,
        publication_sha256 TEXT NOT NULL,
        publication_json TEXT NOT NULL,
        FOREIGN KEY(binding_sha256) REFERENCES phase8_reference_bindings(binding_sha256)
    )""",
    """CREATE TABLE phase8_trusted_approval_preflights(
        preflight_sha256 TEXT PRIMARY KEY,
        preflight_id TEXT NOT NULL UNIQUE,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        preflight_json TEXT NOT NULL,
        preflight_blob_json TEXT NOT NULL,
        FOREIGN KEY(binding_sha256) REFERENCES phase8_reference_bindings(binding_sha256)
    )""",
    """CREATE TABLE phase8_approvals(
        approval_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL,
        trusted_preflight_sha256 TEXT NOT NULL,
        successor_of TEXT,
        approval_sha256 TEXT NOT NULL UNIQUE,
        approval_json TEXT NOT NULL,
        approval_blob_json TEXT NOT NULL,
        FOREIGN KEY(binding_sha256) REFERENCES phase8_reference_bindings(binding_sha256),
        FOREIGN KEY(trusted_preflight_sha256)
            REFERENCES phase8_trusted_approval_preflights(preflight_sha256)
    )""",
    """CREATE TABLE phase8_approval_events(
        event_sha256 TEXT PRIMARY KEY,
        approval_id TEXT NOT NULL,
        event_sequence INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        event_json TEXT NOT NULL,
        event_blob_json TEXT NOT NULL,
        UNIQUE(approval_id,event_sequence),
        FOREIGN KEY(approval_id) REFERENCES phase8_approvals(approval_id)
    )""",
    """CREATE TABLE phase8_approval_current(
        approval_id TEXT PRIMARY KEY,
        event_sequence INTEGER NOT NULL,
        event_sha256 TEXT NOT NULL,
        state TEXT NOT NULL,
        publication_sha256 TEXT NOT NULL,
        publication_json TEXT NOT NULL,
        FOREIGN KEY(approval_id) REFERENCES phase8_approvals(approval_id),
        FOREIGN KEY(event_sha256) REFERENCES phase8_approval_events(event_sha256)
    )""",
    """CREATE TABLE phase8_scope_approval_current(
        scope_key TEXT PRIMARY KEY,
        approval_id TEXT NOT NULL,
        publication_sha256 TEXT NOT NULL,
        publication_json TEXT NOT NULL,
        FOREIGN KEY(approval_id) REFERENCES phase8_approvals(approval_id)
    )""",
    """CREATE TABLE phase8_decisions(
        decision_sha256 TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        previous_decision_sha256 TEXT,
        decision_json TEXT NOT NULL,
        decision_blob_json TEXT NOT NULL,
        UNIQUE(scope_key,sequence)
    )""",
    """CREATE TABLE phase8_decision_current(
        scope_key TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        decision_sha256 TEXT NOT NULL,
        publication_sha256 TEXT NOT NULL,
        publication_json TEXT NOT NULL,
        FOREIGN KEY(decision_sha256) REFERENCES phase8_decisions(decision_sha256)
    )""",
    """CREATE TABLE phase8_publication_receipts(
        publication_sha256 TEXT PRIMARY KEY,
        publication_kind TEXT NOT NULL,
        publication_key TEXT NOT NULL,
        activated_object_sha256 TEXT NOT NULL,
        phase7_scope_key TEXT NOT NULL,
        phase7_commit_sha256 TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL UNIQUE,
        receipt_json TEXT NOT NULL
    )""",
)

# ``phase8_decision_current`` is deliberately only the latest immutable
# decision-history input, never an effective authorization pointer.  Approval
# lifecycle/head/policy/time can change without rewriting history.  The sole
# public effective-current boundary is ``load_current_decision`` below; it
# joins/revalidates those live facts on every call (including after restart)
# and returns DENIED when any fence changed.  No public method returns this raw
# table row as current authority.


def _schema_inventory(connection: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        {"type": row[0], "name": row[1], "table": row[2], "sql": row[3]}
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    ]


def _create_schema(connection: sqlite3.Connection) -> str:
    for statement in _SCHEMA:
        connection.execute(statement)
    for table in (
        "phase8_schema_state",
        "phase8_reference_bindings",
        "phase8_trusted_approval_preflights",
        "phase8_approvals",
        "phase8_approval_events",
        "phase8_decisions",
        "phase8_publication_receipts",
    ):
        for action in ("UPDATE", "DELETE"):
            connection.execute(
                f"""CREATE TRIGGER {table}_immutable_{action.lower()}
                BEFORE {action} ON {table} BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END"""
            )
    return canonical_sha256(_schema_inventory(connection))


def _expected_schema_digest() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        return _create_schema(connection)
    finally:
        connection.close()


_SCHEMA_DIGEST = _expected_schema_digest()


class Phase8EvidenceEgressStore:
    """Independent durable Phase-8 shadow store.

    Construction is inert.  When ``enabled`` is false, every operation fails
    before validating or opening the database/CAS paths.
    """

    def __init__(
        self,
        database_path: Path,
        cas_root: Path,
        *,
        enabled: bool = PHASE8_DEFAULT_ENABLED,
        busy_timeout_ms: int = 5000,
    ):
        self._path = Path(database_path)
        self._cas_root = Path(cas_root)
        self._enabled = enabled
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 300_000:
            _fail(Phase8ContractError, "busy_timeout_ms is outside bounds")
        self._busy_timeout_ms = busy_timeout_ms
        self._initialized = False

    def _require_enabled(self) -> None:
        if self._enabled is not True:
            _fail(Phase8Disabled, "Phase-8 durable runtime is disabled")

    def _validate_paths(self) -> None:
        if (
            not self._path.is_absolute()
            or self._path == Path(self._path.anchor)
            or not self._cas_root.is_absolute()
            or self._cas_root == Path(self._cas_root.anchor)
        ):
            _fail(Phase8StoreError, "Phase-8 database and CAS roots must be absolute")
        try:
            parent = self._path.parent.resolve(strict=True)
            parent_lstat = self._path.parent.lstat()
            cas = self._cas_root.resolve(strict=True)
            cas_lstat = self._cas_root.lstat()
        except OSError as exc:
            raise Phase8StoreError("Phase-8 parent/CAS root is unavailable") from exc
        if (
            parent != self._path.parent
            or cas != self._cas_root
            or stat.S_ISLNK(parent_lstat.st_mode)
            or stat.S_ISLNK(cas_lstat.st_mode)
            or not stat.S_ISDIR(parent_lstat.st_mode)
            or not stat.S_ISDIR(cas_lstat.st_mode)
        ):
            _fail(Phase8StoreError, "Phase-8 roots cannot contain symlinks")
        if not Path("/proc/self/fd").is_dir():
            _fail(Phase8StoreError, "descriptor-anchored SQLite is unavailable")

    def _open_parent(self, owner: str) -> OwnedDescriptor:
        descriptor = OwnedDescriptor.from_opener(
            lambda: os.open(
                self._path.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            ),
            owner=owner,
            label="Phase-8 database parent",
        )
        try:
            opened = os.fstat(descriptor.fileno(owner))
            named = os.lstat(self._path.parent)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (int(opened.st_dev), int(opened.st_ino))
                != (int(named.st_dev), int(named.st_ino))
            ):
                _fail(Phase8StoreError, "Phase-8 database parent identity differs")
            return descriptor
        except BaseException as primary:
            run_cleanup(
                [("close failed Phase-8 parent anchor", descriptor.cleanup(owner))],
                primary=primary,
            )
            raise

    @staticmethod
    def _identity(stat_value: os.stat_result) -> tuple[int, int, int, int]:
        return (
            int(stat_value.st_dev),
            int(stat_value.st_ino),
            int(stat_value.st_nlink),
            stat.S_IMODE(stat_value.st_mode),
        )

    def _assert_database_descriptor(self, descriptor: int) -> tuple[int, int, int, int]:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_nlink) != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            _fail(Phase8StoreError, "Phase-8 database must be regular/link1/mode0600")
        named = os.lstat(self._path)
        if self._identity(opened) != self._identity(named):
            _fail(Phase8StoreError, "Phase-8 database path identity differs")
        return self._identity(opened)

    def _assert_no_sidecars(self) -> None:
        for suffix in _SIDECARS:
            if os.path.lexists(f"{self._path}{suffix}"):
                _fail(Phase8StoreError, f"Phase-8 database has an unexpected {suffix} sidecar")

    @staticmethod
    def _fd_uri(descriptor: int, mode: str) -> str:
        return f"file:/proc/self/fd/{descriptor}?mode={mode}"

    def _configure(
        self,
        connection: _AnchoredConnection,
        *,
        deadline: object | None,
    ) -> None:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            f"PRAGMA busy_timeout={_remaining_ms(deadline, self._busy_timeout_ms, 'SQLite configure')}"
        )

    def _verify_schema(self, connection: sqlite3.Connection) -> None:
        try:
            rows = connection.execute(
                "SELECT singleton,schema_version,absolute_path_sha256 "
                "FROM phase8_schema_state"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise Phase8StoreError(
                "Phase-8 exact SQLite schema differs"
            ) from exc
        if (
            len(rows) == 1
            and rows[0]["singleton"] == 1
            and rows[0]["schema_version"]
            == "phase8-evidence-egress-runtime-sqlite-v1"
        ):
            _fail(
                Phase8SchemaIncompatible,
                "Phase-8 v1 store is incompatible with the v2 publication "
                "contract; in-place upgrade is unsupported",
            )
        if (
            len(rows) != 1
            or rows[0]["singleton"] != 1
            or rows[0]["schema_version"] != PHASE8_STORE_SCHEMA
        ):
            _fail(Phase8StoreError, "Phase-8 ownership marker differs")
        if canonical_sha256(_schema_inventory(connection)) != _SCHEMA_DIGEST:
            _fail(Phase8StoreError, "Phase-8 exact SQLite schema differs")
        expected_path = canonical_sha256(
            {"schema_version": "phase8-absolute-database-path-v1", "absolute_path": os.fspath(self._path)}
        )
        if rows[0]["absolute_path_sha256"] != expected_path:
            _fail(Phase8StoreError, "Phase-8 database path binding differs")
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if [str(row[0]) for row in integrity] != ["ok"]:
            _fail(Phase8StoreError, "Phase-8 SQLite integrity differs")
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            _fail(Phase8StoreError, "Phase-8 SQLite foreign keys differ")

    def _initialize(self, *, deadline: object | None) -> None:
        self._validate_paths()
        _check_deadline(deadline, "SQLite initialization")
        parent_owner = "phase8-init-parent"
        creator_owner = "phase8-init-creator"
        cleanup_owner = "phase8-init-cleanup"
        parent = self._open_parent(parent_owner)
        creator: OwnedDescriptor | None = None
        cleanup: OwnedDescriptor | None = None
        connection: _AnchoredConnection | None = None
        created_identity: tuple[int, int, int, int] | None = None
        committed = False
        try:
            self._assert_no_sidecars()
            creator = OwnedDescriptor.from_opener(
                lambda: os.open(
                    self._path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent.fileno(parent_owner),
                ),
                owner=creator_owner,
                label="Phase-8 database creator",
            )
            fcntl.flock(creator.fileno(creator_owner), fcntl.LOCK_EX)
            created_identity = self._assert_database_descriptor(creator.fileno(creator_owner))
            if os.fstat(creator.fileno(creator_owner)).st_size != 0:
                _fail(Phase8StoreError, "new Phase-8 database is not empty")
            cleanup = creator.duplicate(
                owner=creator_owner,
                new_owner=cleanup_owner,
                label="Phase-8 cleanup anchor",
            )
            connection = sqlite3.connect(
                self._fd_uri(creator.fileno(creator_owner), "rw"),
                uri=True,
                timeout=max(0.001, _remaining_ms(deadline, self._busy_timeout_ms, "SQLite connect") / 1000),
                factory=_AnchoredConnection,
            )
            connection.adopt(creator, creator_owner, parent, parent_owner)
            self._configure(connection, deadline=deadline)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            digest = _create_schema(connection)
            if digest != _SCHEMA_DIGEST:
                _fail(Phase8StoreError, "Phase-8 initialized schema differs")
            connection.execute(
                "INSERT INTO phase8_schema_state VALUES(1,?,?)",
                (
                    PHASE8_STORE_SCHEMA,
                    canonical_sha256(
                        {"schema_version": "phase8-absolute-database-path-v1", "absolute_path": os.fspath(self._path)}
                    ),
                ),
            )
            _check_deadline(deadline, "SQLite initialization commit")
            try:
                connection.commit()
            except BaseException:
                committed = not connection.in_transaction
                raise
            else:
                committed = True
            os.fsync(connection._parent_anchor.fileno(_CONNECTION_OWNER))
            self._verify_schema(connection)
            connection.close()
            connection = None
            if cleanup is not None:
                cleanup.close(cleanup_owner)
        except FileExistsError as exc:
            raise Phase8StoreError("Phase-8 path appeared during exclusive creation") from exc
        except BaseException as primary:
            if connection is not None:
                run_cleanup(
                    [
                        ("rollback Phase-8 initialization", RetryableCleanup(connection.rollback)),
                        ("close Phase-8 initialization", RetryableCleanup(connection.close)),
                    ],
                    primary=primary,
                )
            callbacks = []
            if creator is not None:
                callbacks.append(("close Phase-8 creator", creator.cleanup(creator_owner)))
            if parent is not None:
                callbacks.append(("close Phase-8 parent", parent.cleanup(parent_owner)))
            if not committed and cleanup is not None and not cleanup.closed and created_identity is not None:
                try:
                    anchored = self._identity(os.fstat(cleanup.fileno(cleanup_owner)))
                    cleanup_parent = self._open_parent("phase8-cleanup-parent")
                    try:
                        named = os.stat(
                            self._path.name,
                            dir_fd=cleanup_parent.fileno("phase8-cleanup-parent"),
                            follow_symlinks=False,
                        )
                        if anchored == created_identity == self._identity(named):
                            resilient_unlink_at(
                                cleanup_parent.fileno("phase8-cleanup-parent"),
                                self._path.name,
                            )
                            os.fsync(cleanup_parent.fileno("phase8-cleanup-parent"))
                    finally:
                        run_cleanup(
                            [("close Phase-8 cleanup parent", cleanup_parent.cleanup("phase8-cleanup-parent"))],
                            primary=primary,
                        )
                except BaseException as cleanup_error:
                    run_cleanup(
                        [("report Phase-8 failed-create cleanup", lambda error=cleanup_error: (_ for _ in ()).throw(error))],
                        primary=primary,
                    )
            if cleanup is not None:
                callbacks.append(("close Phase-8 cleanup anchor", cleanup.cleanup(cleanup_owner)))
            run_cleanup(callbacks, primary=primary)
            raise
        finally:
            if connection is None:
                callbacks = []
                if creator is not None:
                    callbacks.append(("close remaining Phase-8 creator", creator.cleanup(creator_owner)))
                if parent is not None:
                    callbacks.append(("close remaining Phase-8 parent", parent.cleanup(parent_owner)))
                if cleanup is not None:
                    callbacks.append(("close remaining Phase-8 cleanup", cleanup.cleanup(cleanup_owner)))
                run_cleanup(callbacks)

    def _ensure_initialized(self, *, deadline: object | None) -> None:
        self._require_enabled()
        if self._initialized:
            return
        self._validate_paths()
        if not os.path.lexists(self._path):
            try:
                self._initialize(deadline=deadline)
            except Phase8StoreError as exc:
                # A concurrent creator may win O_EXCL after our read-only
                # absence check.  It owns initialization; the descriptor lock
                # in _connect waits boundedly and then verifies the exact DB.
                if not isinstance(exc.__cause__, FileExistsError):
                    raise
        self._initialized = True

    def _connect(self, *, deadline: object | None) -> _AnchoredConnection:
        self._ensure_initialized(deadline=deadline)
        parent_owner = "phase8-open-parent"
        database_owner = "phase8-open-database"
        parent = self._open_parent(parent_owner)
        database: OwnedDescriptor | None = None
        connection: _AnchoredConnection | None = None
        try:
            database = OwnedDescriptor.from_opener(
                lambda: os.open(
                    self._path,
                    os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                ),
                owner=database_owner,
                label="Phase-8 database",
            )
            lock_started = time.monotonic()
            while True:
                try:
                    fcntl.flock(
                        database.fileno(database_owner),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    break
                except BlockingIOError as exc:
                    _check_deadline(deadline, "SQLite ownership lock")
                    if (time.monotonic() - lock_started) * 1000 >= self._busy_timeout_ms:
                        raise Phase8StoreError("Phase-8 SQLite ownership lock is busy") from exc
                    time.sleep(0.005)
            self._assert_no_sidecars()
            self._assert_database_descriptor(database.fileno(database_owner))
            header = os.pread(database.fileno(database_owner), 100, 0)
            if len(header) != 100 or header[:16] != _SQLITE_HEADER or header[18:20] != b"\x01\x01":
                _fail(Phase8StoreError, "Phase-8 rollback-journal header differs")
            connection = sqlite3.connect(
                self._fd_uri(database.fileno(database_owner), "rw"),
                uri=True,
                timeout=max(0.001, _remaining_ms(deadline, self._busy_timeout_ms, "SQLite connect") / 1000),
                factory=_AnchoredConnection,
            )
            connection.adopt(database, database_owner, parent, parent_owner)
            self._configure(connection, deadline=deadline)
            self._verify_schema(connection)
            return connection
        except BaseException as primary:
            callbacks = []
            if connection is not None:
                callbacks.append(("close failed Phase-8 connection", RetryableCleanup(connection.close)))
            if database is not None:
                callbacks.append(("close untransferred Phase-8 database", database.cleanup(database_owner)))
            callbacks.append(("close untransferred Phase-8 parent", parent.cleanup(parent_owner)))
            run_cleanup(callbacks, primary=primary)
            raise

    def _transaction(
        self,
        connection: _AnchoredConnection,
        *,
        deadline: object | None,
        adapter_fence: AdapterFence | None,
    ) -> None:
        _adapter_fence(adapter_fence, "before_sqlite_begin")
        _check_deadline(deadline, "SQLite begin")
        connection.execute(
            f"PRAGMA busy_timeout={_remaining_ms(deadline, self._busy_timeout_ms, 'SQLite begin')}"
        )
        connection.execute("BEGIN IMMEDIATE")

    def _commit(
        self,
        connection: _AnchoredConnection,
        *,
        idempotency_key: str,
        deadline: object | None,
        adapter_fence: AdapterFence | None,
        post_commit_fence: PostCommitFence | None = None,
        post_commit_reconciler: PostCommitReconciler | None = None,
    ) -> None:
        _adapter_fence(adapter_fence, "before_sqlite_commit")
        _check_deadline(deadline, "SQLite commit")
        connection.commit()
        try:
            # A generation/head/deadline can lose immediately after SQLite's
            # durable boundary.  Recheck all three before allowing a mutable
            # current projection to escape as the winning shadow head.
            _adapter_fence(adapter_fence, "after_sqlite_commit")
            _check_deadline(deadline, "SQLite post-commit")
            if post_commit_fence is not None:
                post_commit_fence(connection)
        except BaseException as error:
            if post_commit_reconciler is not None:
                self._reconcile_post_commit(
                    connection,
                    post_commit_reconciler,
                    error,
                )
            if getattr(error, "code", None) in {
                "PHASE78_DEADLINE_EXCEEDED",
                "PHASE8_DEADLINE_EXCEEDED",
            } or isinstance(error, TimeoutError):
                raise Phase78OutcomeUncertain(idempotency_key) from error
            raise

    @staticmethod
    def _reconcile_post_commit(
        connection: sqlite3.Connection,
        reconciler: PostCommitReconciler,
        primary: BaseException,
    ) -> None:
        """Best-effort cleanup only; read-time publication guards are safety."""

        try:
            connection.execute("BEGIN IMMEDIATE")
            reconciler(connection)
            connection.commit()
        except BaseException as reconciliation_error:
            if connection.in_transaction:
                try:
                    connection.rollback()
                except BaseException:
                    pass
            if hasattr(primary, "add_note"):
                primary.add_note(
                    "Phase-8 post-commit current projection reconciliation "
                    "also failed: "
                    f"{type(reconciliation_error).__name__}"
                )

    @staticmethod
    def _publication_receipt_is_current(
        connection: sqlite3.Connection,
        publication: Mapping[str, object],
        *,
        activated_object_sha256: str,
    ) -> bool:
        if publication.get("publication_kind") != "approval-revocation":
            return True
        row = connection.execute(
            "SELECT * FROM phase8_publication_receipts "
            "WHERE publication_sha256=?",
            (publication["publication_sha256"],),
        ).fetchone()
        if row is None:
            return False
        receipt = _decode_json(row["receipt_json"], "publication receipt")
        if set(receipt) != {
            "schema_version",
            "publication",
            "publication_sha256",
            "publication_kind",
            "publication_key",
            "activated_object_sha256",
            "phase7_scope_key",
            "phase7_commit_sha256",
            "authoritative",
            "authority_transferred",
            "dispatch_performed",
            "receipt_sha256",
        }:
            _fail(Phase8StoreError, "publication receipt fields differ")
        digest = _hashed(receipt, "receipt_sha256", "publication receipt")
        if (
            digest != row["receipt_sha256"]
            or receipt["schema_version"] != PHASE8_PUBLICATION_RECEIPT_SCHEMA
            or receipt["publication"] != publication
            or receipt["publication_sha256"]
            != publication["publication_sha256"]
            or receipt["publication_kind"] != publication["publication_kind"]
            or receipt["publication_key"] != publication["publication_key"]
            or receipt["activated_object_sha256"] != activated_object_sha256
            or receipt["phase7_scope_key"] != publication["phase7_scope_key"]
            or receipt["phase7_commit_sha256"]
            != publication["phase7_commit_sha256"]
            or row["publication_kind"] != publication["publication_kind"]
            or row["publication_key"] != publication["publication_key"]
            or row["activated_object_sha256"] != activated_object_sha256
            or row["phase7_scope_key"] != publication["phase7_scope_key"]
            or row["phase7_commit_sha256"]
            != publication["phase7_commit_sha256"]
        ):
            _fail(Phase8StoreError, "publication receipt identity differs")
        _false_safety(receipt, "publication receipt")
        return True

    def _ensure_publication_receipt(
        self,
        *,
        connection: _AnchoredConnection | None = None,
        publication: Mapping[str, object],
        activated_object_sha256: str,
        idempotency_key: str,
        phase7_current_head_verifier: CurrentHeadVerifier,
        publication_head_verifier: PublicationHeadVerifier | None,
        activation_fence: PostCommitFence,
        deadline: object | None,
        adapter_fence: AdapterFence | None,
    ) -> None:
        """Publish a durable terminal guard after revoke activation."""

        if publication.get("publication_kind") != "approval-revocation":
            return
        activated = _sha(
            activated_object_sha256, "activated_object_sha256"
        )
        body: dict[str, object] = {
            "schema_version": PHASE8_PUBLICATION_RECEIPT_SCHEMA,
            "publication": dict(publication),
            "publication_sha256": publication["publication_sha256"],
            "publication_kind": publication["publication_kind"],
            "publication_key": publication["publication_key"],
            "activated_object_sha256": activated,
            "phase7_scope_key": publication["phase7_scope_key"],
            "phase7_commit_sha256": publication["phase7_commit_sha256"],
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
        receipt = {**body, "receipt_sha256": canonical_sha256(body)}
        owns_connection = connection is None
        if connection is None:
            connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            self._transaction(
                connection, deadline=deadline, adapter_fence=adapter_fence
            )
            existing = connection.execute(
                "SELECT * FROM phase8_publication_receipts "
                "WHERE publication_sha256=?",
                (publication["publication_sha256"],),
            ).fetchone()
            if existing is not None:
                if existing["receipt_sha256"] != receipt["receipt_sha256"]:
                    _fail(
                        Phase8ReplayConflict,
                        "publication receipt bytes differ",
                    )
                connection.rollback()
                _adapter_fence(adapter_fence, "after_sqlite_replay")
                _check_deadline(deadline, "publication receipt replay")
                activation_fence(connection)
                if not self._head_is_current(
                    phase7_current_head_verifier,
                    str(publication["phase7_scope_key"]),
                    str(publication["phase7_commit_sha256"]),
                ) or not _publication_is_current(
                    publication, publication_head_verifier
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "publication receipt replay is stale",
                    )
                return
            connection.execute(
                """INSERT INTO phase8_publication_receipts(
                   publication_sha256,publication_kind,publication_key,
                   activated_object_sha256,phase7_scope_key,
                   phase7_commit_sha256,receipt_sha256,receipt_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    publication["publication_sha256"],
                    publication["publication_kind"],
                    publication["publication_key"],
                    activated,
                    publication["phase7_scope_key"],
                    publication["phase7_commit_sha256"],
                    receipt["receipt_sha256"],
                    _canonical_json(receipt),
                ),
            )

            def receipt_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                activation_fence(committed)
                if not self._publication_receipt_is_current(
                    committed,
                    publication,
                    activated_object_sha256=activated,
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "publication receipt is unavailable after commit",
                    )
                if not self._head_is_current(
                    phase7_current_head_verifier,
                    str(publication["phase7_scope_key"]),
                    str(publication["phase7_commit_sha256"]),
                ) or not _publication_is_current(
                    publication, publication_head_verifier
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "publication receipt became stale",
                    )

            self._commit(
                connection,
                idempotency_key=idempotency_key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=receipt_post_commit_fence,
            )
        except BaseException as error:
            primary = error
            if connection.in_transaction:
                run_cleanup(
                    [
                        (
                            "rollback Phase-8 publication receipt",
                            RetryableCleanup(connection.rollback),
                        )
                    ],
                    primary=error,
                )
            raise
        finally:
            if owns_connection:
                self._close(connection, primary)

    @staticmethod
    def _close(connection: _AnchoredConnection, primary: BaseException | None = None) -> None:
        run_cleanup(
            [("close Phase-8 SQLite connection", RetryableCleanup(connection.close))],
            primary=primary,
        )

    def _cas(self) -> ReferenceCas:
        return ReferenceCas(self._cas_root)

    def _put_fact(self, value: Mapping[str, object], *, deadline: object | None) -> CasBlobFact:
        return self._cas().put(canonical_bytes(value), deadline=deadline)

    def _verify_fact(
        self,
        value: object,
        expected: Mapping[str, object],
        *,
        deadline: object | None,
    ) -> None:
        if not isinstance(value, Mapping):
            _fail(Phase8StoreError, "persisted CAS fact is malformed")
        if set(value) != {"schema_version", "blob_ref", "sha256", "byte_length"}:
            _fail(Phase8StoreError, "persisted CAS fact fields differ")
        try:
            fact = CasBlobFact(
                str(value["schema_version"]),
                str(value["blob_ref"]),
                str(value["sha256"]),
                int(value["byte_length"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise Phase8StoreError("persisted CAS fact is malformed") from exc
        if (
            fact.schema_version != "phase8-cas-blob-v1"
            or fact.blob_ref != f"sha256:{fact.sha256}"
            or _SHA256.fullmatch(fact.sha256) is None
            or fact.byte_length < 0
        ):
            _fail(Phase8StoreError, "persisted CAS fact identity differs")
        raw = self._cas().get(
            fact.blob_ref, expected_length=fact.byte_length, deadline=deadline
        )
        if raw != canonical_bytes(expected):
            _fail(Phase8StoreError, "persisted CAS fact content differs")

    @staticmethod
    def _head_is_current(
        verifier: CurrentHeadVerifier | None,
        scope_key: str,
        commit_sha256: str,
    ) -> bool:
        if verifier is None:
            _fail(Phase8ContractError, "a Phase-7 current-head verifier is required")
        try:
            return verifier(scope_key, commit_sha256) is True
        except Phase8RuntimeError:
            raise
        except BaseException as exc:
            if getattr(exc, "code", None) in {
                "PHASE78_DEADLINE_EXCEEDED",
                "PHASE78_REQUEST_CANCELLED",
            }:
                raise
            raise Phase8CurrentConflict("Phase-7 current-head verification failed") from exc

    def record_reference_binding(
        self,
        *,
        idempotency_key: str,
        logical_id: str,
        phase3_artifact_state: object,
        phase3_artifact_occurrence: object,
        phase6_access_proof: object,
        phase7_result: object,
        phase7_receipt: object,
        phase7_effective_verdict: object,
        reference_package_blob: Mapping[str, object] | CasBlobFact,
        reference_receipt_blob: Mapping[str, object] | CasBlobFact,
        phase7_current_head_verifier: CurrentHeadVerifier,
        publication_identity: Mapping[str, object] | None = None,
        publication_head_verifier: PublicationHeadVerifier | None = None,
        expected_current_binding_sha256: str | None = None,
        deadline: object | None = None,
        adapter_fence: AdapterFence | None = None,
    ) -> ReferenceBindingResult:
        """Bind exact Phase-3/6/7/reference facts and publish one current head."""

        self._require_enabled()  # before paths, CAS, SQLite, or verifier callbacks
        key = _identifier(idempotency_key, "idempotency_key")
        logical = _identifier(logical_id, "logical_id")
        if expected_current_binding_sha256 is not None:
            _sha(expected_current_binding_sha256, "expected_current_binding_sha256")
        _check_deadline(deadline, "reference binding validation")
        state, state_wire = _artifact_state(phase3_artifact_state)
        occurrence = _occurrence(phase3_artifact_occurrence)
        occurrence_wire = _json_safe(
            occurrence.as_dict(), "phase3_artifact_occurrence"
        )
        proof = _access_proof(phase6_access_proof)
        authority, source_coordinate = _cross_check_phase3_6(
            state, state_wire, occurrence, proof
        )
        state_sha = _sha(state_wire.get("state_sha256"), "state_sha256")
        proof_sha = _sha(proof.get("proof_sha256"), "proof_sha256")
        p7_result, p7_receipt, p7_effective = _phase7_bundle(
            phase7_result,
            phase7_receipt,
            phase7_effective_verdict,
            state_sha256=state_sha,
            authority_revision=int(state_wire["through_revision"]),
            occurrence=occurrence,
            proof_sha256=proof_sha,
        )
        package = load_reference_package(
            cas_root=self._cas_root,
            package_blob=reference_package_blob,
            receipt_blob=reference_receipt_blob,
            deadline=deadline,
        )
        package_occurrence = _occurrence(package.package["phase3_artifact_occurrence"])
        if package_occurrence != occurrence:
            _fail(Phase8CurrentConflict, "reference package occurrence differs")
        source_path = str(package.package["source"]["normalized_path"])
        logical_path = PurePosixPath(source_path)
        if logical_path.is_absolute() or ".." in logical_path.parts or "\\" in source_path:
            _fail(Phase8ContractError, "reference source path is not logical")
        if source_path != occurrence.normalized_path:
            _fail(Phase8CurrentConflict, "reference package source path differs")
        scope_identity: dict[str, object] = {
            "schema_version": "phase8-reference-scope-v1",
            "workflow_id": occurrence.workflow_id,
            "project_id": authority["project_id"],
            "normalized_path": occurrence.normalized_path,
            "reference_id": package.package["reference_id"],
        }
        scope_key = canonical_sha256(scope_identity)
        p7_scope = _sha(p7_result["scope_key"], "phase7_result.scope_key")
        p7_commit = _sha(p7_result["commit_sha256"], "phase7_result.commit_sha256")
        if not self._head_is_current(phase7_current_head_verifier, p7_scope, p7_commit):
            _fail(Phase8CurrentConflict, "Phase-7 grounding head is stale")
        publication = _publication_identity(
            publication_identity,
            publication_key=key,
            phase7_scope_key=p7_scope,
            phase7_commit_sha256=p7_commit,
        )
        publication_sha = str(publication["publication_sha256"])
        publication_json = _canonical_json(publication)
        if not _publication_is_current(publication, publication_head_verifier):
            _fail(Phase8CurrentConflict, "publication generation is not current")

        # CAS first: every independently verified component is durable and
        # re-readable before SQLite can publish a pointer.  Orphans on rollback
        # are harmless immutable facts.
        component_values: dict[str, Mapping[str, object]] = {
            "phase3_artifact_state": state_wire,
            "phase3_artifact_occurrence": occurrence_wire,
            "phase6_access_proof": proof,
            "phase7_result": p7_result,
            "phase7_receipt": p7_receipt,
            "phase7_effective_verdict": p7_effective,
            "reference_package": package.package,
            "reference_package_receipt": package.receipt,
        }
        component_blobs = {
            name: self._put_fact(value, deadline=deadline).as_dict()
            for name, value in component_values.items()
        }
        package_blob_wire = (
            reference_package_blob.as_dict()
            if type(reference_package_blob) is CasBlobFact
            else _mapping(reference_package_blob, "reference_package_blob")
        )
        receipt_blob_wire = (
            reference_receipt_blob.as_dict()
            if type(reference_receipt_blob) is CasBlobFact
            else _mapping(reference_receipt_blob, "reference_receipt_blob")
        )
        request: dict[str, object] = {
            "schema_version": "phase8-reference-binding-request-v1",
            "idempotency_key": key,
            "logical_id": logical,
            "scope_key": scope_key,
            "phase3_artifact_state_sha256": state_sha,
            "phase3_artifact_occurrence_id": occurrence.occurrence_id,
            "phase6_access_proof_sha256": proof_sha,
            "phase7_commit_sha256": p7_commit,
            "reference_package_sha256": package.package["package_sha256"],
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
        request_sha = canonical_sha256(request)
        binding_body: dict[str, object] = {
            "schema_version": PHASE8_BINDING_SCHEMA,
            "logical_id": logical,
            "scope": scope_identity,
            "scope_key": scope_key,
            "phase3_artifact_state": state_wire,
            "phase3_artifact_state_sha256": state_sha,
            "phase3_artifact_occurrence": occurrence_wire,
            "phase3_artifact_occurrence_id": occurrence.occurrence_id,
            "phase6_access_proof": proof,
            "phase6_access_proof_sha256": proof_sha,
            "authority_coordinate_sha256": proof["source_binding"]["authority_coordinate_sha256"],
            "source_snapshot_coordinate_sha256": proof["source_binding"]["source_snapshot_coordinate_sha256"],
            "authority_coordinate": authority,
            "source_snapshot_coordinate": source_coordinate,
            "phase7_result": p7_result,
            "phase7_receipt": p7_receipt,
            "phase7_effective_verdict": p7_effective,
            "phase7_scope_key": p7_scope,
            "phase7_commit_sha256": p7_commit,
            "reference_package_blob": package_blob_wire,
            "reference_receipt_blob": receipt_blob_wire,
            "reference_package_sha256": package.package["package_sha256"],
            "reference_record_sha256": package.record.record_sha256,
            "component_blobs": component_blobs,
            "request_sha256": request_sha,
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
        binding = {**binding_body, "binding_sha256": canonical_sha256(binding_body)}
        binding_sha = str(binding["binding_sha256"])
        binding_blob = self._put_fact(binding, deadline=deadline)
        _adapter_fence(adapter_fence, "after_cas_before_sqlite")

        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            self._transaction(
                connection, deadline=deadline, adapter_fence=adapter_fence
            )
            existing = connection.execute(
                "SELECT * FROM phase8_reference_bindings WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    _fail(Phase8ReplayConflict, "reference binding key reused with different request")
                replay = self._binding_result_from_row(
                    existing, replayed=True, deadline=deadline
                )
                self._verify_binding_deep(replay.binding, deadline=deadline)
                replay_current = connection.execute(
                    "SELECT sequence,binding_sha256,publication_sha256,publication_json "
                    "FROM phase8_reference_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                if (
                    replay_current is not None
                    and int(replay_current["sequence"]) == int(existing["sequence"])
                    and replay_current["binding_sha256"] == binding_sha
                    and replay_current["publication_sha256"] == publication_sha
                ):
                    connection.rollback()
                    _adapter_fence(adapter_fence, "after_sqlite_replay")
                    _check_deadline(deadline, "reference binding replay")
                    if not self._head_is_current(
                        phase7_current_head_verifier, p7_scope, p7_commit
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "Phase-7 head changed before binding replay",
                        )
                    if not _publication_is_current(
                        publication, publication_head_verifier
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "publication generation changed before binding replay",
                        )
                    return replace(replay, current=True)
                publication_takeover = (
                    replay_current is not None
                    and int(replay_current["sequence"]) == int(existing["sequence"])
                    and replay_current["binding_sha256"] == binding_sha
                )
                replay_previous = (
                    None
                    if replay_current is None
                    else str(replay_current["binding_sha256"])
                )
                replay_previous_sequence = (
                    0 if replay_current is None else int(replay_current["sequence"])
                )
                if not publication_takeover and (
                    replay_previous != expected_current_binding_sha256
                    or existing["previous_binding_sha256"]
                    != expected_current_binding_sha256
                    or int(existing["sequence"]) != replay_previous_sequence + 1
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "reference binding replay lost its exact predecessor",
                    )
                replay_previous_publication_sha = (
                    None
                    if replay_current is None
                    else str(replay_current["publication_sha256"])
                )
                replay_previous_publication_json = (
                    None
                    if replay_current is None
                    else str(replay_current["publication_json"])
                )
                replay_sequence = int(existing["sequence"])
                connection.execute(
                    """INSERT INTO phase8_reference_current(
                       scope_key,sequence,binding_sha256,
                       publication_sha256,publication_json
                       ) VALUES(?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                       sequence=excluded.sequence,
                       binding_sha256=excluded.binding_sha256,
                       publication_sha256=excluded.publication_sha256,
                       publication_json=excluded.publication_json""",
                    (
                        scope_key,
                        replay_sequence,
                        binding_sha,
                        publication_sha,
                        publication_json,
                    ),
                )

                def replay_post_commit_fence(
                    committed: sqlite3.Connection,
                ) -> None:
                    winning = committed.execute(
                        "SELECT sequence,binding_sha256,publication_sha256 "
                        "FROM phase8_reference_current WHERE scope_key=?",
                        (scope_key,),
                    ).fetchone()
                    if (
                        winning is None
                        or int(winning["sequence"]) != replay_sequence
                        or winning["binding_sha256"] != binding_sha
                        or winning["publication_sha256"] != publication_sha
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "reference binding replay lost its post-commit head",
                        )
                    if not self._head_is_current(
                        phase7_current_head_verifier, p7_scope, p7_commit
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "Phase-7 head changed after binding replay",
                        )
                    if not _publication_is_current(
                        publication, publication_head_verifier
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "publication generation changed after binding replay",
                        )

                def reconcile_replayed_reference(
                    committed: sqlite3.Connection,
                ) -> None:
                    winning = committed.execute(
                        "SELECT sequence,binding_sha256 "
                        "FROM phase8_reference_current WHERE scope_key=?",
                        (scope_key,),
                    ).fetchone()
                    if (
                        winning is None
                        or int(winning["sequence"]) != replay_sequence
                        or winning["binding_sha256"] != binding_sha
                    ):
                        return
                    if replay_previous is None:
                        committed.execute(
                            "DELETE FROM phase8_reference_current "
                            "WHERE scope_key=? AND sequence=? AND binding_sha256=?",
                            (scope_key, replay_sequence, binding_sha),
                        )
                    elif publication_takeover:
                        committed.execute(
                            "UPDATE phase8_reference_current "
                            "SET publication_sha256=?,publication_json=? "
                            "WHERE scope_key=? AND sequence=? AND binding_sha256=? "
                            "AND publication_sha256=?",
                            (
                                replay_previous_publication_sha,
                                replay_previous_publication_json,
                                scope_key,
                                replay_sequence,
                                binding_sha,
                                publication_sha,
                            ),
                        )
                    else:
                        committed.execute(
                            "UPDATE phase8_reference_current "
                            "SET sequence=?,binding_sha256=?,publication_sha256=?,publication_json=? "
                            "WHERE scope_key=? AND sequence=? AND binding_sha256=?",
                            (
                                replay_previous_sequence,
                                replay_previous,
                                replay_previous_publication_sha,
                                replay_previous_publication_json,
                                scope_key,
                                replay_sequence,
                                binding_sha,
                            ),
                        )

                self._commit(
                    connection,
                    idempotency_key=key,
                    deadline=deadline,
                    adapter_fence=adapter_fence,
                    post_commit_fence=replay_post_commit_fence,
                    post_commit_reconciler=reconcile_replayed_reference,
                )
                return replace(replay, current=True)
            conflict = connection.execute(
                "SELECT request_sha256 FROM phase8_reference_bindings WHERE logical_id=?",
                (logical,),
            ).fetchone()
            if conflict is not None:
                _fail(Phase8ReplayConflict, "reference logical_id already binds different bytes")
            current = connection.execute(
                "SELECT * FROM phase8_reference_current WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            previous = None if current is None else str(current["binding_sha256"])
            sequence = 1 if current is None else int(current["sequence"]) + 1
            previous_publication_sha = (
                None if current is None else str(current["publication_sha256"])
            )
            previous_publication_json = (
                None if current is None else str(current["publication_json"])
            )
            if previous != expected_current_binding_sha256:
                _fail(Phase8CurrentConflict, "reference binding compare-and-swap differs")
            if not self._head_is_current(phase7_current_head_verifier, p7_scope, p7_commit):
                _fail(Phase8CurrentConflict, "Phase-7 head changed before binding commit")
            connection.execute(
                """INSERT INTO phase8_reference_bindings(
                    binding_sha256,logical_id,idempotency_key,request_sha256,
                    scope_key,sequence,previous_binding_sha256,binding_json,binding_blob_json
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    binding_sha, logical, key, request_sha, scope_key, sequence,
                    previous, _canonical_json(binding), _canonical_json(binding_blob.as_dict()),
                ),
            )
            def reference_history_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                historical = committed.execute(
                    "SELECT sequence,binding_sha256,previous_binding_sha256 "
                    "FROM phase8_reference_bindings WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                if (
                    historical is None
                    or int(historical["sequence"]) != sequence
                    or historical["binding_sha256"] != binding_sha
                    or historical["previous_binding_sha256"] != previous
                ):
                    _fail(
                        Phase8StoreError,
                        "reference binding history differs after commit",
                    )
                if not self._head_is_current(
                    phase7_current_head_verifier, p7_scope, p7_commit
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "Phase-7 head changed after binding commit",
                    )

            # First durable boundary: immutable history/idempotency only.
            # A crash here exposes no new current pointer.  The same exact key
            # may later replay and activate after winning-fence validation.
            self._commit(
                connection,
                idempotency_key=key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=reference_history_post_commit_fence,
            )

            self._transaction(
                connection, deadline=deadline, adapter_fence=adapter_fence
            )
            activation_current = connection.execute(
                "SELECT sequence,binding_sha256 FROM phase8_reference_current "
                "WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            activation_previous = (
                None
                if activation_current is None
                else str(activation_current["binding_sha256"])
            )
            activation_previous_sequence = (
                0 if activation_current is None else int(activation_current["sequence"])
            )
            if (
                activation_previous != previous
                or activation_previous_sequence + 1 != sequence
            ):
                _fail(
                    Phase8CurrentConflict,
                    "reference binding activation lost its exact predecessor",
                )
            if not self._head_is_current(
                phase7_current_head_verifier, p7_scope, p7_commit
            ):
                _fail(
                    Phase8CurrentConflict,
                    "Phase-7 head changed before binding activation",
                )
            if not _publication_is_current(publication, publication_head_verifier):
                _fail(
                    Phase8CurrentConflict,
                    "publication generation changed before binding activation",
                )
            connection.execute(
                """INSERT INTO phase8_reference_current(
                   scope_key,sequence,binding_sha256,
                   publication_sha256,publication_json
                   ) VALUES(?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                   sequence=excluded.sequence,
                   binding_sha256=excluded.binding_sha256,
                   publication_sha256=excluded.publication_sha256,
                   publication_json=excluded.publication_json""",
                (
                    scope_key,
                    sequence,
                    binding_sha,
                    publication_sha,
                    publication_json,
                ),
            )

            def reference_activation_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                winning = committed.execute(
                    "SELECT sequence,binding_sha256,publication_sha256 "
                    "FROM phase8_reference_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                if (
                    winning is None
                    or int(winning["sequence"]) != sequence
                    or winning["binding_sha256"] != binding_sha
                    or winning["publication_sha256"] != publication_sha
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "reference binding lost its exact activation head",
                    )
                if not self._head_is_current(
                    phase7_current_head_verifier, p7_scope, p7_commit
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "Phase-7 head changed after binding activation",
                    )
                if not _publication_is_current(
                    publication, publication_head_verifier
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "publication generation changed after binding activation",
                    )

            def reconcile_reference_current(
                committed: sqlite3.Connection,
            ) -> None:
                winning = committed.execute(
                    "SELECT sequence,binding_sha256,publication_sha256 "
                    "FROM phase8_reference_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                if (
                    winning is None
                    or int(winning["sequence"]) != sequence
                    or winning["binding_sha256"] != binding_sha
                    or winning["publication_sha256"] != publication_sha
                ):
                    return
                if previous is None:
                    committed.execute(
                        "DELETE FROM phase8_reference_current "
                        "WHERE scope_key=? AND sequence=? AND binding_sha256=?",
                        (scope_key, sequence, binding_sha),
                    )
                else:
                    committed.execute(
                        "UPDATE phase8_reference_current "
                        "SET sequence=?,binding_sha256=?,publication_sha256=?,publication_json=? "
                        "WHERE scope_key=? AND sequence=? AND binding_sha256=?",
                        (
                            sequence - 1,
                            previous,
                            previous_publication_sha,
                            previous_publication_json,
                            scope_key,
                            sequence,
                            binding_sha,
                        ),
                    )

            self._commit(
                connection,
                idempotency_key=key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=reference_activation_post_commit_fence,
                post_commit_reconciler=reconcile_reference_current,
            )
            return ReferenceBindingResult(
                binding_sha,
                scope_key,
                sequence,
                previous,
                key,
                False,
                True,
                binding,
            )
        except BaseException as error:
            primary = error
            if connection.in_transaction:
                run_cleanup(
                    [("rollback Phase-8 reference binding", RetryableCleanup(connection.rollback))],
                    primary=error,
                )
            raise
        finally:
            self._close(connection, primary)

    def _binding_result_from_row(
        self,
        row: sqlite3.Row,
        *,
        replayed: bool,
        deadline: object | None,
        current: bool = False,
    ) -> ReferenceBindingResult:
        binding = _decode_json(row["binding_json"], "reference binding")
        binding_sha = _hashed(binding, "binding_sha256", "reference binding")
        if binding_sha != row["binding_sha256"]:
            _fail(Phase8StoreError, "reference binding row identity differs")
        blob = _decode_json(row["binding_blob_json"], "reference binding blob")
        self._verify_fact(blob, binding, deadline=deadline)
        return ReferenceBindingResult(
            binding_sha,
            str(row["scope_key"]),
            int(row["sequence"]),
            None if row["previous_binding_sha256"] is None else str(row["previous_binding_sha256"]),
            str(row["idempotency_key"]),
            replayed,
            current,
            binding,
        )

    def load_reference_binding(
        self,
        binding_sha256: str,
        *,
        deadline: object | None = None,
    ) -> ReferenceBindingResult:
        self._require_enabled()
        digest = _sha(binding_sha256, "binding_sha256")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            row = connection.execute(
                "SELECT * FROM phase8_reference_bindings WHERE binding_sha256=?",
                (digest,),
            ).fetchone()
            if row is None:
                _fail(Phase8NotFound, "reference binding is unavailable")
            result = self._binding_result_from_row(row, replayed=False, deadline=deadline)
            self._verify_binding_deep(result.binding, deadline=deadline)
            # Immutable history alone never proves that this binding is the
            # effective current publication.
            return result
        except BaseException as error:
            primary = error
            raise
        finally:
            self._close(connection, primary)

    def load_reference_binding_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        deadline: object | None = None,
    ) -> ReferenceBindingResult:
        """Query immutable binding history by the caller's exact replay key."""

        self._require_enabled()
        key = _identifier(idempotency_key, "idempotency_key")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            row = connection.execute(
                "SELECT * FROM phase8_reference_bindings WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row is None:
                _fail(Phase8NotFound, "reference binding history is unavailable")
            result = self._binding_result_from_row(
                row, replayed=True, deadline=deadline
            )
            self._verify_binding_deep(result.binding, deadline=deadline)
            # Exact-key history remains durable and replayable even when its
            # activation never succeeded or is no longer current.
            return result
        except BaseException as error:
            primary = error
            raise
        finally:
            self._close(connection, primary)

    def _verify_binding_deep(
        self, binding: Mapping[str, object], *, deadline: object | None
    ) -> None:
        _false_safety(binding, "reference binding")
        state, state_wire = _artifact_state(binding.get("phase3_artifact_state"))
        occurrence = _occurrence(binding.get("phase3_artifact_occurrence"))
        proof = _access_proof(binding.get("phase6_access_proof"))
        _cross_check_phase3_6(state, state_wire, occurrence, proof)
        _phase7_bundle(
            binding.get("phase7_result"),
            binding.get("phase7_receipt"),
            binding.get("phase7_effective_verdict"),
            state_sha256=str(binding.get("phase3_artifact_state_sha256")),
            authority_revision=int(state_wire["through_revision"]),
            occurrence=occurrence,
            proof_sha256=str(binding.get("phase6_access_proof_sha256")),
        )
        package = load_reference_package(
            cas_root=self._cas_root,
            package_blob=binding.get("reference_package_blob"),
            receipt_blob=binding.get("reference_receipt_blob"),
            deadline=deadline,
        )
        if (
            package.package.get("package_sha256") != binding.get("reference_package_sha256")
            or package.record.record_sha256 != binding.get("reference_record_sha256")
            or _occurrence(package.package["phase3_artifact_occurrence"]) != occurrence
        ):
            _fail(Phase8StoreError, "reference package binding differs on restart")
        components = binding.get("component_blobs")
        if not isinstance(components, Mapping):
            _fail(Phase8StoreError, "reference component CAS mapping is malformed")
        expected = {
            "phase3_artifact_state": state_wire,
            "phase3_artifact_occurrence": _json_safe(
                occurrence.as_dict(), "phase3_artifact_occurrence"
            ),
            "phase6_access_proof": proof,
            "phase7_result": _mapping(binding.get("phase7_result"), "phase7 result"),
            "phase7_receipt": _mapping(binding.get("phase7_receipt"), "phase7 receipt"),
            "phase7_effective_verdict": _mapping(binding.get("phase7_effective_verdict"), "phase7 effective"),
            "reference_package": package.package,
            "reference_package_receipt": package.receipt,
        }
        if set(components) != set(expected):
            _fail(Phase8StoreError, "reference component CAS coverage differs")
        for name, value in expected.items():
            self._verify_fact(components[name], value, deadline=deadline)

    def load_current_reference_binding(
        self,
        scope_key: str,
        *,
        phase7_current_head_verifier: CurrentHeadVerifier,
        publication_head_verifier: PublicationHeadVerifier | None = None,
        expected_binding_sha256: str | None = None,
        deadline: object | None = None,
    ) -> ReferenceBindingResult:
        self._require_enabled()
        scope = _sha(scope_key, "scope_key")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            row = connection.execute(
                """SELECT b.*,c.publication_sha256 AS current_publication_sha256,
                          c.publication_json AS current_publication_json
                   FROM phase8_reference_current c
                   JOIN phase8_reference_bindings b ON b.binding_sha256=c.binding_sha256
                   WHERE c.scope_key=?""",
                (scope,),
            ).fetchone()
            if row is None:
                _fail(Phase8NotFound, "current reference binding is unavailable")
            if expected_binding_sha256 is not None and row["binding_sha256"] != _sha(expected_binding_sha256, "expected_binding_sha256"):
                _fail(Phase8CurrentConflict, "current reference binding changed")
            result = self._binding_result_from_row(row, replayed=False, deadline=deadline)
            self._verify_binding_deep(result.binding, deadline=deadline)
            publication = _decode_publication(
                row["current_publication_sha256"],
                row["current_publication_json"],
                field="reference current publication",
            )
            if (
                publication["phase7_scope_key"]
                != result.binding["phase7_scope_key"]
                or publication["phase7_commit_sha256"]
                != result.binding["phase7_commit_sha256"]
                or not _publication_is_current(
                    publication, publication_head_verifier
                )
            ):
                _fail(
                    Phase8CurrentConflict,
                    "reference publication generation is not current",
                )
            if not self._head_is_current(
                phase7_current_head_verifier,
                str(result.binding["phase7_scope_key"]),
                str(result.binding["phase7_commit_sha256"]),
            ):
                _fail(Phase8CurrentConflict, "Phase-7 head drifted after binding")
            # Promote only after the pointer publication, exact winning
            # generation, and live Phase-7 head have all been qualified.
            return replace(result, current=True)
        except BaseException as error:
            primary = error
            raise
        finally:
            self._close(connection, primary)

    def _trusted_preflight_from_row(
        self,
        row: sqlite3.Row,
        *,
        replayed: bool,
        deadline: object | None,
    ) -> TrustedApprovalPreflightResult:
        preflight = _decode_json(row["preflight_json"], "trusted approval preflight")
        expected_fields = {
            "schema_version", "preflight_id", "approval_id", "binding_sha256",
            "scope_key", "issuer", "subject", "generation_set",
            "logical_issued_at", "not_before", "expires_at",
            "decision_evaluated_at", "successor_of",
            "expected_predecessor_event_sha256", "policy_sha256",
            "data_egress_request", "data_egress_request_sha256",
            "staged_manifest", "staged_manifest_sha256", "exact_artifacts",
            "phase7_scope_key", "phase7_commit_sha256",
            "reference_package_sha256", "reference_record_sha256",
            "authoritative", "authority_transferred", "dispatch_performed",
            "preflight_sha256",
        }
        if set(preflight) != expected_fields:
            _fail(Phase8StoreError, "trusted approval preflight fields differ")
        if preflight.get("schema_version") != PHASE8_APPROVAL_PREFLIGHT_SCHEMA:
            _fail(Phase8StoreError, "trusted approval preflight schema differs")
        digest = _hashed(
            preflight, "preflight_sha256", "trusted approval preflight"
        )
        if digest != row["preflight_sha256"]:
            _fail(Phase8StoreError, "trusted approval preflight row identity differs")
        _false_safety(preflight, "trusted approval preflight")
        for field in (
            "binding_sha256", "scope_key", "policy_sha256",
            "data_egress_request_sha256", "staged_manifest_sha256",
            "phase7_scope_key", "phase7_commit_sha256",
            "reference_package_sha256", "reference_record_sha256",
        ):
            _sha(preflight.get(field), f"trusted preflight.{field}")
        _identifier(preflight.get("preflight_id"), "preflight_id")
        _identifier(preflight.get("approval_id"), "approval_id")
        issuer = _mapping(preflight.get("issuer"), "trusted preflight issuer")
        subject = _mapping(preflight.get("subject"), "trusted preflight subject")
        if set(issuer) != {"id", "generation"} or set(subject) != {"id", "generation"}:
            _fail(Phase8StoreError, "trusted preflight principals differ")
        for principal, value in (("issuer", issuer), ("subject", subject)):
            _identifier(value.get("id"), f"trusted preflight {principal}.id")
            _identifier(
                value.get("generation"),
                f"trusted preflight {principal}.generation",
            )
        issued = _integer(preflight.get("logical_issued_at"), "logical_issued_at")
        starts = _integer(preflight.get("not_before"), "not_before")
        expires = _integer(preflight.get("expires_at"), "expires_at")
        evaluated = _integer(
            preflight.get("decision_evaluated_at"), "decision_evaluated_at"
        )
        if not issued <= starts <= evaluated < expires:
            _fail(Phase8StoreError, "trusted preflight logical times differ")
        predecessor = preflight.get("successor_of")
        predecessor_event = preflight.get("expected_predecessor_event_sha256")
        if predecessor is None:
            if predecessor_event is not None:
                _fail(Phase8StoreError, "trusted preflight predecessor fields differ")
        else:
            _identifier(predecessor, "trusted preflight successor_of")
            _sha(predecessor_event, "trusted preflight predecessor event")
        request = _mapping(
            preflight.get("data_egress_request"), "trusted preflight request"
        )
        if canonical_sha256(request) != preflight["data_egress_request_sha256"]:
            _fail(Phase8StoreError, "trusted preflight request hash differs")
        staged = _historical_staged_manifest(
            request,
            preflight.get("staged_manifest"),
            field="trusted preflight",
        )
        if (
            staged["staged_manifest_sha256"]
            != preflight.get("staged_manifest_sha256")
            or staged["policy_sha256"] != preflight.get("policy_sha256")
            or list(staged["artifacts"]) != preflight.get("exact_artifacts")
            or staged["subject"] != subject["id"]
        ):
            _fail(Phase8StoreError, "trusted preflight exact staging differs")
        self._verify_fact(
            _decode_json(
                row["preflight_blob_json"], "trusted approval preflight CAS fact"
            ),
            preflight,
            deadline=deadline,
        )
        return TrustedApprovalPreflightResult(preflight, replayed)

    def load_trusted_approval_preflight(
        self,
        preflight_sha256: str,
        *,
        deadline: object | None = None,
    ) -> TrustedApprovalPreflightResult:
        """Load a durable operator-created receipt; never infer one from a grant."""

        self._require_enabled()
        digest = _sha(preflight_sha256, "preflight_sha256")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            row = connection.execute(
                "SELECT * FROM phase8_trusted_approval_preflights WHERE preflight_sha256=?",
                (digest,),
            ).fetchone()
            if row is None:
                _fail(Phase8NotFound, "trusted approval preflight is unavailable")
            return self._trusted_preflight_from_row(
                row, replayed=False, deadline=deadline
            )
        except BaseException as error:
            primary = error
            raise
        finally:
            self._close(connection, primary)

    def register_trusted_approval_preflight(
        self,
        *,
        idempotency_key: str,
        preflight_id: str,
        approval_id: str,
        binding_sha256: str,
        issuer_id: str,
        issuer_generation: str,
        subject_id: str,
        subject_generation: str,
        logical_issued_at: int,
        not_before: int,
        expires_at: int,
        decision_evaluated_at: int,
        data_egress_request: Mapping[str, object],
        phase7_current_head_verifier: CurrentHeadVerifier,
        publication_head_verifier: PublicationHeadVerifier | None = None,
        successor_of: str | None = None,
        expected_predecessor_event_sha256: str | None = None,
        deadline: object | None = None,
        adapter_fence: AdapterFence | None = None,
    ) -> TrustedApprovalPreflightResult:
        """Persist an operator-only local approval intent.

        CLI, Web, service and the Phase-78 Runner intentionally expose no path
        to this method.  A Phase-6 ``snapshot:view`` proof is an upstream read
        fence only and can never create this receipt.
        """

        self._require_enabled()
        key = _identifier(idempotency_key, "idempotency_key")
        preflight_identity = _identifier(preflight_id, "preflight_id")
        approval_identity = _identifier(approval_id, "approval_id")
        binding_identity = _sha(binding_sha256, "binding_sha256")
        issuer = _identifier(issuer_id, "issuer_id")
        issuer_gen = _identifier(issuer_generation, "issuer_generation")
        subject = _identifier(subject_id, "subject_id")
        subject_gen = _identifier(subject_generation, "subject_generation")
        issued = _integer(logical_issued_at, "logical_issued_at")
        starts = _integer(not_before, "not_before")
        expires = _integer(expires_at, "expires_at")
        evaluated = _integer(decision_evaluated_at, "decision_evaluated_at")
        if not issued <= starts <= evaluated < expires:
            _fail(Phase8ContractError, "trusted approval time interval is invalid")
        if successor_of is not None:
            successor_of = _identifier(successor_of, "successor_of")
            if expected_predecessor_event_sha256 is None:
                _fail(Phase8ContractError, "successor preflight requires event CAS")
            expected_predecessor_event_sha256 = _sha(
                expected_predecessor_event_sha256,
                "expected_predecessor_event_sha256",
            )
        elif expected_predecessor_event_sha256 is not None:
            _fail(Phase8ContractError, "predecessor event without successor is invalid")
        request = _mapping(data_egress_request, "data_egress_request")
        try:
            staged = evaluate_data_egress(request).staged_manifest.as_dict()
        except DataEgressError as exc:
            raise Phase8ContractError("trusted preflight request does not stage") from exc
        if staged["subject"] != subject:
            _fail(Phase8ContractError, "trusted preflight subject differs from request")
        binding_result = self.load_current_reference_binding(
            self.load_reference_binding(binding_identity, deadline=deadline).scope_key,
            phase7_current_head_verifier=phase7_current_head_verifier,
            publication_head_verifier=publication_head_verifier,
            expected_binding_sha256=binding_identity,
            deadline=deadline,
        )
        binding = binding_result.binding
        if (
            binding["phase7_effective_verdict"].get("aggregate_verdict") != "PASS"
            or binding["phase7_result"].get("grounding_valid") is not True
        ):
            _fail(Phase8CurrentConflict, "trusted preflight evidence is not grounded PASS")
        authority = _mapping(binding["authority_coordinate"], "authority coordinate")
        generation_set = {
            name: authority[name]
            for name in (
                "project_generation", "run_generation", "runtime_generation",
                "scheduler_generation",
            )
        }
        if successor_of is not None:
            predecessor = self.load_current_approval(
                str(binding["scope_key"]),
                phase7_current_head_verifier=phase7_current_head_verifier,
                publication_head_verifier=publication_head_verifier,
                deadline=deadline,
            )
            if (
                predecessor.approval["approval_id"] != successor_of
                or predecessor.lifecycle_event["event_sha256"]
                != expected_predecessor_event_sha256
                or predecessor.lifecycle_event["state"] != "ACTIVE"
            ):
                _fail(Phase8CurrentConflict, "trusted preflight predecessor is stale")
        body: dict[str, object] = {
            "schema_version": PHASE8_APPROVAL_PREFLIGHT_SCHEMA,
            "preflight_id": preflight_identity,
            "approval_id": approval_identity,
            "binding_sha256": binding_identity,
            "scope_key": binding["scope_key"],
            "issuer": {"id": issuer, "generation": issuer_gen},
            "subject": {"id": subject, "generation": subject_gen},
            "generation_set": generation_set,
            "logical_issued_at": issued,
            "not_before": starts,
            "expires_at": expires,
            "decision_evaluated_at": evaluated,
            "successor_of": successor_of,
            "expected_predecessor_event_sha256": expected_predecessor_event_sha256,
            "policy_sha256": staged["policy_sha256"],
            "data_egress_request": request,
            "data_egress_request_sha256": canonical_sha256(request),
            "staged_manifest": staged,
            "staged_manifest_sha256": staged["staged_manifest_sha256"],
            "exact_artifacts": list(staged["artifacts"]),
            "phase7_scope_key": binding["phase7_scope_key"],
            "phase7_commit_sha256": binding["phase7_commit_sha256"],
            "reference_package_sha256": binding["reference_package_sha256"],
            "reference_record_sha256": binding["reference_record_sha256"],
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
        preflight = {**body, "preflight_sha256": canonical_sha256(body)}
        request_sha = canonical_sha256(
            {
                "schema_version": "phase8-trusted-preflight-registration-request-v1",
                "idempotency_key": key,
                "preflight": preflight,
            }
        )
        blob = self._put_fact(preflight, deadline=deadline)
        _adapter_fence(adapter_fence, "after_cas_before_sqlite")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            self._transaction(connection, deadline=deadline, adapter_fence=adapter_fence)
            existing = connection.execute(
                "SELECT * FROM phase8_trusted_approval_preflights WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    _fail(Phase8ReplayConflict, "trusted preflight key reused")
                connection.rollback()
                replay = self._trusted_preflight_from_row(
                    existing, replayed=True, deadline=deadline
                )
                _adapter_fence(adapter_fence, "after_sqlite_replay")
                _check_deadline(deadline, "trusted preflight replay")
                current = connection.execute(
                    "SELECT binding_sha256,publication_sha256,publication_json "
                    "FROM phase8_reference_current "
                    "WHERE scope_key=?",
                    (binding["scope_key"],),
                ).fetchone()
                if current is None or current["binding_sha256"] != binding_identity:
                    _fail(
                        Phase8CurrentConflict,
                        "reference binding changed before preflight replay",
                    )
                replay_publication = _decode_publication(
                    current["publication_sha256"],
                    current["publication_json"],
                    field="preflight replay reference publication",
                )
                if not _publication_is_current(
                    replay_publication, publication_head_verifier
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "reference publication changed before preflight replay",
                    )
                if not self._head_is_current(
                    phase7_current_head_verifier,
                    str(binding["phase7_scope_key"]),
                    str(binding["phase7_commit_sha256"]),
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "Phase-7 head changed before preflight replay",
                    )
                return replay
            if connection.execute(
                "SELECT 1 FROM phase8_trusted_approval_preflights WHERE preflight_id=?",
                (preflight_identity,),
            ).fetchone() is not None:
                _fail(Phase8ReplayConflict, "preflight_id already binds different bytes")
            current = connection.execute(
                "SELECT binding_sha256,publication_sha256,publication_json "
                "FROM phase8_reference_current WHERE scope_key=?",
                (binding["scope_key"],),
            ).fetchone()
            if current is None or current["binding_sha256"] != binding_identity:
                _fail(Phase8CurrentConflict, "reference binding changed before preflight")
            current_publication = _decode_publication(
                current["publication_sha256"],
                current["publication_json"],
                field="preflight reference publication",
            )
            if not _publication_is_current(
                current_publication, publication_head_verifier
            ):
                _fail(
                    Phase8CurrentConflict,
                    "reference publication changed before preflight",
                )
            if not self._head_is_current(
                phase7_current_head_verifier,
                str(binding["phase7_scope_key"]),
                str(binding["phase7_commit_sha256"]),
            ):
                _fail(Phase8CurrentConflict, "Phase-7 head changed before preflight")
            connection.execute(
                """INSERT INTO phase8_trusted_approval_preflights(
                   preflight_sha256,preflight_id,idempotency_key,request_sha256,
                   binding_sha256,scope_key,preflight_json,preflight_blob_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    preflight["preflight_sha256"], preflight_identity, key,
                    request_sha, binding_identity, binding["scope_key"],
                    _canonical_json(preflight), _canonical_json(blob.as_dict()),
                ),
            )
            def preflight_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                winning = committed.execute(
                    "SELECT binding_sha256,publication_sha256,publication_json "
                    "FROM phase8_reference_current "
                    "WHERE scope_key=?",
                    (binding["scope_key"],),
                ).fetchone()
                if winning is None or winning["binding_sha256"] != binding_identity:
                    _fail(
                        Phase8CurrentConflict,
                        "reference binding changed after preflight commit",
                    )
                binding_publication = _decode_publication(
                    winning["publication_sha256"],
                    winning["publication_json"],
                    field="preflight reference publication",
                )
                if not _publication_is_current(
                    binding_publication, publication_head_verifier
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "reference publication changed after preflight commit",
                    )
                if not self._head_is_current(
                    phase7_current_head_verifier,
                    str(binding["phase7_scope_key"]),
                    str(binding["phase7_commit_sha256"]),
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "Phase-7 head changed after preflight commit",
                    )

            self._commit(
                connection,
                idempotency_key=key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=preflight_post_commit_fence,
            )
            return TrustedApprovalPreflightResult(preflight, False)
        except BaseException as error:
            primary = error
            if connection.in_transaction:
                run_cleanup(
                    [("rollback trusted Phase-8 preflight", RetryableCleanup(connection.rollback))],
                    primary=error,
                )
            raise
        finally:
            self._close(connection, primary)

    @staticmethod
    def _approval_event(
        *,
        approval_id: str,
        sequence: int,
        state: str,
        effective_at: int,
        reason_code: str,
        previous_event_sha256: str | None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "schema_version": PHASE8_APPROVAL_EVENT_SCHEMA,
            "approval_id": approval_id,
            "event_sequence": sequence,
            "state": state,
            "effective_at": effective_at,
            "reason_code": reason_code,
            "previous_event_sha256": previous_event_sha256,
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
        return {**body, "event_sha256": canonical_sha256(body)}

    def _load_approval_row(
        self,
        connection: sqlite3.Connection,
        approval_id: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        approval = connection.execute(
            "SELECT * FROM phase8_approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        event = connection.execute(
            """SELECT e.* FROM phase8_approval_current c
               JOIN phase8_approval_events e ON e.event_sha256=c.event_sha256
               WHERE c.approval_id=?""",
            (approval_id,),
        ).fetchone()
        if approval is None or event is None:
            _fail(Phase8NotFound, "approval is unavailable")
        return approval, event

    def _approval_from_rows(
        self,
        approval_row: sqlite3.Row,
        event_row: sqlite3.Row,
        *,
        replayed: bool,
        deadline: object | None,
    ) -> ApprovalResult:
        approval = _decode_json(approval_row["approval_json"], "approval")
        event = _decode_json(event_row["event_json"], "approval lifecycle event")
        if (
            _hashed(approval, "approval_sha256", "approval") != approval_row["approval_sha256"]
            or _hashed(event, "event_sha256", "approval lifecycle event") != event_row["event_sha256"]
            or approval["approval_id"] != event["approval_id"]
        ):
            _fail(Phase8StoreError, "approval durable identity differs")
        _false_safety(approval, "approval")
        _false_safety(event, "approval lifecycle event")
        if approval.get("schema_version") != PHASE8_APPROVAL_SCHEMA:
            _fail(Phase8StoreError, "approval schema differs")
        if event.get("schema_version") != PHASE8_APPROVAL_EVENT_SCHEMA:
            _fail(Phase8StoreError, "approval event schema differs")
        if event.get("state") not in {"ACTIVE", "REVOKED", "EXPIRED", "SUPERSEDED"}:
            _fail(Phase8StoreError, "approval event state differs")
        _integer(event.get("event_sequence"), "event_sequence", minimum=1)
        issued = _integer(approval.get("logical_issued_at"), "logical_issued_at")
        starts = _integer(approval.get("not_before"), "not_before")
        expires = _integer(approval.get("expires_at"), "expires_at")
        if not issued <= starts < expires or approval.get("approved") is not True:
            _fail(Phase8StoreError, "approval time/grant contract differs")
        staged = _historical_staged_manifest(
            approval.get("data_egress_request"),
            approval.get("staged_manifest"),
            field="stored approval",
        )
        if (
            staged.get("staged_manifest_sha256") != approval.get("staged_manifest_sha256")
            or staged.get("policy_sha256") != approval.get("policy_sha256")
            or [
                {"artifact_id": item["artifact_id"], "sha256": item["sha256"]}
                for item in staged["artifacts"]
            ] != approval.get("exact_artifacts")
        ):
            _fail(Phase8StoreError, "stored approval exact staging differs")
        preflight = _mapping(
            approval.get("trusted_preflight"), "stored trusted approval preflight"
        )
        preflight_sha = _hashed(
            preflight, "preflight_sha256", "stored trusted approval preflight"
        )
        if (
            preflight.get("schema_version") != PHASE8_APPROVAL_PREFLIGHT_SCHEMA
            or preflight_sha != approval.get("trusted_preflight_sha256")
            or preflight_sha != approval_row["trusted_preflight_sha256"]
            or preflight.get("preflight_id") != approval.get("trusted_preflight_id")
            or preflight.get("approval_id") != approval.get("approval_id")
            or preflight.get("binding_sha256") != approval.get("binding_sha256")
            or preflight.get("scope_key") != approval.get("scope_key")
            or preflight.get("issuer") != approval.get("issuer")
            or preflight.get("subject") != approval.get("subject")
            or preflight.get("generation_set") != approval.get("generation_set")
            or preflight.get("logical_issued_at") != approval.get("logical_issued_at")
            or preflight.get("not_before") != approval.get("not_before")
            or preflight.get("expires_at") != approval.get("expires_at")
            or preflight.get("decision_evaluated_at")
            != approval.get("decision_evaluated_at")
            or preflight.get("successor_of") != approval.get("successor_of")
            or preflight.get("expected_predecessor_event_sha256")
            != approval.get("expected_predecessor_event_sha256")
            or preflight.get("policy_sha256") != approval.get("policy_sha256")
            or preflight.get("staged_manifest") != staged
            or preflight.get("staged_manifest_sha256")
            != approval.get("staged_manifest_sha256")
            or preflight.get("exact_artifacts") != list(staged["artifacts"])
            or preflight.get("phase7_commit_sha256")
            != approval.get("phase7_commit_sha256")
            or preflight.get("reference_package_sha256")
            != approval.get("reference_package_sha256")
            or preflight.get("reference_record_sha256")
            != approval.get("reference_record_sha256")
        ):
            _fail(Phase8StoreError, "stored approval trusted preflight differs")
        _false_safety(preflight, "stored trusted approval preflight")
        self._verify_fact(
            approval.get("trusted_preflight_blob"),
            preflight,
            deadline=deadline,
        )
        self._verify_fact(
            _decode_json(approval_row["approval_blob_json"], "approval CAS fact"),
            approval,
            deadline=deadline,
        )
        self._verify_fact(
            _decode_json(event_row["event_blob_json"], "event CAS fact"),
            event,
            deadline=deadline,
        )
        return ApprovalResult(approval, event, replayed)

    def load_approval(
        self,
        approval_id: str,
        *,
        deadline: object | None = None,
    ) -> ApprovalResult:
        self._require_enabled()
        identity = _identifier(approval_id, "approval_id")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            rows = self._load_approval_row(connection, identity)
            return self._approval_from_rows(*rows, replayed=False, deadline=deadline)
        except BaseException as error:
            primary = error
            raise
        finally:
            self._close(connection, primary)

    def load_current_approval(
        self,
        scope_key: str,
        *,
        phase7_current_head_verifier: CurrentHeadVerifier,
        publication_head_verifier: PublicationHeadVerifier | None = None,
        deadline: object | None = None,
    ) -> ApprovalResult:
        self._require_enabled()
        scope = _sha(scope_key, "scope_key")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            row = connection.execute(
                "SELECT approval_id,publication_sha256,publication_json "
                "FROM phase8_scope_approval_current WHERE scope_key=?",
                (scope,),
            ).fetchone()
            if row is None:
                _fail(Phase8NotFound, "current approval is unavailable")
            rows = self._load_approval_row(connection, str(row["approval_id"]))
            result = self._approval_from_rows(
                *rows, replayed=False, deadline=deadline
            )
            binding_row = connection.execute(
                "SELECT binding_json FROM phase8_reference_bindings WHERE binding_sha256=?",
                (result.approval["binding_sha256"],),
            ).fetchone()
            current_binding = connection.execute(
                "SELECT binding_sha256,publication_sha256,publication_json "
                "FROM phase8_reference_current WHERE scope_key=?",
                (scope,),
            ).fetchone()
            approval_event_current = connection.execute(
                "SELECT publication_sha256,publication_json "
                "FROM phase8_approval_current WHERE approval_id=?",
                (result.approval["approval_id"],),
            ).fetchone()
            if (
                binding_row is None
                or current_binding is None
                or current_binding["binding_sha256"]
                != result.approval["binding_sha256"]
                or approval_event_current is None
            ):
                _fail(Phase8CurrentConflict, "approval reference binding is stale")
            binding = _decode_json(binding_row["binding_json"], "approval binding")
            self._verify_binding_deep(binding, deadline=deadline)
            publications = (
                _decode_publication(
                    row["publication_sha256"],
                    row["publication_json"],
                    field="scope approval publication",
                ),
                _decode_publication(
                    approval_event_current["publication_sha256"],
                    approval_event_current["publication_json"],
                    field="approval lifecycle publication",
                ),
                _decode_publication(
                    current_binding["publication_sha256"],
                    current_binding["publication_json"],
                    field="approval reference publication",
                ),
            )
            if any(
                publication["phase7_scope_key"] != binding["phase7_scope_key"]
                or publication["phase7_commit_sha256"]
                != binding["phase7_commit_sha256"]
                or not _publication_is_current(
                    publication, publication_head_verifier
                )
                for publication in publications
            ) or not self._publication_receipt_is_current(
                connection,
                publications[1],
                activated_object_sha256=str(
                    result.lifecycle_event["event_sha256"]
                ),
            ):
                _fail(
                    Phase8CurrentConflict,
                    "approval publication generation is not current",
                )
            if not self._head_is_current(
                phase7_current_head_verifier,
                str(binding["phase7_scope_key"]),
                str(binding["phase7_commit_sha256"]),
            ):
                _fail(Phase8CurrentConflict, "approval Phase-7 head is stale")
            return result
        except BaseException as error:
            primary = error
            raise
        finally:
            self._close(connection, primary)

    def issue_approval(
        self,
        *,
        idempotency_key: str,
        approval_id: str,
        binding_sha256: str,
        issuer_id: str,
        issuer_generation: str,
        subject_id: str,
        subject_generation: str,
        logical_issued_at: int,
        not_before: int,
        expires_at: int,
        data_egress_request: Mapping[str, object],
        trusted_preflight_sha256: str,
        phase7_current_head_verifier: CurrentHeadVerifier,
        publication_identity: Mapping[str, object] | None = None,
        publication_head_verifier: PublicationHeadVerifier | None = None,
        successor_of: str | None = None,
        expected_predecessor_event_sha256: str | None = None,
        deadline: object | None = None,
        adapter_fence: AdapterFence | None = None,
    ) -> ApprovalResult:
        """Issue an exact, scoped approval; optionally supersede current."""

        self._require_enabled()
        key = _identifier(idempotency_key, "idempotency_key")
        approval_identity = _identifier(approval_id, "approval_id")
        binding_identity = _sha(binding_sha256, "binding_sha256")
        preflight_identity = _sha(
            trusted_preflight_sha256, "trusted_preflight_sha256"
        )
        issuer = _identifier(issuer_id, "issuer_id")
        issuer_gen = _identifier(issuer_generation, "issuer_generation")
        subject = _identifier(subject_id, "subject_id")
        subject_gen = _identifier(subject_generation, "subject_generation")
        issued = _integer(logical_issued_at, "logical_issued_at")
        starts = _integer(not_before, "not_before")
        expires = _integer(expires_at, "expires_at")
        if not issued <= starts < expires:
            _fail(Phase8ContractError, "approval time interval is invalid")
        if successor_of is not None:
            successor_of = _identifier(successor_of, "successor_of")
            if expected_predecessor_event_sha256 is None:
                _fail(Phase8ContractError, "successor requires predecessor event CAS")
            expected_predecessor_event_sha256 = _sha(
                expected_predecessor_event_sha256,
                "expected_predecessor_event_sha256",
            )
        elif expected_predecessor_event_sha256 is not None:
            _fail(Phase8ContractError, "predecessor event without successor is invalid")
        request = _mapping(data_egress_request, "data_egress_request")
        try:
            staged = evaluate_data_egress(request).staged_manifest.as_dict()
        except DataEgressError as exc:
            raise Phase8ContractError("data egress request does not stage") from exc
        if staged["subject"] != subject:
            _fail(Phase8ContractError, "approval subject differs from staged request")
        binding_result = self.load_reference_binding(binding_identity, deadline=deadline)
        binding = binding_result.binding
        scope_key = str(binding["scope_key"])
        self.load_current_reference_binding(
            scope_key,
            phase7_current_head_verifier=phase7_current_head_verifier,
            publication_head_verifier=publication_head_verifier,
            expected_binding_sha256=binding_identity,
            deadline=deadline,
        )
        authority = _mapping(binding["authority_coordinate"], "authority coordinate")
        publication = _publication_identity(
            publication_identity,
            publication_key=key,
            phase7_scope_key=str(binding["phase7_scope_key"]),
            phase7_commit_sha256=str(binding["phase7_commit_sha256"]),
        )
        publication_sha = str(publication["publication_sha256"])
        publication_json = _canonical_json(publication)
        if not _publication_is_current(publication, publication_head_verifier):
            _fail(Phase8CurrentConflict, "approval publication is not current")
        generation_set = {
            name: authority[name]
            for name in (
                "project_generation", "run_generation", "runtime_generation",
                "scheduler_generation",
            )
        }
        if binding["phase7_effective_verdict"].get("aggregate_verdict") != "PASS" or binding["phase7_result"].get("grounding_valid") is not True:
            _fail(Phase8CurrentConflict, "Phase-7 current evidence is not grounded PASS")
        trusted = self.load_trusted_approval_preflight(
            preflight_identity, deadline=deadline
        ).preflight
        if (
            trusted.get("approval_id") != approval_identity
            or trusted.get("binding_sha256") != binding_identity
            or trusted.get("scope_key") != scope_key
            or trusted.get("issuer")
            != {"id": issuer, "generation": issuer_gen}
            or trusted.get("subject")
            != {"id": subject, "generation": subject_gen}
            or trusted.get("generation_set") != generation_set
            or trusted.get("logical_issued_at") != issued
            or trusted.get("not_before") != starts
            or trusted.get("expires_at") != expires
            or trusted.get("successor_of") != successor_of
            or trusted.get("expected_predecessor_event_sha256")
            != expected_predecessor_event_sha256
            or trusted.get("data_egress_request") != request
            or trusted.get("policy_sha256") != staged["policy_sha256"]
            or trusted.get("staged_manifest") != staged
            or trusted.get("staged_manifest_sha256")
            != staged["staged_manifest_sha256"]
            or trusted.get("exact_artifacts") != list(staged["artifacts"])
            or trusted.get("phase7_scope_key") != binding["phase7_scope_key"]
            or trusted.get("phase7_commit_sha256") != binding["phase7_commit_sha256"]
            or trusted.get("reference_package_sha256")
            != binding["reference_package_sha256"]
            or trusted.get("reference_record_sha256")
            != binding["reference_record_sha256"]
        ):
            _fail(
                Phase8CurrentConflict,
                "trusted local approval preflight differs from issue request",
            )
        trusted_blob = self._put_fact(trusted, deadline=deadline)
        predecessor: ApprovalResult | None = None
        predecessor_event: dict[str, object] | None = None
        predecessor_event_blob: CasBlobFact | None = None
        if successor_of is not None:
            history_connection = self._connect(deadline=deadline)
            history_primary: BaseException | None = None
            try:
                predecessor_row = history_connection.execute(
                    "SELECT * FROM phase8_approvals WHERE approval_id=?",
                    (successor_of,),
                ).fetchone()
                predecessor_event_row = history_connection.execute(
                    "SELECT * FROM phase8_approval_events WHERE approval_id=? AND event_sha256=?",
                    (successor_of, expected_predecessor_event_sha256),
                ).fetchone()
                if predecessor_row is None or predecessor_event_row is None:
                    _fail(Phase8CurrentConflict, "predecessor lifecycle identity is unavailable")
                predecessor = self._approval_from_rows(
                    predecessor_row,
                    predecessor_event_row,
                    replayed=False,
                    deadline=deadline,
                )
            except BaseException as error:
                history_primary = error
                raise
            finally:
                self._close(history_connection, history_primary)
            predecessor_event = self._approval_event(
                approval_id=successor_of,
                sequence=int(predecessor.lifecycle_event["event_sequence"]) + 1,
                state="SUPERSEDED",
                effective_at=issued,
                reason_code="SUCCESSOR_ISSUED",
                previous_event_sha256=str(predecessor.lifecycle_event["event_sha256"]),
            )
            predecessor_event_blob = self._put_fact(predecessor_event, deadline=deadline)
        approval_body: dict[str, object] = {
            "schema_version": PHASE8_APPROVAL_SCHEMA,
            "approval_id": approval_identity,
            "scope_key": scope_key,
            "binding_sha256": binding_identity,
            "trusted_preflight_sha256": preflight_identity,
            "trusted_preflight_id": trusted["preflight_id"],
            "trusted_preflight": trusted,
            "trusted_preflight_blob": trusted_blob.as_dict(),
            "issuer": {"id": issuer, "generation": issuer_gen},
            "subject": {"id": subject, "generation": subject_gen},
            "generation_set": generation_set,
            "logical_issued_at": issued,
            "not_before": starts,
            "expires_at": expires,
            "decision_evaluated_at": trusted["decision_evaluated_at"],
            "successor_of": successor_of,
            "expected_predecessor_event_sha256": expected_predecessor_event_sha256,
            "policy_sha256": staged["policy_sha256"],
            "phase7_commit_sha256": binding["phase7_commit_sha256"],
            "reference_package_sha256": binding["reference_package_sha256"],
            "reference_record_sha256": binding["reference_record_sha256"],
            "data_egress_request": request,
            "staged_manifest": staged,
            "staged_manifest_sha256": staged["staged_manifest_sha256"],
            "exact_artifacts": [
                {"artifact_id": item["artifact_id"], "sha256": item["sha256"]}
                for item in staged["artifacts"]
            ],
            "approved": True,
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
        approval = {**approval_body, "approval_sha256": canonical_sha256(approval_body)}
        event = self._approval_event(
            approval_id=approval_identity,
            sequence=1,
            state="ACTIVE",
            effective_at=starts,
            reason_code="APPROVAL_ISSUED",
            previous_event_sha256=None,
        )
        request_identity = {
            "schema_version": "phase8-approval-issue-request-v1",
            "idempotency_key": key,
            "approval": approval,
        }
        request_sha = canonical_sha256(request_identity)
        approval_blob = self._put_fact(approval, deadline=deadline)
        event_blob = self._put_fact(event, deadline=deadline)
        _adapter_fence(adapter_fence, "after_cas_before_sqlite")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            self._transaction(connection, deadline=deadline, adapter_fence=adapter_fence)
            existing = connection.execute(
                "SELECT * FROM phase8_approvals WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    _fail(Phase8ReplayConflict, "approval key reused with different request")
                replay_event_head = connection.execute(
                    "SELECT * FROM phase8_approval_current WHERE approval_id=?",
                    (existing["approval_id"],),
                ).fetchone()
                event_row = connection.execute(
                    "SELECT * FROM phase8_approval_events WHERE event_sha256=?",
                    (replay_event_head["event_sha256"],),
                ).fetchone()
                replay = self._approval_from_rows(
                    existing, event_row, replayed=True, deadline=deadline
                )
                replay_scope = connection.execute(
                    "SELECT approval_id,publication_sha256,publication_json "
                    "FROM phase8_scope_approval_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                replay_same_scope = (
                    replay_scope is not None
                    and replay_scope["approval_id"] == approval_identity
                )
                replay_initial_event = (
                    replay_event_head is not None
                    and replay_event_head["event_sha256"] == event["event_sha256"]
                    and replay_event_head["state"] == "ACTIVE"
                )
                if (
                    replay_same_scope
                    and replay_initial_event
                    and replay_scope["publication_sha256"] == publication_sha
                    and replay_event_head["publication_sha256"] == publication_sha
                ):
                    connection.rollback()
                    _adapter_fence(adapter_fence, "after_sqlite_replay")
                    _check_deadline(deadline, "approval issue replay")
                    if not self._head_is_current(
                        phase7_current_head_verifier,
                        str(binding["phase7_scope_key"]),
                        str(binding["phase7_commit_sha256"]),
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "Phase-7 head changed before approval replay",
                        )
                    if not _publication_is_current(
                        publication, publication_head_verifier
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval publication changed before replay",
                        )
                    return replay
                if replay_same_scope and not replay_initial_event:
                    # Immutable issue history is still replayable, but a
                    # later lifecycle head must never be reactivated.
                    connection.rollback()
                    _adapter_fence(adapter_fence, "after_sqlite_replay")
                    _check_deadline(deadline, "approval issue historical replay")
                    return replay
                publication_takeover = replay_same_scope and replay_initial_event
                replay_predecessor = (
                    None
                    if replay_scope is None
                    else str(replay_scope["approval_id"])
                )
                if not publication_takeover and replay_predecessor != successor_of:
                    _fail(
                        Phase8CurrentConflict,
                        "approval replay lost its exact predecessor",
                    )
                previous_scope_publication_sha = (
                    None
                    if replay_scope is None
                    else str(replay_scope["publication_sha256"])
                )
                previous_scope_publication_json = (
                    None
                    if replay_scope is None
                    else str(replay_scope["publication_json"])
                )
                previous_issue_publication_sha = str(
                    replay_event_head["publication_sha256"]
                )
                previous_issue_publication_json = str(
                    replay_event_head["publication_json"]
                )
                predecessor_publication_sha = None
                predecessor_publication_json = None
                if successor_of is not None and not publication_takeover:
                    predecessor_head = connection.execute(
                        "SELECT event_sha256,state,publication_sha256,publication_json "
                        "FROM phase8_approval_current "
                        "WHERE approval_id=?",
                        (successor_of,),
                    ).fetchone()
                    if (
                        predecessor_head is None
                        or predecessor_head["event_sha256"]
                        != expected_predecessor_event_sha256
                        or predecessor_head["state"] != "ACTIVE"
                        or predecessor_event is None
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval replay predecessor lifecycle changed",
                        )
                    predecessor_publication_sha = str(
                        predecessor_head["publication_sha256"]
                    )
                    predecessor_publication_json = str(
                        predecessor_head["publication_json"]
                    )
                    connection.execute(
                        "UPDATE phase8_approval_current "
                        "SET event_sequence=?,event_sha256=?,state='SUPERSEDED',"
                        "publication_sha256=?,publication_json=? "
                        "WHERE approval_id=? AND event_sha256=? AND state='ACTIVE'",
                        (
                            predecessor_event["event_sequence"],
                            predecessor_event["event_sha256"],
                            publication_sha,
                            publication_json,
                            successor_of,
                            expected_predecessor_event_sha256,
                        ),
                    )
                connection.execute(
                    "UPDATE phase8_approval_current "
                    "SET publication_sha256=?,publication_json=? "
                    "WHERE approval_id=? AND event_sha256=? AND state='ACTIVE'",
                    (
                        publication_sha,
                        publication_json,
                        approval_identity,
                        event["event_sha256"],
                    ),
                )
                connection.execute(
                    """INSERT INTO phase8_scope_approval_current(
                       scope_key,approval_id,publication_sha256,publication_json
                       ) VALUES(?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                       approval_id=excluded.approval_id,
                       publication_sha256=excluded.publication_sha256,
                       publication_json=excluded.publication_json""",
                    (
                        scope_key,
                        approval_identity,
                        publication_sha,
                        publication_json,
                    ),
                )

                def replay_approval_post_commit_fence(
                    committed: sqlite3.Connection,
                ) -> None:
                    winning = committed.execute(
                        "SELECT approval_id,publication_sha256 "
                        "FROM phase8_scope_approval_current "
                        "WHERE scope_key=?",
                        (scope_key,),
                    ).fetchone()
                    winning_event = committed.execute(
                        "SELECT event_sha256,state,publication_sha256 "
                        "FROM phase8_approval_current WHERE approval_id=?",
                        (approval_identity,),
                    ).fetchone()
                    if (
                        winning is None
                        or winning["approval_id"] != approval_identity
                        or winning["publication_sha256"] != publication_sha
                        or winning_event is None
                        or winning_event["event_sha256"] != event["event_sha256"]
                        or winning_event["state"] != "ACTIVE"
                        or winning_event["publication_sha256"] != publication_sha
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval replay lost its post-commit head",
                        )
                    if not self._head_is_current(
                        phase7_current_head_verifier,
                        str(binding["phase7_scope_key"]),
                        str(binding["phase7_commit_sha256"]),
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "Phase-7 head changed after approval replay",
                        )
                    if not _publication_is_current(
                        publication, publication_head_verifier
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval publication changed after replay",
                        )

                def reconcile_replayed_approval(
                    committed: sqlite3.Connection,
                ) -> None:
                    winning = committed.execute(
                        "SELECT approval_id,publication_sha256 "
                        "FROM phase8_scope_approval_current "
                        "WHERE scope_key=?",
                        (scope_key,),
                    ).fetchone()
                    if (
                        winning is None
                        or winning["approval_id"] != approval_identity
                        or winning["publication_sha256"] != publication_sha
                    ):
                        return
                    if publication_takeover:
                        committed.execute(
                            "UPDATE phase8_scope_approval_current "
                            "SET publication_sha256=?,publication_json=? "
                            "WHERE scope_key=? AND approval_id=? "
                            "AND publication_sha256=?",
                            (
                                previous_scope_publication_sha,
                                previous_scope_publication_json,
                                scope_key,
                                approval_identity,
                                publication_sha,
                            ),
                        )
                    elif successor_of is None:
                        committed.execute(
                            "DELETE FROM phase8_scope_approval_current "
                            "WHERE scope_key=? AND approval_id=?",
                            (scope_key, approval_identity),
                        )
                    else:
                        committed.execute(
                            "UPDATE phase8_scope_approval_current "
                            "SET approval_id=?,publication_sha256=?,publication_json=? "
                            "WHERE scope_key=? AND approval_id=?",
                            (
                                successor_of,
                                previous_scope_publication_sha,
                                previous_scope_publication_json,
                                scope_key,
                                approval_identity,
                            ),
                        )
                        if predecessor_event is not None:
                            committed.execute(
                                "UPDATE phase8_approval_current "
                                "SET event_sequence=?,event_sha256=?,state='ACTIVE',"
                                "publication_sha256=?,publication_json=? "
                                "WHERE approval_id=? AND event_sha256=? "
                                "AND state='SUPERSEDED'",
                                (
                                    predecessor.lifecycle_event["event_sequence"],
                                    predecessor.lifecycle_event["event_sha256"],
                                    predecessor_publication_sha,
                                    predecessor_publication_json,
                                    successor_of,
                                    predecessor_event["event_sha256"],
                                ),
                            )
                    committed.execute(
                        "UPDATE phase8_approval_current "
                        "SET publication_sha256=?,publication_json=? "
                        "WHERE approval_id=? AND event_sha256=? AND state='ACTIVE' "
                        "AND publication_sha256=?",
                        (
                            previous_issue_publication_sha,
                            previous_issue_publication_json,
                            approval_identity,
                            event["event_sha256"],
                            publication_sha,
                        ),
                    )

                self._commit(
                    connection,
                    idempotency_key=key,
                    deadline=deadline,
                    adapter_fence=adapter_fence,
                    post_commit_fence=replay_approval_post_commit_fence,
                    post_commit_reconciler=reconcile_replayed_approval,
                )
                return replay
            if connection.execute(
                "SELECT 1 FROM phase8_approvals WHERE approval_id=?", (approval_identity,)
            ).fetchone() is not None:
                _fail(Phase8ReplayConflict, "approval_id already binds different bytes")
            current_binding = connection.execute(
                "SELECT binding_sha256,publication_sha256,publication_json "
                "FROM phase8_reference_current WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            if current_binding is None or current_binding["binding_sha256"] != binding_identity:
                _fail(Phase8CurrentConflict, "reference binding changed before approval")
            current_binding_publication = _decode_publication(
                current_binding["publication_sha256"],
                current_binding["publication_json"],
                field="approval reference publication",
            )
            if not _publication_is_current(
                current_binding_publication, publication_head_verifier
            ):
                _fail(
                    Phase8CurrentConflict,
                    "reference publication changed before approval",
                )
            if not self._head_is_current(
                phase7_current_head_verifier,
                str(binding["phase7_scope_key"]),
                str(binding["phase7_commit_sha256"]),
            ):
                _fail(Phase8CurrentConflict, "Phase-7 head changed before approval commit")
            current_approval = connection.execute(
                "SELECT approval_id,publication_sha256,publication_json "
                "FROM phase8_scope_approval_current WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            prior_scope_publication_sha = (
                None
                if current_approval is None
                else str(current_approval["publication_sha256"])
            )
            prior_scope_publication_json = (
                None
                if current_approval is None
                else str(current_approval["publication_json"])
            )
            prior_predecessor_publication_sha = None
            prior_predecessor_publication_json = None
            if successor_of is None:
                if current_approval is not None:
                    _fail(Phase8CurrentConflict, "scope already has an approval; issue a successor")
            elif current_approval is None or current_approval["approval_id"] != successor_of:
                _fail(Phase8CurrentConflict, "approval predecessor is no longer current")
            if predecessor_event is not None and predecessor_event_blob is not None:
                current_event = connection.execute(
                    "SELECT * FROM phase8_approval_current WHERE approval_id=?", (successor_of,)
                ).fetchone()
                if current_event is None or current_event["event_sha256"] != expected_predecessor_event_sha256:
                    _fail(Phase8CurrentConflict, "predecessor lifecycle changed before commit")
                prior_predecessor_publication_sha = str(
                    current_event["publication_sha256"]
                )
                prior_predecessor_publication_json = str(
                    current_event["publication_json"]
                )
                connection.execute(
                    """INSERT INTO phase8_approval_events(
                       event_sha256,approval_id,event_sequence,idempotency_key,request_sha256,event_json,event_blob_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        predecessor_event["event_sha256"], successor_of,
                        predecessor_event["event_sequence"], f"{key}:supersede",
                        canonical_sha256(predecessor_event), _canonical_json(predecessor_event),
                        _canonical_json(predecessor_event_blob.as_dict()),
                    ),
                )
            connection.execute(
                """INSERT INTO phase8_approvals(
                   approval_id,idempotency_key,request_sha256,scope_key,binding_sha256,
                   trusted_preflight_sha256,successor_of,approval_sha256,
                   approval_json,approval_blob_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    approval_identity, key, request_sha, scope_key, binding_identity,
                    preflight_identity, successor_of, approval["approval_sha256"],
                    _canonical_json(approval), _canonical_json(approval_blob.as_dict()),
                ),
            )
            connection.execute(
                """INSERT INTO phase8_approval_events(
                   event_sha256,approval_id,event_sequence,idempotency_key,request_sha256,event_json,event_blob_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    event["event_sha256"], approval_identity, 1, key,
                    request_sha, _canonical_json(event), _canonical_json(event_blob.as_dict()),
                ),
            )
            connection.execute(
                """INSERT INTO phase8_approval_current(
                   approval_id,event_sequence,event_sha256,state,
                   publication_sha256,publication_json
                   ) VALUES(?,?,?,'ACTIVE',?,?)""",
                (
                    approval_identity,
                    1,
                    event["event_sha256"],
                    publication_sha,
                    publication_json,
                ),
            )
            def approval_history_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                winning_binding = committed.execute(
                    "SELECT binding_sha256 FROM phase8_reference_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                historical_approval = committed.execute(
                    "SELECT approval_id FROM phase8_approvals "
                    "WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                winning_event = committed.execute(
                    "SELECT event_sha256,state,publication_sha256 "
                    "FROM phase8_approval_current "
                    "WHERE approval_id=?",
                    (approval_identity,),
                ).fetchone()
                if (
                    winning_binding is None
                    or winning_binding["binding_sha256"] != binding_identity
                    or historical_approval is None
                    or historical_approval["approval_id"] != approval_identity
                    or winning_event is None
                    or winning_event["event_sha256"] != event["event_sha256"]
                    or winning_event["state"] != "ACTIVE"
                    or winning_event["publication_sha256"] != publication_sha
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval lost its exact post-commit head",
                    )
                if predecessor_event is not None:
                    predecessor_head = committed.execute(
                        "SELECT event_sha256,state FROM phase8_approval_current "
                        "WHERE approval_id=?",
                        (successor_of,),
                    ).fetchone()
                    if (
                        predecessor_head is None
                        or predecessor_head["event_sha256"]
                        != expected_predecessor_event_sha256
                        or predecessor_head["state"] != "ACTIVE"
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval predecessor changed after history commit",
                        )
                if not self._head_is_current(
                    phase7_current_head_verifier,
                    str(binding["phase7_scope_key"]),
                    str(binding["phase7_commit_sha256"]),
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "Phase-7 head changed after approval commit",
                    )

            # Commit immutable issue/lifecycle history first.  Scope-current
            # and predecessor-current are activated only after the durable
            # history survives the post-commit generation/head fence.
            self._commit(
                connection,
                idempotency_key=key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=approval_history_post_commit_fence,
            )

            self._transaction(
                connection, deadline=deadline, adapter_fence=adapter_fence
            )
            activation_binding = connection.execute(
                "SELECT binding_sha256,publication_sha256,publication_json "
                "FROM phase8_reference_current "
                "WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            activation_scope = connection.execute(
                "SELECT approval_id,publication_sha256,publication_json "
                "FROM phase8_scope_approval_current "
                "WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            activation_predecessor = (
                None if activation_scope is None else str(activation_scope["approval_id"])
            )
            if (
                activation_binding is None
                or activation_binding["binding_sha256"] != binding_identity
                or activation_predecessor != successor_of
            ):
                _fail(
                    Phase8CurrentConflict,
                    "approval activation lost its exact binding/predecessor",
                )
            activation_binding_publication = _decode_publication(
                activation_binding["publication_sha256"],
                activation_binding["publication_json"],
                field="approval activation reference publication",
            )
            if not _publication_is_current(
                activation_binding_publication, publication_head_verifier
            ):
                _fail(
                    Phase8CurrentConflict,
                    "reference publication changed before approval activation",
                )
            if not self._head_is_current(
                phase7_current_head_verifier,
                str(binding["phase7_scope_key"]),
                str(binding["phase7_commit_sha256"]),
            ):
                _fail(
                    Phase8CurrentConflict,
                    "Phase-7 head changed before approval activation",
                )
            if not _publication_is_current(publication, publication_head_verifier):
                _fail(
                    Phase8CurrentConflict,
                    "approval publication changed before activation",
                )
            if predecessor_event is not None:
                activation_event = connection.execute(
                    "SELECT event_sha256,state,publication_sha256,publication_json "
                    "FROM phase8_approval_current "
                    "WHERE approval_id=?",
                    (successor_of,),
                ).fetchone()
                if (
                    activation_event is None
                    or activation_event["event_sha256"]
                    != expected_predecessor_event_sha256
                    or activation_event["state"] != "ACTIVE"
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval predecessor changed before activation",
                    )
                connection.execute(
                    "UPDATE phase8_approval_current "
                    "SET event_sequence=?,event_sha256=?,state='SUPERSEDED',"
                    "publication_sha256=?,publication_json=? "
                    "WHERE approval_id=? AND event_sha256=? AND state='ACTIVE'",
                    (
                        predecessor_event["event_sequence"],
                        predecessor_event["event_sha256"],
                        publication_sha,
                        publication_json,
                        successor_of,
                        expected_predecessor_event_sha256,
                    ),
                )
            connection.execute(
                """INSERT INTO phase8_scope_approval_current(
                   scope_key,approval_id,publication_sha256,publication_json
                   ) VALUES(?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                   approval_id=excluded.approval_id,
                   publication_sha256=excluded.publication_sha256,
                   publication_json=excluded.publication_json""",
                (
                    scope_key,
                    approval_identity,
                    publication_sha,
                    publication_json,
                ),
            )

            def approval_activation_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                winning_approval = committed.execute(
                    "SELECT approval_id,publication_sha256 "
                    "FROM phase8_scope_approval_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                if (
                    winning_approval is None
                    or winning_approval["approval_id"] != approval_identity
                    or winning_approval["publication_sha256"] != publication_sha
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval lost its exact activation head",
                    )
                if predecessor_event is not None:
                    predecessor_head = committed.execute(
                        "SELECT event_sha256,state,publication_sha256 "
                        "FROM phase8_approval_current "
                        "WHERE approval_id=?",
                        (successor_of,),
                    ).fetchone()
                    if (
                        predecessor_head is None
                        or predecessor_head["event_sha256"]
                        != predecessor_event["event_sha256"]
                        or predecessor_head["state"] != "SUPERSEDED"
                        or predecessor_head["publication_sha256"]
                        != publication_sha
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval predecessor lost its superseded activation head",
                        )
                issue_head = committed.execute(
                    "SELECT event_sha256,state,publication_sha256 "
                    "FROM phase8_approval_current WHERE approval_id=?",
                    (approval_identity,),
                ).fetchone()
                if (
                    issue_head is None
                    or issue_head["event_sha256"] != event["event_sha256"]
                    or issue_head["state"] != "ACTIVE"
                    or issue_head["publication_sha256"] != publication_sha
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval lifecycle publication changed after activation",
                    )
                if not self._head_is_current(
                    phase7_current_head_verifier,
                    str(binding["phase7_scope_key"]),
                    str(binding["phase7_commit_sha256"]),
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "Phase-7 head changed after approval activation",
                    )
                if not _publication_is_current(
                    publication, publication_head_verifier
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval publication changed after activation",
                    )

            def reconcile_approval_current(
                committed: sqlite3.Connection,
            ) -> None:
                winning = committed.execute(
                    "SELECT approval_id,publication_sha256 "
                    "FROM phase8_scope_approval_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                if (
                    winning is None
                    or winning["approval_id"] != approval_identity
                    or winning["publication_sha256"] != publication_sha
                ):
                    return
                if successor_of is None:
                    committed.execute(
                        "DELETE FROM phase8_scope_approval_current "
                        "WHERE scope_key=? AND approval_id=?",
                        (scope_key, approval_identity),
                    )
                else:
                    committed.execute(
                        "UPDATE phase8_scope_approval_current "
                        "SET approval_id=?,publication_sha256=?,publication_json=? "
                        "WHERE scope_key=? AND approval_id=?",
                        (
                            successor_of,
                            prior_scope_publication_sha,
                            prior_scope_publication_json,
                            scope_key,
                            approval_identity,
                        ),
                    )
                    predecessor_head = committed.execute(
                        "SELECT event_sha256,state FROM phase8_approval_current "
                        "WHERE approval_id=?",
                        (successor_of,),
                    ).fetchone()
                    if (
                        predecessor_head is not None
                        and predecessor_event is not None
                        and predecessor_head["event_sha256"]
                        == predecessor_event["event_sha256"]
                        and predecessor_head["state"] == "SUPERSEDED"
                    ):
                        committed.execute(
                            "UPDATE phase8_approval_current "
                            "SET event_sequence=?,event_sha256=?,state=?,"
                            "publication_sha256=?,publication_json=? "
                            "WHERE approval_id=? AND event_sha256=?",
                            (
                                predecessor.lifecycle_event["event_sequence"],
                                predecessor.lifecycle_event["event_sha256"],
                                predecessor.lifecycle_event["state"],
                                prior_predecessor_publication_sha,
                                prior_predecessor_publication_json,
                                successor_of,
                                predecessor_event["event_sha256"],
                            ),
                        )

            self._commit(
                connection,
                idempotency_key=key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=approval_activation_post_commit_fence,
                post_commit_reconciler=reconcile_approval_current,
            )
            return ApprovalResult(approval, event, False)
        except BaseException as error:
            primary = error
            if connection.in_transaction:
                run_cleanup(
                    [("rollback Phase-8 approval issue", RetryableCleanup(connection.rollback))],
                    primary=error,
                )
            raise
        finally:
            self._close(connection, primary)

    def _transition_approval(
        self,
        *,
        idempotency_key: str,
        approval_id: str,
        expected_event_sha256: str,
        state: str,
        effective_at: int,
        reason_code: str,
        phase7_current_head_verifier: CurrentHeadVerifier | None,
        publication_identity: Mapping[str, object] | None,
        publication_head_verifier: PublicationHeadVerifier | None,
        deadline: object | None,
        adapter_fence: AdapterFence | None,
    ) -> ApprovalResult:
        self._require_enabled()
        key = _identifier(idempotency_key, "idempotency_key")
        identity = _identifier(approval_id, "approval_id")
        expected = _sha(expected_event_sha256, "expected_event_sha256")
        moment = _integer(effective_at, "effective_at")
        reason = _identifier(reason_code, "reason_code")
        if state not in {"REVOKED", "EXPIRED"}:
            _fail(Phase8ContractError, "unsupported approval transition")
        approval_snapshot = self.load_approval(identity, deadline=deadline)
        binding_result = self.load_reference_binding(
            str(approval_snapshot.approval["binding_sha256"]),
            deadline=deadline,
        )
        binding = binding_result.binding
        if phase7_current_head_verifier is None:
            expected_scope = str(binding["phase7_scope_key"])
            expected_commit = str(binding["phase7_commit_sha256"])

            def phase7_current_head_verifier(
                scope: str,
                commit: str,
            ) -> bool:
                return scope == expected_scope and commit == expected_commit

        publication = _publication_identity(
            publication_identity,
            publication_key=key,
            phase7_scope_key=str(binding["phase7_scope_key"]),
            phase7_commit_sha256=str(binding["phase7_commit_sha256"]),
        )
        publication_sha = str(publication["publication_sha256"])
        publication_json = _canonical_json(publication)
        if not self._head_is_current(
            phase7_current_head_verifier,
            str(binding["phase7_scope_key"]),
            str(binding["phase7_commit_sha256"]),
        ):
            _fail(Phase8CurrentConflict, "approval transition Phase-7 head is stale")
        if not _publication_is_current(publication, publication_head_verifier):
            _fail(
                Phase8CurrentConflict,
                "approval transition publication is not current",
            )

        def ensure_transition_receipt(
            committed_connection: _AnchoredConnection,
            event_sha256: str,
        ) -> None:
            def exact_activation(committed: sqlite3.Connection) -> None:
                winning = committed.execute(
                    "SELECT event_sha256,state,publication_sha256 "
                    "FROM phase8_approval_current WHERE approval_id=?",
                    (identity,),
                ).fetchone()
                if (
                    winning is None
                    or winning["event_sha256"] != event_sha256
                    or winning["state"] != state
                    or winning["publication_sha256"] != publication_sha
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval transition receipt lost exact activation",
                    )

            self._ensure_publication_receipt(
                connection=committed_connection,
                publication=publication,
                activated_object_sha256=event_sha256,
                idempotency_key=key,
                phase7_current_head_verifier=phase7_current_head_verifier,
                publication_head_verifier=publication_head_verifier,
                activation_fence=exact_activation,
                deadline=deadline,
                adapter_fence=adapter_fence,
            )
        replay_connection = self._connect(deadline=deadline)
        replay_primary: BaseException | None = None
        try:
            existing = replay_connection.execute(
                "SELECT * FROM phase8_approval_events WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                stored_event = _decode_json(existing["event_json"], "approval transition")
                expected_request_sha = canonical_sha256(
                    {
                        "schema_version": "phase8-approval-transition-request-v1",
                        "idempotency_key": key,
                        "event": stored_event,
                    }
                )
                if (
                    existing["request_sha256"] != expected_request_sha
                    or stored_event.get("approval_id") != identity
                    or stored_event.get("state") != state
                    or stored_event.get("effective_at") != moment
                    or stored_event.get("reason_code") != reason
                    or stored_event.get("previous_event_sha256") != expected
                ):
                    _fail(Phase8ReplayConflict, "transition key reused with different request")
                approval_row = replay_connection.execute(
                    "SELECT * FROM phase8_approvals WHERE approval_id=?", (identity,)
                ).fetchone()
                if approval_row is None:
                    _fail(Phase8StoreError, "transition approval is unavailable")
                replay = self._approval_from_rows(
                    approval_row, existing, replayed=True, deadline=deadline
                )
                current = replay_connection.execute(
                    "SELECT event_sha256,state,publication_sha256,publication_json "
                    "FROM phase8_approval_current "
                    "WHERE approval_id=?",
                    (identity,),
                ).fetchone()
                if (
                    current is not None
                    and current["event_sha256"] == existing["event_sha256"]
                    and current["state"] == state
                    and current["publication_sha256"] == publication_sha
                ):
                    _adapter_fence(adapter_fence, "after_sqlite_replay")
                    _check_deadline(deadline, "approval transition replay")
                    if not self._head_is_current(
                        phase7_current_head_verifier,
                        str(binding["phase7_scope_key"]),
                        str(binding["phase7_commit_sha256"]),
                    ) or not _publication_is_current(
                        publication, publication_head_verifier
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval transition replay publication is stale",
                        )
                    ensure_transition_receipt(
                        replay_connection, str(existing["event_sha256"])
                    )
                    return replay
                if (
                    current is not None
                    and current["event_sha256"] == existing["event_sha256"]
                    and current["state"] == state
                ):
                    previous_publication_sha = str(current["publication_sha256"])
                    previous_publication_json = str(current["publication_json"])
                    self._transaction(
                        replay_connection,
                        deadline=deadline,
                        adapter_fence=adapter_fence,
                    )
                    replay_connection.execute(
                        "UPDATE phase8_approval_current "
                        "SET publication_sha256=?,publication_json=? "
                        "WHERE approval_id=? AND event_sha256=? AND state=?",
                        (
                            publication_sha,
                            publication_json,
                            identity,
                            existing["event_sha256"],
                            state,
                        ),
                    )

                    def takeover_post_commit_fence(
                        committed: sqlite3.Connection,
                    ) -> None:
                        winning = committed.execute(
                            "SELECT event_sha256,state,publication_sha256 "
                            "FROM phase8_approval_current WHERE approval_id=?",
                            (identity,),
                        ).fetchone()
                        if (
                            winning is None
                            or winning["event_sha256"]
                            != existing["event_sha256"]
                            or winning["state"] != state
                            or winning["publication_sha256"] != publication_sha
                            or not self._head_is_current(
                                phase7_current_head_verifier,
                                str(binding["phase7_scope_key"]),
                                str(binding["phase7_commit_sha256"]),
                            )
                            or not _publication_is_current(
                                publication, publication_head_verifier
                            )
                        ):
                            _fail(
                                Phase8CurrentConflict,
                                "approval transition takeover is stale",
                            )

                    def reconcile_takeover(
                        committed: sqlite3.Connection,
                    ) -> None:
                        committed.execute(
                            "UPDATE phase8_approval_current "
                            "SET publication_sha256=?,publication_json=? "
                            "WHERE approval_id=? AND event_sha256=? AND state=? "
                            "AND publication_sha256=?",
                            (
                                previous_publication_sha,
                                previous_publication_json,
                                identity,
                                existing["event_sha256"],
                                state,
                                publication_sha,
                            ),
                        )

                    self._commit(
                        replay_connection,
                        idempotency_key=key,
                        deadline=deadline,
                        adapter_fence=adapter_fence,
                        post_commit_fence=takeover_post_commit_fence,
                        post_commit_reconciler=reconcile_takeover,
                    )
                    ensure_transition_receipt(
                        replay_connection, str(existing["event_sha256"])
                    )
                    return replay
                if (
                    current is None
                    or current["event_sha256"] != expected
                    or current["state"] != "ACTIVE"
                ):
                    # A later winning lifecycle head cannot be displaced by
                    # replaying older immutable transition history.
                    return replay
        except BaseException as error:
            replay_primary = error
            raise
        finally:
            self._close(replay_connection, replay_primary)
        loaded = self.load_approval(identity, deadline=deadline)
        if moment < int(loaded.approval["logical_issued_at"]):
            _fail(Phase8ContractError, "approval transition precedes issuance")
        if loaded.lifecycle_event["event_sha256"] != expected:
            _fail(Phase8CurrentConflict, "approval lifecycle changed")
        event = self._approval_event(
            approval_id=identity,
            sequence=int(loaded.lifecycle_event["event_sequence"]) + 1,
            state=state,
            effective_at=moment,
            reason_code=reason,
            previous_event_sha256=expected,
        )
        request_sha = canonical_sha256(
            {
                "schema_version": "phase8-approval-transition-request-v1",
                "idempotency_key": key,
                "event": event,
            }
        )
        event_blob = self._put_fact(event, deadline=deadline)
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            self._transaction(connection, deadline=deadline, adapter_fence=adapter_fence)
            existing = connection.execute(
                "SELECT * FROM phase8_approval_events WHERE idempotency_key=?", (key,)
            ).fetchone()
            approval_row = connection.execute(
                "SELECT * FROM phase8_approvals WHERE approval_id=?", (identity,)
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    _fail(Phase8ReplayConflict, "transition key reused with different request")
                current = connection.execute(
                    "SELECT event_sha256,state,publication_sha256,publication_json "
                    "FROM phase8_approval_current "
                    "WHERE approval_id=?",
                    (identity,),
                ).fetchone()
                replay = self._approval_from_rows(
                    approval_row, existing, replayed=True, deadline=deadline
                )
                if (
                    current is not None
                    and current["event_sha256"] == existing["event_sha256"]
                    and current["state"] == state
                    and current["publication_sha256"] == publication_sha
                ):
                    connection.rollback()
                    _adapter_fence(adapter_fence, "after_sqlite_replay")
                    _check_deadline(deadline, "approval transition replay")
                    if not self._head_is_current(
                        phase7_current_head_verifier,
                        str(binding["phase7_scope_key"]),
                        str(binding["phase7_commit_sha256"]),
                    ) or not _publication_is_current(
                        publication, publication_head_verifier
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval transition replay publication is stale",
                        )
                    ensure_transition_receipt(
                        connection, str(existing["event_sha256"])
                    )
                    return replay
                if (
                    current is None
                    or current["event_sha256"] != expected
                    or current["state"] != "ACTIVE"
                ):
                    connection.rollback()
                    return replay
                replay_previous_publication_sha = str(
                    current["publication_sha256"]
                )
                replay_previous_publication_json = str(
                    current["publication_json"]
                )
                connection.execute(
                    "UPDATE phase8_approval_current "
                    "SET event_sequence=?,event_sha256=?,state=?,"
                    "publication_sha256=?,publication_json=? "
                    "WHERE approval_id=? AND event_sha256=? AND state='ACTIVE'",
                    (
                        existing["event_sequence"],
                        existing["event_sha256"],
                        state,
                        publication_sha,
                        publication_json,
                        identity,
                        expected,
                    ),
                )

                def replay_transition_post_commit_fence(
                    committed: sqlite3.Connection,
                ) -> None:
                    winning = committed.execute(
                        "SELECT event_sha256,state,publication_sha256 "
                        "FROM phase8_approval_current "
                        "WHERE approval_id=?",
                        (identity,),
                    ).fetchone()
                    if (
                        winning is None
                        or winning["event_sha256"] != existing["event_sha256"]
                        or winning["state"] != state
                        or winning["publication_sha256"] != publication_sha
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval transition replay lost its post-commit head",
                        )
                    if not self._head_is_current(
                        phase7_current_head_verifier,
                        str(binding["phase7_scope_key"]),
                        str(binding["phase7_commit_sha256"]),
                    ) or not _publication_is_current(
                        publication, publication_head_verifier
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval transition replay became stale",
                        )

                def reconcile_replayed_transition(
                    committed: sqlite3.Connection,
                ) -> None:
                    winning = committed.execute(
                        "SELECT event_sha256,state,publication_sha256 "
                        "FROM phase8_approval_current "
                        "WHERE approval_id=?",
                        (identity,),
                    ).fetchone()
                    if (
                        winning is None
                        or winning["event_sha256"] != existing["event_sha256"]
                        or winning["state"] != state
                        or winning["publication_sha256"] != publication_sha
                    ):
                        return
                    committed.execute(
                        "UPDATE phase8_approval_current "
                        "SET event_sequence=?,event_sha256=?,state='ACTIVE',"
                        "publication_sha256=?,publication_json=? "
                        "WHERE approval_id=? AND event_sha256=? AND state=?",
                        (
                            loaded.lifecycle_event["event_sequence"],
                            loaded.lifecycle_event["event_sha256"],
                            replay_previous_publication_sha,
                            replay_previous_publication_json,
                            identity,
                            existing["event_sha256"],
                            state,
                        ),
                    )

                self._commit(
                    connection,
                    idempotency_key=key,
                    deadline=deadline,
                    adapter_fence=adapter_fence,
                    post_commit_fence=replay_transition_post_commit_fence,
                    post_commit_reconciler=reconcile_replayed_transition,
                )
                ensure_transition_receipt(
                    connection, str(existing["event_sha256"])
                )
                return replay
            current = connection.execute(
                "SELECT * FROM phase8_approval_current WHERE approval_id=?", (identity,)
            ).fetchone()
            if current is None or current["event_sha256"] != expected or current["state"] != "ACTIVE":
                _fail(Phase8CurrentConflict, "approval is no longer active/current")
            previous_publication_sha = str(current["publication_sha256"])
            previous_publication_json = str(current["publication_json"])
            connection.execute(
                """INSERT INTO phase8_approval_events(
                   event_sha256,approval_id,event_sequence,idempotency_key,request_sha256,event_json,event_blob_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    event["event_sha256"], identity, event["event_sequence"], key,
                    request_sha, _canonical_json(event), _canonical_json(event_blob.as_dict()),
                ),
            )

            def transition_history_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                historical = committed.execute(
                    "SELECT event_sha256 FROM phase8_approval_events "
                    "WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                winning = committed.execute(
                    "SELECT event_sequence,event_sha256,state "
                    "FROM phase8_approval_current WHERE approval_id=?",
                    (identity,),
                ).fetchone()
                if (
                    historical is None
                    or historical["event_sha256"] != event["event_sha256"]
                    or winning is None
                    or winning["event_sha256"] != expected
                    or winning["state"] != "ACTIVE"
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval lifecycle changed after history commit",
                    )
                if not self._head_is_current(
                    phase7_current_head_verifier,
                    str(binding["phase7_scope_key"]),
                    str(binding["phase7_commit_sha256"]),
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval Phase-7 head changed after transition history",
                    )

            self._commit(
                connection,
                idempotency_key=key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=transition_history_post_commit_fence,
            )

            self._transaction(
                connection, deadline=deadline, adapter_fence=adapter_fence
            )
            activation = connection.execute(
                "SELECT event_sha256,state,publication_sha256,publication_json "
                "FROM phase8_approval_current "
                "WHERE approval_id=?",
                (identity,),
            ).fetchone()
            if (
                activation is None
                or activation["event_sha256"] != expected
                or activation["state"] != "ACTIVE"
            ):
                _fail(
                    Phase8CurrentConflict,
                    "approval lifecycle activation lost its exact predecessor",
                )
            if not self._head_is_current(
                phase7_current_head_verifier,
                str(binding["phase7_scope_key"]),
                str(binding["phase7_commit_sha256"]),
            ) or not _publication_is_current(
                publication, publication_head_verifier
            ):
                _fail(
                    Phase8CurrentConflict,
                    "approval lifecycle publication changed before activation",
                )
            connection.execute(
                "UPDATE phase8_approval_current "
                "SET event_sequence=?,event_sha256=?,state=?,"
                "publication_sha256=?,publication_json=? "
                "WHERE approval_id=? AND event_sha256=? AND state='ACTIVE'",
                (
                    event["event_sequence"],
                    event["event_sha256"],
                    state,
                    publication_sha,
                    publication_json,
                    identity,
                    expected,
                ),
            )

            def transition_activation_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                winning = committed.execute(
                    "SELECT event_sequence,event_sha256,state,publication_sha256 "
                    "FROM phase8_approval_current WHERE approval_id=?",
                    (identity,),
                ).fetchone()
                if (
                    winning is None
                    or int(winning["event_sequence"])
                    != int(event["event_sequence"])
                    or winning["event_sha256"] != event["event_sha256"]
                    or winning["state"] != state
                    or winning["publication_sha256"] != publication_sha
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval lifecycle lost its exact activation head",
                    )
                if not self._head_is_current(
                    phase7_current_head_verifier,
                    str(binding["phase7_scope_key"]),
                    str(binding["phase7_commit_sha256"]),
                ) or not _publication_is_current(
                    publication, publication_head_verifier
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval lifecycle activation became stale",
                    )

            def reconcile_transition_current(
                committed: sqlite3.Connection,
            ) -> None:
                winning = committed.execute(
                    "SELECT event_sha256,state,publication_sha256 "
                    "FROM phase8_approval_current "
                    "WHERE approval_id=?",
                    (identity,),
                ).fetchone()
                if (
                    winning is None
                    or winning["event_sha256"] != event["event_sha256"]
                    or winning["state"] != state
                    or winning["publication_sha256"] != publication_sha
                ):
                    return
                committed.execute(
                    "UPDATE phase8_approval_current "
                    "SET event_sequence=?,event_sha256=?,state=?,"
                    "publication_sha256=?,publication_json=? "
                    "WHERE approval_id=? AND event_sha256=?",
                    (
                        loaded.lifecycle_event["event_sequence"],
                        loaded.lifecycle_event["event_sha256"],
                        loaded.lifecycle_event["state"],
                        previous_publication_sha,
                        previous_publication_json,
                        identity,
                        event["event_sha256"],
                    ),
                )

            self._commit(
                connection,
                idempotency_key=key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=transition_activation_post_commit_fence,
                post_commit_reconciler=reconcile_transition_current,
            )
            ensure_transition_receipt(connection, str(event["event_sha256"]))
            return ApprovalResult(loaded.approval, event, False)
        except BaseException as error:
            primary = error
            if connection.in_transaction:
                run_cleanup(
                    [("rollback Phase-8 approval transition", RetryableCleanup(connection.rollback))],
                    primary=error,
                )
            raise
        finally:
            self._close(connection, primary)

    def revoke_approval(
        self,
        *,
        idempotency_key: str,
        approval_id: str,
        expected_event_sha256: str,
        revoked_at: int,
        reason_code: str = "OPERATOR_REVOKED",
        phase7_current_head_verifier: CurrentHeadVerifier | None = None,
        publication_identity: Mapping[str, object] | None = None,
        publication_head_verifier: PublicationHeadVerifier | None = None,
        deadline: object | None = None,
        adapter_fence: AdapterFence | None = None,
    ) -> ApprovalResult:
        return self._transition_approval(
            idempotency_key=idempotency_key,
            approval_id=approval_id,
            expected_event_sha256=expected_event_sha256,
            state="REVOKED",
            effective_at=revoked_at,
            reason_code=reason_code,
            phase7_current_head_verifier=phase7_current_head_verifier,
            publication_identity=publication_identity,
            publication_head_verifier=publication_head_verifier,
            deadline=deadline,
            adapter_fence=adapter_fence,
        )

    def expire_approval(
        self,
        *,
        idempotency_key: str,
        approval_id: str,
        expected_event_sha256: str,
        expired_at: int,
        phase7_current_head_verifier: CurrentHeadVerifier | None = None,
        publication_identity: Mapping[str, object] | None = None,
        publication_head_verifier: PublicationHeadVerifier | None = None,
        deadline: object | None = None,
        adapter_fence: AdapterFence | None = None,
    ) -> ApprovalResult:
        loaded = self.load_approval(approval_id, deadline=deadline)
        if expired_at < int(loaded.approval["expires_at"]):
            _fail(Phase8ContractError, "expiry transition precedes expires_at")
        return self._transition_approval(
            idempotency_key=idempotency_key,
            approval_id=approval_id,
            expected_event_sha256=expected_event_sha256,
            state="EXPIRED",
            effective_at=expired_at,
            reason_code="APPROVAL_EXPIRED",
            phase7_current_head_verifier=phase7_current_head_verifier,
            publication_identity=publication_identity,
            publication_head_verifier=publication_head_verifier,
            deadline=deadline,
            adapter_fence=adapter_fence,
        )

    @staticmethod
    def _pure_approval(approval: Mapping[str, object]) -> dict[str, object]:
        staged = approval["staged_manifest"]
        return {
            "schema_version": DATA_EGRESS_APPROVAL_SCHEMA,
            "approval_id": approval["approval_id"],
            "approved": True,
            "staged_manifest_sha256": approval["staged_manifest_sha256"],
            "subject": approval["subject"]["id"],
            "policy_sha256": approval["policy_sha256"],
            "purpose": staged["purpose"],
            "artifacts": list(approval["exact_artifacts"]),
        }

    def _approval_decision_context(
        self,
        *,
        connection: sqlite3.Connection,
        scope_key: str,
        binding_sha256: str,
        staged_manifest: Mapping[str, object],
        evaluated_at: int,
        phase7_current_head_verifier: CurrentHeadVerifier,
        publication_head_verifier: PublicationHeadVerifier | None,
        binding: Mapping[str, object],
        require_exact_preflight_time: bool,
        deadline: object | None,
    ) -> tuple[dict[str, object] | None, dict[str, object] | None, str | None]:
        current_binding = connection.execute(
            "SELECT binding_sha256,publication_sha256,publication_json "
            "FROM phase8_reference_current WHERE scope_key=?",
            (scope_key,),
        ).fetchone()
        if current_binding is None or current_binding["binding_sha256"] != binding_sha256:
            return None, None, "REFERENCE_BINDING_DRIFT"
        if not self._head_is_current(
            phase7_current_head_verifier,
            str(binding["phase7_scope_key"]),
            str(binding["phase7_commit_sha256"]),
        ):
            return None, None, "PHASE7_HEAD_DRIFT"
        scope_approval = connection.execute(
            "SELECT approval_id,publication_sha256,publication_json "
            "FROM phase8_scope_approval_current WHERE scope_key=?",
            (scope_key,),
        ).fetchone()
        if scope_approval is None:
            return None, None, "APPROVAL_MISSING"
        approval_current = connection.execute(
            "SELECT publication_sha256,publication_json "
            "FROM phase8_approval_current WHERE approval_id=?",
            (scope_approval["approval_id"],),
        ).fetchone()
        if approval_current is None:
            return None, None, "APPROVAL_MISSING"
        approval_row, event_row = self._load_approval_row(
            connection, str(scope_approval["approval_id"])
        )
        publications = (
            _decode_publication(
                current_binding["publication_sha256"],
                current_binding["publication_json"],
                field="decision reference publication",
            ),
            _decode_publication(
                scope_approval["publication_sha256"],
                scope_approval["publication_json"],
                field="decision approval-scope publication",
            ),
            _decode_publication(
                approval_current["publication_sha256"],
                approval_current["publication_json"],
                field="decision approval-lifecycle publication",
            ),
        )
        if any(
            publication["phase7_scope_key"] != binding["phase7_scope_key"]
            or publication["phase7_commit_sha256"]
            != binding["phase7_commit_sha256"]
            or not _publication_is_current(
                publication, publication_head_verifier
            )
            for publication in publications
        ) or not self._publication_receipt_is_current(
            connection,
            publications[2],
            activated_object_sha256=str(event_row["event_sha256"]),
        ):
            return None, None, "PUBLICATION_GENERATION_DRIFT"
        loaded = self._approval_from_rows(
            approval_row, event_row, replayed=False, deadline=deadline
        )
        approval = dict(loaded.approval)
        event = dict(loaded.lifecycle_event)
        if approval["binding_sha256"] != binding_sha256:
            return approval, event, "APPROVAL_BINDING_DRIFT"
        if event["state"] == "REVOKED":
            return approval, event, "APPROVAL_REVOKED"
        if event["state"] == "SUPERSEDED":
            return approval, event, "APPROVAL_SUPERSEDED"
        if event["state"] == "EXPIRED" or evaluated_at >= int(approval["expires_at"]):
            return approval, event, "APPROVAL_EXPIRED"
        trusted_evaluated_at = int(approval["decision_evaluated_at"])
        if require_exact_preflight_time and evaluated_at != trusted_evaluated_at:
            return approval, event, "TRUSTED_PREFLIGHT_TIME_MISMATCH"
        if evaluated_at < trusted_evaluated_at:
            return approval, event, "EVALUATION_TIME_ROLLBACK"
        if evaluated_at < int(approval["not_before"]):
            return approval, event, "APPROVAL_NOT_YET_ACTIVE"
        if approval["policy_sha256"] != data_egress_policy_sha256():
            return approval, event, "POLICY_DRIFT"
        if approval["staged_manifest_sha256"] != staged_manifest["staged_manifest_sha256"]:
            return approval, event, "APPROVAL_REQUEST_DRIFT"
        return approval, event, None

    def evaluate_egress(
        self,
        *,
        idempotency_key: str,
        binding_sha256: str,
        data_egress_request: Mapping[str, object],
        evaluated_at: int,
        phase7_current_head_verifier: CurrentHeadVerifier,
        publication_identity: Mapping[str, object] | None = None,
        publication_head_verifier: PublicationHeadVerifier | None = None,
        expected_previous_decision_sha256: str | None = None,
        deadline: object | None = None,
        adapter_fence: AdapterFence | None = None,
    ) -> DecisionResult:
        """Persist one shadow decision.  ``AUTHORIZED`` never dispatches."""

        self._require_enabled()
        key = _identifier(idempotency_key, "idempotency_key")
        binding_identity = _sha(binding_sha256, "binding_sha256")
        moment = _integer(evaluated_at, "evaluated_at")
        if expected_previous_decision_sha256 is not None:
            expected_previous_decision_sha256 = _sha(
                expected_previous_decision_sha256,
                "expected_previous_decision_sha256",
            )
        request = _mapping(data_egress_request, "data_egress_request")
        try:
            unapproved = evaluate_data_egress(request)
        except DataEgressError as exc:
            raise Phase8ContractError("data egress request does not stage") from exc
        staged = unapproved.staged_manifest.as_dict()
        binding_result = self.load_reference_binding(binding_identity, deadline=deadline)
        binding = binding_result.binding
        scope_key = str(binding["scope_key"])
        publication = _publication_identity(
            publication_identity,
            publication_key=key,
            phase7_scope_key=str(binding["phase7_scope_key"]),
            phase7_commit_sha256=str(binding["phase7_commit_sha256"]),
        )
        publication_sha = str(publication["publication_sha256"])
        publication_json = _canonical_json(publication)
        if not _publication_is_current(publication, publication_head_verifier):
            _fail(Phase8CurrentConflict, "decision publication is not current")
        if staged["subject"] == "":
            _fail(Phase8ContractError, "egress subject is unavailable")

        # Read current lifecycle first, then CAS-persist the immutable decision
        # before the publishing transaction.  The transaction repeats every
        # current/head check, so only immutable orphan CAS bytes can remain.
        probe = self._connect(deadline=deadline)
        probe_primary: BaseException | None = None
        try:
            approval, event, denial_reason = self._approval_decision_context(
                connection=probe,
                scope_key=scope_key,
                binding_sha256=binding_identity,
                staged_manifest=staged,
                evaluated_at=moment,
                phase7_current_head_verifier=phase7_current_head_verifier,
                publication_head_verifier=publication_head_verifier,
                binding=binding,
                require_exact_preflight_time=True,
                deadline=deadline,
            )
        except BaseException as error:
            probe_primary = error
            raise
        finally:
            self._close(probe, probe_primary)
        pure_approval = None if approval is None or denial_reason is not None else self._pure_approval(approval)
        try:
            pure = evaluate_data_egress(request, pure_approval).as_dict()
        except DataEgressError as exc:
            raise Phase8ContractError("bound approval does not satisfy pure policy") from exc
        status = pure["status"] if denial_reason is None else "DENIED"
        reason = pure["reason_code"] if denial_reason is None else denial_reason
        decision_body: dict[str, object] = {
            "schema_version": PHASE8_DECISION_SCHEMA,
            "scope_key": scope_key,
            "binding_sha256": binding_identity,
            "phase7_scope_key": binding["phase7_scope_key"],
            "phase7_commit_sha256": binding["phase7_commit_sha256"],
            "evaluated_at": moment,
            "status": status,
            "reason_code": reason,
            "request": request,
            "request_sha256": staged["request_sha256"],
            "staged_manifest_sha256": staged["staged_manifest_sha256"],
            "approval_id": None if approval is None else approval["approval_id"],
            "approval_sha256": None if approval is None else approval["approval_sha256"],
            "approval_event_sha256": None if event is None else event["event_sha256"],
            "pure_decision": pure,
            "pure_decision_sha256": pure["decision_sha256"],
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
        }
        decision = {**decision_body, "decision_sha256": canonical_sha256(decision_body)}
        persistence_request = {
            "schema_version": "phase8-decision-persistence-request-v1",
            "idempotency_key": key,
            "binding_sha256": binding_identity,
            "data_egress_request": request,
            "evaluated_at": moment,
            "expected_previous_decision_sha256": expected_previous_decision_sha256,
        }
        persistence_sha = canonical_sha256(persistence_request)
        decision_blob = self._put_fact(decision, deadline=deadline)
        _adapter_fence(adapter_fence, "after_cas_before_sqlite")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            self._transaction(connection, deadline=deadline, adapter_fence=adapter_fence)
            existing = connection.execute(
                "SELECT * FROM phase8_decisions WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != persistence_sha:
                    _fail(Phase8ReplayConflict, "decision key reused with different request")
                replay = self._decision_from_row(
                    existing, replayed=True, deadline=deadline
                )
                replay_current = connection.execute(
                    "SELECT sequence,decision_sha256,publication_sha256,publication_json "
                    "FROM phase8_decision_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                if (
                    replay_current is not None
                    and int(replay_current["sequence"]) == int(existing["sequence"])
                    and replay_current["decision_sha256"]
                    == existing["decision_sha256"]
                    and replay_current["publication_sha256"] == publication_sha
                ):
                    connection.rollback()
                    _adapter_fence(adapter_fence, "after_sqlite_replay")
                    _check_deadline(deadline, "egress decision replay")
                    replay_approval, replay_event, replay_denial = (
                        self._approval_decision_context(
                            connection=connection,
                            scope_key=scope_key,
                            binding_sha256=binding_identity,
                            staged_manifest=staged,
                            evaluated_at=moment,
                            phase7_current_head_verifier=phase7_current_head_verifier,
                            publication_head_verifier=publication_head_verifier,
                            binding=binding,
                            require_exact_preflight_time=True,
                            deadline=deadline,
                        )
                    )
                    if (
                        replay_denial != denial_reason
                        or (
                            None
                            if replay_approval is None
                            else replay_approval["approval_sha256"]
                        )
                        != decision["approval_sha256"]
                        or (
                            None
                            if replay_event is None
                            else replay_event["event_sha256"]
                        )
                        != decision["approval_event_sha256"]
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval/head changed before decision replay",
                        )
                    if not _publication_is_current(
                        publication, publication_head_verifier
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "decision publication changed before replay",
                        )
                    return replay
                publication_takeover = (
                    replay_current is not None
                    and int(replay_current["sequence"])
                    == int(existing["sequence"])
                    and replay_current["decision_sha256"]
                    == existing["decision_sha256"]
                )
                replay_previous = (
                    None
                    if replay_current is None
                    else str(replay_current["decision_sha256"])
                )
                replay_previous_sequence = (
                    0 if replay_current is None else int(replay_current["sequence"])
                )
                if not publication_takeover and (
                    replay_previous != expected_previous_decision_sha256
                    or existing["previous_decision_sha256"]
                    != expected_previous_decision_sha256
                    or int(existing["sequence"]) != replay_previous_sequence + 1
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "decision replay lost its exact predecessor",
                    )
                replay_previous_publication_sha = (
                    None
                    if replay_current is None
                    else str(replay_current["publication_sha256"])
                )
                replay_previous_publication_json = (
                    None
                    if replay_current is None
                    else str(replay_current["publication_json"])
                )
                replay_sequence = int(existing["sequence"])
                connection.execute(
                    """INSERT INTO phase8_decision_current(
                       scope_key,sequence,decision_sha256,
                       publication_sha256,publication_json
                       ) VALUES(?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                       sequence=excluded.sequence,
                       decision_sha256=excluded.decision_sha256,
                       publication_sha256=excluded.publication_sha256,
                       publication_json=excluded.publication_json""",
                    (
                        scope_key,
                        replay_sequence,
                        existing["decision_sha256"],
                        publication_sha,
                        publication_json,
                    ),
                )

                def replay_decision_post_commit_fence(
                    committed: sqlite3.Connection,
                ) -> None:
                    winning = committed.execute(
                        "SELECT sequence,decision_sha256,publication_sha256 "
                        "FROM phase8_decision_current WHERE scope_key=?",
                        (scope_key,),
                    ).fetchone()
                    if (
                        winning is None
                        or int(winning["sequence"]) != replay_sequence
                        or winning["decision_sha256"]
                        != existing["decision_sha256"]
                        or winning["publication_sha256"] != publication_sha
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "decision replay lost its post-commit head",
                        )
                    replay_approval, replay_event, replay_denial = (
                        self._approval_decision_context(
                            connection=committed,
                            scope_key=scope_key,
                            binding_sha256=binding_identity,
                            staged_manifest=staged,
                            evaluated_at=moment,
                            phase7_current_head_verifier=phase7_current_head_verifier,
                            publication_head_verifier=publication_head_verifier,
                            binding=binding,
                            require_exact_preflight_time=True,
                            deadline=None,
                        )
                    )
                    if (
                        replay_denial != denial_reason
                        or (
                            None
                            if replay_approval is None
                            else replay_approval["approval_sha256"]
                        )
                        != decision["approval_sha256"]
                        or (
                            None
                            if replay_event is None
                            else replay_event["event_sha256"]
                        )
                        != decision["approval_event_sha256"]
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "approval/head changed after decision replay",
                        )
                    if not _publication_is_current(
                        publication, publication_head_verifier
                    ):
                        _fail(
                            Phase8CurrentConflict,
                            "decision publication changed after replay",
                        )

                def reconcile_replayed_decision(
                    committed: sqlite3.Connection,
                ) -> None:
                    winning = committed.execute(
                        "SELECT sequence,decision_sha256,publication_sha256 "
                        "FROM phase8_decision_current WHERE scope_key=?",
                        (scope_key,),
                    ).fetchone()
                    if (
                        winning is None
                        or int(winning["sequence"]) != replay_sequence
                        or winning["decision_sha256"]
                        != existing["decision_sha256"]
                        or winning["publication_sha256"] != publication_sha
                    ):
                        return
                    if replay_previous is None:
                        committed.execute(
                            "DELETE FROM phase8_decision_current "
                            "WHERE scope_key=? AND sequence=? AND decision_sha256=?",
                            (scope_key, replay_sequence, existing["decision_sha256"]),
                        )
                    elif publication_takeover:
                        committed.execute(
                            "UPDATE phase8_decision_current "
                            "SET publication_sha256=?,publication_json=? "
                            "WHERE scope_key=? AND sequence=? AND decision_sha256=? "
                            "AND publication_sha256=?",
                            (
                                replay_previous_publication_sha,
                                replay_previous_publication_json,
                                scope_key,
                                replay_sequence,
                                existing["decision_sha256"],
                                publication_sha,
                            ),
                        )
                    else:
                        committed.execute(
                            "UPDATE phase8_decision_current "
                            "SET sequence=?,decision_sha256=?,"
                            "publication_sha256=?,publication_json=? "
                            "WHERE scope_key=? AND sequence=? AND decision_sha256=?",
                            (
                                replay_previous_sequence,
                                replay_previous,
                                replay_previous_publication_sha,
                                replay_previous_publication_json,
                                scope_key,
                                replay_sequence,
                                existing["decision_sha256"],
                            ),
                        )

                self._commit(
                    connection,
                    idempotency_key=key,
                    deadline=deadline,
                    adapter_fence=adapter_fence,
                    post_commit_fence=replay_decision_post_commit_fence,
                    post_commit_reconciler=reconcile_replayed_decision,
                )
                return replay
            current = connection.execute(
                "SELECT * FROM phase8_decision_current WHERE scope_key=?", (scope_key,)
            ).fetchone()
            previous = None if current is None else str(current["decision_sha256"])
            sequence = 1 if current is None else int(current["sequence"]) + 1
            previous_publication_sha = (
                None if current is None else str(current["publication_sha256"])
            )
            previous_publication_json = (
                None if current is None else str(current["publication_json"])
            )
            if previous != expected_previous_decision_sha256:
                _fail(Phase8CurrentConflict, "decision compare-and-swap differs")
            current_approval, current_event, current_denial = self._approval_decision_context(
                connection=connection,
                scope_key=scope_key,
                binding_sha256=binding_identity,
                staged_manifest=staged,
                evaluated_at=moment,
                phase7_current_head_verifier=phase7_current_head_verifier,
                publication_head_verifier=publication_head_verifier,
                binding=binding,
                require_exact_preflight_time=True,
                deadline=deadline,
            )
            current_approval_sha = None if current_approval is None else current_approval["approval_sha256"]
            current_event_sha = None if current_event is None else current_event["event_sha256"]
            if (
                current_denial != denial_reason
                or current_approval_sha != decision["approval_sha256"]
                or current_event_sha != decision["approval_event_sha256"]
            ):
                _fail(Phase8CurrentConflict, "approval/head changed before decision commit")
            connection.execute(
                """INSERT INTO phase8_decisions(
                   decision_sha256,idempotency_key,request_sha256,scope_key,sequence,
                   previous_decision_sha256,decision_json,decision_blob_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    decision["decision_sha256"], key, persistence_sha, scope_key,
                    sequence, previous, _canonical_json(decision),
                    _canonical_json(decision_blob.as_dict()),
                ),
            )

            def decision_history_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                historical = committed.execute(
                    "SELECT sequence,decision_sha256,previous_decision_sha256 "
                    "FROM phase8_decisions WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                current_head = committed.execute(
                    "SELECT sequence,decision_sha256 "
                    "FROM phase8_decision_current WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                current_head_sha = (
                    None
                    if current_head is None
                    else str(current_head["decision_sha256"])
                )
                if (
                    historical is None
                    or int(historical["sequence"]) != sequence
                    or historical["decision_sha256"]
                    != decision["decision_sha256"]
                    or historical["previous_decision_sha256"] != previous
                    or current_head_sha != previous
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "decision history lost its exact predecessor",
                    )
                history_approval, history_event, history_denial = (
                    self._approval_decision_context(
                        connection=committed,
                        scope_key=scope_key,
                        binding_sha256=binding_identity,
                        staged_manifest=staged,
                        evaluated_at=moment,
                        phase7_current_head_verifier=phase7_current_head_verifier,
                        publication_head_verifier=publication_head_verifier,
                        binding=binding,
                        require_exact_preflight_time=True,
                        deadline=None,
                    )
                )
                if (
                    history_denial != denial_reason
                    or (
                        None
                        if history_approval is None
                        else history_approval["approval_sha256"]
                    )
                    != decision["approval_sha256"]
                    or (
                        None
                        if history_event is None
                        else history_event["event_sha256"]
                    )
                    != decision["approval_event_sha256"]
                    or not _publication_is_current(
                        publication, publication_head_verifier
                    )
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval/publication changed after decision history",
                    )

            self._commit(
                connection,
                idempotency_key=key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=decision_history_post_commit_fence,
            )

            self._transaction(
                connection, deadline=deadline, adapter_fence=adapter_fence
            )
            activation_current = connection.execute(
                "SELECT sequence,decision_sha256 "
                "FROM phase8_decision_current WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            activation_previous = (
                None
                if activation_current is None
                else str(activation_current["decision_sha256"])
            )
            activation_previous_sequence = (
                0
                if activation_current is None
                else int(activation_current["sequence"])
            )
            if (
                activation_previous != previous
                or activation_previous_sequence + 1 != sequence
            ):
                _fail(
                    Phase8CurrentConflict,
                    "decision activation lost its exact predecessor",
                )
            activation_approval, activation_event, activation_denial = (
                self._approval_decision_context(
                    connection=connection,
                    scope_key=scope_key,
                    binding_sha256=binding_identity,
                    staged_manifest=staged,
                    evaluated_at=moment,
                    phase7_current_head_verifier=phase7_current_head_verifier,
                    publication_head_verifier=publication_head_verifier,
                    binding=binding,
                    require_exact_preflight_time=True,
                    deadline=deadline,
                )
            )
            if (
                activation_denial != denial_reason
                or (
                    None
                    if activation_approval is None
                    else activation_approval["approval_sha256"]
                )
                != decision["approval_sha256"]
                or (
                    None
                    if activation_event is None
                    else activation_event["event_sha256"]
                )
                != decision["approval_event_sha256"]
                or not _publication_is_current(
                    publication, publication_head_verifier
                )
            ):
                _fail(
                    Phase8CurrentConflict,
                    "approval/publication changed before decision activation",
                )
            connection.execute(
                """INSERT INTO phase8_decision_current(
                   scope_key,sequence,decision_sha256,
                   publication_sha256,publication_json
                   ) VALUES(?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                   sequence=excluded.sequence,
                   decision_sha256=excluded.decision_sha256,
                   publication_sha256=excluded.publication_sha256,
                   publication_json=excluded.publication_json""",
                (
                    scope_key,
                    sequence,
                    decision["decision_sha256"],
                    publication_sha,
                    publication_json,
                ),
            )

            def decision_post_commit_fence(
                committed: sqlite3.Connection,
            ) -> None:
                winning = committed.execute(
                    "SELECT sequence,decision_sha256,publication_sha256 "
                    "FROM phase8_decision_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                if (
                    winning is None
                    or int(winning["sequence"]) != sequence
                    or winning["decision_sha256"] != decision["decision_sha256"]
                    or winning["publication_sha256"] != publication_sha
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "egress decision lost its exact post-commit head",
                    )
                post_approval, post_event, post_denial = (
                    self._approval_decision_context(
                        connection=committed,
                        scope_key=scope_key,
                        binding_sha256=binding_identity,
                        staged_manifest=staged,
                        evaluated_at=moment,
                        phase7_current_head_verifier=phase7_current_head_verifier,
                        publication_head_verifier=publication_head_verifier,
                        binding=binding,
                        require_exact_preflight_time=True,
                        deadline=None,
                    )
                )
                if (
                    post_denial != denial_reason
                    or (
                        None
                        if post_approval is None
                        else post_approval["approval_sha256"]
                    )
                    != decision["approval_sha256"]
                    or (
                        None if post_event is None else post_event["event_sha256"]
                    )
                    != decision["approval_event_sha256"]
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "approval/head changed after decision commit",
                    )
                if not _publication_is_current(
                    publication, publication_head_verifier
                ):
                    _fail(
                        Phase8CurrentConflict,
                        "decision publication changed after activation",
                    )

            def reconcile_decision_current(
                committed: sqlite3.Connection,
            ) -> None:
                winning = committed.execute(
                    "SELECT sequence,decision_sha256,publication_sha256 "
                    "FROM phase8_decision_current "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                if (
                    winning is None
                    or int(winning["sequence"]) != sequence
                    or winning["decision_sha256"] != decision["decision_sha256"]
                    or winning["publication_sha256"] != publication_sha
                ):
                    return
                if previous is None:
                    committed.execute(
                        "DELETE FROM phase8_decision_current "
                        "WHERE scope_key=? AND sequence=? AND decision_sha256=?",
                        (scope_key, sequence, decision["decision_sha256"]),
                    )
                else:
                    committed.execute(
                        "UPDATE phase8_decision_current "
                        "SET sequence=?,decision_sha256=?,"
                        "publication_sha256=?,publication_json=? "
                        "WHERE scope_key=? AND sequence=? AND decision_sha256=?",
                        (
                            sequence - 1,
                            previous,
                            previous_publication_sha,
                            previous_publication_json,
                            scope_key,
                            sequence,
                            decision["decision_sha256"],
                        ),
                    )

            self._commit(
                connection,
                idempotency_key=key,
                deadline=deadline,
                adapter_fence=adapter_fence,
                post_commit_fence=decision_post_commit_fence,
                post_commit_reconciler=reconcile_decision_current,
            )
            return DecisionResult(decision, False)
        except BaseException as error:
            primary = error
            if connection.in_transaction:
                run_cleanup(
                    [("rollback Phase-8 decision", RetryableCleanup(connection.rollback))],
                    primary=error,
                )
            raise
        finally:
            self._close(connection, primary)

    def _decision_from_row(
        self, row: sqlite3.Row, *, replayed: bool, deadline: object | None
    ) -> DecisionResult:
        decision = _decode_json(row["decision_json"], "egress decision")
        digest = _hashed(decision, "decision_sha256", "egress decision")
        if digest != row["decision_sha256"]:
            _fail(Phase8StoreError, "egress decision row identity differs")
        _false_safety(decision, "egress decision")
        self._verify_fact(
            _decode_json(row["decision_blob_json"], "decision CAS fact"),
            decision,
            deadline=deadline,
        )
        pure = decision.get("pure_decision")
        if not isinstance(pure, Mapping) or not _verify_historical_pure_decision(pure):
            _fail(Phase8StoreError, "pure egress decision does not revalidate")
        if pure.get("decision_sha256") != decision.get("pure_decision_sha256"):
            _fail(Phase8StoreError, "pure decision binding differs")
        return DecisionResult(decision, replayed)

    def load_decision(
        self,
        decision_sha256: str,
        *,
        deadline: object | None = None,
    ) -> DecisionResult:
        self._require_enabled()
        digest = _sha(decision_sha256, "decision_sha256")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            row = connection.execute(
                "SELECT * FROM phase8_decisions WHERE decision_sha256=?", (digest,)
            ).fetchone()
            if row is None:
                _fail(Phase8NotFound, "decision is unavailable")
            return self._decision_from_row(row, replayed=False, deadline=deadline)
        except BaseException as error:
            primary = error
            raise
        finally:
            self._close(connection, primary)

    def load_decision_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        deadline: object | None = None,
    ) -> DecisionResult:
        """Query immutable decision history by the caller's exact replay key."""

        self._require_enabled()
        key = _identifier(idempotency_key, "idempotency_key")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            row = connection.execute(
                "SELECT * FROM phase8_decisions WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row is None:
                _fail(Phase8NotFound, "decision history is unavailable")
            return self._decision_from_row(row, replayed=True, deadline=deadline)
        except BaseException as error:
            primary = error
            raise
        finally:
            self._close(connection, primary)

    def load_current_decision(
        self,
        scope_key: str,
        *,
        evaluated_at: int,
        phase7_current_head_verifier: CurrentHeadVerifier,
        publication_head_verifier: PublicationHeadVerifier | None = None,
        deadline: object | None = None,
    ) -> dict[str, object]:
        """Return the sole live effective-current view, preserving history.

        The durable current row identifies the latest decision-history input;
        it is never itself authorization.  This method always rejoins approval
        lifecycle, reference/P7 head, policy and time, so revoke, expiry or
        supersession is visible as DENIED after process restart without
        mutating the original AUTHORIZED decision.
        """

        self._require_enabled()
        scope = _sha(scope_key, "scope_key")
        moment = _integer(evaluated_at, "evaluated_at")
        connection = self._connect(deadline=deadline)
        primary: BaseException | None = None
        try:
            row = connection.execute(
                """SELECT d.*,
                          c.publication_sha256 AS current_publication_sha256,
                          c.publication_json AS current_publication_json
                   FROM phase8_decision_current c
                   JOIN phase8_decisions d ON d.decision_sha256=c.decision_sha256
                   WHERE c.scope_key=?""",
                (scope,),
            ).fetchone()
            if row is None:
                _fail(Phase8NotFound, "current decision is unavailable")
            stored = self._decision_from_row(row, replayed=False, deadline=deadline).decision
            decision_publication = _decode_publication(
                row["current_publication_sha256"],
                row["current_publication_json"],
                field="decision current publication",
            )
            if (
                decision_publication["phase7_scope_key"]
                != stored["phase7_scope_key"]
                or decision_publication["phase7_commit_sha256"]
                != stored["phase7_commit_sha256"]
                or not _publication_is_current(
                    decision_publication, publication_head_verifier
                )
            ):
                _fail(
                    Phase8CurrentConflict,
                    "decision publication generation is not current",
                )
            binding_row = connection.execute(
                "SELECT binding_json FROM phase8_reference_bindings WHERE binding_sha256=?",
                (stored["binding_sha256"],),
            ).fetchone()
            if binding_row is None:
                _fail(Phase8StoreError, "decision reference binding is unavailable")
            binding = _decode_json(binding_row["binding_json"], "decision binding")
            self._verify_binding_deep(binding, deadline=deadline)
            staged = stored["pure_decision"]["staged_manifest"]
            current_approval, current_event, reason = self._approval_decision_context(
                connection=connection,
                scope_key=scope,
                binding_sha256=str(stored["binding_sha256"]),
                staged_manifest=staged,
                evaluated_at=moment,
                phase7_current_head_verifier=phase7_current_head_verifier,
                publication_head_verifier=publication_head_verifier,
                binding=binding,
                require_exact_preflight_time=False,
                deadline=deadline,
            )
            if (
                reason is None
                and current_approval is not None
                and current_approval.get("approval_sha256")
                != stored.get("approval_sha256")
            ):
                reason = "APPROVAL_SUPERSEDED"
            elif (
                reason is None
                and current_event is not None
                and current_event.get("event_sha256")
                != stored.get("approval_event_sha256")
            ):
                reason = "APPROVAL_LIFECYCLE_DRIFT"
            status = str(stored["status"])
            effective_reason = str(stored["reason_code"])
            if reason is not None:
                status = "DENIED"
                effective_reason = reason
            body: dict[str, object] = {
                "schema_version": PHASE8_CURRENT_VIEW_SCHEMA,
                "scope_key": scope,
                "source_decision_sha256": stored["decision_sha256"],
                "evaluated_at": moment,
                "status": status,
                "reason_code": effective_reason,
                "history_retained": True,
                "authoritative": False,
                "authority_transferred": False,
                "dispatch_performed": False,
            }
            return {**body, "current_view_sha256": canonical_sha256(body)}
        except BaseException as error:
            primary = error
            raise
        finally:
            self._close(connection, primary)


class Phase8EvidenceEgressRunner:
    """Thin typed facade for later lazy CLI/Web/service adapters."""

    def __init__(self, store: Phase8EvidenceEgressStore):
        if type(store) is not Phase8EvidenceEgressStore:
            _fail(Phase8ContractError, "runner requires a Phase8 store")
        self.store = store

    def bind(self, **kwargs: object) -> dict[str, object]:
        return self.store.record_reference_binding(**kwargs).as_dict()

    def issue(self, **kwargs: object) -> dict[str, object]:
        return self.store.issue_approval(**kwargs).as_dict()

    def revoke(self, **kwargs: object) -> dict[str, object]:
        return self.store.revoke_approval(**kwargs).as_dict()

    def evaluate(self, **kwargs: object) -> dict[str, object]:
        return self.store.evaluate_egress(**kwargs).as_dict()

    def load_binding(self, binding_sha256: str, **kwargs: object) -> dict[str, object]:
        return self.store.load_reference_binding(binding_sha256, **kwargs).as_dict()

    def current_binding(self, scope_key: str, **kwargs: object) -> dict[str, object]:
        return self.store.load_current_reference_binding(scope_key, **kwargs).as_dict()

    def load_approval(self, approval_id: str, **kwargs: object) -> dict[str, object]:
        return self.store.load_approval(approval_id, **kwargs).as_dict()

    def current_approval(self, scope_key: str, **kwargs: object) -> dict[str, object]:
        return self.store.load_current_approval(scope_key, **kwargs).as_dict()

    def load_decision(self, decision_sha256: str, **kwargs: object) -> dict[str, object]:
        return self.store.load_decision(decision_sha256, **kwargs).as_dict()

    def current_decision(self, scope_key: str, **kwargs: object) -> dict[str, object]:
        return self.store.load_current_decision(scope_key, **kwargs)


def run_phase8_evidence_egress_shadow(
    *,
    enabled: bool = PHASE8_DEFAULT_ENABLED,
    database_path: Path,
    cas_root: Path,
) -> Phase8EvidenceEgressRunner:
    """Create an inert/default-off runner without allocating resources."""

    return Phase8EvidenceEgressRunner(
        Phase8EvidenceEgressStore(database_path, cas_root, enabled=enabled)
    )


__all__ = (
    "PHASE8_DEFAULT_ENABLED",
    "PHASE8_STORE_SCHEMA",
    "PHASE8_PUBLICATION_SCHEMA",
    "PHASE8_PUBLICATION_RECEIPT_SCHEMA",
    "ApprovalResult",
    "DecisionResult",
    "Phase8ContractError",
    "Phase8CurrentConflict",
    "Phase8DeadlineExceeded",
    "Phase8Disabled",
    "Phase8EvidenceEgressRunner",
    "Phase8EvidenceEgressStore",
    "build_phase8_publication_identity",
    "Phase8NotFound",
    "Phase8ReplayConflict",
    "Phase8RuntimeError",
    "Phase8SchemaIncompatible",
    "Phase8StoreError",
    "ReferenceBindingResult",
    "run_phase8_evidence_egress_shadow",
)
