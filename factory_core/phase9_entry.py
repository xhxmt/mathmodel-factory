"""Read-only, candidate-bound Phase-9 entry collection and verification.

This module deliberately has no migration, writer, process-launch, network,
provider, outbox-dispatch, delivery, or release capability.  It opens only
caller-selected SQLite files in read-only/query-only mode and turns every
missing or unverifiable fact into a named blocker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import sqlite3
import stat
import subprocess
from typing import Iterable, Mapping, Sequence
import unicodedata

from .authority_production_schema import (
    AUTHORITY_PRODUCTION_SCHEMA_VERSION,
    PRODUCTION_MIGRATIONS,
    connect_authority_ro,
    verify_production_installation,
)
from .canonical import canonical_bytes, canonical_sha256
from .phase9_p0_evidence import (
    PHASE9_P0_COMMAND_RECORD_SCHEMA,
    PHASE9_P0_AUTHORITY_RUNNER_EVIDENCE_SCHEMA,
    PHASE9_P0_EVIDENCE_ROOT_SCHEMA,
    PHASE9_P0_EXECUTION_CONTEXT_BINDING_SCHEMA,
    PHASE9_P0_FORMAL_DOMAIN,
    PHASE9_P0_PRODUCER_SOURCE_PATH,
    PHASE9_P0_PRODUCER_TYPE,
    PHASE9_P0_PRODUCER_VERSION,
    PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH,
    PHASE9_P0_RECEIPT_SCHEMA,
    PHASE9_P0_RUNNER_ATTESTATION_SCHEMA,
    PHASE9_P0_RUNNER_AUTHORIZATION_SCHEMA,
    PHASE9_P0_RUNNER_AUTHORIZATION_TTL_SECONDS,
    P0_REQUIREMENTS,
    TRUSTED_PYTEST_EVENT_SCHEMA,
    Phase9P0EvidenceError,
    formal_p0_paths,
    phase9_p0_spec_sha256,
    validate_formal_phase9_p0_evidence,
)
from .phase9_run_generation import (
    GIT_TRACKED_SOURCE_ENTRY_SCHEMA,
    GIT_TRACKED_SOURCE_INVENTORY_SCHEMA,
    OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA,
    OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA,
    OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA,
    PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS,
    RUN_GENERATION_AUTHORIZATION_CONSUMPTION_SCHEMA,
    RUN_GENERATION_RECEIPT_SCHEMA,
    GitTrackedSourceEntryV1,
    GitTrackedSourceInventoryV1,
    OfficialInputFileEvidenceV1,
    OfficialInputManifestEvidenceV1,
    Phase9RunGenerationSafetyError,
    _StableDirectoryTree,
    read_current_git_source_snapshot,
    run_generation_request_from_dict,
    verify_official_input_snapshot,
)


PHASE9_ENTRY_STATE_SCHEMA = "phase9-entry-state-receipt-v2"
PHASE9_ENTRY_GATE_SCHEMA = "phase9-entry-gate-result-v3"
PHASE9_OPERATOR_AUTHORIZATION_SCHEMA = (
    "phase9-operator-authorization-receipt-v2"
)
PHASE9_OFFICIAL_INPUT_MANIFEST_SCHEMA = (
    "authority-phase9-official-input-manifest-evidence-v1"
)
PHASE9_OFFICIAL_INPUT_FILE_SCHEMA = (
    "authority-phase9-official-input-file-evidence-v1"
)
PHASE9_EXECUTION_CONTEXT_SCHEMA = (
    "authority-phase9-execution-context-evidence-v1"
)

_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_P0_RUNNER_CAPABILITIES = {
    "network_access": False,
    "provider_call": False,
    "outbox_dispatch": False,
    "delivery": False,
    "release": False,
    "migration": False,
    "deployment": False,
    "cutover": False,
}
_ACTIVE_PROJECT_STATUS = frozenset({"running", "retrying"})
_ACTIVE_SOLVER_STATUS = frozenset(
    {"submitting", "submitted", "running", "SUBMITTING", "SUBMITTED", "RUNNING"}
)
_P0_MAX_FILE_BYTES = 64 * 1024 * 1024
_P0_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_P0_MAX_MEMBERS = 4096


class Phase9EntryError(RuntimeError):
    """A supplied entry fact is malformed or cannot be proved read-only."""


def _plain_text(value: object, path: str, *, identifier: bool = False) -> str:
    if type(value) is not str or not value:
        raise Phase9EntryError(f"{path} must be a non-empty plain string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise Phase9EntryError(f"{path} must be valid UTF-8") from exc
    if identifier and _IDENTIFIER.fullmatch(value) is None:
        raise Phase9EntryError(f"{path} must be a bounded identifier")
    return value


def _sha(value: object, path: str) -> str:
    text = _plain_text(value, path)
    if _SHA256.fullmatch(text) is None:
        raise Phase9EntryError(f"{path} must be lowercase SHA-256")
    return text


def _git_sha(value: object, path: str) -> str:
    text = _plain_text(value, path)
    if _GIT_SHA.fullmatch(text) is None:
        raise Phase9EntryError(f"{path} must be a lowercase Git object id")
    return text


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise Phase9EntryError(f"{path} must be an integer >= {minimum}")
    return value


def _exact_mapping(
    value: object, path: str, required: Iterable[str], optional: Iterable[str] = ()
) -> Mapping[str, object]:
    if type(value) is not dict:
        raise Phase9EntryError(f"{path} must be a plain object")
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = required_set - set(value)
    extra = set(value) - allowed
    if missing or extra:
        raise Phase9EntryError(
            f"{path} keys differ: missing={sorted(missing)!r} extra={sorted(extra)!r}"
        )
    return value


def _validate_p0_runner_capabilities(value: object, path: str) -> None:
    item = _exact_mapping(value, path, set(_P0_RUNNER_CAPABILITIES))
    if any(
        type(item[name]) is not bool or item[name] is not expected
        for name, expected in _P0_RUNNER_CAPABILITIES.items()
    ):
        raise Phase9EntryError(
            f"{path} must contain the exact boolean capability fence"
        )


def _canonical_json(value: object, path: str) -> bytes:
    try:
        raw = canonical_bytes(value)
    except (TypeError, ValueError) as exc:
        raise Phase9EntryError(f"{path} is not canonical JSON data") from exc
    return raw


@dataclass(frozen=True)
class CandidateIdentity:
    commit: str
    tree: str
    parent: str

    def __post_init__(self) -> None:
        _git_sha(self.commit, "candidate.commit")
        _git_sha(self.tree, "candidate.tree")
        _git_sha(self.parent, "candidate.parent")
        if self.commit == self.parent:
            raise Phase9EntryError("candidate commit and parent must differ")

    def as_dict(self) -> dict[str, str]:
        return {
            "commit": self.commit,
            "tree": self.tree,
            "parent": self.parent,
        }

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def candidate_identity_from_dict(value: object) -> CandidateIdentity:
    item = _exact_mapping(value, "candidate", {"commit", "tree", "parent"})
    return CandidateIdentity(
        commit=_git_sha(item["commit"], "candidate.commit"),
        tree=_git_sha(item["tree"], "candidate.tree"),
        parent=_git_sha(item["parent"], "candidate.parent"),
    )


def _regular_file_bytes(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise Phase9EntryError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise Phase9EntryError(f"{label} must be one non-hardlinked regular file")
    if before.st_size > maximum_bytes:
        raise Phase9EntryError(f"{label} exceeds {maximum_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum_bytes + 1)
            after = os.fstat(stream.fileno())
        final = path.lstat()
    except OSError as exc:
        raise Phase9EntryError(f"{label} cannot be read safely: {path}") from exc
    identity = lambda value: (
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
    if (
        len(raw) > maximum_bytes
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or identity(before) != identity(opened)
        or identity(opened) != identity(after)
        or identity(after) != identity(final)
    ):
        raise Phase9EntryError(f"{label} changed while being read")
    return raw


def read_canonical_json_file(
    path: str | Path, *, maximum_bytes: int = 1024 * 1024, label: str = "JSON receipt"
) -> dict[str, object]:
    raw = _regular_file_bytes(Path(path), maximum_bytes=maximum_bytes, label=label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase9EntryError(f"{label} is not strict UTF-8 JSON") from exc
    if type(value) is not dict or raw != canonical_bytes(value):
        raise Phase9EntryError(f"{label} bytes are not canonical JSON")
    return value


def validate_p0_receipt(
    value: object,
    *,
    requirement: str,
    candidate: CandidateIdentity,
    evidence_files: Mapping[str, bytes],
) -> str:
    del value, requirement, candidate, evidence_files
    raise Phase9EntryError(
        "one P0 receipt cannot be validated outside its complete formal evidence set"
    )


def validate_operator_authorization(
    value: object,
    *,
    candidate: CandidateIdentity,
    project_id: str,
    workflow_id: str,
    run_generation: str,
    trusted_now: int,
    p0_evidence_root_sha256: str,
) -> str:
    item = _exact_mapping(
        value,
        "operator_authorization",
        {
            "schema",
            "candidate",
            "project_id",
            "workflow_id",
            "run_generation",
            "authorized_operation",
            "authorization_mechanism",
            "authorization_evidence_sha256",
            "p0_evidence_root_sha256",
            "operator_account",
            "operator_uid",
            "authorized",
            "issued_at",
            "expires_at",
            "receipt_sha256",
        },
    )
    if item["schema"] != PHASE9_OPERATOR_AUTHORIZATION_SCHEMA:
        raise Phase9EntryError("operator authorization schema differs")
    if candidate_identity_from_dict(item["candidate"]) != candidate:
        raise Phase9EntryError("operator authorization belongs to another candidate")
    if (
        item["project_id"] != project_id
        or item["workflow_id"] != workflow_id
        or item["run_generation"] != run_generation
        or item["authorized_operation"] != "PHASE9_ENTRY"
        or item["authorized"] is not True
    ):
        raise Phase9EntryError("operator authorization coordinate differs")
    if item["authorization_mechanism"] != "CONTROLLED_OS_ACCOUNT":
        raise Phase9EntryError("self-asserted operator labels are not authorization")
    uid = _integer(item["operator_uid"], "operator_authorization.operator_uid")
    account = _plain_text(
        item["operator_account"],
        "operator_authorization.operator_account",
        identifier=True,
    )
    try:
        actual_account = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError as exc:
        raise Phase9EntryError("current controlled OS account cannot be resolved") from exc
    if uid != os.geteuid() or account != actual_account:
        raise Phase9EntryError("authorization is not being evaluated by its controlled OS account")
    _sha(item["authorization_evidence_sha256"], "authorization evidence")
    if (
        _sha(
            item["p0_evidence_root_sha256"],
            "operator_authorization.p0_evidence_root_sha256",
        )
        != p0_evidence_root_sha256
    ):
        raise Phase9EntryError("operator authorization P0 evidence root differs")
    issued = _integer(
        item["issued_at"], "operator_authorization.issued_at", minimum=1
    )
    expires = _integer(
        item["expires_at"], "operator_authorization.expires_at", minimum=1
    )
    if expires < issued or not issued <= trusted_now <= expires:
        raise Phase9EntryError(
            "operator authorization is not valid at trusted current time"
        )
    body = dict(item)
    claimed = _sha(body.pop("receipt_sha256"), "operator authorization receipt")
    if canonical_sha256(body) != claimed:
        raise Phase9EntryError("operator authorization receipt hash differs")
    return claimed


def _validate_p0_receipt_set_details(
    receipts: Mapping[str, object],
    *,
    candidate: CandidateIdentity,
    project_id: str,
    workflow_id: str,
    run_generation: str,
    source_inventory_sha256: str,
    evidence_root: str | Path,
    evidence_root_sha256: str,
) -> object:
    if type(receipts) is not dict:
        raise Phase9EntryError("P0 receipts must be a plain object")
    if set(receipts) != set(P0_REQUIREMENTS):
        raise Phase9EntryError(
            "P0 receipt set must contain exactly all nine required receipts"
        )
    files, actual_root_sha256 = _strict_p0_evidence_tree(evidence_root)
    expected_root_sha256 = _sha(evidence_root_sha256, "p0_evidence_root_sha256")
    if actual_root_sha256 != expected_root_sha256:
        raise Phase9EntryError("P0 evidence root inventory/hash differs")
    if set(files) != set(formal_p0_paths()):
        raise Phase9EntryError("P0 evidence root is not the fixed formal inventory")
    try:
        return validate_formal_phase9_p0_evidence(
            receipts=receipts,
            files=files,
            candidate=candidate.as_dict(),
            coordinate={
                "project_id": project_id,
                "workflow_id": workflow_id,
                "run_generation": run_generation,
            },
            source_inventory_sha256=source_inventory_sha256,
        )
    except Phase9P0EvidenceError as exc:
        raise Phase9EntryError(str(exc)) from exc


def validate_p0_receipt_set(
    receipts: Mapping[str, object],
    *,
    candidate: CandidateIdentity,
    project_id: str,
    workflow_id: str,
    run_generation: str,
    source_inventory_sha256: str,
    evidence_root: str | Path,
    evidence_root_sha256: str,
) -> dict[str, str]:
    """Validate external bytes only; READY also requires a DB attestation."""

    result = _validate_p0_receipt_set_details(
        receipts,
        candidate=candidate,
        project_id=project_id,
        workflow_id=workflow_id,
        run_generation=run_generation,
        source_inventory_sha256=source_inventory_sha256,
        evidence_root=evidence_root,
        evidence_root_sha256=evidence_root_sha256,
    )
    return dict(result.receipt_sha256s)


def _safe_relative_path(value: object, path: str) -> str:
    text = _plain_text(value, path)
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or text != pure.as_posix()
        or "\\" in text
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(part.endswith((" ", ".")) for part in pure.parts)
        or any(ord(character) < 32 for character in text)
    ):
        raise Phase9EntryError(f"{path} is not a safe canonical relative path")
    return text


def _strict_p0_evidence_tree(
    root_value: str | Path,
) -> tuple[dict[str, bytes], str]:
    files: dict[str, bytes] = {}
    directories: list[str] = []
    collision_keys: set[str] = set()
    total_bytes = 0
    try:
        with _StableDirectoryTree(
            root_value, label="P0 evidence root"
        ) as tree:
            pending: list[tuple[str, ...]] = [()]
            while pending:
                parts = pending.pop()
                for name in tree.list_directory(parts):
                    relative = _safe_relative_path(
                        PurePosixPath(*parts, name).as_posix(),
                        "P0 evidence member path",
                    )
                    collision_key = unicodedata.normalize("NFC", relative).casefold()
                    if collision_key in collision_keys:
                        raise Phase9EntryError(
                            "P0 evidence paths collide by Unicode normalization or case"
                        )
                    collision_keys.add(collision_key)
                    metadata = tree.member_stat(parts, name)
                    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                        metadata.st_mode
                    ):
                        directories.append(relative)
                        tree.directory((*parts, name))
                        pending.append((*parts, name))
                    elif stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(
                        metadata.st_mode
                    ) and metadata.st_nlink == 1:
                        raw, _opened = tree.read_regular_file(
                            parts, name, maximum_bytes=_P0_MAX_FILE_BYTES
                        )
                        total_bytes += len(raw)
                        files[relative] = raw
                    else:
                        raise Phase9EntryError(
                            "P0 evidence tree contains a symlink, hardlink, or special file"
                        )
                    if len(files) + len(directories) >= _P0_MAX_MEMBERS:
                        raise Phase9EntryError("P0 evidence tree has too many members")
                    if total_bytes > _P0_MAX_TOTAL_BYTES:
                        raise Phase9EntryError("P0 evidence tree is too large")
            tree.verify_unchanged()
    except Phase9RunGenerationSafetyError as exc:
        raise Phase9EntryError(str(exc)) from exc

    expected_directories: set[str] = set()
    for relative in files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if set(directories) != expected_directories:
        raise Phase9EntryError("P0 evidence root contains an unexpected empty directory")
    inventory = {
        "schema": PHASE9_P0_EVIDENCE_ROOT_SCHEMA,
        "directories": sorted(directories, key=lambda value: value.encode("utf-8")),
        "files": [
            {
                "path": path,
                "byte_length": len(files[path]),
                "raw_bytes_sha256": hashlib.sha256(files[path]).hexdigest(),
            }
            for path in sorted(files, key=lambda value: value.encode("utf-8"))
        ],
    }
    return files, canonical_sha256(inventory)


def p0_evidence_root_sha256(root: str | Path) -> str:
    """Return the strict, complete inventory digest for a P0 evidence root."""

    return _strict_p0_evidence_tree(root)[1]


def _git_object_id(kind: str, raw: bytes) -> bytes:
    return hashlib.sha1(
        kind.encode("ascii") + b" " + str(len(raw)).encode("ascii") + b"\0" + raw,
        usedforsecurity=False,
    ).digest()


def _tree_id(entries: dict[str, object]) -> bytes:
    encoded: list[tuple[bytes, bytes]] = []
    for name, value in entries.items():
        name_bytes = name.encode("utf-8")
        if type(value) is dict:
            object_id = _tree_id(value)
            encoded.append((name_bytes + b"/", b"40000 " + name_bytes + b"\0" + object_id))
        else:
            mode, object_id = value
            encoded.append(
                (name_bytes, mode.encode("ascii") + b" " + name_bytes + b"\0" + object_id)
            )
    raw = b"".join(value for _, value in sorted(encoded, key=lambda item: item[0]))
    return _git_object_id("tree", raw)


def _verify_candidate_metadata(
    repository: Path,
    *,
    candidate: CandidateIdentity,
    inventory_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    manifest_path = repository / "MANIFEST.json"
    checksums_path = repository / "checksums" / "SHA256SUMS"
    manifest_raw = _regular_file_bytes(
        manifest_path,
        maximum_bytes=8 * 1024 * 1024,
        label="candidate MANIFEST.json",
    )
    try:
        manifest_value = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase9EntryError("candidate MANIFEST.json is not strict UTF-8 JSON") from exc
    expected_manifest_raw = (
        json.dumps(manifest_value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if type(manifest_value) is not dict or manifest_raw != expected_manifest_raw:
        raise Phase9EntryError("candidate MANIFEST.json bytes are not canonical JSON")
    manifest = _exact_mapping(
        manifest_value,
        "candidate_manifest",
        {
            "schema",
            "builder",
            "archive_root",
            "deterministic_timestamp",
            "inventory_sha256",
            "metadata",
            "closure",
            "files",
        },
    )
    if (
        manifest["schema"] != "paper-factory-full-shadow-candidate-manifest-v2"
        or manifest["builder"] != "paper-factory-deterministic-zip-v1"
        or manifest["deterministic_timestamp"] != "1980-01-01T00:00:00Z"
    ):
        raise Phase9EntryError("candidate MANIFEST.json schema/builder differs")
    archive_root = _safe_relative_path(
        manifest["archive_root"], "candidate_manifest.archive_root"
    )
    if "/" in archive_root:
        raise Phase9EntryError("candidate manifest archive root must be one component")
    metadata = _exact_mapping(
        manifest["metadata"],
        "candidate_manifest.metadata",
        {
            "authorization_scope",
            "candidate_commit",
            "candidate_parent",
            "candidate_tree",
            "freeze_utc",
            "purpose",
            "schema",
            "shadow_only",
        },
    )
    if (
        metadata["schema"] != "phase1-8-phase9-candidate-build-metadata-v1"
        or metadata["shadow_only"] is not True
        or metadata["candidate_commit"] != candidate.commit
        or metadata["candidate_tree"] != candidate.tree
        or metadata["candidate_parent"] != candidate.parent
    ):
        raise Phase9EntryError("candidate manifest identity metadata differs")
    authorization_scope = _exact_mapping(
        metadata["authorization_scope"],
        "candidate_manifest.metadata.authorization_scope",
        {
            "cutover",
            "delivery",
            "deployment",
            "migration",
            "phase9_a_forensic_replay",
            "production_outbox",
            "provider_or_network",
            "release",
        },
    )
    if any(value is not False for value in authorization_scope.values()):
        raise Phase9EntryError("candidate manifest claims forbidden authorization")

    expected_files = [
        {
            "archive_path": f"{archive_root}/{row['path']}",
            "mode": 0o755 if row["mode"] == "0755" else 0o644,
            "sha256": row["sha256"],
            "size": row["size"],
            "source_path": row["path"],
        }
        for row in inventory_rows
    ]
    if manifest["files"] != expected_files:
        raise Phase9EntryError("candidate manifest files differ from frozen inventory")
    paths_raw = "".join(f"{row['path']}\n" for row in inventory_rows).encode("utf-8")
    if manifest["inventory_sha256"] != hashlib.sha256(paths_raw).hexdigest():
        raise Phase9EntryError("candidate manifest inventory hash differs")
    closure = _exact_mapping(
        manifest["closure"],
        "candidate_manifest.closure",
        {"manifest", "checksums", "checksums_cover", "checksums_exclude"},
    )
    if closure != {
        "manifest": f"{archive_root}/MANIFEST.json",
        "checksums": f"{archive_root}/checksums/SHA256SUMS",
        "checksums_cover": "every payload member plus MANIFEST.json",
        "checksums_exclude": "checksums/SHA256SUMS (self-reference is forbidden)",
    }:
        raise Phase9EntryError("candidate manifest checksum closure differs")

    checksums_raw = _regular_file_bytes(
        checksums_path,
        maximum_bytes=8 * 1024 * 1024,
        label="candidate checksums/SHA256SUMS",
    )
    try:
        checksums_text = checksums_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Phase9EntryError("candidate checksums are not UTF-8") from exc
    if not checksums_text.endswith("\n") or "\r" in checksums_text:
        raise Phase9EntryError("candidate checksums are not canonical text")
    checksums: dict[str, str] = {}
    previous = ""
    for index, line in enumerate(checksums_text.splitlines(), start=1):
        digest, separator, member = line.partition("  ")
        member = _safe_relative_path(member, f"candidate checksums line {index}.path")
        if (
            separator != "  "
            or _SHA256.fullmatch(digest) is None
            or member <= previous
            or member in checksums
        ):
            raise Phase9EntryError("candidate checksums are malformed or unsorted")
        checksums[member] = digest
        previous = member
    expected_checksums = {
        f"{archive_root}/{row['path']}": str(row["sha256"])
        for row in inventory_rows
    }
    expected_checksums[f"{archive_root}/MANIFEST.json"] = hashlib.sha256(
        manifest_raw
    ).hexdigest()
    if checksums != expected_checksums:
        raise Phase9EntryError("candidate checksums differ from payload and manifest")
    return {
        "archive_root": archive_root,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "checksums_sha256": hashlib.sha256(checksums_raw).hexdigest(),
        "binding": "CANONICAL_MANIFEST_AND_EXACT_PAYLOAD_CHECKSUMS",
    }


def _verify_fresh_inventory(
    repository: Path, inventory: Path, candidate: CandidateIdentity
) -> dict[str, object]:
    raw = _regular_file_bytes(
        inventory, maximum_bytes=32 * 1024 * 1024, label="candidate source inventory"
    )
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise Phase9EntryError("candidate inventory must be UTF-8") from exc
    if not lines or lines[0] != "path\tsize\tmode\tsha256":
        raise Phase9EntryError("candidate inventory header differs")
    paths: list[str] = []
    inventory_rows: list[dict[str, object]] = []
    tracked_entries: list[GitTrackedSourceEntryV1] = []
    tree: dict[str, object] = {}
    total_bytes = 0
    for index, line in enumerate(lines[1:], start=2):
        fields = line.split("\t")
        if len(fields) != 4:
            raise Phase9EntryError(f"candidate inventory line {index} is malformed")
        relative = _safe_relative_path(fields[0], f"inventory line {index}.path")
        try:
            size = int(fields[1])
        except ValueError as exc:
            raise Phase9EntryError(f"candidate inventory line {index} size differs") from exc
        if size < 0 or fields[2] not in {"0644", "0755"}:
            raise Phase9EntryError(f"candidate inventory line {index} metadata differs")
        digest = _sha(fields[3], f"inventory line {index}.sha256")
        if paths and relative <= paths[-1]:
            raise Phase9EntryError("candidate inventory paths are not unique bytewise sorted")
        paths.append(relative)
        inventory_rows.append(
            {"path": relative, "size": size, "mode": fields[2], "sha256": digest}
        )
        file_path = repository.joinpath(*PurePosixPath(relative).parts)
        file_raw = _regular_file_bytes(
            file_path,
            maximum_bytes=max(size, 1),
            label=f"candidate source {relative}",
        )
        file_info = file_path.stat()
        actual_mode = "0755" if file_info.st_mode & 0o111 else "0644"
        if (
            len(file_raw) != size
            or actual_mode != fields[2]
            or hashlib.sha256(file_raw).hexdigest() != digest
        ):
            raise Phase9EntryError(f"candidate source bytes differ: {relative}")
        total_bytes += size
        current = tree
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            existing = current.setdefault(part, {})
            if type(existing) is not dict:
                raise Phase9EntryError("candidate inventory has a file/directory collision")
            current = existing
        if parts[-1] in current:
            raise Phase9EntryError("candidate inventory path collides")
        mode = "100755" if fields[2] == "0755" else "100644"
        object_id = _git_object_id("blob", file_raw)
        current[parts[-1]] = (mode, object_id)
        tracked_entries.append(
            GitTrackedSourceEntryV1(
                GIT_TRACKED_SOURCE_ENTRY_SCHEMA,
                relative,
                mode,
                "blob",
                object_id.hex(),
                size,
                digest,
            )
        )
    if not paths:
        raise Phase9EntryError("candidate inventory is empty")
    candidate_metadata = {"MANIFEST.json", "checksums/SHA256SUMS"}
    metadata_present = {
        relative for relative in candidate_metadata
        if repository.joinpath(*PurePosixPath(relative).parts).exists()
    }
    if metadata_present not in (set(), candidate_metadata):
        raise Phase9EntryError("fresh extraction candidate metadata is incomplete")
    actual_files: list[str] = []
    for item in repository.rglob("*"):
        relative = item.relative_to(repository).as_posix()
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise Phase9EntryError(f"fresh extraction contains a symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise Phase9EntryError(
                f"fresh extraction contains a special file: {relative}"
            )
        if relative not in candidate_metadata:
            actual_files.append(relative)
    actual_files.sort()
    if actual_files != paths:
        raise Phase9EntryError("fresh extraction files differ from the frozen inventory")
    payload_tree = _tree_id(tree).hex()
    metadata_evidence: dict[str, object] | None = None
    if metadata_present:
        metadata_evidence = _verify_candidate_metadata(
            repository,
            candidate=candidate,
            inventory_rows=inventory_rows,
        )
    elif payload_tree != candidate.tree:
        raise Phase9EntryError("fresh extraction Git tree differs from candidate")
    result: dict[str, object] = {
        "mode": "FRESH_INVENTORY",
        "candidate": candidate.as_dict(),
        "inventory_sha256": hashlib.sha256(raw).hexdigest(),
        "source_inventory_sha256": GitTrackedSourceInventoryV1(
            GIT_TRACKED_SOURCE_INVENTORY_SCHEMA,
            candidate.commit,
            candidate.tree,
            candidate.parent,
            tuple(tracked_entries),
            len(tracked_entries),
            total_bytes,
        ).inventory_sha256,
        "path_count": len(paths),
        "total_bytes": total_bytes,
        "verified_tree": candidate.tree,
        "payload_tree": payload_tree,
        "candidate_metadata_present": bool(metadata_present),
    }
    if metadata_evidence is not None:
        result["candidate_metadata"] = metadata_evidence
    return result


def verify_candidate_source(
    repository: str | Path,
    *,
    candidate: CandidateIdentity,
    inventory: str | Path | None = None,
) -> dict[str, object]:
    root = Path(repository).resolve(strict=True)
    if not root.is_dir():
        raise Phase9EntryError("candidate source root must be a directory")
    git_marker = root / ".git"
    if git_marker.exists():
        if inventory is not None:
            raise Phase9EntryError("source mode must be Git or fresh inventory, not both")
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    "HEAD^{commit}",
                    "HEAD^{tree}",
                    "HEAD^",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise Phase9EntryError("candidate Git identity cannot be read") from exc
        lines = completed.stdout.splitlines()
        if lines != [candidate.commit, candidate.tree, candidate.parent]:
            raise Phase9EntryError("live Git identity differs from candidate")
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        ).stdout
        if dirty:
            raise Phase9EntryError("candidate Git source is not an exact clean checkout")
        try:
            snapshot = read_current_git_source_snapshot(root)
        except Phase9RunGenerationSafetyError as exc:
            raise Phase9EntryError(str(exc)) from exc
        if (
            snapshot.source.source_commit != candidate.commit
            or snapshot.source.source_tree != candidate.tree
            or snapshot.source.source_parent != candidate.parent
        ):
            raise Phase9EntryError("live Git source snapshot differs from candidate")
        return {
            "mode": "GIT",
            "candidate": candidate.as_dict(),
            "verified_tree": candidate.tree,
            "source_inventory_sha256": snapshot.source_inventory_sha256,
            "worktree_clean": True,
        }
    if inventory is None:
        raise Phase9EntryError("fresh candidate source requires a frozen inventory")
    return _verify_fresh_inventory(root, Path(inventory), candidate)


def verify_official_input_manifest(
    value: object, *, input_root: str | Path
) -> tuple[str, str]:
    item = _exact_mapping(
        value,
        "official_input_manifest",
        {
            "schema_version",
            "input_generation",
            "files",
        },
    )
    if item["schema_version"] != PHASE9_OFFICIAL_INPUT_MANIFEST_SCHEMA:
        raise Phase9EntryError("official input manifest schema differs")
    _plain_text(
        item["input_generation"],
        "input_generation",
        identifier=True,
    )
    if type(item["files"]) is not list or not item["files"]:
        raise Phase9EntryError("official input entries must be a non-empty list")
    files: list[OfficialInputFileEvidenceV1] = []
    previous = ""
    for index, raw_entry in enumerate(item["files"]):
        entry = _exact_mapping(
            raw_entry,
            f"official_input.entries[{index}]",
            {"schema_version", "logical_path", "byte_length", "raw_bytes_sha256"},
        )
        if entry["schema_version"] != PHASE9_OFFICIAL_INPUT_FILE_SCHEMA:
            raise Phase9EntryError("official input file evidence schema differs")
        relative = _safe_relative_path(
            entry["logical_path"], f"official_input.entries[{index}].logical_path"
        )
        if previous and relative <= previous:
            raise Phase9EntryError("official input paths must be unique bytewise sorted")
        previous = relative
        size = _integer(
            entry["byte_length"], f"official_input.entries[{index}].byte_length"
        )
        digest = _sha(
            entry["raw_bytes_sha256"],
            f"official_input.entries[{index}].raw_bytes_sha256",
        )
        files.append(
            OfficialInputFileEvidenceV1(
                OFFICIAL_INPUT_FILE_EVIDENCE_SCHEMA,
                relative,
                size,
                digest,
            )
        )
    evidence = OfficialInputManifestEvidenceV1(
        OFFICIAL_INPUT_MANIFEST_EVIDENCE_SCHEMA,
        str(item["input_generation"]),
        tuple(files),
    )
    try:
        verify_official_input_snapshot(input_root, evidence)
    except Phase9RunGenerationSafetyError as exc:
        raise Phase9EntryError(str(exc)) from exc
    return evidence.manifest_sha256, evidence.raw_bytes_set_sha256


def validate_execution_context(
    value: object,
) -> str:
    item = _exact_mapping(
        value,
        "execution_context",
        {
            "schema_version",
            "context_id",
            "runtime_environment_sha256",
            "dependency_lock_sha256",
            "launcher_argv_sha256",
            "captured_at",
        },
    )
    if item["schema_version"] != PHASE9_EXECUTION_CONTEXT_SCHEMA:
        raise Phase9EntryError("execution context schema differs")
    _plain_text(item["context_id"], "execution_context.context_id", identifier=True)
    for name in (
        "runtime_environment_sha256",
        "dependency_lock_sha256",
        "launcher_argv_sha256",
    ):
        _sha(item[name], f"execution_context.{name}")
    _integer(item["captured_at"], "execution_context.captured_at")
    return canonical_sha256(item)


@dataclass(frozen=True)
class Phase9EntryState:
    candidate: CandidateIdentity
    source_inventory_sha256: str
    project_id: str
    workflow_id: str
    project_revision: int
    project_generation: str
    run_generation: str
    runtime_generation: str
    scheduler_generation: str
    contract_pin_set_sha256: str
    run_mode: str
    modeling_consultation_contract: str
    delivery_capability: str
    creation_receipt_sha256: str
    request_sha256: str
    official_input_manifest_sha256: str
    official_input_raw_bytes_set_sha256: str
    execution_context_receipt_sha256: str
    operator_authorization_receipt_sha256: str
    production_schema_version: int
    production_migration_count: int
    writer_switch_mode: str
    writer_enabled: bool
    consumer_enabled: bool
    active_process_count: int
    pending_outbox_count: int
    unresolved_migration_count: int
    old_generation_post_boundary_event_count: int
    old_generation_read_only_guards_verified: bool
    p0_runner_attestation: dict[str, object] | None = None

    def runner_live_body(self) -> dict[str, object]:
        return {
            "schema": PHASE9_ENTRY_STATE_SCHEMA,
            "candidate": self.candidate.as_dict(),
            "coordinate": {
                "project_id": self.project_id,
                "workflow_id": self.workflow_id,
                "project_revision": self.project_revision,
                "project_generation": self.project_generation,
                "run_generation": self.run_generation,
                "runtime_generation": self.runtime_generation,
                "scheduler_generation": self.scheduler_generation,
                "contract_pin_set_sha256": self.contract_pin_set_sha256,
            },
            "generation": {
                "source_inventory_sha256": self.source_inventory_sha256,
                "run_mode": self.run_mode,
                "modeling_consultation_contract": self.modeling_consultation_contract,
                "delivery_capability": self.delivery_capability,
                "creation_receipt_sha256": self.creation_receipt_sha256,
                "request_sha256": self.request_sha256,
                "official_input_manifest_sha256": (
                    self.official_input_manifest_sha256
                ),
                "official_input_raw_bytes_set_sha256": (
                    self.official_input_raw_bytes_set_sha256
                ),
                "execution_context_receipt_sha256": (
                    self.execution_context_receipt_sha256
                ),
                "operator_authorization_receipt_sha256": (
                    self.operator_authorization_receipt_sha256
                ),
            },
            "migration": {
                "production_schema_version": self.production_schema_version,
                "production_migration_count": self.production_migration_count,
                "unresolved_migration_count": self.unresolved_migration_count,
            },
            "fences": {
                "writer_switch_mode": self.writer_switch_mode,
                "writer_enabled": self.writer_enabled,
                "consumer_enabled": self.consumer_enabled,
                "delivery_disabled": self.delivery_capability == "DISABLED",
                "old_generation_read_only_guards_verified": (
                    self.old_generation_read_only_guards_verified
                ),
            },
            "quiescence": {
                "active_process_count": self.active_process_count,
                "pending_outbox_count": self.pending_outbox_count,
                "old_generation_post_boundary_event_count": (
                    self.old_generation_post_boundary_event_count
                ),
            },
            "collector_capabilities": {
                "sqlite_write": False,
                "migration": False,
                "process_launch": False,
                "network_access": False,
                "provider_call": False,
                "outbox_dispatch": False,
                "delivery": False,
                "release": False,
                "deployment": False,
                "cutover": False,
            },
        }

    @property
    def runner_live_binding_sha256(self) -> str:
        return canonical_sha256(self.runner_live_body())

    def body(self) -> dict[str, object]:
        body = self.runner_live_body()
        body["p0_runner_attestation"] = self.p0_runner_attestation
        return body

    def as_dict(self) -> dict[str, object]:
        body = self.body()
        body["state_receipt_sha256"] = canonical_sha256(body)
        return body

    @property
    def state_receipt_sha256(self) -> str:
        return canonical_sha256(self.body())


def _p0_runner_attestation(
    connection: sqlite3.Connection, state: Phase9EntryState
) -> dict[str, object] | None:
    row = connection.execute(
        "SELECT * FROM authority_production_phase9_p0_runner_attestations "
        "WHERE workflow_id=? AND run_generation=?",
        (state.workflow_id, state.run_generation),
    ).fetchone()
    if row is None:
        return None
    authorization = connection.execute(
        "SELECT * FROM authority_production_phase9_p0_runner_authorizations "
        "WHERE authorization_id=?",
        (row["authorization_id"],),
    ).fetchone()
    consumption = connection.execute(
        "SELECT * FROM authority_production_phase9_p0_runner_consumptions "
        "WHERE authorization_id=?",
        (row["authorization_id"],),
    ).fetchone()
    if authorization is None or consumption is None:
        raise Phase9EntryError("P0 runner attestation provenance is incomplete")
    try:
        value = json.loads(str(row["attestation_json"]))
        authorization_value = json.loads(str(authorization["authorization_json"]))
        consumption_value = json.loads(str(consumption["consumption_json"]))
    except json.JSONDecodeError as exc:
        raise Phase9EntryError("P0 runner attestation provenance JSON is malformed") from exc
    authorization_item = _exact_mapping(
        authorization_value,
        "P0 runner authorization",
        {
            "schema", "evidence_domain", "authorization_id", "nonce_sha256",
            "candidate", "coordinate", "source_inventory_sha256",
            "live_binding_sha256", "spec_sha256", "producer", "operator_uid",
            "operator_account", "issued_at", "expires_at",
            "intended_evidence_root", "python_identity_sha256",
            "execution_context_binding", "capabilities",
            "authorization_receipt_sha256",
        },
    )
    consumption_item = _exact_mapping(
        consumption_value,
        "P0 runner consumption",
        {
            "schema", "evidence_domain", "authorization_id",
            "authorization_receipt_sha256", "nonce", "nonce_sha256",
            "invocation_id", "candidate", "coordinate",
            "source_inventory_sha256", "live_binding_sha256", "spec_sha256",
            "producer", "operator_uid", "operator_account", "issued_at",
            "expires_at", "consumed_at", "intended_evidence_root",
            "python_identity_sha256", "execution_context_binding",
            "capabilities", "evidence_sha256",
        },
    )
    item = _exact_mapping(
        value,
        "P0 runner attestation",
        {
            "schema",
            "evidence_domain",
            "authorization_id",
            "authorization_receipt_sha256",
            "consumption_receipt_sha256",
            "authority_runner_evidence_sha256",
            "invocation_id",
            "candidate",
            "coordinate",
            "source_inventory_sha256",
            "live_binding_sha256",
            "spec_sha256",
            "execution_context_binding",
            "operator_uid",
            "operator_account",
            "issued_at",
            "expires_at",
            "consumed_at",
            "started_at",
            "finished_at",
            "attested_at",
            "exit_code",
            "evidence_root_sha256",
            "command_record_sha256",
            "raw_log_byte_length",
            "raw_log_sha256",
            "junit_byte_length",
            "junit_sha256",
            "outcome_sha256",
            "receipt_sha256s",
            "receipt_set_sha256",
            "capabilities",
            "attestation_sha256",
        },
    )
    body = dict(item)
    claimed = _sha(body.pop("attestation_sha256"), "P0 runner attestation")
    receipt_hashes = _exact_mapping(
        item["receipt_sha256s"],
        "P0 runner attestation receipt set",
        set(P0_REQUIREMENTS),
    )
    if any(
        _sha(value, f"P0 runner attestation receipt {name}") != value
        for name, value in receipt_hashes.items()
    ):
        raise Phase9EntryError("P0 runner attestation receipt set differs")
    coordinate = _exact_mapping(
        item["coordinate"],
        "P0 runner attestation coordinate",
        {"project_id", "workflow_id", "run_generation"},
    )
    candidate_value = _exact_mapping(
        item["candidate"],
        "P0 runner attestation candidate",
        {"commit", "tree", "parent"},
    )
    expected_coordinate = {
        "project_id": state.project_id,
        "workflow_id": state.workflow_id,
        "run_generation": state.run_generation,
    }
    for name in (
        "nonce_sha256", "source_inventory_sha256", "live_binding_sha256",
        "spec_sha256", "python_identity_sha256", "authorization_receipt_sha256",
    ):
        _sha(authorization_item[name], f"P0 runner authorization {name}")
    for name in (
        "authorization_receipt_sha256", "nonce_sha256",
        "source_inventory_sha256", "live_binding_sha256", "spec_sha256",
        "python_identity_sha256", "evidence_sha256",
    ):
        _sha(consumption_item[name], f"P0 runner consumption {name}")
    for name in ("operator_uid", "issued_at", "expires_at"):
        _integer(authorization_item[name], f"P0 runner authorization {name}")
    for name in ("operator_uid", "issued_at", "expires_at", "consumed_at"):
        _integer(consumption_item[name], f"P0 runner consumption {name}")
    for value, path in (
        (authorization_item["authorization_id"], "authorization authorization_id"),
        (authorization_item["operator_account"], "authorization operator_account"),
        (authorization_item["intended_evidence_root"], "authorization evidence root"),
        (consumption_item["nonce"], "consumption nonce"),
        (consumption_item["invocation_id"], "consumption invocation_id"),
    ):
        _plain_text(value, f"P0 runner {path}")
    producer = _exact_mapping(
        authorization_item["producer"],
        "P0 runner producer",
        {
            "producer_type", "producer_version", "source_path",
            "source_blob_sha256", "sandbox_path", "sandbox_sha256",
            "trusted_reporter_source_path", "trusted_reporter_blob_sha256",
            "trusted_reporter_schema", "loaded_source_root",
            "loaded_source_inventory_sha256",
        },
    )
    _sha(producer["source_blob_sha256"], "P0 runner producer source blob")
    _sha(producer["sandbox_sha256"], "P0 runner producer sandbox")
    _sha(
        producer["trusted_reporter_blob_sha256"],
        "P0 runner trusted reporter source blob",
    )
    _plain_text(producer["loaded_source_root"], "P0 runner loaded source root")
    _sha(
        producer["loaded_source_inventory_sha256"],
        "P0 runner loaded source inventory",
    )
    execution_binding = _exact_mapping(
        authorization_item["execution_context_binding"],
        "P0 runner execution context binding",
        {
            "schema", "execution_context", "execution_context_receipt_sha256",
            "runtime_environment", "dependency_lock_sha256", "launcher",
            "binding_sha256",
        },
    )
    execution_context = _exact_mapping(
        execution_binding["execution_context"],
        "P0 runner generation execution context",
        {
            "schema_version", "context_id", "runtime_environment_sha256",
            "dependency_lock_sha256", "launcher_argv_sha256", "captured_at",
        },
    )
    binding_body = dict(execution_binding)
    binding_claimed = _sha(
        binding_body.pop("binding_sha256"),
        "P0 runner execution context binding",
    )
    runtime_descriptor = _exact_mapping(
        execution_binding["runtime_environment"],
        "P0 runner runtime environment descriptor",
        {
            "schema", "python_runtime_sha256", "dependency_lock_sha256",
            "sandbox_sha256", "trusted_reporter_blob_sha256",
            "trusted_reporter_schema", "interpreter_flags",
            "environment_policy", "descriptor_sha256",
        },
    )
    launcher_descriptor = _exact_mapping(
        execution_binding["launcher"],
        "P0 runner launcher descriptor",
        {
            "schema", "producer_source_blob_sha256",
            "loaded_source_inventory_sha256",
            "trusted_reporter_blob_sha256", "sandbox_sha256",
            "interpreter_flags", "pytest_flags", "dynamic_arguments",
            "test_nodes", "sandbox_mount_policy", "descriptor_sha256",
        },
    )
    runtime_body = dict(runtime_descriptor)
    runtime_claimed = _sha(
        runtime_body.pop("descriptor_sha256"),
        "P0 runner runtime environment descriptor",
    )
    launcher_body = dict(launcher_descriptor)
    launcher_claimed = _sha(
        launcher_body.pop("descriptor_sha256"),
        "P0 runner launcher descriptor",
    )
    if (
        execution_binding["schema"]
        != PHASE9_P0_EXECUTION_CONTEXT_BINDING_SCHEMA
        or canonical_sha256(execution_context)
        != state.execution_context_receipt_sha256
        or execution_binding["execution_context_receipt_sha256"]
        != state.execution_context_receipt_sha256
        or execution_binding["dependency_lock_sha256"]
        != execution_context["dependency_lock_sha256"]
        or runtime_descriptor["python_runtime_sha256"]
        != authorization_item["python_identity_sha256"]
        or runtime_descriptor["dependency_lock_sha256"]
        != execution_context["dependency_lock_sha256"]
        or runtime_descriptor["sandbox_sha256"] != producer["sandbox_sha256"]
        or runtime_descriptor["trusted_reporter_blob_sha256"]
        != producer["trusted_reporter_blob_sha256"]
        or launcher_descriptor["producer_source_blob_sha256"]
        != producer["source_blob_sha256"]
        or launcher_descriptor["loaded_source_inventory_sha256"]
        != producer["loaded_source_inventory_sha256"]
        or launcher_descriptor["trusted_reporter_blob_sha256"]
        != producer["trusted_reporter_blob_sha256"]
        or launcher_descriptor["sandbox_sha256"] != producer["sandbox_sha256"]
        or execution_context["runtime_environment_sha256"]
        != runtime_descriptor["descriptor_sha256"]
        or execution_context["launcher_argv_sha256"]
        != launcher_descriptor["descriptor_sha256"]
        or canonical_sha256(runtime_body) != runtime_claimed
        or canonical_sha256(launcher_body) != launcher_claimed
        or canonical_sha256(binding_body) != binding_claimed
    ):
        raise Phase9EntryError("P0 runner execution context binding differs")
    _validate_p0_runner_capabilities(
        authorization_item["capabilities"], "P0 runner authorization capabilities"
    )
    _validate_p0_runner_capabilities(
        consumption_item["capabilities"], "P0 runner consumption capabilities"
    )
    _validate_p0_runner_capabilities(
        item["capabilities"], "P0 runner attestation capabilities"
    )
    for hash_field in (
        "authorization_receipt_sha256",
        "consumption_receipt_sha256",
        "authority_runner_evidence_sha256",
        "source_inventory_sha256",
        "live_binding_sha256",
        "spec_sha256",
        "evidence_root_sha256",
        "command_record_sha256",
        "raw_log_sha256",
        "junit_sha256",
        "outcome_sha256",
        "receipt_set_sha256",
    ):
        _sha(item[hash_field], f"P0 runner attestation {hash_field}")
    if (
        item["schema"] != PHASE9_P0_RUNNER_ATTESTATION_SCHEMA
        or item["evidence_domain"] != PHASE9_P0_FORMAL_DOMAIN
        or candidate_value != state.candidate.as_dict()
        or coordinate != expected_coordinate
        or item["source_inventory_sha256"] != state.source_inventory_sha256
        or item["live_binding_sha256"] != state.runner_live_binding_sha256
        or item["spec_sha256"] != phase9_p0_spec_sha256()
        or item["execution_context_binding"] != execution_binding
        or item["receipt_set_sha256"] != canonical_sha256(receipt_hashes)
        or item["capabilities"] != _P0_RUNNER_CAPABILITIES
        or item["exit_code"] != 0
        or canonical_sha256(body) != claimed
        or row["attestation_sha256"] != claimed
        or canonical_bytes(item).decode("utf-8") != row["attestation_json"]
        or row["authorization_id"] != item["authorization_id"]
        or row["authorization_receipt_sha256"]
        != item["authorization_receipt_sha256"]
        or row["consumption_receipt_sha256"]
        != item["consumption_receipt_sha256"]
        or row["authority_runner_evidence_sha256"]
        != item["authority_runner_evidence_sha256"]
        or item["authority_runner_evidence_sha256"]
        != hashlib.sha256(canonical_bytes(consumption_item)).hexdigest()
        or item["authorization_id"] != authorization_item["authorization_id"]
        or item["authorization_receipt_sha256"]
        != authorization_item["authorization_receipt_sha256"]
        or item["consumption_receipt_sha256"]
        != consumption_item["evidence_sha256"]
        or item["invocation_id"] != consumption_item["invocation_id"]
        or item["operator_uid"] != authorization_item["operator_uid"]
        or item["operator_account"] != authorization_item["operator_account"]
        or item["issued_at"] != authorization_item["issued_at"]
        or item["expires_at"] != authorization_item["expires_at"]
        or item["consumed_at"] != consumption_item["consumed_at"]
        or row["invocation_id"] != item["invocation_id"]
        or row["project_id"] != state.project_id
        or row["workflow_id"] != state.workflow_id
        or row["run_generation"] != state.run_generation
        or row["source_inventory_sha256"] != state.source_inventory_sha256
        or row["live_binding_sha256"] != state.runner_live_binding_sha256
        or row["spec_sha256"] != item["spec_sha256"]
        or row["evidence_root_sha256"] != item["evidence_root_sha256"]
        or row["command_record_sha256"] != item["command_record_sha256"]
        or row["raw_log_byte_length"] != item["raw_log_byte_length"]
        or row["raw_log_sha256"] != item["raw_log_sha256"]
        or row["junit_byte_length"] != item["junit_byte_length"]
        or row["junit_sha256"] != item["junit_sha256"]
        or row["outcome_sha256"] != item["outcome_sha256"]
        or row["receipt_set_sha256"] != item["receipt_set_sha256"]
        or row["started_at"] != item["started_at"]
        or row["finished_at"] != item["finished_at"]
        or row["attested_at"] != item["attested_at"]
        or row["exit_code"] != item["exit_code"]
        or authorization["authorization_receipt_sha256"]
        != item["authorization_receipt_sha256"]
        or consumption["consumption_receipt_sha256"]
        != item["consumption_receipt_sha256"]
        or type(authorization_value) is not dict
        or type(consumption_value) is not dict
        or canonical_bytes(authorization_value).decode("utf-8")
        != authorization["authorization_json"]
        or canonical_bytes(consumption_value).decode("utf-8")
        != consumption["consumption_json"]
        or authorization_item["schema"]
        != PHASE9_P0_RUNNER_AUTHORIZATION_SCHEMA
        or authorization_item["evidence_domain"] != PHASE9_P0_FORMAL_DOMAIN
        or authorization_item["authorization_id"] != row["authorization_id"]
        or authorization_item["candidate"] != state.candidate.as_dict()
        or authorization_item["coordinate"] != expected_coordinate
        or authorization_item["source_inventory_sha256"]
        != state.source_inventory_sha256
        or authorization_item["live_binding_sha256"]
        != state.runner_live_binding_sha256
        or authorization_item["spec_sha256"] != phase9_p0_spec_sha256()
        or authorization_item["capabilities"] != _P0_RUNNER_CAPABILITIES
        or producer["producer_type"] != PHASE9_P0_PRODUCER_TYPE
        or producer["producer_version"] != PHASE9_P0_PRODUCER_VERSION
        or producer["source_path"] != PHASE9_P0_PRODUCER_SOURCE_PATH
        or producer["sandbox_path"] != "/usr/bin/bwrap"
        or producer["trusted_reporter_source_path"]
        != PHASE9_P0_TRUSTED_REPORTER_SOURCE_PATH
        or producer["trusted_reporter_schema"] != TRUSTED_PYTEST_EVENT_SCHEMA
        or authorization["authorization_id"]
        != authorization_item["authorization_id"]
        or authorization["nonce_sha256"] != authorization_item["nonce_sha256"]
        or authorization["project_id"] != state.project_id
        or authorization["workflow_id"] != state.workflow_id
        or authorization["run_generation"] != state.run_generation
        or authorization["source_commit"] != state.candidate.commit
        or authorization["source_tree"] != state.candidate.tree
        or authorization["source_parent"] != state.candidate.parent
        or authorization["source_inventory_sha256"]
        != state.source_inventory_sha256
        or authorization["live_binding_sha256"]
        != state.runner_live_binding_sha256
        or authorization["spec_sha256"] != phase9_p0_spec_sha256()
        or authorization["operator_uid"] != authorization_item["operator_uid"]
        or authorization["operator_account"]
        != authorization_item["operator_account"]
        or authorization["issued_at"] != authorization_item["issued_at"]
        or authorization["expires_at"] != authorization_item["expires_at"]
        or authorization["intended_evidence_root"]
        != authorization_item["intended_evidence_root"]
        or authorization["python_identity_sha256"]
        != authorization_item["python_identity_sha256"]
        or consumption_item["schema"]
        != PHASE9_P0_AUTHORITY_RUNNER_EVIDENCE_SCHEMA
        or consumption_item["evidence_domain"] != PHASE9_P0_FORMAL_DOMAIN
        or consumption_item["authorization_id"] != row["authorization_id"]
        or consumption_item["authorization_receipt_sha256"]
        != authorization_item["authorization_receipt_sha256"]
        or consumption_item["nonce_sha256"] != authorization_item["nonce_sha256"]
        or hashlib.sha256(
            str(consumption_item["nonce"]).encode("utf-8")
        ).hexdigest()
        != consumption_item["nonce_sha256"]
        or consumption_item["invocation_id"] != row["invocation_id"]
        or consumption_item["candidate"] != state.candidate.as_dict()
        or consumption_item["coordinate"] != expected_coordinate
        or consumption_item["source_inventory_sha256"]
        != state.source_inventory_sha256
        or consumption_item["live_binding_sha256"]
        != state.runner_live_binding_sha256
        or consumption_item["spec_sha256"] != phase9_p0_spec_sha256()
        or consumption_item["producer"] != authorization_item["producer"]
        or consumption_item["execution_context_binding"] != execution_binding
        or consumption_item["operator_uid"] != authorization_item["operator_uid"]
        or consumption_item["operator_account"]
        != authorization_item["operator_account"]
        or consumption_item["issued_at"] != authorization_item["issued_at"]
        or consumption_item["expires_at"] != authorization_item["expires_at"]
        or consumption_item["intended_evidence_root"]
        != authorization_item["intended_evidence_root"]
        or consumption_item["python_identity_sha256"]
        != authorization_item["python_identity_sha256"]
        or consumption_item["capabilities"] != _P0_RUNNER_CAPABILITIES
        or consumption["authorization_id"] != row["authorization_id"]
        or consumption["nonce_sha256"] != consumption_item["nonce_sha256"]
        or consumption["invocation_id"] != consumption_item["invocation_id"]
        or consumption["consumed_at"] != consumption_item["consumed_at"]
        or authorization_item["expires_at"]
        - authorization_item["issued_at"]
        > PHASE9_P0_RUNNER_AUTHORIZATION_TTL_SECONDS
        or not (
            authorization_item["issued_at"]
            <= consumption_item["consumed_at"]
            <= authorization_item["expires_at"]
        )
    ):
        raise Phase9EntryError("P0 runner attestation provenance differs")
    authorization_body = dict(authorization_value)
    authorization_claimed = authorization_body.pop(
        "authorization_receipt_sha256", None
    )
    consumption_body = dict(consumption_value)
    consumption_claimed = consumption_body.pop("evidence_sha256", None)
    if (
        authorization_claimed != authorization["authorization_receipt_sha256"]
        or canonical_sha256(authorization_body) != authorization_claimed
        or consumption_claimed != consumption["consumption_receipt_sha256"]
        or canonical_sha256(consumption_body) != consumption_claimed
    ):
        raise Phase9EntryError("P0 runner authorization/consumption hashes differ")
    for name in (
        "operator_uid",
        "issued_at",
        "expires_at",
        "consumed_at",
        "started_at",
        "finished_at",
        "attested_at",
        "raw_log_byte_length",
        "junit_byte_length",
    ):
        _integer(item[name], f"P0 runner attestation {name}")
    if _integer(item["exit_code"], "P0 runner attestation exit_code") != 0:
        raise Phase9EntryError("P0 runner attestation exit code differs")
    if not (
        item["issued_at"]
        <= item["consumed_at"]
        <= item["started_at"]
        <= item["finished_at"]
        <= item["attested_at"]
        <= item["expires_at"]
    ):
        raise Phase9EntryError("P0 runner attestation time bounds differ")
    return dict(item)


def _parse_creation_receipt(
    row: sqlite3.Row,
    *,
    generation: sqlite3.Row,
    candidate: CandidateIdentity,
) -> Mapping[str, object]:
    try:
        value = json.loads(str(row["receipt_json"]))
    except json.JSONDecodeError as exc:
        raise Phase9EntryError("run-generation creation receipt JSON is malformed") from exc
    if type(value) is not dict:
        raise Phase9EntryError("run-generation creation receipt must be an object")
    raw = canonical_bytes(value).decode("utf-8")
    if raw != row["receipt_json"] or canonical_sha256(value) != row["receipt_sha256"]:
        raise Phase9EntryError("run-generation creation receipt bytes/hash differ")
    body = _exact_mapping(
        value,
        "run-generation creation receipt",
        {
            "schema",
            "run_generation",
            "workflow_id",
            "operation_kind",
            "request_sha256",
            "request",
            "official_input_manifest_sha256",
            "official_input_raw_bytes_set_sha256",
            "execution_context_receipt_sha256",
            "operator_authorization_receipt_sha256",
            "operator_authorization_consumption_sha256",
            "authorization_target_sha256",
            "source_inventory_sha256",
            "occurred_at",
        },
    )
    if body["schema"] != RUN_GENERATION_RECEIPT_SCHEMA:
        raise Phase9EntryError("run-generation creation receipt schema differs")
    request = body["request"]
    if type(request) is not dict or canonical_sha256(request) != row["request_sha256"]:
        raise Phase9EntryError("run-generation request bytes/hash differ")
    try:
        decoded_request = run_generation_request_from_dict(
            request,
            trusted_now=_integer(body["occurred_at"], "creation receipt occurred_at"),
        )
    except Phase9RunGenerationSafetyError as exc:
        raise Phase9EntryError(
            f"run-generation request is not valid typed evidence: {exc}"
        ) from exc
    if decoded_request.as_dict() != request:
        raise Phase9EntryError("run-generation request typed round-trip differs")
    request_inventory_sha256 = _sha(
        request.get("source_inventory_sha256"),
        "run-generation request source inventory",
    )
    if body["source_inventory_sha256"] != request_inventory_sha256:
        raise Phase9EntryError("run-generation receipt source inventory differs")
    _sha(
        body["operator_authorization_consumption_sha256"],
        "operator authorization consumption",
    )
    authorization_target_sha256 = _sha(
        body["authorization_target_sha256"], "authorization target"
    )
    target_request = dict(request)
    authorization = target_request.pop("operator_authorization", None)
    if type(authorization) is not dict:
        raise Phase9EntryError("run-generation operator authorization is malformed")
    target_sha256 = decoded_request.authorization_target_sha256
    if target_sha256 != authorization_target_sha256:
        raise Phase9EntryError("run-generation authorization target differs")
    if authorization.get("schema_version") != OPERATOR_AUTHORIZATION_EVIDENCE_SCHEMA:
        raise Phase9EntryError("run-generation operator authorization schema differs")
    authorization_receipt_sha256 = canonical_sha256(authorization)
    if (
        authorization.get("authorized_request_sha256") != target_sha256
        or body["operator_authorization_receipt_sha256"]
        != authorization_receipt_sha256
    ):
        raise Phase9EntryError("run-generation operator authorization binding differs")
    statement = dict(authorization)
    claimed_statement_sha256 = statement.pop(
        "authorization_statement_sha256", None
    )
    if claimed_statement_sha256 != canonical_sha256(
        {
            "schema": "authority-phase9-operator-authorization-statement-v2",
            "authorization": statement,
        }
    ):
        raise Phase9EntryError("run-generation authorization statement differs")
    expected_consumption_sha256 = canonical_sha256(
        {
            "schema": RUN_GENERATION_AUTHORIZATION_CONSUMPTION_SCHEMA,
            "authorization_id": authorization.get("authorization_id"),
            "authorization_receipt_sha256": authorization_receipt_sha256,
            "authorization_target_sha256": target_sha256,
            "request_sha256": row["request_sha256"],
            "run_generation": body["run_generation"],
            "workflow_id": body["workflow_id"],
            "consumed_at": body["occurred_at"],
        }
    )
    if body["operator_authorization_consumption_sha256"] != expected_consumption_sha256:
        raise Phase9EntryError("run-generation authorization consumption differs")
    source = request.get("source")
    if type(source) is not dict or source != {
        "schema_version": "authority-phase9-git-source-identity-v1",
        "source_commit": candidate.commit,
        "source_tree": candidate.tree,
        "source_parent": candidate.parent,
    }:
        raise Phase9EntryError("run-generation source identity differs from candidate")
    expected = {
        "run_generation": generation["run_generation"],
        "workflow_id": generation["workflow_id"],
        "operation_kind": generation["operation_kind"],
        "request_sha256": generation["request_sha256"],
        "official_input_manifest_sha256": generation["official_input_manifest_sha256"],
        "official_input_raw_bytes_set_sha256": generation[
            "official_input_raw_bytes_set_sha256"
        ],
        "execution_context_receipt_sha256": generation[
            "execution_context_receipt_sha256"
        ],
        "operator_authorization_receipt_sha256": generation[
            "operator_authorization_receipt_sha256"
        ],
        "authorization_target_sha256": generation[
            "authorization_target_sha256"
        ],
        "source_inventory_sha256": generation["source_inventory_sha256"],
        "occurred_at": generation["created_at"],
    }
    for name, expected_value in expected.items():
        if body[name] != expected_value:
            raise Phase9EntryError(f"run-generation creation receipt {name} differs")
    for name in ("run_generation", "workflow_id", "operation_kind", "request_sha256", "occurred_at"):
        if row[name] != expected[name]:
            raise Phase9EntryError(f"run-generation receipt row {name} differs")
    request_pairs = {
        "project_id": generation["project_id"],
        "workflow_id": generation["workflow_id"],
        "project_revision": generation["project_revision"],
        "project_generation": generation["project_generation"],
        "runtime_generation": generation["runtime_generation"],
        "scheduler_generation": generation["scheduler_generation"],
        "predecessor_run_generation": generation["predecessor_run_generation"],
        "predecessor_creation_receipt_sha256": generation[
            "predecessor_creation_receipt_sha256"
        ],
        "predecessor_terminal_receipt_sha256": generation[
            "predecessor_terminal_receipt_sha256"
        ],
        "operation_kind": generation["operation_kind"],
        "run_mode": generation["run_mode"],
        "modeling_consultation_contract": generation[
            "modeling_consultation_contract"
        ],
        "delivery_capability": generation["delivery_capability"],
        "source_inventory_sha256": generation["source_inventory_sha256"],
    }
    for name, expected_value in request_pairs.items():
        if request.get(name) != expected_value:
            raise Phase9EntryError(f"run-generation request {name} differs")
    return body


def _active_process_count(connection: sqlite3.Connection) -> int:
    project = connection.execute(
        "SELECT status, runner_pid FROM project_state WHERE singleton=1"
    ).fetchone()
    if project is None:
        raise Phase9EntryError("project state is missing")
    count = int(
        project["runner_pid"] is not None
        or str(project["status"]).lower() in _ACTIVE_PROJECT_STATUS
    )
    solver_rows = connection.execute("SELECT status FROM solver_jobs").fetchall()
    count += sum(str(row["status"]) in _ACTIVE_SOLVER_STATUS for row in solver_rows)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='authority_production_phase9_runtime_runs'").fetchone():
        # A dispatcher crash is unresolved work, even if its parent PID is gone.
        # An UNCERTAIN terminal cannot turn an unobserved attempt into idle state.
        count += connection.execute(
            "SELECT COUNT(*) FROM authority_production_phase9_runtime_runs r "
            "LEFT JOIN authority_production_phase9_runtime_terminals t ON t.runtime_id=r.runtime_id "
            "WHERE t.runtime_id IS NULL OR t.terminal_status='UNCERTAIN'"
        ).fetchone()[0]
    return count


def _outbox_counts(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    start_revision: int,
    run_generation: str,
) -> tuple[int, int]:
    current_pending = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM authority_outbox o
            JOIN authority_production_outbox_delivery_state ds
              ON ds.message_id=o.message_id
            WHERE o.workflow_id=?
              AND ds.status IN (
                  'PENDING', 'CLAIMED', 'RETRY_WAIT',
                  'RECONCILIATION_REQUIRED'
              )
            """,
            (workflow_id,),
        ).fetchone()[0]
    )
    rows = connection.execute(
        """
        SELECT e.envelope_json AS event_json
        FROM authority_events e
        WHERE e.workflow_id=? AND e.revision>?
        ORDER BY e.revision, e.event_id
        """,
        (workflow_id, start_revision),
    ).fetchall()
    old_generation = 0
    for row in rows:
        try:
            event = json.loads(str(row["event_json"]))
        except json.JSONDecodeError as exc:
            raise Phase9EntryError("Authority event JSON is malformed") from exc
        if type(event) is not dict or type(event.get("run_generation")) is not str:
            raise Phase9EntryError("Authority event generation binding is malformed")
        if event["run_generation"] != run_generation:
            old_generation += 1
    return current_pending, old_generation


