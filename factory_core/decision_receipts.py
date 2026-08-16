from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


DECISION_RECEIPT_SCHEMA = "factory-human-decision-receipt-v1"


@dataclass(frozen=True)
class DecisionReceiptVerification:
    valid: bool
    errors: tuple[str, ...]
    path: str | None = None
    sha256: str | None = None
    legacy_unbound: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "path": self.path,
            "sha256": self.sha256,
            "legacy_unbound": self.legacy_unbound,
        }


def _read_contained_once(project: Path, relative: Path) -> bytes:
    """Read one regular file through an O_NOFOLLOW descriptor chain."""

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(project, directory_flags)
    try:
        for component in relative.parts[:-1]:
            next_descriptor = os.open(
                component,
                directory_flags | nofollow,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            relative.parts[-1], os.O_RDONLY | nofollow, dir_fd=descriptor
        )
        try:
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise OSError("receipt is not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(file_descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise OSError("receipt changed while it was being read")
            data = b"".join(chunks)
            if len(data) != after.st_size:
                raise OSError("receipt size changed while it was being read")
            return data
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _decision_identity(decision: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {
        "artifact_refs",
        "projection_refs",
        "decision_id",
        "outcome",
        "receipt_verification",
    }
    return {key: value for key, value in decision.items() if key not in excluded}


def verify_decision_receipt(
    project_dir: str | Path,
    decision: Mapping[str, Any],
) -> DecisionReceiptVerification:
    """Verify receipt bytes, path safety, schema, and database identity."""

    project = Path(project_dir).resolve()
    if decision.get("subject_fingerprint") == "LEGACY_UNBOUND":
        if decision.get("kind") == "approval":
            return DecisionReceiptVerification(
                False,
                ("legacy approval has no immutable content-bound receipt",),
                legacy_unbound=True,
            )
        return DecisionReceiptVerification(
            True, (), legacy_unbound=True
        )

    errors: list[str] = []
    refs = decision.get("artifact_refs")
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], Mapping):
        return DecisionReceiptVerification(
            False, ("decision must reference exactly one immutable receipt",)
        )
    reference = refs[0]
    relative = reference.get("path")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return DecisionReceiptVerification(False, ("decision receipt path is invalid",))
    relative_path = Path(relative)
    if ".." in relative_path.parts or "\\" in relative:
        return DecisionReceiptVerification(
            False, ("decision receipt path escapes its evidence root",), path=relative
        )
    expected_gate = re.sub(r"[^A-Za-z0-9._-]", "_", str(decision.get("gate") or ""))
    expected_relative = Path(
        ".factory",
        "decisions",
        expected_gate,
        str(decision.get("request_id") or ""),
        f"{decision.get('decision_id')}.json",
    ).as_posix()
    if relative != expected_relative:
        errors.append("decision receipt path does not match database identity")

    lexical = Path(os.path.abspath(project / relative_path))
    try:
        path_relative = lexical.relative_to(project)
    except ValueError:
        return DecisionReceiptVerification(
            False, tuple(errors + ["decision receipt escapes project"]), path=relative
        )
    try:
        data = _read_contained_once(project, path_relative)
    except OSError:
        errors.append(
            "decision receipt is missing, non-regular, or traverses a symlink"
        )
        return DecisionReceiptVerification(False, tuple(errors), path=relative)

    actual_size = len(data)
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if reference.get("size") != actual_size:
        errors.append("decision receipt size mismatch")
    if reference.get("sha256") != actual_sha256:
        errors.append("decision receipt SHA-256 mismatch")
    try:
        receipt = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        errors.append("decision receipt JSON is invalid")
        return DecisionReceiptVerification(
            False, tuple(errors), path=relative, sha256=actual_sha256
        )
    if not isinstance(receipt, dict):
        errors.append("decision receipt root is not an object")
        return DecisionReceiptVerification(
            False, tuple(errors), path=relative, sha256=actual_sha256
        )
    expected_fields = {
        "schema_version": DECISION_RECEIPT_SCHEMA,
        "decision_id": decision.get("decision_id"),
        "request_id": decision.get("request_id"),
        "gate": decision.get("gate"),
        "generation": decision.get("generation"),
        "kind": decision.get("kind"),
        "outcome": decision.get("outcome"),
        "approved": decision.get("approved"),
        "subject_fingerprint": decision.get("subject_fingerprint"),
        "options_fingerprint": decision.get("options_fingerprint"),
        "decision": _decision_identity(decision),
        "projection_refs": decision.get("projection_refs") or [],
    }
    for field, expected in expected_fields.items():
        if receipt.get(field) != expected:
            errors.append(f"decision receipt {field} mismatch")
    decided_at = receipt.get("decided_at")
    if not isinstance(decided_at, int) or isinstance(decided_at, bool):
        errors.append("decision receipt decided_at is invalid")
    return DecisionReceiptVerification(
        not errors, tuple(errors), path=relative, sha256=actual_sha256
    )


def verified_approval_receipts(
    project_dir: str | Path,
    *,
    require_content_freeze: bool | None = None,
) -> list[dict[str, Any]]:
    """Return exact current approval receipts consumed by finalization."""

    project = Path(project_dir).resolve()
    from .storage import SQLiteStateStore

    store = SQLiteStateStore(project)
    if not store.exists:
        return []
    if require_content_freeze is None:
        require_content_freeze = store.contest_policy() is not None
    records: list[dict[str, Any]] = []
    for gate in ("content_freeze", "delivery_freeze_override"):
        decision = store.decision(gate)
        if decision is None or decision.get("approved") is not True:
            if gate == "content_freeze" and require_content_freeze:
                raise ValueError("verified content-freeze approval receipt is required")
            continue
        verification = verify_decision_receipt(project, decision)
        if not verification.valid:
            raise ValueError(
                f"{gate} approval receipt is invalid: "
                + "; ".join(verification.errors)
            )
        reference = decision["artifact_refs"][0]
        records.append(
            {
                "gate": gate,
                "request_id": decision.get("request_id"),
                "decision_id": decision.get("decision_id"),
                "generation": decision.get("generation"),
                "path": reference.get("path"),
                "size": reference.get("size"),
                "sha256": reference.get("sha256"),
                "subject_fingerprint": decision.get("subject_fingerprint"),
                "options_fingerprint": decision.get("options_fingerprint"),
            }
        )
    return records
