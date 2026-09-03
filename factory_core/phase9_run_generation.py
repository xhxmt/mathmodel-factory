"""Candidate-bound, default-off Phase9 run-generation creation.

The public service owns its SQLite transaction and accepts no connection or
SQL from callers.  It does not start a replay, worker, provider, outbox,
delivery, migration, release, deployment, or cutover.  Its only mutation is an
append-only generation/receipt lineage plus the single current-generation
pointer and the matching Authority workflow coordinate.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Callable, Mapping
import unicodedata

from .authority_production_schema import (
    authority_database_path,
    connect_authority_rw,
    legacy_source_identity_sha256,
    verify_production_installation,
)
from .canonical import canonical_bytes, canonical_sha256
from .contract_pins import (
    CONTRACT_PIN_SET_SCHEMA,
    ContractPinSetV1,
    ContractPinValidationError,
    validate_contract_pin_set,
)
from .workflow_contract_v2 import compile_workflow_contract_bundle_v2


RUN_GENERATION_REQUEST_SCHEMA = "authority-phase9-run-generation-request-v2"
RUN_GENERATION_RECEIPT_SCHEMA = (
    "authority-phase9-run-generation-creation-receipt-v2"
)
GIT_SOURCE_IDENTITY_SCHEMA = "authority-phase9-git-source-identity-v1"
GIT_TRACKED_SOURCE_ENTRY_SCHEMA = "authority-phase9-git-tracked-source-entry-v1"
GIT_TRACKED_SOURCE_INVENTORY_SCHEMA = (
    "authority-phase9-git-tracked-source-inventory-v1"
)
RUN_GENERATION_AUTHORIZATION_TARGET_SCHEMA = (
    "authority-phase9-run-generation-authorization-target-v2"
)
RUN_GENERATION_INTENT_SCHEMA = "authority-phase9-run-generation-intent-v1"
RUN_GENERATION_AUTHORIZATION_CONSUMPTION_SCHEMA = (
    "authority-phase9-run-generation-authorization-consumption-v1"
)
OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA = (
    "authority-phase9-official-input-file-evidence-v1"
)
OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA = (
    "authority-phase9-official-input-manifest-evidence-v1"
)
EXECUTION_CONTEXT_EVIDENCE_SCHEMA = (
    "authority-phase9-execution-context-evidence-v1"
)
OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA = (
    "authority-phase9-operator-authorization-evidence-v2"
)

CREATE = "CREATE"
ROTATE = "ROTATE"
DELIVERY_DISABLED = "DISABLED"
FORENSIC_REPLAY = "FORENSIC_REPLAY"
LEGACY_NOT_APPLICABLE = "LEGACY_NOT_APPLICABLE"
V1_ONLY = "V1_ONLY"
LEGACY_UNKNOWN = "legacy_unknown"
PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS = 300

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OID = re.compile(r"[0-9a-f]{40}\Z")


class Phase9RunGenerationError(RuntimeError):
    """Base error for the narrow run-generation creation boundary."""


class Phase9RunGenerationConflict(Phase9RunGenerationError):
    """A replay key, predecessor, source, or coordinate is stale/conflicting."""


class Phase9RunGenerationSafetyError(Phase9RunGenerationError):
    """Default-off or typed-evidence safety prerequisites are not satisfied."""


@dataclass(frozen=True)
class GitSourceIdentityV1:
    schema_version: str
    source_commit: str
    source_tree: str
    source_parent: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "source_parent": self.source_parent,
        }


@dataclass(frozen=True)
class GitTrackedSourceEntryV1:
    schema_version: str
    logical_path: str
    git_mode: str
    git_object_type: str
    git_object_id: str
    byte_length: int | None
    raw_bytes_sha256: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "logical_path": self.logical_path,
            "git_mode": self.git_mode,
            "git_object_type": self.git_object_type,
            "git_object_id": self.git_object_id,
            "byte_length": self.byte_length,
            "raw_bytes_sha256": self.raw_bytes_sha256,
        }


@dataclass(frozen=True)
class GitTrackedSourceInventoryV1:
    schema_version: str
    source_commit: str
    source_tree: str
    source_parent: str
    entries: tuple[GitTrackedSourceEntryV1, ...]
    path_count: int
    total_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "source_parent": self.source_parent,
            "entries": [item.as_dict() for item in self.entries],
            "path_count": self.path_count,
            "total_bytes": self.total_bytes,
        }

    @property
    def inventory_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class GitSourceSnapshotV1:
    source: GitSourceIdentityV1
    tracked_inventory: GitTrackedSourceInventoryV1

    @property
    def source_commit(self) -> str:
        return self.source.source_commit

    @property
    def source_tree(self) -> str:
        return self.source.source_tree

    @property
    def source_parent(self) -> str:
        return self.source.source_parent

    @property
    def source_inventory_sha256(self) -> str:
        return self.tracked_inventory.inventory_sha256


@dataclass(frozen=True)
class OfficialInputFileEvidenceV1:
    schema_version: str
    logical_path: str
    byte_length: int
    raw_bytes_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "logical_path": self.logical_path,
            "byte_length": self.byte_length,
            "raw_bytes_sha256": self.raw_bytes_sha256,
        }


@dataclass(frozen=True)
class OfficialInputManifestEvidenceV1:
    schema_version: str
    input_generation: str
    files: tuple[OfficialInputFileEvidenceV1, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "input_generation": self.input_generation,
            "files": [item.as_dict() for item in self.files],
        }

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def raw_bytes_set_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "authority-phase9-official-input-raw-bytes-set-v1",
                "files": [
                    {
                        "logical_path": item.logical_path,
                        "byte_length": item.byte_length,
                        "raw_bytes_sha256": item.raw_bytes_sha256,
                    }
                    for item in self.files
                ],
            }
        )


@dataclass(frozen=True)
class ExecutionContextEvidenceV1:
    schema_version: str
    context_id: str
    runtime_environment_sha256: str
    dependency_lock_sha256: str
    launcher_argv_sha256: str
    captured_at: int

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "runtime_environment_sha256": self.runtime_environment_sha256,
            "dependency_lock_sha256": self.dependency_lock_sha256,
            "launcher_argv_sha256": self.launcher_argv_sha256,
            "captured_at": self.captured_at,
        }

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class OperatorAuthorizationEvidenceV1:
    schema_version: str
    authorization_id: str
    authorization_mechanism: str
    authorization_evidence_sha256: str
    authorized: bool
    operator_uid: int
    operator_account: str
    authorizer_subject: str
    operator_subject: str
    operation_kind: str
    project_id: str
    workflow_id: str
    source_commit: str
    authorized_request_sha256: str
    issued_at: int
    expires_at: int
    authorization_statement_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authorization_id": self.authorization_id,
            "authorization_mechanism": self.authorization_mechanism,
            "authorization_evidence_sha256": (
                self.authorization_evidence_sha256
            ),
            "authorized": self.authorized,
            "operator_uid": self.operator_uid,
            "operator_account": self.operator_account,
            "authorizer_subject": self.authorizer_subject,
            "operator_subject": self.operator_subject,
            "operation_kind": self.operation_kind,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "source_commit": self.source_commit,
            "authorized_request_sha256": self.authorized_request_sha256,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "authorization_statement_sha256": (
                self.authorization_statement_sha256
            ),
        }

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def expected_statement_sha256(self) -> str:
        value = self.as_dict()
        del value["authorization_statement_sha256"]
        return canonical_sha256(
            {
                "schema": "authority-phase9-operator-authorization-statement-v2",
                "authorization": value,
            }
        )


@dataclass(frozen=True)
class RunGenerationRequestV1:
    schema_version: str
    idempotency_key: str
    operation_kind: str
    project_id: str
    workflow_id: str
    project_revision: int
    project_generation: str
    runtime_generation: str
    scheduler_generation: str
    predecessor_run_generation: str | None
    predecessor_creation_receipt_sha256: str | None
    predecessor_terminal_receipt_sha256: str | None
    run_mode: str
    modeling_consultation_contract: str
    delivery_capability: str
    source: GitSourceIdentityV1
    source_inventory_sha256: str
    contract_pins: ContractPinSetV1
    official_inputs: OfficialInputManifestEvidenceV1
    execution_context: ExecutionContextEvidenceV1
    operator_authorization: OperatorAuthorizationEvidenceV1
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
            "runtime_generation": self.runtime_generation,
            "scheduler_generation": self.scheduler_generation,
            "predecessor_run_generation": self.predecessor_run_generation,
            "predecessor_creation_receipt_sha256": (
                self.predecessor_creation_receipt_sha256
            ),
            "predecessor_terminal_receipt_sha256": (
                self.predecessor_terminal_receipt_sha256
            ),
            "run_mode": self.run_mode,
            "modeling_consultation_contract": (
                self.modeling_consultation_contract
            ),
            "delivery_capability": self.delivery_capability,
            "source": self.source.as_dict(),
            "source_inventory_sha256": self.source_inventory_sha256,
            "contract_pins": {
                item.name: getattr(self.contract_pins, item.name)
                for item in fields(ContractPinSetV1)
            },
            "official_inputs": self.official_inputs.as_dict(),
            "execution_context": self.execution_context.as_dict(),
            "operator_authorization": self.operator_authorization.as_dict(),
            "occurred_at": self.occurred_at,
        }

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def generation_intent(self) -> dict[str, object]:
        """Canonical operation intent from which the generation is derived.

        The authorization is deliberately excluded so the generation identity
        is stable across equivalent authorization envelopes.  All operation,
        source, input, context, pin, mode and predecessor fields remain bound.
        """

        value = self.as_dict()
        del value["operator_authorization"]
        return {
            "schema": RUN_GENERATION_INTENT_SCHEMA,
            "request": value,
        }

    @property
    def authorization_target(self) -> dict[str, object]:
        """Bind the complete no-auth request and its derived generation.

        Keeping generation derivation outside the authorization envelope
        avoids a hash cycle.  Naming the derived identity in the signed target
        prevents an otherwise equivalent authorization from being interpreted
        as authority for a different successor identity.
        """

        return {
            "schema": RUN_GENERATION_AUTHORIZATION_TARGET_SCHEMA,
            "derived_run_generation": self.derived_run_generation,
            "intent": self.generation_intent,
        }

    @property
    def authorization_target_sha256(self) -> str:
        return canonical_sha256(self.authorization_target)

    @property
    def derived_project_generation(self) -> str:
        digest = canonical_sha256(
            {
                "schema": "authority-phase9-project-generation-v1",
                "project_id": self.project_id,
                "workflow_id": self.workflow_id,
                "source": self.source.as_dict(),
                "source_inventory_sha256": self.source_inventory_sha256,
                "contract_pin_set_sha256": canonical_sha256(self.contract_pins),
                "official_input_manifest_sha256": (
                    self.official_inputs.manifest_sha256
                ),
                "official_input_raw_bytes_set_sha256": (
                    self.official_inputs.raw_bytes_set_sha256
                ),
            }
        )
        return f"project-generation:{digest}"

    @property
    def derived_run_generation(self) -> str:
        return f"run-generation:{canonical_sha256(self.generation_intent)}"


@dataclass(frozen=True)
class RunGenerationCreationResult:
    run_generation: str
    workflow_id: str
    operation_kind: str
    request_sha256: str
    receipt_id: str
    receipt_sha256: str
    occurred_at: int
    replayed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": RUN_GENERATION_RECEIPT_SCHEMA,
            "run_generation": self.run_generation,
            "workflow_id": self.workflow_id,
            "operation_kind": self.operation_kind,
            "request_sha256": self.request_sha256,
            "receipt_id": self.receipt_id,
            "receipt_sha256": self.receipt_sha256,
            "occurred_at": self.occurred_at,
            "replayed": self.replayed,
        }


def _exact(value: object, expected: type, path: str) -> None:
    if type(value) is not expected:
        raise Phase9RunGenerationSafetyError(
            f"{path} has an unsupported runtime type"
        )
    for item in fields(expected):
        try:
            object.__getattribute__(value, item.name)
        except AttributeError as exc:  # pragma: no cover - defensive boundary
            raise Phase9RunGenerationSafetyError(
                f"{path}.{item.name} is missing"
            ) from exc


def _text(value: object, path: str, *, identifier: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise Phase9RunGenerationSafetyError(
            f"{path} must be a non-empty trimmed string"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise Phase9RunGenerationSafetyError(
            f"{path} must contain valid UTF-8"
        ) from exc
    if identifier and _IDENTIFIER.fullmatch(value) is None:
        raise Phase9RunGenerationSafetyError(f"{path} is not a safe identifier")
    return value


def _relative_posix_path(value: object, path: str) -> str:
    result = _text(value, path)
    pure = PurePosixPath(result)
    if (
        pure.is_absolute()
        or result != pure.as_posix()
        or "\\" in result
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(part.endswith((".", " ")) for part in pure.parts)
        or any(ord(character) < 32 for character in result)
    ):
        raise Phase9RunGenerationSafetyError(
            f"{path} must be a normalized relative POSIX path"
        )
    return result


def _concrete(value: object, path: str) -> str:
    result = _text(value, path, identifier=True)
    if result == LEGACY_UNKNOWN:
        raise Phase9RunGenerationSafetyError(f"{path} must be concrete")
    return result


def _sha(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise Phase9RunGenerationSafetyError(f"{path} must be lowercase SHA-256")
    return value


def _git_oid(value: object, path: str) -> str:
    if type(value) is not str or _GIT_OID.fullmatch(value) is None:
        raise Phase9RunGenerationSafetyError(
            f"{path} must be a concrete lowercase 40-hex Git object ID"
        )
    return value


def _nonnegative(value: object, path: str) -> int:
    if type(value) is not int or value < 0:
        raise Phase9RunGenerationSafetyError(
            f"{path} must be a plain nonnegative integer"
        )
    return value


def validate_git_source_identity(value: GitSourceIdentityV1) -> GitSourceIdentityV1:
    _exact(value, GitSourceIdentityV1, "source")
    if value.schema_version != GIT_SOURCE_IDENTITY_SCHEMA:
        raise Phase9RunGenerationSafetyError("source schema is unsupported")
    _git_oid(value.source_commit, "source.source_commit")
    _git_oid(value.source_tree, "source.source_tree")
    _git_oid(value.source_parent, "source.source_parent")
    return value


def validate_official_inputs(
    value: OfficialInputManifestEvidenceV1,
) -> OfficialInputManifestEvidenceV1:
    _exact(value, OfficialInputManifestEvidenceV1, "official_inputs")
    if value.schema_version != OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA:
        raise Phase9RunGenerationSafetyError(
            "official input manifest evidence schema is unsupported"
        )
    _concrete(value.input_generation, "official_inputs.input_generation")
    if type(value.files) is not tuple or not value.files:
        raise Phase9RunGenerationSafetyError(
            "official_inputs.files must be a non-empty immutable tuple"
        )
    paths: list[str] = []
    collision_keys: set[str] = set()
    for index, item in enumerate(value.files):
        path = f"official_inputs.files[{index}]"
        _exact(item, OfficialInputFileEvidenceV1, path)
        if item.schema_version != OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA:
            raise Phase9RunGenerationSafetyError(
                f"{path} evidence schema is unsupported"
            )
        logical = _relative_posix_path(
            item.logical_path, f"{path}.logical_path"
        )
        collision_key = unicodedata.normalize("NFC", logical).casefold()
        if collision_key in collision_keys:
            raise Phase9RunGenerationSafetyError(
                "official input paths collide by Unicode normalization or case"
            )
        collision_keys.add(collision_key)
        _nonnegative(item.byte_length, f"{path}.byte_length")
        _sha(item.raw_bytes_sha256, f"{path}.raw_bytes_sha256")
        paths.append(logical)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise Phase9RunGenerationSafetyError(
            "official input files must be unique and logical-path sorted"
        )
    return value


def validate_execution_context(
    value: ExecutionContextEvidenceV1,
) -> ExecutionContextEvidenceV1:
    _exact(value, ExecutionContextEvidenceV1, "execution_context")
    if value.schema_version != EXECUTION_CONTEXT_EVIDENCE_SCHEMA:
        raise Phase9RunGenerationSafetyError(
            "execution context evidence schema is unsupported"
        )
    _concrete(value.context_id, "execution_context.context_id")
    _sha(
        value.runtime_environment_sha256,
        "execution_context.runtime_environment_sha256",
    )
    _sha(value.dependency_lock_sha256, "execution_context.dependency_lock_sha256")
    _sha(value.launcher_argv_sha256, "execution_context.launcher_argv_sha256")
    _nonnegative(value.captured_at, "execution_context.captured_at")
    return value


def validate_operator_authorization(
    value: OperatorAuthorizationEvidenceV1,
    request: RunGenerationRequestV1,
    *,
    trusted_now: int,
) -> OperatorAuthorizationEvidenceV1:
    _exact(value, OperatorAuthorizationEvidenceV1, "operator_authorization")
    if value.schema_version != OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA:
        raise Phase9RunGenerationSafetyError(
            "operator authorization requires typed canonical evidence"
        )
    _concrete(value.authorization_id, "operator_authorization.authorization_id")
    if value.authorization_mechanism != "CONTROLLED_OS_ACCOUNT":
        raise Phase9RunGenerationSafetyError(
            "only verified CONTROLLED_OS_ACCOUNT authorization is supported"
        )
    _sha(
        value.authorization_evidence_sha256,
        "operator_authorization.authorization_evidence_sha256",
    )
    if value.authorized is not True:
        raise Phase9RunGenerationSafetyError("operator is not authorized")
    uid = _nonnegative(value.operator_uid, "operator_authorization.operator_uid")
    account = _concrete(
        value.operator_account, "operator_authorization.operator_account"
    )
    try:
        current_uid = os.geteuid()
        current_account = pwd.getpwuid(current_uid).pw_name
    except (AttributeError, KeyError) as exc:
        raise Phase9RunGenerationSafetyError(
            "controlled OS account identity cannot be verified"
        ) from exc
    if uid != current_uid or account != current_account:
        raise Phase9RunGenerationSafetyError(
            "operator authorization does not match the executing OS account"
        )
    _concrete(value.authorizer_subject, "operator_authorization.authorizer_subject")
    _concrete(value.operator_subject, "operator_authorization.operator_subject")
    _sha(
        value.authorization_statement_sha256,
        "operator_authorization.authorization_statement_sha256",
    )
    _sha(
        value.authorized_request_sha256,
        "operator_authorization.authorized_request_sha256",
    )
    if value.authorization_statement_sha256 != value.expected_statement_sha256:
        raise Phase9RunGenerationSafetyError(
            "operator authorization statement hash differs from canonical statement"
        )
    issued = _nonnegative(value.issued_at, "operator_authorization.issued_at")
    expires = _nonnegative(value.expires_at, "operator_authorization.expires_at")
    if expires < issued or not issued <= trusted_now <= expires:
        raise Phase9RunGenerationSafetyError(
            "operator authorization is not valid at trusted current time"
        )
    if trusted_now - issued > PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS:
        raise Phase9RunGenerationSafetyError(
            "operator authorization issue time exceeds trusted clock skew"
        )
    if (
        value.operation_kind != request.operation_kind
        or value.project_id != request.project_id
        or value.workflow_id != request.workflow_id
        or value.source_commit != request.source.source_commit
        or value.authorized_request_sha256 != request.authorization_target_sha256
    ):
        raise Phase9RunGenerationSafetyError(
            "operator authorization coordinate differs from request"
        )
    return value


def validate_run_generation_request(
    value: RunGenerationRequestV1,
    *,
    trusted_now: int | None = None,
) -> RunGenerationRequestV1:
    now = int(time.time()) if trusted_now is None else _nonnegative(
        trusted_now, "trusted_now"
    )
    _exact(value, RunGenerationRequestV1, "request")
    if value.schema_version != RUN_GENERATION_REQUEST_SCHEMA:
        raise Phase9RunGenerationSafetyError("run-generation request schema is unsupported")
    _concrete(value.idempotency_key, "request.idempotency_key")
    if value.operation_kind not in {CREATE, ROTATE}:
        raise Phase9RunGenerationSafetyError("operation_kind must be CREATE or ROTATE")
    _concrete(value.project_id, "request.project_id")
    _concrete(value.workflow_id, "request.workflow_id")
    _nonnegative(value.project_revision, "request.project_revision")
    _concrete(value.project_generation, "request.project_generation")
    if value.operation_kind == CREATE and (
        value.project_generation != value.derived_project_generation
    ):
        raise Phase9RunGenerationSafetyError(
            "CREATE project generation must be derived from canonical project identity"
        )
    _concrete(value.runtime_generation, "request.runtime_generation")
    _concrete(value.scheduler_generation, "request.scheduler_generation")
    if value.run_mode != FORENSIC_REPLAY:
        raise Phase9RunGenerationSafetyError(
            "Phase9 run-generation mode must be FORENSIC_REPLAY"
        )
    if value.modeling_consultation_contract != LEGACY_NOT_APPLICABLE:
        raise Phase9RunGenerationSafetyError(
            "Phase9 modeling consultation contract must be LEGACY_NOT_APPLICABLE"
        )
    if value.delivery_capability != DELIVERY_DISABLED:
        raise Phase9RunGenerationSafetyError(
            "Phase9 run-generation delivery capability must remain DISABLED"
        )
    if value.operation_kind == CREATE:
        if (
            value.predecessor_run_generation is not None
            or value.predecessor_creation_receipt_sha256 is not None
            or value.predecessor_terminal_receipt_sha256 is not None
        ):
            raise Phase9RunGenerationSafetyError(
                "CREATE cannot claim a predecessor"
            )
    else:
        _concrete(
            value.predecessor_run_generation,
            "request.predecessor_run_generation",
        )
        _sha(
            value.predecessor_creation_receipt_sha256,
            "request.predecessor_creation_receipt_sha256",
        )
        _sha(
            value.predecessor_terminal_receipt_sha256,
            "request.predecessor_terminal_receipt_sha256",
        )
    validate_git_source_identity(value.source)
    _sha(value.source_inventory_sha256, "request.source_inventory_sha256")
    try:
        validate_contract_pin_set(
            value.contract_pins, compile_workflow_contract_bundle_v2()
        )
    except ContractPinValidationError as exc:
        raise Phase9RunGenerationSafetyError(
            f"contract pin source authorization failed: {exc}"
        ) from exc
    validate_official_inputs(value.official_inputs)
    validate_execution_context(value.execution_context)
    _nonnegative(value.occurred_at, "request.occurred_at")
    if abs(value.occurred_at - now) > PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS:
        raise Phase9RunGenerationSafetyError(
            "request occurrence metadata exceeds trusted clock skew"
        )
    if value.execution_context.captured_at > value.occurred_at:
        raise Phase9RunGenerationSafetyError(
            "execution context was captured after request occurrence"
        )
    validate_operator_authorization(
        value.operator_authorization,
        value,
        trusted_now=now,
    )
    return value


def _exact_mapping(
    value: object, path: str, keys: set[str]
) -> Mapping[str, object]:
    if type(value) is not dict:
        raise Phase9RunGenerationSafetyError(f"{path} must be a plain object")
    if set(value) != keys:
        raise Phase9RunGenerationSafetyError(
            f"{path} keys differ: missing={sorted(keys-set(value))}, "
            f"extra={sorted(set(value)-keys)}"
        )
    return value


def run_generation_request_from_dict(
    value: object, *, trusted_now: int | None = None
) -> RunGenerationRequestV1:
    """Decode one strict JSON-domain request into the typed creation API."""

    item = _exact_mapping(
        value,
        "request",
        {
            "schema_version", "idempotency_key", "operation_kind", "project_id",
            "workflow_id", "project_revision", "project_generation",
            "runtime_generation", "scheduler_generation",
            "predecessor_run_generation", "predecessor_creation_receipt_sha256",
            "predecessor_terminal_receipt_sha256",
            "run_mode", "modeling_consultation_contract", "delivery_capability",
            "source", "source_inventory_sha256", "contract_pins",
            "official_inputs", "execution_context",
            "operator_authorization", "occurred_at",
        },
    )
    source = _exact_mapping(
        item["source"],
        "request.source",
        {"schema_version", "source_commit", "source_tree", "source_parent"},
    )
    pin_names = {field.name for field in fields(ContractPinSetV1)}
    pins = _exact_mapping(item["contract_pins"], "request.contract_pins", pin_names)
    official = _exact_mapping(
        item["official_inputs"],
        "request.official_inputs",
        {"schema_version", "input_generation", "files"},
    )
    if type(official["files"]) is not list:
        raise Phase9RunGenerationSafetyError(
            "request.official_inputs.files must be an array"
        )
    official_files = []
    for index, raw in enumerate(official["files"]):
        file_item = _exact_mapping(
            raw,
            f"request.official_inputs.files[{index}]",
            {"schema_version", "logical_path", "byte_length", "raw_bytes_sha256"},
        )
        official_files.append(OfficialInputFileEvidenceV1(**file_item))
    context = _exact_mapping(
        item["execution_context"],
        "request.execution_context",
        {
            "schema_version", "context_id", "runtime_environment_sha256",
            "dependency_lock_sha256", "launcher_argv_sha256", "captured_at",
        },
    )
    authorization = _exact_mapping(
        item["operator_authorization"],
        "request.operator_authorization",
        {
            "schema_version", "authorization_id", "authorization_mechanism",
            "authorization_evidence_sha256", "authorized", "authorizer_subject",
            "operator_uid", "operator_account",
            "operator_subject", "operation_kind", "project_id", "workflow_id",
            "source_commit", "authorized_request_sha256", "issued_at", "expires_at",
            "authorization_statement_sha256",
        },
    )
    decoded = RunGenerationRequestV1(
        schema_version=item["schema_version"],
        idempotency_key=item["idempotency_key"],
        operation_kind=item["operation_kind"],
        project_id=item["project_id"],
        workflow_id=item["workflow_id"],
        project_revision=item["project_revision"],
        project_generation=item["project_generation"],
        runtime_generation=item["runtime_generation"],
        scheduler_generation=item["scheduler_generation"],
        predecessor_run_generation=item["predecessor_run_generation"],
        predecessor_creation_receipt_sha256=item[
            "predecessor_creation_receipt_sha256"
        ],
        predecessor_terminal_receipt_sha256=item[
            "predecessor_terminal_receipt_sha256"
        ],
        run_mode=item["run_mode"],
        modeling_consultation_contract=item["modeling_consultation_contract"],
        delivery_capability=item["delivery_capability"],
        source=GitSourceIdentityV1(**source),
        source_inventory_sha256=item["source_inventory_sha256"],
        contract_pins=ContractPinSetV1(**pins),
        official_inputs=OfficialInputManifestEvidenceV1(
            schema_version=official["schema_version"],
            input_generation=official["input_generation"],
            files=tuple(official_files),
        ),
        execution_context=ExecutionContextEvidenceV1(**context),
        operator_authorization=OperatorAuthorizationEvidenceV1(**authorization),
        occurred_at=item["occurred_at"],
    )
    return validate_run_generation_request(decoded, trusted_now=trusted_now)


_SAFE_GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "LC_ALL": "C",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
}
_SAFE_GIT_PREFIX = (
    "git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "submodule.recurse=false",
    "-c",
    "pager.branch=false",
    "-c",
    "pager.log=false",
)
_SOURCE_MAX_ENTRIES = 100_000
_SOURCE_MAX_FILE_BYTES = 64 * 1024 * 1024
_SOURCE_MAX_TOTAL_BYTES = 1024 * 1024 * 1024


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class _StableDirectoryTree:
    """Hold directory descriptors and reject namespace changes during a read."""

    def __init__(self, root_value: str | Path, *, label: str) -> None:
        self.path = Path(os.path.abspath(os.fspath(root_value)))
        self.label = label
        self._directories: dict[tuple[str, ...], int] = {}
        self._directory_identities: dict[tuple[str, ...], tuple[int, ...]] = {}
        self._directory_names: dict[tuple[str, ...], tuple[str, ...]] = {}
        self._file_identities: list[
            tuple[tuple[str, ...], str, tuple[int, ...]]
        ] = []

    def __enter__(self) -> "_StableDirectoryTree":
        try:
            before = self.path.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise Phase9RunGenerationSafetyError(
                    f"{self.label} must be a non-symlink directory"
                )
            descriptor = os.open(
                self.path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(descriptor)
        except Phase9RunGenerationSafetyError:
            raise
        except OSError as exc:
            raise Phase9RunGenerationSafetyError(
                f"{self.label} is unavailable"
            ) from exc
        if _stat_identity(before) != _stat_identity(opened):
            os.close(descriptor)
            raise Phase9RunGenerationSafetyError(
                f"{self.label} changed while being opened"
            )
        self._directories[()] = descriptor
        self._directory_identities[()] = _stat_identity(opened)
        return self

    def __exit__(self, _kind, _value, _traceback) -> None:
        for descriptor in reversed(tuple(self._directories.values())):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def directory(self, parts: tuple[str, ...]) -> int:
        if parts in self._directories:
            return self._directories[parts]
        parent_parts = parts[:-1]
        parent = self.directory(parent_parts)
        name = parts[-1]
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise Phase9RunGenerationSafetyError(
                    f"{self.label} contains a symlink or special directory"
                )
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
            opened = os.fstat(descriptor)
        except Phase9RunGenerationSafetyError:
            raise
        except OSError as exc:
            raise Phase9RunGenerationSafetyError(
                f"{self.label} directory cannot be opened safely"
            ) from exc
        if _stat_identity(before) != _stat_identity(opened):
            os.close(descriptor)
            raise Phase9RunGenerationSafetyError(
                f"{self.label} directory changed while being opened"
            )
        self._directories[parts] = descriptor
        self._directory_identities[parts] = _stat_identity(opened)
        return descriptor

    def list_directory(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        descriptor = self.directory(parts)
        try:
            before = os.fstat(descriptor)
            names = os.listdir(descriptor)
            after = os.fstat(descriptor)
        except OSError as exc:
            raise Phase9RunGenerationSafetyError(
                f"{self.label} cannot be enumerated"
            ) from exc
        if _stat_identity(before) != _stat_identity(after):
            raise Phase9RunGenerationSafetyError(
                f"{self.label} directory changed while being enumerated"
            )
        canonical: list[str] = []
        for name in names:
            relative = PurePosixPath(*parts, name).as_posix()
            _relative_posix_path(relative, f"{self.label} member path")
            canonical.append(name)
        result = tuple(sorted(canonical, key=lambda item: item.encode("utf-8")))
        previous = self._directory_names.setdefault(parts, result)
        if previous != result:
            raise Phase9RunGenerationSafetyError(
                f"{self.label} directory entries changed while being enumerated"
            )
        return result

    def member_stat(
        self, parent_parts: tuple[str, ...], name: str
    ) -> os.stat_result:
        try:
            return os.stat(
                name,
                dir_fd=self.directory(parent_parts),
                follow_symlinks=False,
            )
        except OSError as exc:
            raise Phase9RunGenerationSafetyError(
                f"{self.label} member is unavailable"
            ) from exc

    def read_regular_file(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        *,
        maximum_bytes: int,
    ) -> tuple[bytes, os.stat_result]:
        parent = self.directory(parent_parts)
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > maximum_bytes
            ):
                raise Phase9RunGenerationSafetyError(
                    f"{self.label} contains a symlink, hardlink, or special file"
                )
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
            try:
                opened = os.fstat(descriptor)
                chunks: list[bytes] = []
                remaining = maximum_bytes + 1
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except Phase9RunGenerationSafetyError:
            raise
        except OSError as exc:
            raise Phase9RunGenerationSafetyError(
                f"{self.label} file cannot be read safely"
            ) from exc
        identity = _stat_identity(opened)
        if (
            len(raw) > maximum_bytes
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _stat_identity(before) != identity
            or _stat_identity(after) != identity
            or _stat_identity(current) != identity
        ):
            raise Phase9RunGenerationSafetyError(
                f"{self.label} file changed while being read"
            )
        self._file_identities.append((parent_parts, name, identity))
        return raw, opened

    def verify_unchanged(self) -> None:
        try:
            for parts, expected_names in self._directory_names.items():
                actual_names = tuple(
                    sorted(
                        os.listdir(self._directories[parts]),
                        key=lambda item: item.encode("utf-8"),
                    )
                )
                if actual_names != expected_names:
                    raise Phase9RunGenerationSafetyError(
                        f"{self.label} directory entries changed while being verified"
                    )
            for parent_parts, name, identity in self._file_identities:
                current = os.stat(
                    name,
                    dir_fd=self._directories[parent_parts],
                    follow_symlinks=False,
                )
                if _stat_identity(current) != identity:
                    raise Phase9RunGenerationSafetyError(
                        f"{self.label} file identity changed while being verified"
                    )
            for parts, descriptor in reversed(tuple(self._directories.items())):
                identity = self._directory_identities[parts]
                if _stat_identity(os.fstat(descriptor)) != identity:
                    raise Phase9RunGenerationSafetyError(
                        f"{self.label} directory changed while being verified"
                    )
                if parts:
                    parent = self._directories[parts[:-1]]
                    current = os.stat(
                        parts[-1], dir_fd=parent, follow_symlinks=False
                    )
                    if _stat_identity(current) != identity:
                        raise Phase9RunGenerationSafetyError(
                            f"{self.label} directory identity changed while being verified"
                        )
            if _stat_identity(self.path.lstat()) != self._directory_identities[()]:
                raise Phase9RunGenerationSafetyError(
                    f"{self.label} root changed while being verified"
                )
        except Phase9RunGenerationSafetyError:
            raise
        except OSError as exc:
            raise Phase9RunGenerationSafetyError(
                f"{self.label} changed while being verified"
            ) from exc


def _run_git(root: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            (*_SAFE_GIT_PREFIX, *arguments),
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
            env=_SAFE_GIT_ENV,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise Phase9RunGenerationSafetyError(
            "current Git source identity could not be read"
        ) from exc


def _read_git_identity(root: Path) -> GitSourceIdentityV1:
    try:
        lineage = _run_git(
            root, "rev-list", "--parents", "-n", "1", "HEAD"
        ).decode("ascii", errors="strict").strip().split()
        tree = _run_git(root, "rev-parse", "HEAD^{tree}").decode(
            "ascii", errors="strict"
        ).strip()
    except UnicodeError as exc:
        raise Phase9RunGenerationSafetyError(
            "current Git source identity is not ASCII"
        ) from exc
    if len(lineage) != 2:
        raise Phase9RunGenerationSafetyError(
            "Phase9 source must have exactly one concrete parent"
        )
    return validate_git_source_identity(
        GitSourceIdentityV1(
            GIT_SOURCE_IDENTITY_SCHEMA, lineage[0], tree, lineage[1]
        )
    )


def read_current_git_source_identity(repository: str | Path) -> GitSourceIdentityV1:
    """Read the exact current commit/tree/single parent from an explicit repo."""

    root = Path(os.path.abspath(os.fspath(repository)))
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise Phase9RunGenerationSafetyError("source repository is missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise Phase9RunGenerationSafetyError(
            "source repository must be a non-symlink directory"
        )
    return _read_git_identity(root)


def _git_blob_object_id(raw: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw,
        usedforsecurity=False,
    ).hexdigest()


def read_current_git_source_snapshot(repository: str | Path) -> GitSourceSnapshotV1:
    """Bind HEAD object IDs to every live tracked byte and executable mode."""

    root = Path(os.path.abspath(os.fspath(repository)))
    source_before = read_current_git_source_identity(root)
    raw_tree = _run_git(root, "ls-tree", "-rz", "--full-tree", "HEAD")
    dirty_before = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        "--ignore-submodules=none",
    )
    if dirty_before:
        raise Phase9RunGenerationSafetyError(
            "source repository tracked worktree or index differs from HEAD"
        )
    records = raw_tree.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    if not records or len(records) > _SOURCE_MAX_ENTRIES:
        raise Phase9RunGenerationSafetyError(
            "source tracked inventory is empty or too large"
        )
    parsed: list[tuple[str, str, str, str]] = []
    collision_keys: set[str] = set()
    for index, record in enumerate(records):
        metadata, separator, path_raw = record.partition(b"\t")
        try:
            mode, object_type, object_id = metadata.decode(
                "ascii", errors="strict"
            ).split()
            logical_path = path_raw.decode("utf-8", errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise Phase9RunGenerationSafetyError(
                f"source tracked inventory entry {index} is malformed"
            ) from exc
        if separator != b"\t":
            raise Phase9RunGenerationSafetyError(
                f"source tracked inventory entry {index} is malformed"
            )
        logical_path = _relative_posix_path(
            logical_path, f"source tracked inventory entry {index}.path"
        )
        collision_key = unicodedata.normalize("NFC", logical_path).casefold()
        if collision_key in collision_keys:
            raise Phase9RunGenerationSafetyError(
                "source tracked paths collide by Unicode normalization or case"
            )
        collision_keys.add(collision_key)
        if mode not in {"100644", "100755", "160000"}:
            raise Phase9RunGenerationSafetyError(
                "source tracked inventory contains an unsupported Git mode"
            )
        if (mode == "160000") != (object_type == "commit") or (
            mode != "160000" and object_type != "blob"
        ):
            raise Phase9RunGenerationSafetyError(
                "source tracked inventory mode/type differs"
            )
        _git_oid(object_id, f"source tracked inventory entry {index}.object_id")
        parsed.append((logical_path, mode, object_type, object_id))
    paths = [item[0] for item in parsed]
    if paths != sorted(paths, key=lambda value: value.encode("utf-8")) or len(
        paths
    ) != len(set(paths)):
        raise Phase9RunGenerationSafetyError(
            "source tracked inventory paths are not unique bytewise sorted"
        )

    entries: list[GitTrackedSourceEntryV1] = []
    total_bytes = 0
    with _StableDirectoryTree(root, label="source repository") as tree:
        for logical_path, mode, object_type, object_id in parsed:
            parts = PurePosixPath(logical_path).parts
            if mode == "160000":
                tree.directory(parts)
                entries.append(
                    GitTrackedSourceEntryV1(
                        GIT_TRACKED_SOURCE_ENTRY_SCHEMA,
                        logical_path,
                        mode,
                        object_type,
                        object_id,
                        None,
                        None,
                    )
                )
                continue
            raw, metadata = tree.read_regular_file(
                parts[:-1], parts[-1], maximum_bytes=_SOURCE_MAX_FILE_BYTES
            )
            expected_executable = mode == "100755"
            if bool(metadata.st_mode & 0o111) != expected_executable:
                raise Phase9RunGenerationSafetyError(
                    f"source tracked executable mode differs: {logical_path}"
                )
            if _git_blob_object_id(raw) != object_id:
                raise Phase9RunGenerationSafetyError(
                    f"source tracked bytes differ from Git object: {logical_path}"
                )
            total_bytes += len(raw)
            if total_bytes > _SOURCE_MAX_TOTAL_BYTES:
                raise Phase9RunGenerationSafetyError(
                    "source tracked inventory bytes are too large"
                )
            entries.append(
                GitTrackedSourceEntryV1(
                    GIT_TRACKED_SOURCE_ENTRY_SCHEMA,
                    logical_path,
                    mode,
                    object_type,
                    object_id,
                    len(raw),
                    hashlib.sha256(raw).hexdigest(),
                )
            )
        tree.verify_unchanged()
    source_after = read_current_git_source_identity(root)
    dirty_after = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        "--ignore-submodules=none",
    )
    if source_after != source_before or dirty_after:
        raise Phase9RunGenerationSafetyError(
            "source repository changed while tracked inventory was verified"
        )
    inventory = GitTrackedSourceInventoryV1(
        GIT_TRACKED_SOURCE_INVENTORY_SCHEMA,
        source_before.source_commit,
        source_before.source_tree,
        source_before.source_parent,
        tuple(entries),
        len(entries),
        total_bytes,
    )
    return GitSourceSnapshotV1(source_before, inventory)


def _loaded_execution_source_root() -> Path:
    """Resolve the one package root that supplied the executing Phase9 code."""

    package = sys.modules.get("factory_core")
    package_file = getattr(package, "__file__", None)
    package_paths = getattr(package, "__path__", None)
    if type(package_file) is not str or package_paths is None:
        raise Phase9RunGenerationSafetyError(
            "loaded factory_core package origin is unavailable"
        )
    try:
        module_path = Path(__file__).resolve(strict=True)
        package_path = Path(package_file).resolve(strict=True)
        module_root = module_path.parent.parent
        package_root = package_path.parent.parent
        resolved_package_paths = tuple(
            Path(item).resolve(strict=True) for item in package_paths
        )
    except (OSError, TypeError) as exc:
        raise Phase9RunGenerationSafetyError(
            "loaded factory_core package origin cannot be resolved"
        ) from exc
    expected_package_path = module_root / "factory_core"
    if (
        package_root != module_root
        or package_path != expected_package_path / "__init__.py"
        or resolved_package_paths != (expected_package_path,)
    ):
        raise Phase9RunGenerationSafetyError(
            "loaded factory_core package and Phase9 module roots differ"
        )
    return module_root


def read_verified_execution_source_snapshot(
    repository: str | Path,
    *,
    execution_root: str | Path | None = None,
) -> GitSourceSnapshotV1:
    """Bind Git objects to the bytes and modes of the code actually executing.

    ``repository`` supplies the immutable Git object identity.  ``execution_root``
    may name a no-``.git`` clean-room export, but it must still be the root from
    which this loaded ``factory_core`` package originated.  No environment
    variable participates in this production check.
    """

    snapshot = read_current_git_source_snapshot(repository)
    loaded_root = _loaded_execution_source_root()
    root = loaded_root if execution_root is None else Path(
        os.path.abspath(os.fspath(execution_root))
    )
    try:
        if root.resolve(strict=True) != loaded_root:
            raise Phase9RunGenerationSafetyError(
                "configured execution source root differs from loaded code"
            )
    except OSError as exc:
        raise Phase9RunGenerationSafetyError(
            "execution source root is unavailable"
        ) from exc

    expected = {
        item.logical_path: item
        for item in snapshot.tracked_inventory.entries
        if item.git_mode != "160000"
    }
    loaded_module_paths: set[str] = set()
    for name, module in tuple(sys.modules.items()):
        if name != "factory_core" and not name.startswith("factory_core."):
            continue
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            continue
        try:
            module_path = Path(module_file).resolve(strict=True)
            logical_path = module_path.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise Phase9RunGenerationSafetyError(
                "loaded factory_core module originates outside execution source"
            ) from exc
        if logical_path not in expected:
            raise Phase9RunGenerationSafetyError(
                "loaded factory_core module is absent from candidate Git tree"
            )
        loaded_module_paths.add(logical_path)
    if "factory_core/phase9_run_generation.py" not in loaded_module_paths:
        raise Phase9RunGenerationSafetyError(
            "executing Phase9 source module origin is unavailable"
        )

    total_bytes = 0
    with _StableDirectoryTree(root, label="execution source root") as tree:
        for logical_path, item in expected.items():
            parts = PurePosixPath(logical_path).parts
            raw, metadata = tree.read_regular_file(
                parts[:-1],
                parts[-1],
                maximum_bytes=_SOURCE_MAX_FILE_BYTES,
            )
            if (
                item.byte_length is None
                or item.raw_bytes_sha256 is None
                or len(raw) != item.byte_length
                or hashlib.sha256(raw).hexdigest() != item.raw_bytes_sha256
                or bool(metadata.st_mode & 0o111) != (item.git_mode == "100755")
                or _git_blob_object_id(raw) != item.git_object_id
            ):
                raise Phase9RunGenerationSafetyError(
                    f"execution source differs from candidate Git tree: {logical_path}"
                )
            total_bytes += len(raw)
            if total_bytes > _SOURCE_MAX_TOTAL_BYTES:
                raise Phase9RunGenerationSafetyError(
                    "execution source inventory bytes are too large"
                )
        tree.verify_unchanged()
    return snapshot


def _regular_file_bytes(
    path: Path, *, maximum_bytes: int, label: str
) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise Phase9RunGenerationSafetyError(f"{label} is unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum_bytes
    ):
        raise Phase9RunGenerationSafetyError(
            f"{label} must be one bounded non-hardlinked regular file"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum_bytes + 1)
            after = os.fstat(stream.fileno())
        final = path.lstat()
    except OSError as exc:
        raise Phase9RunGenerationSafetyError(f"{label} cannot be read safely") from exc
    if (
        len(raw) > maximum_bytes
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or _stat_identity(before) != _stat_identity(opened)
        or _stat_identity(opened) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(final)
    ):
        raise Phase9RunGenerationSafetyError(f"{label} changed while being read")
    return raw


def verify_official_input_snapshot(
    root_value: str | Path,
    evidence: OfficialInputManifestEvidenceV1,
) -> tuple[tuple[str, int, str], ...]:
    validate_official_inputs(evidence)
    expected = {item.logical_path: item for item in evidence.files}
    expected_directories: set[str] = set()
    for logical_path in expected:
        parent = PurePosixPath(logical_path).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    snapshot: list[tuple[str, int, str]] = []
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    collision_keys: set[str] = set()
    with _StableDirectoryTree(
        root_value, label="official input root"
    ) as tree:
        pending: list[tuple[str, ...]] = [()]
        while pending:
            parts = pending.pop()
            for name in tree.list_directory(parts):
                logical_path = PurePosixPath(*parts, name).as_posix()
                collision_key = unicodedata.normalize("NFC", logical_path).casefold()
                if collision_key in collision_keys:
                    raise Phase9RunGenerationSafetyError(
                        "official input tree paths collide by Unicode normalization or case"
                    )
                collision_keys.add(collision_key)
                metadata = tree.member_stat(parts, name)
                if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                    metadata.st_mode
                ):
                    if logical_path not in expected_directories:
                        raise Phase9RunGenerationSafetyError(
                            "official input root directory inventory differs from manifest"
                        )
                    actual_directories.add(logical_path)
                    tree.directory((*parts, name))
                    pending.append((*parts, name))
                    continue
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(
                    metadata.st_mode
                ) or metadata.st_nlink != 1:
                    raise Phase9RunGenerationSafetyError(
                        "official input tree contains a symlink, hardlink, or special file"
                    )
                item = expected.get(logical_path)
                if item is None:
                    raise Phase9RunGenerationSafetyError(
                        "official input root file inventory differs from manifest"
                    )
                raw, _opened = tree.read_regular_file(
                    parts, name, maximum_bytes=item.byte_length
                )
                digest = hashlib.sha256(raw).hexdigest()
                if len(raw) != item.byte_length or digest != item.raw_bytes_sha256:
                    raise Phase9RunGenerationSafetyError(
                        f"official input bytes differ: {item.logical_path}"
                    )
                actual_files.add(logical_path)
                snapshot.append((logical_path, len(raw), digest))
        if actual_files != set(expected) or actual_directories != expected_directories:
            raise Phase9RunGenerationSafetyError(
                "official input root inventory differs from manifest"
            )
        tree.verify_unchanged()
    snapshot.sort(key=lambda item: item[0].encode("utf-8"))
    return tuple(snapshot)


def _verified_execution_context_receipt(
    path: str | Path, evidence: ExecutionContextEvidenceV1
) -> bytes:
    validate_execution_context(evidence)
    raw = _regular_file_bytes(
        Path(path), maximum_bytes=1024 * 1024, label="execution context receipt"
    )
    expected = canonical_bytes(evidence.as_dict())
    if raw != expected or hashlib.sha256(raw).hexdigest() != evidence.receipt_sha256:
        raise Phase9RunGenerationSafetyError(
            "execution context receipt canonical bytes/hash differ"
        )
    return raw


def verify_execution_context_receipt(
    path: str | Path, evidence: ExecutionContextEvidenceV1
) -> bytes:
    """Verify and return the canonical external execution-context receipt bytes."""

    return _verified_execution_context_receipt(path, evidence)


def _pin_json(pin_set: ContractPinSetV1) -> str:
    return canonical_bytes(
        {item.name: getattr(pin_set, item.name) for item in fields(ContractPinSetV1)}
    ).decode("utf-8")


def _receipt_body(request: RunGenerationRequestV1) -> dict[str, object]:
    return {
        "schema": RUN_GENERATION_RECEIPT_SCHEMA,
        "run_generation": request.derived_run_generation,
        "workflow_id": request.workflow_id,
        "operation_kind": request.operation_kind,
        "request_sha256": request.request_sha256,
        "request": request.as_dict(),
        "official_input_manifest_sha256": request.official_inputs.manifest_sha256,
        "official_input_raw_bytes_set_sha256": (
            request.official_inputs.raw_bytes_set_sha256
        ),
        "execution_context_receipt_sha256": (
            request.execution_context.receipt_sha256
        ),
        "operator_authorization_receipt_sha256": (
            request.operator_authorization.receipt_sha256
        ),
        "operator_authorization_consumption_sha256": canonical_sha256(
            _authorization_consumption_body(request)
        ),
        "authorization_target_sha256": request.authorization_target_sha256,
        "source_inventory_sha256": request.source_inventory_sha256,
        "occurred_at": request.occurred_at,
    }


def _authorization_consumption_body(
    request: RunGenerationRequestV1,
) -> dict[str, object]:
    return {
        "schema": RUN_GENERATION_AUTHORIZATION_CONSUMPTION_SCHEMA,
        "authorization_id": request.operator_authorization.authorization_id,
        "authorization_receipt_sha256": (
            request.operator_authorization.receipt_sha256
        ),
        "authorization_target_sha256": request.authorization_target_sha256,
        "request_sha256": request.request_sha256,
        "run_generation": request.derived_run_generation,
        "workflow_id": request.workflow_id,
        "consumed_at": request.occurred_at,
    }


def _result(
    request: RunGenerationRequestV1,
    *,
    receipt_id: str,
    receipt_sha256: str,
    replayed: bool,
) -> RunGenerationCreationResult:
    return RunGenerationCreationResult(
        request.derived_run_generation,
        request.workflow_id,
        request.operation_kind,
        request.request_sha256,
        receipt_id,
        receipt_sha256,
        request.occurred_at,
        replayed,
    )


class Phase9RunGenerationService:
    """Atomic create/rotate service with a live Git source identity reader."""

    def __init__(
        self,
        database: str | Path,
        *,
        expected_source_fence_sha256: str,
        source_repository: str | Path,
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
        self.official_input_root = Path(official_input_root)
        self.execution_context_receipt_path = Path(
            execution_context_receipt_path
        )
        self.execution_root = (
            None if execution_root is None else Path(execution_root)
        )
        if self.execution_root is not None and not self.execution_root.is_absolute():
            raise Phase9RunGenerationSafetyError(
                "execution_root must be an absolute path"
            )
        if fault_hook is not None and not callable(fault_hook):
            raise Phase9RunGenerationSafetyError("fault_hook must be callable")
        if clock is not None and not callable(clock):
            raise Phase9RunGenerationSafetyError("clock must be callable")
        self._fault_hook = fault_hook
        self._clock = (lambda: int(time.time())) if clock is None else clock

    def _fault(self, checkpoint: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(checkpoint)

    def _trusted_now(self) -> int:
        return _nonnegative(self._clock(), "trusted_now")

    def _verify_database(self, connection: sqlite3.Connection) -> None:
        verify_production_installation(connection, require_ready=True)
        if legacy_source_identity_sha256(connection) != self.expected_source_fence_sha256:
            raise Phase9RunGenerationConflict("authority source fence differs")

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
            writer is None
            or consumer is None
            or writer["switch_mode"] != V1_ONLY
            or bool(writer["writer_enabled"])
            or bool(consumer["consumer_enabled"])
        ):
            raise Phase9RunGenerationSafetyError(
                "run-generation creation requires V1_ONLY with writer and consumer disabled"
            )

    @staticmethod
    def _persist_pin(
        connection: sqlite3.Connection,
        request: RunGenerationRequestV1,
    ) -> str:
        pin_sha256 = canonical_sha256(request.contract_pins)
        pin_json = _pin_json(request.contract_pins)
        row = connection.execute(
            "SELECT * FROM authority_contract_pin_sets WHERE pin_set_sha256=?",
            (pin_sha256,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO authority_contract_pin_sets(
                    pin_set_sha256, schema_version, pin_set_json, provenance,
                    first_recorded_revision
                ) VALUES (?, ?, ?, 'RECORDED_SHADOW', ?)
                """,
                (
                    pin_sha256,
                    CONTRACT_PIN_SET_SCHEMA,
                    pin_json,
                    max(1, request.project_revision),
                ),
            )
        elif (
            row["schema_version"] != CONTRACT_PIN_SET_SCHEMA
            or row["pin_set_json"] != pin_json
        ):
            raise Phase9RunGenerationConflict("recorded contract pin identity differs")
        return pin_sha256

    @staticmethod
    def _persist_source_inventory(
        connection: sqlite3.Connection,
        request: RunGenerationRequestV1,
        inventory: GitTrackedSourceInventoryV1,
    ) -> None:
        inventory_json = canonical_bytes(inventory.as_dict()).decode("utf-8")
        row = connection.execute(
            "SELECT * FROM authority_production_run_generation_source_inventories "
            "WHERE inventory_sha256=?",
            (inventory.inventory_sha256,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO authority_production_run_generation_source_inventories(
                    inventory_sha256, schema_version, source_commit, source_tree,
                    source_parent, path_count, total_bytes, inventory_json,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inventory.inventory_sha256,
                    inventory.schema_version,
                    inventory.source_commit,
                    inventory.source_tree,
                    inventory.source_parent,
                    inventory.path_count,
                    inventory.total_bytes,
                    inventory_json,
                    request.occurred_at,
                ),
            )
            return
        if (
            row["schema_version"] != inventory.schema_version
            or row["source_commit"] != inventory.source_commit
            or row["source_tree"] != inventory.source_tree
            or row["source_parent"] != inventory.source_parent
            or row["path_count"] != inventory.path_count
            or row["total_bytes"] != inventory.total_bytes
            or row["inventory_json"] != inventory_json
        ):
            raise Phase9RunGenerationConflict(
                "recorded source inventory identity differs"
            )

    @staticmethod
    def _consume_authorization(
        connection: sqlite3.Connection,
        request: RunGenerationRequestV1,
    ) -> str:
        body = _authorization_consumption_body(request)
        receipt_json = canonical_bytes(body).decode("utf-8")
        receipt_sha256 = canonical_sha256(body)
        authorization = request.operator_authorization
        conflict = connection.execute(
            """
            SELECT 1
            FROM authority_production_run_generation_authorization_consumptions
            WHERE authorization_id=? OR authorization_receipt_sha256=?
               OR authorization_target_sha256=? OR request_sha256=?
               OR run_generation=? OR receipt_sha256=?
            LIMIT 1
            """,
            (
                authorization.authorization_id,
                authorization.receipt_sha256,
                request.authorization_target_sha256,
                request.request_sha256,
                request.derived_run_generation,
                receipt_sha256,
            ),
        ).fetchone()
        if conflict is not None:
            raise Phase9RunGenerationConflict(
                "operator authorization was already consumed"
            )
        connection.execute(
            """
            INSERT INTO authority_production_run_generation_authorization_consumptions(
                authorization_id, authorization_receipt_sha256,
                authorization_target_sha256, request_sha256, run_generation,
                workflow_id, consumed_at, receipt_json, receipt_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                authorization.authorization_id,
                authorization.receipt_sha256,
                request.authorization_target_sha256,
                request.request_sha256,
                request.derived_run_generation,
                request.workflow_id,
                request.occurred_at,
                receipt_json,
                receipt_sha256,
            ),
        )
        return receipt_sha256

    @staticmethod
    def _replay(
        connection: sqlite3.Connection,
        request: RunGenerationRequestV1,
    ) -> RunGenerationCreationResult | None:
        row = connection.execute(
            """
            SELECT * FROM authority_production_run_generation_idempotency
            WHERE workflow_id=? AND idempotency_key=?
            """,
            (request.workflow_id, request.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request.request_sha256:
            raise Phase9RunGenerationConflict(
                "run-generation idempotency key has different request bytes"
            )
        receipt = connection.execute(
            """
            SELECT * FROM authority_production_run_generation_creation_receipts
            WHERE run_generation=? AND receipt_sha256=?
            """,
            (row["run_generation"], row["creation_receipt_sha256"]),
        ).fetchone()
        consumption = connection.execute(
            """
            SELECT *
            FROM authority_production_run_generation_authorization_consumptions
            WHERE run_generation=? AND request_sha256=?
            """,
            (request.derived_run_generation, request.request_sha256),
        ).fetchone()
        expected_consumption = _authorization_consumption_body(request)
        expected_consumption_json = canonical_bytes(expected_consumption).decode(
            "utf-8"
        )
        expected_consumption_sha256 = canonical_sha256(expected_consumption)
        expected_body = _receipt_body(request)
        expected_bytes = canonical_bytes(expected_body).decode("utf-8")
        expected_sha = canonical_sha256(expected_body)
        if (
            receipt is None
            or consumption is None
            or row["run_generation"] != request.derived_run_generation
            or receipt["request_sha256"] != request.request_sha256
            or receipt["receipt_json"] != expected_bytes
            or receipt["receipt_sha256"] != expected_sha
            or consumption["authorization_id"]
            != request.operator_authorization.authorization_id
            or consumption["authorization_receipt_sha256"]
            != request.operator_authorization.receipt_sha256
            or consumption["authorization_target_sha256"]
            != request.authorization_target_sha256
            or consumption["receipt_json"] != expected_consumption_json
            or consumption["receipt_sha256"] != expected_consumption_sha256
        ):
            raise Phase9RunGenerationConflict(
                "run-generation replay receipt identity differs"
            )
        return _result(
            request,
            receipt_id=str(receipt["receipt_id"]),
            receipt_sha256=expected_sha,
            replayed=True,
        )

    @staticmethod
    def _verify_coordinate(
        connection: sqlite3.Connection,
        request: RunGenerationRequestV1,
    ) -> sqlite3.Row:
        workflow = connection.execute(
            "SELECT * FROM authority_workflows WHERE workflow_id=?",
            (request.workflow_id,),
        ).fetchone()
        project = connection.execute(
            """
            SELECT project_id, runtime_generation, scheduler_generation, revision
            FROM project_state WHERE singleton=1
            """
        ).fetchone()
        if workflow is None or project is None:
            raise Phase9RunGenerationConflict("project/workflow coordinate is missing")
        if (
            workflow["project_id"] != request.project_id
            or project["project_id"] != request.project_id
            or workflow["current_revision_availability"] != "RECORDED"
            or workflow["current_revision"] != request.project_revision
            or project["revision"] != request.project_revision
            or workflow["runtime_generation"] != request.runtime_generation
            or project["runtime_generation"] != request.runtime_generation
            or workflow["scheduler_generation"] != request.scheduler_generation
            or project["scheduler_generation"] != request.scheduler_generation
        ):
            raise Phase9RunGenerationConflict(
                "current project/workflow/revision/runtime/scheduler coordinate differs"
            )
        for name in ("runtime_generation", "scheduler_generation"):
            if workflow[name] == LEGACY_UNKNOWN:
                raise Phase9RunGenerationSafetyError(
                    f"current {name} must be concrete"
                )
        return workflow

    @staticmethod
    def _verify_predecessor(
        connection: sqlite3.Connection,
        request: RunGenerationRequestV1,
        workflow: sqlite3.Row,
    ) -> dict[str, object] | None:
        current = connection.execute(
            "SELECT * FROM authority_production_run_generation_current "
            "WHERE workflow_id=?",
            (request.workflow_id,),
        ).fetchone()
        if request.operation_kind == CREATE:
            if current is not None or workflow["run_generation"] != LEGACY_UNKNOWN:
                raise Phase9RunGenerationConflict(
                    "CREATE requires no current generation and legacy_unknown run binding"
                )
            if workflow["project_generation"] not in {
                LEGACY_UNKNOWN,
                request.project_generation,
            }:
                raise Phase9RunGenerationConflict(
                    "CREATE project generation conflicts with current workflow"
                )
            return None
        predecessor = request.predecessor_run_generation
        predecessor_receipt = request.predecessor_creation_receipt_sha256
        predecessor_terminal = request.predecessor_terminal_receipt_sha256
        if (
            current is None
            or current["run_generation"] != predecessor
            or current["creation_receipt_sha256"] != predecessor_receipt
            or workflow["run_generation"] != predecessor
            or workflow["project_generation"] != request.project_generation
        ):
            raise Phase9RunGenerationConflict(
                "ROTATE predecessor/current coordinate differs"
            )
        receipt = connection.execute(
            """
            SELECT receipt_sha256
            FROM authority_production_run_generation_creation_receipts
            WHERE run_generation=?
            """,
            (predecessor,),
        ).fetchone()
        if receipt is None or receipt["receipt_sha256"] != predecessor_receipt:
            raise Phase9RunGenerationConflict(
                "ROTATE requires a concrete predecessor creation receipt"
            )
        try:
            # Local import avoids a module-initialization cycle: forensic replay
            # imports the typed run-generation request contracts from this module.
            from .phase9_forensic_replay import (
                Phase9ForensicReplayError,
                validate_current_phase9_completed_replay_in_transaction,
            )

            return validate_current_phase9_completed_replay_in_transaction(
                connection,
                workflow_id=request.workflow_id,
                expected_run_generation=str(predecessor),
                expected_terminal_receipt_sha256=str(predecessor_terminal),
            )
        except Phase9ForensicReplayError as exc:
            raise Phase9RunGenerationConflict(
                "ROTATE requires the current predecessor terminal receipt and "
                "a strictly valid completed graph"
            ) from exc

    def create_or_rotate(
        self, request: RunGenerationRequestV1
    ) -> RunGenerationCreationResult:
        value = validate_run_generation_request(
            request, trusted_now=self._trusted_now()
        )
        official_snapshot = verify_official_input_snapshot(
            self.official_input_root, value.official_inputs
        )
        execution_context_bytes = _verified_execution_context_receipt(
            self.execution_context_receipt_path, value.execution_context
        )
        connection = connect_authority_rw(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_database(connection)
            self._control_fence(connection)
            source_snapshot = read_verified_execution_source_snapshot(
                self.source_repository,
                execution_root=self.execution_root,
            )
            if (
                source_snapshot.source != value.source
                or source_snapshot.source_inventory_sha256
                != value.source_inventory_sha256
            ):
                raise Phase9RunGenerationConflict(
                    "request source identity or tracked inventory is not current"
                )
            replay = self._replay(connection, value)
            if replay is not None:
                connection.commit()
                return replay
            workflow = self._verify_coordinate(connection, value)
            predecessor_graph = self._verify_predecessor(
                connection, value, workflow
            )
            pin_sha256 = self._persist_pin(connection, value)
            self._fault("after_contract_pin")
            self._persist_source_inventory(
                connection, value, source_snapshot.tracked_inventory
            )
            self._fault("after_source_inventory")

            connection.execute(
                """
                INSERT INTO authority_production_run_generations(
                    run_generation, workflow_id, project_id, project_revision,
                    project_generation, runtime_generation, scheduler_generation,
                    predecessor_run_generation,
                    predecessor_creation_receipt_sha256,
                    predecessor_terminal_receipt_sha256,
                    operation_kind, run_mode,
                    modeling_consultation_contract, delivery_capability,
                    source_commit, source_tree, source_parent,
                    source_inventory_sha256, contract_pin_set_sha256,
                    official_input_manifest_sha256,
                    official_input_raw_bytes_set_sha256,
                    execution_context_receipt_sha256,
                    operator_authorization_receipt_sha256, authorization_id,
                    authorization_target_sha256, request_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.derived_run_generation,
                    value.workflow_id,
                    value.project_id,
                    value.project_revision,
                    value.project_generation,
                    value.runtime_generation,
                    value.scheduler_generation,
                    value.predecessor_run_generation,
                    value.predecessor_creation_receipt_sha256,
                    value.predecessor_terminal_receipt_sha256,
                    value.operation_kind,
                    value.run_mode,
                    value.modeling_consultation_contract,
                    value.delivery_capability,
                    value.source.source_commit,
                    value.source.source_tree,
                    value.source.source_parent,
                    value.source_inventory_sha256,
                    pin_sha256,
                    value.official_inputs.manifest_sha256,
                    value.official_inputs.raw_bytes_set_sha256,
                    value.execution_context.receipt_sha256,
                    value.operator_authorization.receipt_sha256,
                    value.operator_authorization.authorization_id,
                    value.authorization_target_sha256,
                    value.request_sha256,
                    value.occurred_at,
                ),
            )
            self._fault("after_generation")

            succession_body = {
                "schema": "authority-phase9-run-generation-succession-v1",
                "workflow_id": value.workflow_id,
                "run_generation": value.derived_run_generation,
                "predecessor_run_generation": value.predecessor_run_generation,
                "predecessor_creation_receipt_sha256": (
                    value.predecessor_creation_receipt_sha256
                ),
                "predecessor_terminal_receipt_sha256": (
                    value.predecessor_terminal_receipt_sha256
                ),
                "request_sha256": value.request_sha256,
            }
            connection.execute(
                """
                INSERT INTO authority_production_run_generation_successions(
                    run_generation, workflow_id, predecessor_run_generation,
                    predecessor_creation_receipt_sha256,
                    predecessor_terminal_receipt_sha256, succession_json,
                    succession_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.derived_run_generation,
                    value.workflow_id,
                    value.predecessor_run_generation,
                    value.predecessor_creation_receipt_sha256,
                    value.predecessor_terminal_receipt_sha256,
                    canonical_bytes(succession_body).decode("utf-8"),
                    canonical_sha256(succession_body),
                ),
            )

            self._consume_authorization(connection, value)
            self._fault("after_authorization_consumption")

            receipt_body = _receipt_body(value)
            receipt_json = canonical_bytes(receipt_body).decode("utf-8")
            receipt_sha256 = canonical_sha256(receipt_body)
            receipt_id = f"run-generation-receipt:{receipt_sha256[:32]}"
            connection.execute(
                """
                INSERT INTO authority_production_run_generation_creation_receipts(
                    receipt_id, run_generation, workflow_id, operation_kind,
                    request_sha256, occurred_at, receipt_json, receipt_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    value.derived_run_generation,
                    value.workflow_id,
                    value.operation_kind,
                    value.request_sha256,
                    value.occurred_at,
                    receipt_json,
                    receipt_sha256,
                ),
            )
            connection.execute(
                """
                INSERT INTO authority_production_run_generation_idempotency(
                    workflow_id, idempotency_key, request_sha256,
                    run_generation, creation_receipt_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    value.workflow_id,
                    value.idempotency_key,
                    value.request_sha256,
                    value.derived_run_generation,
                    receipt_sha256,
                ),
            )
            self._fault("after_receipt")

            if value.operation_kind == CREATE:
                connection.execute(
                    """
                    INSERT INTO authority_production_run_generation_current(
                        workflow_id, run_generation, creation_receipt_sha256,
                        updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        value.workflow_id,
                        value.derived_run_generation,
                        receipt_sha256,
                        value.occurred_at,
                    ),
                )
            else:
                assert predecessor_graph is not None
                updated = connection.execute(
                    """
                    UPDATE authority_production_run_generation_current
                    SET run_generation=?, creation_receipt_sha256=?, updated_at=?
                    WHERE workflow_id=? AND run_generation=?
                      AND creation_receipt_sha256=?
                      AND EXISTS (
                          SELECT 1
                          FROM authority_production_phase9_replay_current replay_current
                          JOIN authority_workflows workflow
                            ON workflow.workflow_id=replay_current.workflow_id
                          JOIN authority_production_run_generations predecessor_generation
                            ON predecessor_generation.workflow_id=replay_current.workflow_id
                           AND predecessor_generation.run_generation=replay_current.run_generation
                          WHERE replay_current.workflow_id=
                                authority_production_run_generation_current.workflow_id
                            AND replay_current.replay_id=?
                            AND replay_current.run_generation=
                                authority_production_run_generation_current.run_generation
                            AND replay_current.terminal_receipt_sha256=?
                            AND replay_current.final_event_sha256=?
                            AND replay_current.state='COMPLETED'
                            AND workflow.current_revision=?
                            AND workflow.run_generation=
                                authority_production_run_generation_current.run_generation
                            AND workflow.project_generation=?
                            AND predecessor_generation.project_id=?
                            AND predecessor_generation.project_revision=?
                            AND predecessor_generation.project_generation=?
                            AND predecessor_generation.runtime_generation=?
                            AND predecessor_generation.scheduler_generation=?
                            AND predecessor_generation.run_mode='FORENSIC_REPLAY'
                            AND predecessor_generation.modeling_consultation_contract=
                                'LEGACY_NOT_APPLICABLE'
                            AND predecessor_generation.delivery_capability='DISABLED'
                            AND predecessor_generation.source_commit=?
                            AND predecessor_generation.source_tree=?
                            AND predecessor_generation.source_parent=?
                            AND predecessor_generation.source_inventory_sha256=?
                      )
                    """,
                    (
                        value.derived_run_generation,
                        receipt_sha256,
                        value.occurred_at,
                        value.workflow_id,
                        value.predecessor_run_generation,
                        value.predecessor_creation_receipt_sha256,
                        predecessor_graph["replay_id"],
                        value.predecessor_terminal_receipt_sha256,
                        predecessor_graph["final_event_sha256"],
                        value.project_revision,
                        workflow["project_generation"],
                        value.project_id,
                        value.project_revision,
                        workflow["project_generation"],
                        value.runtime_generation,
                        value.scheduler_generation,
                        value.source.source_commit,
                        value.source.source_tree,
                        value.source.source_parent,
                        value.source_inventory_sha256,
                    ),
                )
                if updated.rowcount != 1:
                    raise Phase9RunGenerationConflict(
                        "ROTATE current pointer CAS is stale"
                    )
            updated = connection.execute(
                """
                UPDATE authority_workflows
                SET project_generation=?, run_generation=?,
                    contract_pin_set_sha256=?, contract_pin_availability='RECORDED',
                    authority_state='RECORDED_SHADOW'
                WHERE workflow_id=? AND current_revision=?
                  AND runtime_generation=? AND scheduler_generation=?
                  AND run_generation=? AND project_generation=?
                """,
                (
                    value.project_generation,
                    value.derived_run_generation,
                    pin_sha256,
                    value.workflow_id,
                    value.project_revision,
                    value.runtime_generation,
                    value.scheduler_generation,
                    workflow["run_generation"],
                    workflow["project_generation"],
                ),
            )
            if updated.rowcount != 1:
                raise Phase9RunGenerationConflict("workflow coordinate CAS is stale")
            self._fault("after_current_pointer")

            current_source_snapshot = read_verified_execution_source_snapshot(
                self.source_repository,
                execution_root=self.execution_root,
            )
            if (
                current_source_snapshot != source_snapshot
                or current_source_snapshot.source != value.source
                or current_source_snapshot.source_inventory_sha256
                != value.source_inventory_sha256
            ):
                raise Phase9RunGenerationConflict(
                    "current source identity or tracked inventory changed during "
                    "run-generation transaction"
                )
            if verify_official_input_snapshot(
                self.official_input_root, value.official_inputs
            ) != official_snapshot:
                raise Phase9RunGenerationConflict(
                    "official input bytes changed during run-generation transaction"
                )
            if _verified_execution_context_receipt(
                self.execution_context_receipt_path, value.execution_context
            ) != execution_context_bytes:
                raise Phase9RunGenerationConflict(
                    "execution context receipt changed during run-generation transaction"
                )
            self._control_fence(connection)
            validate_run_generation_request(
                value, trusted_now=self._trusted_now()
            )
            self._fault("before_commit")
            connection.commit()
            return _result(
                value,
                receipt_id=receipt_id,
                receipt_sha256=receipt_sha256,
                replayed=False,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