def collect_phase9_entry_state_in_transaction(
    connection: sqlite3.Connection,
    *,
    expected_source_fence_sha256: str,
    workflow_id: str,
    candidate: CandidateIdentity,
) -> Phase9EntryState:
    """Collect one entry state inside the caller's already-open transaction.

    This helper deliberately does not change PRAGMAs and does not begin, commit,
    roll back, or close the supplied connection.  Callers that need the entry
    state and another Phase9 pre-commit check to share one SQLite snapshot must
    own that transaction themselves.
    """

    if not isinstance(connection, sqlite3.Connection):
        raise Phase9EntryError("connection must be an open SQLite connection")
    workflow_key = _plain_text(workflow_id, "workflow_id", identifier=True)
    expected_source_fence = _sha(
        expected_source_fence_sha256, "expected_source_fence_sha256"
    )
    production = verify_production_installation(connection, require_ready=True)
    if production["source_fence_sha256"] != expected_source_fence:
        raise Phase9EntryError("production source fence differs from expected snapshot")
    workflow = connection.execute(
        "SELECT * FROM authority_workflows WHERE workflow_id=?", (workflow_key,)
    ).fetchone()
    pointer = connection.execute(
        "SELECT * FROM authority_production_run_generation_current WHERE workflow_id=?",
        (workflow_key,),
    ).fetchone()
    if workflow is None or pointer is None:
        raise Phase9EntryError("current workflow/run-generation is missing")
    generation = connection.execute(
        "SELECT * FROM authority_production_run_generations WHERE run_generation=?",
        (pointer["run_generation"],),
    ).fetchone()
    receipt = connection.execute(
        "SELECT * FROM authority_production_run_generation_creation_receipts "
        "WHERE run_generation=?",
        (pointer["run_generation"],),
    ).fetchone()
    if generation is None or receipt is None:
        raise Phase9EntryError("current run-generation companion rows are missing")
    if (
        pointer["creation_receipt_sha256"] != receipt["receipt_sha256"]
        or generation["workflow_id"] != workflow_key
        or generation["project_id"] != workflow["project_id"]
        or generation["project_revision"] != workflow["current_revision"]
        or generation["project_generation"] != workflow["project_generation"]
        or generation["run_generation"] != workflow["run_generation"]
        or generation["runtime_generation"] != workflow["runtime_generation"]
        or generation["scheduler_generation"] != workflow["scheduler_generation"]
        or generation["contract_pin_set_sha256"]
        != workflow["contract_pin_set_sha256"]
        or workflow["current_revision_availability"] != "RECORDED"
        or workflow["contract_pin_availability"] != "RECORDED"
    ):
        raise Phase9EntryError("current run-generation/workflow coordinate differs")
    for name in (
        "project_generation",
        "run_generation",
        "runtime_generation",
        "scheduler_generation",
    ):
        if generation[name] == "legacy_unknown":
            raise Phase9EntryError(f"current {name} is legacy_unknown")
    if (
        generation["source_commit"] != candidate.commit
        or generation["source_tree"] != candidate.tree
        or generation["source_parent"] != candidate.parent
    ):
        raise Phase9EntryError("current run-generation belongs to another candidate")
    creation_body = _parse_creation_receipt(
        receipt, generation=generation, candidate=candidate
    )
    inventory_row = connection.execute(
        "SELECT * FROM authority_production_run_generation_source_inventories "
        "WHERE inventory_sha256=?",
        (generation["source_inventory_sha256"],),
    ).fetchone()
    consumption_row = connection.execute(
        "SELECT * FROM authority_production_run_generation_authorization_consumptions "
        "WHERE run_generation=? AND request_sha256=?",
        (generation["run_generation"], generation["request_sha256"]),
    ).fetchone()
    if inventory_row is None or consumption_row is None:
        raise Phase9EntryError("run-generation provenance companion row is missing")
    try:
        inventory_value = json.loads(str(inventory_row["inventory_json"]))
        consumption_value = json.loads(str(consumption_row["receipt_json"]))
    except json.JSONDecodeError as exc:
        raise Phase9EntryError("run-generation provenance JSON is malformed") from exc
    inventory_sha256 = _sha(
        generation["source_inventory_sha256"], "source inventory"
    )
    if (
        type(inventory_value) is not dict
        or canonical_bytes(inventory_value).decode("utf-8")
        != inventory_row["inventory_json"]
        or canonical_sha256(inventory_value) != inventory_sha256
        or inventory_row["schema_version"] != GIT_TRACKED_SOURCE_INVENTORY_SCHEMA
        or inventory_value.get("schema_version")
        != GIT_TRACKED_SOURCE_INVENTORY_SCHEMA
        or inventory_row["source_commit"] != candidate.commit
        or inventory_row["source_tree"] != candidate.tree
        or inventory_row["source_parent"] != candidate.parent
        or inventory_value.get("source_commit") != candidate.commit
        or inventory_value.get("source_tree") != candidate.tree
        or inventory_value.get("source_parent") != candidate.parent
        or inventory_value.get("path_count") != inventory_row["path_count"]
        or inventory_value.get("total_bytes") != inventory_row["total_bytes"]
    ):
        raise Phase9EntryError("run-generation source inventory provenance differs")
    consumption_sha256 = creation_body[
        "operator_authorization_consumption_sha256"
    ]
    if (
        type(consumption_value) is not dict
        or canonical_bytes(consumption_value).decode("utf-8")
        != consumption_row["receipt_json"]
        or canonical_sha256(consumption_value) != consumption_sha256
        or consumption_row["receipt_sha256"] != consumption_sha256
        or consumption_row["authorization_target_sha256"]
        != generation["authorization_target_sha256"]
        or consumption_row["request_sha256"] != generation["request_sha256"]
        or consumption_row["workflow_id"] != generation["workflow_id"]
        or consumption_value.get("run_generation") != generation["run_generation"]
    ):
        raise Phase9EntryError(
            "run-generation authorization consumption provenance differs"
        )
    writer = connection.execute(
        "SELECT switch_mode, writer_enabled FROM "
        "authority_production_writer_state WHERE singleton=1"
    ).fetchone()
    consumer = connection.execute(
        "SELECT consumer_enabled FROM authority_production_consumer_state WHERE singleton=1"
    ).fetchone()
    if writer is None or consumer is None:
        raise Phase9EntryError("delivery control state is missing")
    current_pending, old_generation = _outbox_counts(
        connection,
        workflow_id=workflow_key,
        start_revision=int(generation["project_revision"]),
        run_generation=str(generation["run_generation"]),
    )
    active = _active_process_count(connection)
    migration_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM authority_production_migrations"
        ).fetchone()[0]
    )
    guards_verified = migration_count == len(PRODUCTION_MIGRATIONS)
    state = Phase9EntryState(
        candidate=candidate,
        source_inventory_sha256=_sha(
            generation["source_inventory_sha256"], "source inventory"
        ),
        project_id=str(generation["project_id"]),
        workflow_id=workflow_key,
        project_revision=int(generation["project_revision"]),
        project_generation=str(generation["project_generation"]),
        run_generation=str(generation["run_generation"]),
        runtime_generation=str(generation["runtime_generation"]),
        scheduler_generation=str(generation["scheduler_generation"]),
        contract_pin_set_sha256=_sha(
            generation["contract_pin_set_sha256"], "contract pin set"
        ),
        run_mode=str(generation["run_mode"]),
        modeling_consultation_contract=str(
            generation["modeling_consultation_contract"]
        ),
        delivery_capability=str(generation["delivery_capability"]),
        creation_receipt_sha256=_sha(
            receipt["receipt_sha256"], "creation receipt"
        ),
        request_sha256=_sha(generation["request_sha256"], "generation request"),
        official_input_manifest_sha256=_sha(
            generation["official_input_manifest_sha256"], "official input manifest"
        ),
        official_input_raw_bytes_set_sha256=_sha(
            generation["official_input_raw_bytes_set_sha256"],
            "official input raw bytes set",
        ),
        execution_context_receipt_sha256=_sha(
            generation["execution_context_receipt_sha256"], "execution context"
        ),
        operator_authorization_receipt_sha256=_sha(
            generation["operator_authorization_receipt_sha256"],
            "creation operator authorization",
        ),
        production_schema_version=int(production["production_schema_version"]),
        production_migration_count=migration_count,
        writer_switch_mode=str(writer["switch_mode"]),
        writer_enabled=bool(writer["writer_enabled"]),
        consumer_enabled=bool(consumer["consumer_enabled"]),
        active_process_count=active,
        pending_outbox_count=current_pending,
        unresolved_migration_count=0,
        old_generation_post_boundary_event_count=old_generation,
        old_generation_read_only_guards_verified=guards_verified,
    )
    return replace(state, p0_runner_attestation=_p0_runner_attestation(connection, state))


