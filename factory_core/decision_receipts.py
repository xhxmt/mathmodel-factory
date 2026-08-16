from __future__ import annotations

import hashlib
import json
import os
import re
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
    cursor = project
    for component in path_relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            errors.append("decision receipt is or traverses a symlink")
            break
    if not lexical.is_file():
        errors.append("decision receipt is missing or not a regular file")
        return DecisionReceiptVerification(False, tuple(errors), path=relative)
    try:
        lexical.resolve(strict=True).relative_to(project)
    except (OSError, ValueError):
        errors.append("decision receipt resolves outside project")
        return DecisionReceiptVerification(False, tuple(errors), path=relative)

    actual_size = lexical.stat().st_size
    actual_sha256 = _sha256(lexical)
    if reference.get("size") != actual_size:
        errors.append("decision receipt size mismatch")
    if reference.get("sha256") != actual_sha256:
        errors.append("decision receipt SHA-256 mismatch")
    try:
        receipt = json.loads(lexical.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
