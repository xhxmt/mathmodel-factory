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
from typing import Callable, Mapping

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


RUN_GENERATION_REQUEST_SCHEMA = "authority-phase9-run-generation-request-v1"
RUN_GENERATION_RECEIPT_SCHEMA = (
    "authority-phase9-run-generation-creation-receipt-v1"
)
GIT_SOURCE_IDENTITY_SCHEMA = "authority-phase9-git-source-identity-v1"
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
    "authority-phase9-operator-authorization-evidence-v1"
)

CREATE = "CREATE"
ROTATE = "ROTATE"
DELIVERY_DISABLED = "DISABLED"
V1_ONLY = "V1_ONLY"
LEGACY_UNKNOWN = "legacy_unknown"

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
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "authorization_statement_sha256": (
                self.authorization_statement_sha256
            ),
        }

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


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
    run_mode: str
    modeling_consultation_contract: str
    delivery_capability: str
    source: GitSourceIdentityV1
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
            "run_mode": self.run_mode,
            "modeling_consultation_contract": (
                self.modeling_consultation_contract
            ),
            "delivery_capability": self.delivery_capability,
            "source": self.source.as_dict(),
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
    def derived_project_generation(self) -> str:
        digest = canonical_sha256(
            {
                "schema": "authority-phase9-project-generation-v1",
                "project_id": self.project_id,
                "workflow_id": self.workflow_id,
                "source": self.source.as_dict(),
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
        return f"run-generation:{self.request_sha256}"


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
    for index, item in enumerate(value.files):
        path = f"official_inputs.files[{index}]"
        _exact(item, OfficialInputFileEvidenceV1, path)
        if item.schema_version != OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA:
            raise Phase9RunGenerationSafetyError(
                f"{path} evidence schema is unsupported"
            )
        logical = _text(item.logical_path, f"{path}.logical_path")
        pure = PurePosixPath(logical)
        if pure.is_absolute() or logical != pure.as_posix() or ".." in pure.parts:
            raise Phase9RunGenerationSafetyError(
                f"{path}.logical_path must be a normalized relative POSIX path"
            )
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
    issued = _nonnegative(value.issued_at, "operator_authorization.issued_at")
    expires = _nonnegative(value.expires_at, "operator_authorization.expires_at")
    if expires < issued or not issued <= request.occurred_at <= expires:
        raise Phase9RunGenerationSafetyError(
            "operator authorization is not valid at request occurrence"
        )
    if (
        value.operation_kind != request.operation_kind
        or value.project_id != request.project_id
        or value.workflow_id != request.workflow_id
        or value.source_commit != request.source.source_commit
    ):
        raise Phase9RunGenerationSafetyError(
            "operator authorization coordinate differs from request"
        )
    return value


def validate_run_generation_request(
    value: RunGenerationRequestV1,
) -> RunGenerationRequestV1:
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
    _text(value.run_mode, "request.run_mode", identifier=True)
    _text(
        value.modeling_consultation_contract,
        "request.modeling_consultation_contract",
        identifier=True,
    )
    if value.delivery_capability != DELIVERY_DISABLED:
        raise Phase9RunGenerationSafetyError(
            "Phase9 run-generation delivery capability must remain DISABLED"
        )
    if value.operation_kind == CREATE:
        if (
            value.predecessor_run_generation is not None
            or value.predecessor_creation_receipt_sha256 is not None
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
    validate_git_source_identity(value.source)
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
    if value.execution_context.captured_at > value.occurred_at:
        raise Phase9RunGenerationSafetyError(
            "execution context was captured after request occurrence"
        )
    validate_operator_authorization(value.operator_authorization, value)
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


def run_generation_request_from_dict(value: object) -> RunGenerationRequestV1:
    """Decode one strict JSON-domain request into the typed creation API."""

    item = _exact_mapping(
        value,
        "request",
        {
            "schema_version", "idempotency_key", "operation_kind", "project_id",
            "workflow_id", "project_revision", "project_generation",
            "runtime_generation", "scheduler_generation",
            "predecessor_run_generation", "predecessor_creation_receipt_sha256",
            "run_mode", "modeling_consultation_contract", "delivery_capability",
            "source", "contract_pins", "official_inputs", "execution_context",
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
            "source_commit", "issued_at", "expires_at",
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
        run_mode=item["run_mode"],
        modeling_consultation_contract=item["modeling_consultation_contract"],
        delivery_capability=item["delivery_capability"],
        source=GitSourceIdentityV1(**source),
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
    return validate_run_generation_request(decoded)


def read_current_git_source_identity(repository: str | Path) -> GitSourceIdentityV1:
    """Read the exact current commit/tree/single parent from an explicit repo."""

    root = Path(repository)
    try:
        metadata = root.lstat()
    except FileNotFoundError as exc:
        raise Phase9RunGenerationSafetyError("source repository is missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise Phase9RunGenerationSafetyError(
            "source repository must be a non-symlink directory"
        )
    resolved = root.resolve()
    command = (
        "git", "-c", "core.hooksPath=/dev/null", "-c", "pager.branch=false",
        "-c", "pager.log=false", "rev-list", "--parents", "-n", "1", "HEAD",
    )
    try:
        lineage = subprocess.run(
            command,
            cwd=resolved,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
        ).stdout.decode("ascii", errors="strict").strip().split()
        tree = subprocess.run(
            (*command[:7], "rev-parse", "HEAD^{tree}"),
            cwd=resolved,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
        ).stdout.decode("ascii", errors="strict").strip()
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise Phase9RunGenerationSafetyError(
            "current Git source identity could not be read"
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
    except OSError as exc:
        raise Phase9RunGenerationSafetyError(f"{label} cannot be read safely") from exc
    identity = lambda value: (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
        value.st_size, value.st_mtime_ns,
    )
    if (
        len(raw) > maximum_bytes
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or identity(before) != identity(opened)
        or identity(opened) != identity(after)
    ):
        raise Phase9RunGenerationSafetyError(f"{label} changed while being read")
    return raw


def _verified_official_input_snapshot(
    root_value: str | Path,
    evidence: OfficialInputManifestEvidenceV1,
) -> tuple[tuple[str, int, str], ...]:
    validate_official_inputs(evidence)
    root = Path(root_value)
    try:
        root_before = root.lstat()
    except OSError as exc:
        raise Phase9RunGenerationSafetyError("official input root is unavailable") from exc
    if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
        raise Phase9RunGenerationSafetyError(
            "official input root must be a non-symlink directory"
        )
    root = root.resolve()
    actual_paths: list[str] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in directory_names:
            metadata = (current / name).lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise Phase9RunGenerationSafetyError(
                    "official input tree contains a symlink or special directory"
                )
        for name in file_names:
            path = current / name
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise Phase9RunGenerationSafetyError(
                    "official input tree contains a symlink, hardlink, or special file"
                )
            actual_paths.append(path.relative_to(root).as_posix())
    expected_paths = [item.logical_path for item in evidence.files]
    if sorted(actual_paths) != expected_paths:
        raise Phase9RunGenerationSafetyError(
            "official input root file inventory differs from manifest"
        )
    snapshot: list[tuple[str, int, str]] = []
    for item in evidence.files:
        relative = PurePosixPath(item.logical_path)
        parent = root
        for part in relative.parts[:-1]:
            parent /= part
            metadata = parent.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise Phase9RunGenerationSafetyError(
                    "official input path traverses a symlink or non-directory"
                )
        raw = _regular_file_bytes(
            root.joinpath(*relative.parts),
            maximum_bytes=item.byte_length,
            label=f"official input {item.logical_path}",
        )
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != item.byte_length or digest != item.raw_bytes_sha256:
            raise Phase9RunGenerationSafetyError(
                f"official input bytes differ: {item.logical_path}"
            )
        snapshot.append((item.logical_path, len(raw), digest))
    root_after = root.lstat()
    if (
        root_before.st_dev,
        root_before.st_ino,
        root_before.st_mtime_ns,
    ) != (root_after.st_dev, root_after.st_ino, root_after.st_mtime_ns):
        raise Phase9RunGenerationSafetyError(
            "official input root changed while being verified"
        )
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
        "occurred_at": request.occurred_at,
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
        fault_hook: Callable[[str], None] | None = None,
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
        if fault_hook is not None and not callable(fault_hook):
            raise Phase9RunGenerationSafetyError("fault_hook must be callable")
        self._fault_hook = fault_hook

    def _fault(self, checkpoint: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(checkpoint)

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
        expected_body = _receipt_body(request)
        expected_bytes = canonical_bytes(expected_body).decode("utf-8")
        expected_sha = canonical_sha256(expected_body)
        if (
            receipt is None
            or row["run_generation"] != request.derived_run_generation
            or receipt["request_sha256"] != request.request_sha256
            or receipt["receipt_json"] != expected_bytes
            or receipt["receipt_sha256"] != expected_sha
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
    ) -> None:
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
            return
        predecessor = request.predecessor_run_generation
        predecessor_receipt = request.predecessor_creation_receipt_sha256
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

    def create_or_rotate(
        self, request: RunGenerationRequestV1
    ) -> RunGenerationCreationResult:
        value = validate_run_generation_request(request)
        official_snapshot = _verified_official_input_snapshot(
            self.official_input_root, value.official_inputs
        )
        execution_context_bytes = _verified_execution_context_receipt(
            self.execution_context_receipt_path, value.execution_context
        )
        current_source = read_current_git_source_identity(self.source_repository)
        if current_source != value.source:
            raise Phase9RunGenerationConflict(
                "request source commit/tree/parent is not current"
            )
        connection = connect_authority_rw(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_database(connection)
            self._control_fence(connection)
            replay = self._replay(connection, value)
            if replay is not None:
                connection.commit()
                return replay
            workflow = self._verify_coordinate(connection, value)
            self._verify_predecessor(connection, value, workflow)
            pin_sha256 = self._persist_pin(connection, value)
            self._fault("after_contract_pin")

            connection.execute(
                """
                INSERT INTO authority_production_run_generations(
                    run_generation, workflow_id, project_id, project_revision,
                    project_generation, runtime_generation, scheduler_generation,
                    predecessor_run_generation,
                    predecessor_creation_receipt_sha256,
                    operation_kind, run_mode,
                    modeling_consultation_contract, delivery_capability,
                    source_commit, source_tree, source_parent,
                    contract_pin_set_sha256, official_input_manifest_sha256,
                    official_input_raw_bytes_set_sha256,
                    execution_context_receipt_sha256,
                    operator_authorization_receipt_sha256, request_sha256,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DISABLED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    value.operation_kind,
                    value.run_mode,
                    value.modeling_consultation_contract,
                    value.source.source_commit,
                    value.source.source_tree,
                    value.source.source_parent,
                    pin_sha256,
                    value.official_inputs.manifest_sha256,
                    value.official_inputs.raw_bytes_set_sha256,
                    value.execution_context.receipt_sha256,
                    value.operator_authorization.receipt_sha256,
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
                "request_sha256": value.request_sha256,
            }
            connection.execute(
                """
                INSERT INTO authority_production_run_generation_successions(
                    run_generation, workflow_id, predecessor_run_generation,
                    predecessor_creation_receipt_sha256, succession_json,
                    succession_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    value.derived_run_generation,
                    value.workflow_id,
                    value.predecessor_run_generation,
                    value.predecessor_creation_receipt_sha256,
                    canonical_bytes(succession_body).decode("utf-8"),
                    canonical_sha256(succession_body),
                ),
            )

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
                updated = connection.execute(
                    """
                    UPDATE authority_production_run_generation_current
                    SET run_generation=?, creation_receipt_sha256=?, updated_at=?
                    WHERE workflow_id=? AND run_generation=?
                      AND creation_receipt_sha256=?
                    """,
                    (
                        value.derived_run_generation,
                        receipt_sha256,
                        value.occurred_at,
                        value.workflow_id,
                        value.predecessor_run_generation,
                        value.predecessor_creation_receipt_sha256,
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
                """,
                (
                    value.project_generation,
                    value.derived_run_generation,
                    pin_sha256,
                    value.workflow_id,
                    value.project_revision,
                    value.runtime_generation,
                    value.scheduler_generation,
                ),
            )
            if updated.rowcount != 1:
                raise Phase9RunGenerationConflict("workflow coordinate CAS is stale")
            self._fault("after_current_pointer")

            if read_current_git_source_identity(self.source_repository) != value.source:
                raise Phase9RunGenerationConflict(
                    "current source changed during run-generation transaction"
                )
            if _verified_official_input_snapshot(
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