def collect_phase9_entry_state(
    database: str | Path,
    *,
    workflow_id: str,
    candidate: CandidateIdentity,
    expected_source_fence_sha256: str | None = None,
) -> Phase9EntryState:
    """Collect one revision-atomic entry state without upgrading the database."""

    connection = connect_authority_ro(database)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        expected = expected_source_fence_sha256
        if expected is None:
            source = connection.execute(
                "SELECT source_fence_sha256 FROM authority_production_schema_state "
                "WHERE singleton=1"
            ).fetchone()
            if source is None:
                raise Phase9EntryError("production source fence is missing")
            expected = _sha(source["source_fence_sha256"], "production source fence")
        result = collect_phase9_entry_state_in_transaction(
            connection,
            expected_source_fence_sha256=expected,
            workflow_id=workflow_id,
            candidate=candidate,
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def verify_phase9_entry_gate(
    *,
    state: Phase9EntryState,
    source_verification: Mapping[str, object],
    p0_receipts: Mapping[str, object],
    p0_evidence_root: str | Path,
    p0_evidence_root_sha256: str,
    operator_authorization: object,
    official_input_manifest: object,
    official_input_root: str | Path,
    execution_context: object,
    evaluated_at: int,
    trusted_now: int,
) -> dict[str, object]:
    """Verify the complete candidate-bound gate and return READY or BLOCKED.

    READY means only that the reviewed entry prerequisites are current.  It is
    never authorization to start Phase9-A or any production side effect.
    """

    requested_evaluated_at = _integer(evaluated_at, "evaluated_at")
    now = _integer(trusted_now, "trusted_now")
    blockers: list[dict[str, str]] = []

    def blocked(code: str, detail: str) -> None:
        blockers.append({"code": code, "detail": detail})

    if (
        abs(requested_evaluated_at - now)
        > PHASE9_AUTHORIZATION_CLOCK_SKEW_SECONDS
    ):
        blocked(
            "REQUEST_TIME_INVALID",
            "request evaluated_at exceeds the trusted clock skew",
        )

    if type(source_verification) is not dict:
        blocked("SOURCE_VERIFICATION_MISSING", "candidate source verification is missing")
        source_sha256 = None
    else:
        try:
            if source_verification.get("candidate") != state.candidate.as_dict():
                raise Phase9EntryError("candidate source verification identity differs")
            if source_verification.get("verified_tree") != state.candidate.tree:
                raise Phase9EntryError("candidate source verification tree differs")
            if (
                source_verification.get("source_inventory_sha256")
                != state.source_inventory_sha256
            ):
                raise Phase9EntryError(
                    "candidate source verification inventory differs"
                )
            source_sha256 = canonical_sha256(source_verification)
        except Phase9EntryError as exc:
            blocked("SOURCE_VERIFICATION_INVALID", str(exc))
            source_sha256 = None
    try:
        claimed_p0_root_sha = _sha(
            p0_evidence_root_sha256, "p0_evidence_root_sha256"
        )
        p0_validation = _validate_p0_receipt_set_details(
            p0_receipts,
            candidate=state.candidate,
            project_id=state.project_id,
            workflow_id=state.workflow_id,
            run_generation=state.run_generation,
            source_inventory_sha256=state.source_inventory_sha256,
            evidence_root=p0_evidence_root,
            evidence_root_sha256=claimed_p0_root_sha,
        )
        attestation = state.p0_runner_attestation
        authority_runner = p0_validation.authority_runner
        if attestation is None:
            raise Phase9EntryError(
                "P0 evidence has no Authority-backed runner attestation"
            )
        expected_attestation_bindings = {
            "authorization_id": authority_runner["authorization_id"],
            "authorization_receipt_sha256": authority_runner[
                "authorization_receipt_sha256"
            ],
            "consumption_receipt_sha256": (
                p0_validation.consumption_receipt_sha256
            ),
            "authority_runner_evidence_sha256": (
                p0_validation.authority_runner_evidence_sha256
            ),
            "invocation_id": authority_runner["invocation_id"],
            "candidate": state.candidate.as_dict(),
            "coordinate": {
                "project_id": state.project_id,
                "workflow_id": state.workflow_id,
                "run_generation": state.run_generation,
            },
            "source_inventory_sha256": state.source_inventory_sha256,
            "live_binding_sha256": state.runner_live_binding_sha256,
            "spec_sha256": phase9_p0_spec_sha256(),
            "operator_uid": authority_runner["operator_uid"],
            "operator_account": authority_runner["operator_account"],
            "issued_at": authority_runner["issued_at"],
            "expires_at": authority_runner["expires_at"],
            "consumed_at": authority_runner["consumed_at"],
            "started_at": p0_validation.started_at,
            "finished_at": p0_validation.finished_at,
            "exit_code": 0,
            "evidence_root_sha256": claimed_p0_root_sha,
            "command_record_sha256": p0_validation.command_record_sha256,
            "raw_log_byte_length": p0_validation.raw_log_byte_length,
            "raw_log_sha256": p0_validation.raw_log_sha256,
            "junit_byte_length": p0_validation.junit_byte_length,
            "junit_sha256": p0_validation.junit_sha256,
            "outcome_sha256": p0_validation.outcome_sha256,
            "receipt_sha256s": p0_validation.receipt_sha256s,
            "receipt_set_sha256": p0_validation.receipt_set_sha256,
            "capabilities": _P0_RUNNER_CAPABILITIES,
        }
        if any(
            attestation.get(name) != value
            for name, value in expected_attestation_bindings.items()
        ) or authority_runner["intended_evidence_root"] != str(
            Path(os.path.abspath(os.fspath(p0_evidence_root)))
        ):
            raise Phase9EntryError(
                "P0 external closure differs from its Authority runner attestation"
            )
        p0_hashes = dict(p0_validation.receipt_sha256s)
        verified_p0_root_sha = claimed_p0_root_sha
    except Phase9EntryError as exc:
        blocked("P0_RECEIPTS_INVALID", str(exc))
        p0_hashes = {}
        verified_p0_root_sha = None
        claimed_p0_root_sha = (
            p0_evidence_root_sha256
            if type(p0_evidence_root_sha256) is str
            and _SHA256.fullmatch(p0_evidence_root_sha256) is not None
            else "0" * 64
        )
    try:
        authorization_sha = validate_operator_authorization(
            operator_authorization,
            candidate=state.candidate,
            project_id=state.project_id,
            workflow_id=state.workflow_id,
            run_generation=state.run_generation,
            trusted_now=now,
            p0_evidence_root_sha256=claimed_p0_root_sha,
        )
    except (Phase9EntryError, KeyError) as exc:
        blocked("OPERATOR_AUTHORIZATION_INVALID", str(exc))
        authorization_sha = None
    try:
        manifest_sha, raw_set_sha = verify_official_input_manifest(
            official_input_manifest, input_root=official_input_root
        )
        if (
            manifest_sha != state.official_input_manifest_sha256
            or raw_set_sha != state.official_input_raw_bytes_set_sha256
        ):
            raise Phase9EntryError(
                "official input bytes are not the current generation's frozen inputs"
            )
    except (Phase9EntryError, OSError) as exc:
        blocked("OFFICIAL_INPUT_INVALID", str(exc))
        manifest_sha = None
        raw_set_sha = None
    try:
        context_sha = validate_execution_context(execution_context)
        if context_sha != state.execution_context_receipt_sha256:
            raise Phase9EntryError(
                "execution context is not the current generation's frozen context"
            )
    except Phase9EntryError as exc:
        blocked("EXECUTION_CONTEXT_INVALID", str(exc))
        context_sha = None
    fence_checks = (
        (
            state.production_schema_version == AUTHORITY_PRODUCTION_SCHEMA_VERSION
            and state.production_migration_count == len(PRODUCTION_MIGRATIONS),
            "MIGRATION_NOT_CURRENT",
            "base/production migration prefix is not the reviewed current prefix",
        ),
        (
            state.unresolved_migration_count == 0,
            "UNRESOLVED_MIGRATION",
            "unresolved migration count must be zero",
        ),
        (
            state.writer_switch_mode == "V1_ONLY"
            and not state.writer_enabled
            and not state.consumer_enabled,
            "DELIVERY_CONTROL_NOT_QUIET",
            "V1_ONLY with writer and consumer disabled is required",
        ),
        (
            state.run_mode == "FORENSIC_REPLAY",
            "RUN_MODE_INVALID",
            "Phase9 run mode must be FORENSIC_REPLAY",
        ),
        (
            state.modeling_consultation_contract == "LEGACY_NOT_APPLICABLE",
            "MODELING_CONTRACT_INVALID",
            "Phase9 modeling consultation contract must be LEGACY_NOT_APPLICABLE",
        ),
        (
            state.delivery_capability == "DISABLED",
            "DELIVERY_CAPABILITY_ENABLED",
            "run generation delivery capability must be DISABLED",
        ),
        (
            state.active_process_count == 0,
            "ACTIVE_PROCESS",
            "active process count must be zero",
        ),
        (
            state.pending_outbox_count == 0,
            "PENDING_OUTBOX",
            "current-generation pending outbox count must be zero",
        ),
        (
            state.old_generation_post_boundary_event_count == 0
            and state.old_generation_read_only_guards_verified,
            "OLD_GENERATION_NOT_READ_ONLY",
            "old generation must be immutable after the generation boundary",
        ),
    )
    for condition, code, detail in fence_checks:
        if not condition:
            blocked(code, detail)
    blockers.sort(key=lambda item: (item["code"], item["detail"]))
    body: dict[str, object] = {
        "schema": PHASE9_ENTRY_GATE_SCHEMA,
        "status": "READY" if not blockers else "BLOCKED",
        "evaluated_at": now,
        "request_evaluated_at": requested_evaluated_at,
        "clock_skew_seconds": requested_evaluated_at - now,
        "candidate": state.candidate.as_dict(),
        "project_id": state.project_id,
        "workflow_id": state.workflow_id,
        "run_generation": state.run_generation,
        "source_inventory_sha256": state.source_inventory_sha256,
        "state_receipt_sha256": state.state_receipt_sha256,
        "source_verification_sha256": source_sha256,
        "creation_receipt_sha256": state.creation_receipt_sha256,
        "operator_authorization_receipt_sha256": authorization_sha,
        "official_input_manifest_sha256": manifest_sha,
        "official_input_raw_bytes_set_sha256": raw_set_sha,
        "execution_context_receipt_sha256": context_sha,
        "p0_evidence_root_sha256": verified_p0_root_sha,
        "p0_receipt_sha256s": p0_hashes,
        "blockers": blockers,
        "authorization_scope": {
            "phase9_a_forensic_replay": False,
            "provider_or_network": False,
            "production_outbox_or_delivery": False,
            "release": False,
            "deployment": False,
            "migration": False,
            "cutover": False,
        },
    }
    body["gate_result_sha256"] = canonical_sha256(body)
    return body


def blocked_phase9_entry_result(
    *,
    candidate: CandidateIdentity,
    evaluated_at: int,
    trusted_now: int | None = None,
    error: BaseException,
    source_verification_sha256: str | None = None,
    p0_evidence_root_sha256: str | None = None,
    p0_receipt_sha256s: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Produce a truthful candidate-bound BLOCKED result for collector failure."""

    if source_verification_sha256 is not None:
        _sha(source_verification_sha256, "source_verification_sha256")
    if p0_evidence_root_sha256 is not None:
        _sha(p0_evidence_root_sha256, "p0_evidence_root_sha256")
    p0_hashes = {} if p0_receipt_sha256s is None else dict(p0_receipt_sha256s)
    if p0_hashes and (
        set(p0_hashes) != set(P0_REQUIREMENTS)
        or any(_sha(value, f"p0_receipt_sha256s.{name}") != value for name, value in p0_hashes.items())
    ):
        raise Phase9EntryError("blocked result P0 receipt hashes differ")

    requested_evaluated_at = _integer(evaluated_at, "evaluated_at")
    now = (
        requested_evaluated_at
        if trusted_now is None
        else _integer(trusted_now, "trusted_now")
    )
    body: dict[str, object] = {
        "schema": PHASE9_ENTRY_GATE_SCHEMA,
        "status": "BLOCKED",
        "evaluated_at": now,
        "request_evaluated_at": requested_evaluated_at,
        "clock_skew_seconds": requested_evaluated_at - now,
        "candidate": candidate.as_dict(),
        "project_id": None,
        "workflow_id": None,
        "run_generation": None,
        "source_inventory_sha256": None,
        "state_receipt_sha256": None,
        "source_verification_sha256": source_verification_sha256,
        "creation_receipt_sha256": None,
        "operator_authorization_receipt_sha256": None,
        "official_input_manifest_sha256": None,
        "official_input_raw_bytes_set_sha256": None,
        "execution_context_receipt_sha256": None,
        "p0_evidence_root_sha256": p0_evidence_root_sha256,
        "p0_receipt_sha256s": p0_hashes,
        "blockers": [
            {
                "code": "STATE_COLLECTION_FAILED",
                "detail": f"{type(error).__name__}: {error}",
            }
        ],
        "authorization_scope": {
            "phase9_a_forensic_replay": False,
            "provider_or_network": False,
            "production_outbox_or_delivery": False,
            "release": False,
            "deployment": False,
            "migration": False,
            "cutover": False,
        },
    }
    body["gate_result_sha256"] = canonical_sha256(body)
    return body
