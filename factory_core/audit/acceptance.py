from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .domain import AuditSnapshot
from .persistence import atomic_write_json, utc_now


SCHEMA_VERSION = "final-acceptance-receipt-v3"
RECEIPT_PATH = Path("judge_outputs/final_acceptance_receipt.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record(project: Path, relative: str) -> dict[str, object]:
    path = (project / relative).resolve()
    path.relative_to(project.resolve())
    if not path.is_file():
        raise ValueError(f"final acceptance artifact is missing: {relative}")
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def build_final_acceptance_receipt(
    project: Path,
    snapshot: AuditSnapshot,
    *,
    status: str,
    override_receipt: str | None = None,
) -> dict[str, object]:
    project = project.resolve()
    from ..decision_receipts import verified_approval_receipts

    approvals = verified_approval_receipts(project)
    production_snapshot = snapshot.identity.get("source") != "injected_fingerprinter"
    if production_snapshot and snapshot.identity.get("approval_receipts") != approvals:
        raise ValueError("audit snapshot approval receipt identity changed")
    artifacts = {
        "pdf": _record(project, f"{project.name}_paper.pdf"),
        "paper_checks": _record(project, "judge_outputs/final_paper_checks.json"),
        "visual_gate": _record(project, "judge_outputs/visual_gate.json"),
        "decision_route": _record(project, "judge_outputs/decision_route.json"),
    }
    if production_snapshot:
        from ..bibliography import (
            BIBLIOGRAPHY_RECEIPT_PATH,
            verify_bibliography_receipt,
        )

        bibliography_valid, bibliography_errors, _ = verify_bibliography_receipt(
            project, project.name
        )
        if not bibliography_valid:
            raise ValueError(
                "bibliography build evidence is invalid: "
                + "; ".join(bibliography_errors)
            )
        from ..bibliography import bibliography_evidence_record

        if snapshot.identity.get("bibliography_build") != bibliography_evidence_record(
            project, project.name
        ):
            raise ValueError("audit snapshot bibliography evidence changed")
        artifacts["bibliography_build_receipt"] = _record(
            project, BIBLIOGRAPHY_RECEIPT_PATH.as_posix()
        )
    if status == "PASS":
        artifacts["judgment_receipt"] = _record(
            project, "judge_outputs/judgment_receipt.json"
        )
    elif status == "OVERRIDDEN" and override_receipt:
        artifacts["override_receipt"] = _record(project, override_receipt)
    else:
        raise ValueError("final acceptance receipt requires PASS or a bound override")

    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "base": project.name,
        "status": status,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_identity_sha256": _canonical_hash(snapshot.identity),
        "submission_bundle": {},
        "bibliography_build_required": production_snapshot,
        "approval_receipts": approvals,
        "artifacts": artifacts,
    }
    from ..submission_bundle import submission_bundle_manifest

    bundle = submission_bundle_manifest(project, project.name)
    receipt["submission_bundle"] = {
        "schema_version": bundle["schema_version"],
        "manifest_sha256": bundle["manifest_sha256"],
        "member_count": len(bundle["members"]),
    }
    receipt["content_sha256"] = _canonical_hash(receipt)
    atomic_write_json(project / RECEIPT_PATH, receipt)
    return receipt


def verify_final_acceptance_receipt(
    project: Path,
    snapshot: AuditSnapshot | None = None,
    *,
    expected_snapshot_id: str | None = None,
    expected_status: str | None = None,
) -> tuple[bool, list[str]]:
    project = project.resolve()
    errors: list[str] = []
    try:
        receipt = json.loads((project / RECEIPT_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, ["final acceptance receipt is missing or invalid"]
    if not isinstance(receipt, dict):
        return False, ["final acceptance receipt root must be an object"]
    declared_hash = receipt.get("content_sha256")
    unsigned = dict(receipt)
    unsigned.pop("content_sha256", None)
    if declared_hash != _canonical_hash(unsigned):
        errors.append("final acceptance receipt content hash mismatch")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        errors.append("final acceptance receipt schema mismatch")
    if receipt.get("base") != project.name:
        errors.append("final acceptance receipt base mismatch")
    if expected_snapshot_id is not None and receipt.get("snapshot_id") != expected_snapshot_id:
        errors.append("final acceptance receipt snapshot mismatch")
    if expected_status is not None and receipt.get("status") != expected_status:
        errors.append("final acceptance receipt status mismatch")
    if snapshot is not None:
        if receipt.get("snapshot_id") != snapshot.snapshot_id:
            errors.append("final acceptance receipt does not bind the current snapshot")
        if receipt.get("snapshot_identity_sha256") != _canonical_hash(snapshot.identity):
            errors.append("final acceptance snapshot identity changed")

    try:
        from ..decision_receipts import verified_approval_receipts

        current_approvals = verified_approval_receipts(project)
        if receipt.get("approval_receipts") != current_approvals:
            errors.append("final acceptance approval receipts changed")
        if snapshot is not None and snapshot.identity.get(
            "source"
        ) != "injected_fingerprinter" and snapshot.identity.get(
            "approval_receipts"
        ) != current_approvals:
            errors.append("audit snapshot does not bind current approval receipts")
    except (OSError, ValueError) as exc:
        errors.append(f"final acceptance approval receipt is invalid: {exc}")

    try:
        from ..submission_bundle import submission_bundle_manifest

        current_bundle = submission_bundle_manifest(project, project.name)
        expected_bundle = {
            "schema_version": current_bundle["schema_version"],
            "manifest_sha256": current_bundle["manifest_sha256"],
            "member_count": len(current_bundle["members"]),
        }
        if receipt.get("submission_bundle") != expected_bundle:
            errors.append("final acceptance submission bundle changed")
    except (OSError, ValueError) as exc:
        errors.append(f"final acceptance submission bundle is invalid: {exc}")

    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("final acceptance artifacts are invalid")
        return False, errors
    required = {"pdf", "paper_checks", "visual_gate", "decision_route"}
    if receipt.get("bibliography_build_required") is True:
        required.add("bibliography_build_receipt")
        try:
            from ..bibliography import verify_bibliography_receipt

            bibliography_valid, bibliography_errors, _ = (
                verify_bibliography_receipt(project, project.name)
            )
            if not bibliography_valid:
                errors.append(
                    "final acceptance bibliography evidence is invalid: "
                    + "; ".join(bibliography_errors)
                )
        except (OSError, ValueError) as exc:
            errors.append(f"final acceptance bibliography verification failed: {exc}")
    if receipt.get("status") == "PASS":
        required.add("judgment_receipt")
    elif receipt.get("status") == "OVERRIDDEN":
        required.add("override_receipt")
    if set(artifacts) != required:
        errors.append("final acceptance artifacts do not match the status contract")
    for name, record in artifacts.items():
        if not isinstance(record, dict):
            errors.append(f"final acceptance artifact {name} is invalid")
            continue
        relative = record.get("path")
        if not isinstance(relative, str) or Path(relative).is_absolute():
            errors.append(f"final acceptance artifact {name} path is invalid")
            continue
        try:
            path = (project / relative).resolve()
            path.relative_to(project)
        except ValueError:
            errors.append(f"final acceptance artifact {name} escapes project")
            continue
        if not path.is_file():
            errors.append(f"final acceptance artifact {name} is missing")
            continue
        if record.get("bytes") != path.stat().st_size or record.get("sha256") != _sha256(path):
            errors.append(f"final acceptance artifact changed: {relative}")
    return not errors, errors
