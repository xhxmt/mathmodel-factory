"""Pure Phase-8 data-egress staging and approval-binding shadow policy.

This module validates declared facts only.  It cannot read artifact bytes,
authenticate an approver, persist an approval, dispatch a transfer, or contact
any provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from factory_core.canonical import CanonicalizationError, canonical_sha256


DATA_EGRESS_REQUEST_SCHEMA = "data-egress-request-v1"
DATA_EGRESS_STAGED_MANIFEST_SCHEMA = "data-egress-staged-manifest-v1"
DATA_EGRESS_APPROVAL_SCHEMA = "data-egress-approval-v1"
DATA_EGRESS_DECISION_SCHEMA = "data-egress-decision-v1"
DATA_EGRESS_POLICY_SCHEMA = "data-egress-policy-v1"

_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_ARTIFACT_CLASSIFICATIONS = frozenset(
    {"public", "internal", "confidential", "restricted"}
)
_TRANSFER_FORMS = frozenset({"raw", "canonical-text", "excerpt", "rendered"})
_POLICY = {
    "schema_version": DATA_EGRESS_POLICY_SCHEMA,
    "authorization_model": "exact-staged-manifest-approval-v1",
    "artifact_order": "artifact-id-ascending",
    "dispatch_capability": False,
}


class DataEgressError(ValueError):
    """Raised when an egress request or approval is outside the wire contract."""


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise DataEgressError(f"{field} must be an object")
    return value


def _require_keys(
    value: Mapping[str, object], expected: set[str], field: str
) -> None:
    if set(value) != expected:
        raise DataEgressError(
            f"{field} fields must be exactly {sorted(expected)!r}"
        )


def _require_nonblank_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataEgressError(f"{field} must be a non-blank string")
    return value.strip()


def _require_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_CHARACTERS for character in value)
    ):
        raise DataEgressError(f"{field} must be lowercase SHA-256 hex")
    return value


def _require_nonnegative_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DataEgressError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class DataEgressArtifact:
    artifact_id: str
    sha256: str
    byte_length: int
    transfer_form: str
    classification: str

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "transfer_form": self.transfer_form,
            "classification": self.classification,
        }

    def approval_binding(self) -> dict[str, str]:
        return {"artifact_id": self.artifact_id, "sha256": self.sha256}


@dataclass(frozen=True)
class DataEgressStagedManifest:
    schema_version: str
    request_sha256: str
    policy_sha256: str
    subject: str
    provider: str
    surface: str
    account_scope: str
    retention: str
    purpose: str
    artifacts: tuple[DataEgressArtifact, ...]
    state: str = "STAGED"

    def _identity_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_sha256": self.request_sha256,
            "policy_sha256": self.policy_sha256,
            "subject": self.subject,
            "provider": self.provider,
            "surface": self.surface,
            "account_scope": self.account_scope,
            "retention": self.retention,
            "purpose": self.purpose,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "state": self.state,
        }

    @property
    def staged_manifest_sha256(self) -> str:
        return canonical_sha256(self._identity_dict())

    def as_dict(self) -> dict[str, object]:
        result = self._identity_dict()
        result["staged_manifest_sha256"] = self.staged_manifest_sha256
        return result


@dataclass(frozen=True)
class DataEgressApproval:
    schema_version: str
    approval_id: str
    approved: bool
    staged_manifest_sha256: str
    subject: str
    policy_sha256: str
    purpose: str
    artifacts: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "approved": self.approved,
            "staged_manifest_sha256": self.staged_manifest_sha256,
            "subject": self.subject,
            "policy_sha256": self.policy_sha256,
            "purpose": self.purpose,
            "artifacts": [
                {"artifact_id": artifact_id, "sha256": sha256}
                for artifact_id, sha256 in self.artifacts
            ],
        }

    @property
    def approval_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class DataEgressDecision:
    schema_version: str
    status: str
    reason_code: str
    policy_sha256: str
    request_sha256: str
    staged_manifest: DataEgressStagedManifest
    approval: DataEgressApproval | None
    dispatch_performed: bool = False

    def _identity_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "reason_code": self.reason_code,
            "policy_sha256": self.policy_sha256,
            "request_sha256": self.request_sha256,
            "staged_manifest": self.staged_manifest.as_dict(),
            "approval": None if self.approval is None else self.approval.as_dict(),
            "approval_sha256": (
                None if self.approval is None else self.approval.approval_sha256
            ),
            "dispatch_performed": self.dispatch_performed,
        }

    @property
    def decision_sha256(self) -> str:
        return canonical_sha256(self._identity_dict())

    def as_dict(self) -> dict[str, object]:
        result = self._identity_dict()
        result["decision_sha256"] = self.decision_sha256
        return result


def data_egress_policy_sha256() -> str:
    """Return the stable identity of this declaration-only shadow policy."""

    return canonical_sha256(_POLICY)


def _compile_artifact(value: object, index: int) -> DataEgressArtifact:
    artifact = _require_mapping(value, f"artifacts[{index}]")
    _require_keys(
        artifact,
        {
            "artifact_id",
            "sha256",
            "byte_length",
            "transfer_form",
            "classification",
        },
        f"artifacts[{index}]",
    )
    transfer_form = _require_nonblank_string(
        artifact["transfer_form"], f"artifacts[{index}].transfer_form"
    )
    if transfer_form not in _TRANSFER_FORMS:
        raise DataEgressError("unsupported artifact transfer_form")
    classification = _require_nonblank_string(
        artifact["classification"], f"artifacts[{index}].classification"
    )
    if classification not in _ARTIFACT_CLASSIFICATIONS:
        raise DataEgressError("unsupported artifact classification")
    return DataEgressArtifact(
        artifact_id=_require_nonblank_string(
            artifact["artifact_id"], f"artifacts[{index}].artifact_id"
        ),
        sha256=_require_sha256(
            artifact["sha256"], f"artifacts[{index}].sha256"
        ),
        byte_length=_require_nonnegative_integer(
            artifact["byte_length"], f"artifacts[{index}].byte_length"
        ),
        transfer_form=transfer_form,
        classification=classification,
    )


def _normalized_request_identity(
    *,
    subject: str,
    provider: str,
    surface: str,
    account_scope: str,
    retention: str,
    purpose: str,
    artifacts: tuple[DataEgressArtifact, ...],
) -> dict[str, object]:
    return {
        "schema_version": DATA_EGRESS_REQUEST_SCHEMA,
        "subject": subject,
        "provider": provider,
        "surface": surface,
        "account_scope": account_scope,
        "retention": retention,
        "purpose": purpose,
        "artifacts": [artifact.as_dict() for artifact in artifacts],
    }


def _stage_request(request: Mapping[str, object]) -> DataEgressStagedManifest:
    root = _require_mapping(request, "request")
    _require_keys(
        root,
        {
            "schema_version",
            "subject",
            "provider",
            "surface",
            "account_scope",
            "retention",
            "purpose",
            "artifacts",
        },
        "request",
    )
    if root["schema_version"] != DATA_EGRESS_REQUEST_SCHEMA:
        raise DataEgressError("unsupported data egress request schema")
    artifact_values = root["artifacts"]
    if not isinstance(artifact_values, list) or not artifact_values:
        raise DataEgressError("artifacts must be a non-empty array")
    artifacts = tuple(
        sorted(
            (
                _compile_artifact(value, index)
                for index, value in enumerate(artifact_values)
            ),
            key=lambda artifact: artifact.artifact_id,
        )
    )
    artifact_ids = [artifact.artifact_id for artifact in artifacts]
    if len(set(artifact_ids)) != len(artifact_ids):
        raise DataEgressError("artifact_id must be unique")
    fields = {
        field: _require_nonblank_string(root[field], field)
        for field in (
            "subject",
            "provider",
            "surface",
            "account_scope",
            "retention",
            "purpose",
        )
    }
    request_identity = _normalized_request_identity(
        **fields,
        artifacts=artifacts,
    )
    return DataEgressStagedManifest(
        schema_version=DATA_EGRESS_STAGED_MANIFEST_SCHEMA,
        request_sha256=canonical_sha256(request_identity),
        policy_sha256=data_egress_policy_sha256(),
        artifacts=artifacts,
        **fields,
    )


def _compile_approval(value: object) -> DataEgressApproval:
    approval = _require_mapping(value, "approval")
    _require_keys(
        approval,
        {
            "schema_version",
            "approval_id",
            "approved",
            "staged_manifest_sha256",
            "subject",
            "policy_sha256",
            "purpose",
            "artifacts",
        },
        "approval",
    )
    if approval["schema_version"] != DATA_EGRESS_APPROVAL_SCHEMA:
        raise DataEgressError("unsupported data egress approval schema")
    if not isinstance(approval["approved"], bool):
        raise DataEgressError("approval.approved must be a boolean")
    artifact_values = approval["artifacts"]
    if not isinstance(artifact_values, list):
        raise DataEgressError("approval.artifacts must be an array")
    artifacts: list[tuple[str, str]] = []
    for index, value_item in enumerate(artifact_values):
        item = _require_mapping(value_item, f"approval.artifacts[{index}]")
        _require_keys(
            item,
            {"artifact_id", "sha256"},
            f"approval.artifacts[{index}]",
        )
        artifacts.append(
            (
                _require_nonblank_string(
                    item["artifact_id"],
                    f"approval.artifacts[{index}].artifact_id",
                ),
                _require_sha256(
                    item["sha256"], f"approval.artifacts[{index}].sha256"
                ),
            )
        )
    if artifacts != sorted(artifacts, key=lambda item: item[0]):
        raise DataEgressError("approval artifacts must use canonical order")
    if len({artifact_id for artifact_id, _sha256 in artifacts}) != len(artifacts):
        raise DataEgressError("approval artifact_id must be unique")
    return DataEgressApproval(
        schema_version=DATA_EGRESS_APPROVAL_SCHEMA,
        approval_id=_require_nonblank_string(
            approval["approval_id"], "approval.approval_id"
        ),
        approved=approval["approved"],
        staged_manifest_sha256=_require_sha256(
            approval["staged_manifest_sha256"],
            "approval.staged_manifest_sha256",
        ),
        subject=_require_nonblank_string(approval["subject"], "approval.subject"),
        policy_sha256=_require_sha256(
            approval["policy_sha256"], "approval.policy_sha256"
        ),
        purpose=_require_nonblank_string(approval["purpose"], "approval.purpose"),
        artifacts=tuple(artifacts),
    )


def _approval_matches(
    approval: DataEgressApproval, manifest: DataEgressStagedManifest
) -> bool:
    return (
        approval.staged_manifest_sha256 == manifest.staged_manifest_sha256
        and approval.subject == manifest.subject
        and approval.policy_sha256 == manifest.policy_sha256
        and approval.purpose == manifest.purpose
        and approval.artifacts
        == tuple(
            (artifact.artifact_id, artifact.sha256)
            for artifact in manifest.artifacts
        )
    )


def evaluate_data_egress(
    request: Mapping[str, object],
    approval: Mapping[str, object] | None = None,
) -> DataEgressDecision:
    """Stage a request and return a declaration-only authorization decision."""

    manifest = _stage_request(request)
    compiled_approval = None if approval is None else _compile_approval(approval)
    if compiled_approval is None:
        status, reason_code = "DENIED", "APPROVAL_MISSING"
    elif not compiled_approval.approved:
        status, reason_code = "DENIED", "APPROVAL_NOT_GRANTED"
    elif not _approval_matches(compiled_approval, manifest):
        status, reason_code = "DENIED", "APPROVAL_BINDING_MISMATCH"
    else:
        status, reason_code = "AUTHORIZED", "EXACT_APPROVAL_MATCH"
    return DataEgressDecision(
        schema_version=DATA_EGRESS_DECISION_SCHEMA,
        status=status,
        reason_code=reason_code,
        policy_sha256=manifest.policy_sha256,
        request_sha256=manifest.request_sha256,
        staged_manifest=manifest,
        approval=compiled_approval,
        dispatch_performed=False,
    )


def _artifact_from_serialized(value: object, index: int) -> DataEgressArtifact:
    return _compile_artifact(value, index)


def _manifest_from_serialized(value: object) -> DataEgressStagedManifest:
    wire = _require_mapping(value, "staged_manifest")
    _require_keys(
        wire,
        {
            "schema_version",
            "request_sha256",
            "policy_sha256",
            "subject",
            "provider",
            "surface",
            "account_scope",
            "retention",
            "purpose",
            "artifacts",
            "state",
            "staged_manifest_sha256",
        },
        "staged_manifest",
    )
    if wire["schema_version"] != DATA_EGRESS_STAGED_MANIFEST_SCHEMA:
        raise DataEgressError("unsupported staged manifest schema")
    if wire["state"] != "STAGED":
        raise DataEgressError("staged manifest state must be STAGED")
    artifacts_value = wire["artifacts"]
    if not isinstance(artifacts_value, list) or not artifacts_value:
        raise DataEgressError("staged manifest artifacts must be a non-empty array")
    artifacts = tuple(
        _artifact_from_serialized(item, index)
        for index, item in enumerate(artifacts_value)
    )
    if list(artifacts) != sorted(artifacts, key=lambda artifact: artifact.artifact_id):
        raise DataEgressError("staged manifest artifacts are not canonical")
    if len({artifact.artifact_id for artifact in artifacts}) != len(artifacts):
        raise DataEgressError("staged manifest artifact_id must be unique")
    manifest = DataEgressStagedManifest(
        schema_version=DATA_EGRESS_STAGED_MANIFEST_SCHEMA,
        request_sha256=_require_sha256(
            wire["request_sha256"], "staged_manifest.request_sha256"
        ),
        policy_sha256=_require_sha256(
            wire["policy_sha256"], "staged_manifest.policy_sha256"
        ),
        subject=_require_nonblank_string(wire["subject"], "staged_manifest.subject"),
        provider=_require_nonblank_string(
            wire["provider"], "staged_manifest.provider"
        ),
        surface=_require_nonblank_string(
            wire["surface"], "staged_manifest.surface"
        ),
        account_scope=_require_nonblank_string(
            wire["account_scope"], "staged_manifest.account_scope"
        ),
        retention=_require_nonblank_string(
            wire["retention"], "staged_manifest.retention"
        ),
        purpose=_require_nonblank_string(wire["purpose"], "staged_manifest.purpose"),
        artifacts=artifacts,
    )
    if wire["staged_manifest_sha256"] != manifest.staged_manifest_sha256:
        raise DataEgressError("staged manifest hash mismatch")
    expected_request_sha256 = canonical_sha256(
        _normalized_request_identity(
            subject=manifest.subject,
            provider=manifest.provider,
            surface=manifest.surface,
            account_scope=manifest.account_scope,
            retention=manifest.retention,
            purpose=manifest.purpose,
            artifacts=manifest.artifacts,
        )
    )
    if manifest.request_sha256 != expected_request_sha256:
        raise DataEgressError("staged manifest request hash mismatch")
    if manifest.policy_sha256 != data_egress_policy_sha256():
        raise DataEgressError("staged manifest policy hash mismatch")
    return manifest


def verify_data_egress_decision(
    decision: Mapping[str, object] | DataEgressDecision,
) -> bool:
    """Verify a JSON-round-tripped decision without reading external state."""

    try:
        wire = decision.as_dict() if isinstance(decision, DataEgressDecision) else decision
        root = _require_mapping(wire, "decision")
        _require_keys(
            root,
            {
                "schema_version",
                "status",
                "reason_code",
                "policy_sha256",
                "request_sha256",
                "staged_manifest",
                "approval",
                "approval_sha256",
                "dispatch_performed",
                "decision_sha256",
            },
            "decision",
        )
        if root["schema_version"] != DATA_EGRESS_DECISION_SCHEMA:
            return False
        if root["dispatch_performed"] is not False:
            return False
        manifest = _manifest_from_serialized(root["staged_manifest"])
        if root["policy_sha256"] != manifest.policy_sha256:
            return False
        if root["request_sha256"] != manifest.request_sha256:
            return False
        approval_value = root["approval"]
        approval = None if approval_value is None else _compile_approval(approval_value)
        approval_sha256 = None if approval is None else approval.approval_sha256
        if root["approval_sha256"] != approval_sha256:
            return False
        if approval is None:
            expected = ("DENIED", "APPROVAL_MISSING")
        elif not approval.approved:
            expected = ("DENIED", "APPROVAL_NOT_GRANTED")
        elif not _approval_matches(approval, manifest):
            expected = ("DENIED", "APPROVAL_BINDING_MISMATCH")
        else:
            expected = ("AUTHORIZED", "EXACT_APPROVAL_MATCH")
        if (root["status"], root["reason_code"]) != expected:
            return False
        reconstructed = DataEgressDecision(
            schema_version=DATA_EGRESS_DECISION_SCHEMA,
            status=expected[0],
            reason_code=expected[1],
            policy_sha256=manifest.policy_sha256,
            request_sha256=manifest.request_sha256,
            staged_manifest=manifest,
            approval=approval,
            dispatch_performed=False,
        )
        return root["decision_sha256"] == reconstructed.decision_sha256
    except (CanonicalizationError, DataEgressError, TypeError, ValueError):
        return False
