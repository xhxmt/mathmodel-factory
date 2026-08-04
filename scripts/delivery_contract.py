#!/usr/bin/env python3
"""Shared delivery-contract helpers for Modeling Factory projects."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import evaluate_modeling_project, workflow_state


CURRENT_CONTRACT_VERSION = "2026-08-04.incremental_audit_v6"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    commit = proc.stdout.strip()
    return commit if proc.returncode == 0 and commit else None


def check_map(ev: evaluate_modeling_project.Evaluation) -> dict[str, evaluate_modeling_project.Check]:
    return {check.name: check for check in ev.checks}


def load_audit_record(project: Path) -> dict[str, Any]:
    return workflow_state.final_audit_record(project)


def audit_record_is_current(
    project: Path, record: dict[str, Any] | None = None
) -> bool:
    if record is not None and record != workflow_state.final_audit_record(project):
        return False
    return workflow_state.final_audit_is_current(project)


def classify_evaluation(ev: evaluate_modeling_project.Evaluation, project: Path | None = None) -> str:
    checks = check_map(ev)
    audit = load_audit_record(project) if project is not None else {}
    if (
        ev.passed
        and ev.inferred_step == 16
        and project is not None
        and audit_record_is_current(project, audit)
    ):
        if project is not None and workflow_state.gate2_delivery_override(project) \
                and not workflow_state.gate2_passed(project):
            return "GATE2_OVERRIDE_DELIVERED"
        if audit.get("status") == "OVERRIDDEN":
            return "GATE2_OVERRIDE_DELIVERED"
        return "CURRENT_PASS"

    delivered_checks = ("papers_pdf", "submission_zip")
    if all(checks.get(name) and checks[name].ok for name in delivered_checks):
        return "LEGACY_DELIVERED"

    return "INVALID_OR_INCOMPLETE"


def artifact_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.is_file(),
        "bytes": path.stat().st_size if path.is_file() else 0,
        "sha256": sha256_file(path),
    }


def build_delivery_manifest(
    project: Path,
    root: Path,
    ev: evaluate_modeling_project.Evaluation,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    project = project.resolve()
    root = root.resolve()
    base = project.name
    failed_checks = [check.__dict__ for check in ev.checks if not check.ok and check.severity != "warning"]

    judgment_receipt_path = project / "judge_outputs" / "judgment_receipt.json"
    decision_route_path = project / "judge_outputs" / "decision_route.json"
    visual_gate_path = project / "judge_outputs" / "visual_gate.json"
    audit_result_path = project / ".factory" / "audits" / "latest.json"

    def load_object(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    receipt = load_object(judgment_receipt_path)
    route = load_object(decision_route_path)
    visual = load_object(visual_gate_path)
    audit = load_object(audit_result_path)
    snapshot_id = audit.get("snapshot_id")
    audit_snapshot_path = (
        project / ".factory" / "audits" / snapshot_id / "snapshot.json"
        if isinstance(snapshot_id, str)
        and len(snapshot_id) == 64
        and all(character in "0123456789abcdef" for character in snapshot_id)
        else project / ".factory" / "audits" / "invalid-snapshot.json"
    )

    return {
        "contract_version": CURRENT_CONTRACT_VERSION,
        "generated_at": generated_at or utc_now(),
        "status": classify_evaluation(ev, project),
        "runner_commit": git_commit(root),
        "project": {
            "base": base,
            "path": str(project),
        },
        "evaluation": {
            "inferred_step": ev.inferred_step,
            "passed": ev.passed,
            "gate2_verdict": workflow_state.gate2_verdict(project),
            "gate2_passed": workflow_state.gate2_passed(project),
            "gate2_delivery_override": workflow_state.gate2_delivery_override(project),
            "judge_policy_mode": route.get("policy_mode"),
            "new_judge_decision": route.get("new_decision"),
            "effective_judge_decision": route.get("effective_decision"),
            "visual_gate_status": visual.get("status"),
            "judgment_receipt_status": receipt.get("status"),
            "audit_status": audit.get("status"),
            "audit_snapshot": snapshot_id,
            "audit_profile": audit.get("profile"),
            "audit_decision": audit.get("decision"),
            "human_alignment": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
            "award_prediction": "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION",
            "failed_checks": failed_checks,
        },
        "artifacts": {
            "project_pdf": artifact_record(project / f"{base}_paper.pdf"),
            "papers_pdf": artifact_record(root / "papers" / f"{base}_paper.pdf"),
            "submission_zip": artifact_record(root / "papers" / f"{base}_submission.zip"),
            "judgment_receipt": artifact_record(judgment_receipt_path),
            "decision_route": artifact_record(decision_route_path),
            "visual_gate": artifact_record(visual_gate_path),
            "audit_result": artifact_record(audit_result_path),
            "audit_snapshot": artifact_record(audit_snapshot_path),
        },
    }


def write_delivery_manifest(project: Path, root: Path, output: Path | None = None) -> dict[str, Any]:
    ev = evaluate_modeling_project.evaluate(project, root)
    manifest = build_delivery_manifest(project, root, ev)
    target = output or (project / "delivery_manifest.json")
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Path to complete/<base> or ongoing/<base> project directory.")
    parser.add_argument("--root", default=None, help="Factory root. Defaults to the repository root.")
    parser.add_argument("--output", default=None, help="Manifest path. Defaults to <project>/delivery_manifest.json.")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else evaluate_modeling_project.repo_root()
    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"Project directory not found: {project}", flush=True)
        return 2

    manifest = write_delivery_manifest(project, root, Path(args.output).resolve() if args.output else None)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] in {"CURRENT_PASS", "GATE2_OVERRIDE_DELIVERED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
