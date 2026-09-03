#!/usr/bin/env python3
"""Shared file-state predicates for Modeling Factory workflow gates."""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.step8_5_gate import collect_step8_5_state
from scripts.submission_fingerprint import final_judge_is_current
from factory_core.governance.overrides import (
    CONTINUE_AFTER_GATE2,
    DELIVER_SNAPSHOT,
    default_override_provider,
)


VERDICT_RE = re.compile(r"^VERDICT:\s*(\S+)", re.M)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def first_verdict(path: Path) -> str | None:
    if not path.is_file():
        return None
    match = VERDICT_RE.search(read_text(path).replace("\r\n", "\n"))
    return match.group(1).strip() if match else None


def gate2_verdict(project: Path) -> str | None:
    return first_verdict(project / "judge_evaluation.md")


def gate2_passed(project: Path) -> bool:
    return gate2_verdict(project) == "PASS"


def gate2_precheck_passed(project: Path) -> bool:
    return gate2_verdict(project) in {"PRECHECK_PASS", "PASS"}


def factory_root_for(project: Path) -> Path:
    resolved = project.resolve()
    if resolved.parent.name in {"ongoing", "complete"}:
        return resolved.parents[1]
    return resolved.parent


def gate2_continuation_override(project: Path, root: Path | None = None) -> bool:
    provider = default_override_provider(root or factory_root_for(project))
    return (
        provider.active_override(project.name, CONTINUE_AFTER_GATE2) is not None
    )


def gate2_delivery_override(
    project: Path,
    root: Path | None = None,
    snapshot_id: str | None = None,
) -> bool:
    if snapshot_id is None:
        try:
            snapshot_id = read_text(
                project / "judge_outputs/final_submission.sha256"
            ).strip()
        except OSError:
            return False
    provider = default_override_provider(root or factory_root_for(project))
    return (
        provider.active_override(
            project.name,
            DELIVER_SNAPSHOT,
            snapshot_id=snapshot_id,
        )
        is not None
    )


def gate2_delivery_allowed(project: Path, root: Path | None = None) -> bool:
    record = final_audit_record(project)
    if record.get("decision") == "ABLATE_NO_JUDGE":
        return False
    return (
        gate2_passed(project)
        or gate2_delivery_override(project, root)
        or delivered_snapshot_override(project, root)
    )


def delivered_snapshot_override(
    project: Path, root: Path | None = None
) -> bool:
    try:
        from factory_core.audit.acceptance import verify_final_acceptance_receipt

        audit = final_audit_record(project)
        if audit.get("decision") == "ABLATE_NO_JUDGE":
            return False
        receipt = json.loads(
            read_text(project / "judge_outputs/final_acceptance_receipt.json")
        )
        snapshot_id = str(receipt.get("snapshot_id") or "")
        valid, _errors = verify_final_acceptance_receipt(
            project,
            expected_snapshot_id=snapshot_id,
            expected_status="OVERRIDDEN",
        )
        evidence = audit.get("evidence")
        override_id = (
            evidence.get("override_id") if isinstance(evidence, dict) else None
        )
        override_receipt = json.loads(
            read_text(project / "judge_outputs/delivery_override_receipt.json")
        )
        authorization = override_receipt.get("authorization")
        if not isinstance(authorization, dict):
            return False
        if authorization.get("override_id") != override_id:
            return False
        provider = default_override_provider(root or factory_root_for(project))
        record = provider.get_override(str(override_id or ""))
        return bool(
            valid
            and record is not None
            and record.base_name == project.name
            and record.scope == DELIVER_SNAPSHOT
            and record.bound_snapshot_id == snapshot_id
            and record.revoked_at is None
            and record.consumed_at is not None
        )
    except (OSError, json.JSONDecodeError, AttributeError, ValueError):
        return False


def final_audit_record(project: Path) -> dict[str, Any]:
    path = project / ".factory" / "audits" / "latest.json"
    try:
        value = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def final_audit_is_current(project: Path, root: Path | None = None) -> bool:
    record = final_audit_record(project)
    snapshot_id = record.get("snapshot_id")
    status = record.get("status")
    if record.get("profile") != "final":
        return False
    if record.get("decision") == "ABLATE_NO_JUDGE":
        return False
    if status not in {"PASS", "OVERRIDDEN"}:
        return False
    if record.get("delivery_allowed") is not True:
        return False
    if status == "PASS" and (
        record.get("decision") != "PASS"
        or record.get("judge_completed") is not True
    ):
        return False
    if not isinstance(snapshot_id, str) or len(snapshot_id) != 64:
        return False
    try:
        int(snapshot_id, 16)
        current = read_text(project / "judge_outputs/final_submission.sha256").strip()
    except (OSError, ValueError):
        return False
    if current != snapshot_id:
        return False
    try:
        from factory_core.audit.acceptance import verify_final_acceptance_receipt

        acceptance_valid, _errors = verify_final_acceptance_receipt(
            project,
            expected_snapshot_id=snapshot_id,
            expected_status=status,
        )
    except (ImportError, OSError, ValueError):
        return False
    if not acceptance_valid:
        return False
    if status == "OVERRIDDEN":
        try:
            route = json.loads(read_text(project / "judge_outputs/decision_route.json"))
        except (json.JSONDecodeError, OSError):
            return False
        return (
            delivered_snapshot_override(project, root)
            and route.get("effective_decision") == "CONTINUE_TO_STEP16"
            and route.get("quality_pass_fabricated") is False
        )
    return True


def step8_5_verdict(project: Path) -> str | None:
    state = collect_step8_5_state(project)
    return state.get("effective_verdict")


def step8_5_passed(project: Path) -> bool:
    state = collect_step8_5_state(project)
    return bool(state.get("ready"))


def zip_file_ok(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except zipfile.BadZipFile:
        return False


def delivery_artifacts_ready(root: Path, base: str, *, project: Path) -> bool:
    from factory_core.delivery.release import current_release_artifacts

    current = current_release_artifacts(
        root / "papers", base, project=project
    )
    if current is not None:
        papers_pdf, submission_zip = current
        return (
            papers_pdf.is_file()
            and papers_pdf.stat().st_size > 0
            and zip_file_ok(submission_zip)
        )
    return False


def step16_ready(project: Path, root: Path, base: str | None = None) -> bool:
    from factory_core.delivery.release import resolve_current_release

    resolved_base = base or project.name
    return (
        resolve_current_release(
            root / "papers", resolved_base, project=project
        ) is not None
        and gate2_delivery_allowed(project, root)
        and step8_5_passed(project)
        and final_audit_is_current(project, root)
        and final_judge_is_current(project, resolved_base)
    )


def collect_state(project: Path, root: Path, base: str | None = None) -> dict[str, Any]:
    resolved_base = base or project.name
    return {
        "base": resolved_base,
        "gate2_verdict": gate2_verdict(project),
        "gate2_precheck_passed": gate2_precheck_passed(project),
        "gate2_passed": gate2_passed(project),
        "gate2_continuation_override": gate2_continuation_override(project, root),
        "gate2_delivery_override": gate2_delivery_override(project, root),
        "gate2_delivery_allowed": gate2_delivery_allowed(project, root),
        "final_audit": final_audit_record(project),
        "final_audit_current": final_audit_is_current(project, root),
        "step8_5": collect_step8_5_state(project),
        "delivery_artifacts_ready": delivery_artifacts_ready(
            root, resolved_base, project=project
        ),
        "final_submission_judge_current": final_judge_is_current(project, resolved_base),
        "step16_ready": step16_ready(project, root, resolved_base),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gate2-verdict")
    p.add_argument("project")

    p = sub.add_parser("gate2-passed")
    p.add_argument("project")

    p = sub.add_parser("gate2-delivery-allowed")
    p.add_argument("project")
    p.add_argument("--root", default=None)

    p = sub.add_parser("gate2-continuation-allowed")
    p.add_argument("project")
    p.add_argument("--root", default=None)

    p = sub.add_parser("gate2-delivery-override")
    p.add_argument("project")
    p.add_argument("--root", default=None)
    p.add_argument("--snapshot", required=True)

    p = sub.add_parser("step8_5-verdict")
    p.add_argument("project")

    p = sub.add_parser("step8_5-passed")
    p.add_argument("project")

    p = sub.add_parser("step16-ready")
    p.add_argument("project")
    p.add_argument("--root", required=True)
    p.add_argument("--base", default=None)

    p = sub.add_parser("json")
    p.add_argument("project")
    p.add_argument("--root", required=True)
    p.add_argument("--base", default=None)

    args = parser.parse_args()
    project = Path(args.project)

    if args.command == "gate2-verdict":
        print(gate2_verdict(project) or "")
        return 0
    if args.command == "gate2-passed":
        return 0 if gate2_passed(project) else 1
    if args.command == "gate2-delivery-allowed":
        return 0 if gate2_delivery_allowed(
            project, Path(args.root) if args.root else None
        ) else 1
    if args.command == "gate2-continuation-allowed":
        return 0 if gate2_continuation_override(
            project, Path(args.root) if args.root else None
        ) else 1
    if args.command == "gate2-delivery-override":
        return 0 if gate2_delivery_override(
            project,
            Path(args.root) if args.root else None,
            args.snapshot,
        ) else 1
    if args.command == "step8_5-verdict":
        print(step8_5_verdict(project) or "")
        return 0
    if args.command == "step8_5-passed":
        return 0 if step8_5_passed(project) else 1
    if args.command == "step16-ready":
        return 0 if step16_ready(project, Path(args.root), args.base) else 1
    if args.command == "json":
        print(json.dumps(collect_state(project, Path(args.root), args.base), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
