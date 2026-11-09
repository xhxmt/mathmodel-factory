from pathlib import Path
from datetime import UTC, datetime
import json
import zipfile

import pytest



def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("paper.pdf", b"pdf")


def mark_final_judge_current(project: Path, base: str) -> None:
    from factory_core.audit.acceptance import build_final_acceptance_receipt
    from factory_core.audit.domain import AuditSnapshot
    from scripts.submission_fingerprint import (
        submission_fingerprint,
        submission_fingerprint_payload,
    )

    write_file(project / f"{base}_paper.tex", "\\begin{document}\nfinal\n\\end{document}\n")
    write_file(project / f"{base}_paper.pdf", "pdf\n")
    write_file(
        project / "judge_outputs/final_paper_checks.json",
        '{"schema_version":"final-paper-checks-v1","checks":[],"hard_failures":[]}\n',
    )
    write_file(project / "logs/compilation/pass3.log", "")
    write_file(project / "logs/compilation/bibliography_backend.log", "")
    from factory_core.bibliography import build_bibliography_receipt

    build_bibliography_receipt(
        project,
        base,
        backend="none",
        backend_version="",
        backend_log="logs/compilation/bibliography_backend.log",
        final_log="logs/compilation/pass3.log",
    )
    fingerprint = submission_fingerprint(project, base, policy_mode="enforce")
    identity = submission_fingerprint_payload(project, base, policy_mode="enforce")
    write_file(
        project / "judge_outputs" / "final_submission.sha256",
        fingerprint + "\n",
    )
    write_file(project / "judge_outputs/visual_gate.json", '{"status":"PASS"}\n')
    write_file(
        project / "judge_outputs/decision_route.json",
        '{"effective_decision":"PASS",'
        '"quality_pass_fabricated":false}\n',
    )
    write_file(
        project / "judge_outputs/judgment_receipt.json",
        '{"status":"VALID"}\n',
    )
    snapshot = AuditSnapshot(
        snapshot_id=fingerprint,
        base=base,
        profile="final",
        created_at=datetime.now(UTC).isoformat(),
        identity=identity,
    )
    build_final_acceptance_receipt(
        project,
        snapshot,
        status="PASS",
    )
    write_file(
        project / ".factory" / "audits" / "latest.json",
        '{"snapshot_id":"'
        + fingerprint
        + '","base":"'
        + base
        + '","status":"PASS","profile":"final",'
        '"decision":"PASS","judge_completed":true'
        + ',"delivery_allowed":true,"override":false}\n',
    )
    write_file(
        project / ".factory" / "audits" / fingerprint / "snapshot.json",
        json.dumps(snapshot.to_dict()) + "\n",
    )


def publish_current(project: Path, root: Path) -> None:
    from factory_core.delivery.release import ReleasePublisher

    snapshot_id = (project / "judge_outputs/final_submission.sha256").read_text(
        encoding="ascii"
    ).strip()

    def package(output: Path) -> bool:
        from factory_core.submission_bundle import submission_bundle_manifest

        bundle = submission_bundle_manifest(project, project.name)
        with zipfile.ZipFile(output, "w") as archive:
            for item in bundle["members"]:
                archive.write(project / item["source_path"], item["archive_path"])
        return True

    ReleasePublisher(root / "papers").publish(
        project,
        snapshot_id,
        status="PASS",
        package_builder=package,
    )


def test_verdict_parser_uses_first_verdict_line(tmp_path):
    path = tmp_path / "judge_evaluation.md"
    write_file(path, "notes\nVERDICT: PASS\r\nVERDICT: REOPEN_REVISION_TEXT\n")

    from scripts.workflow_state import first_verdict

    assert first_verdict(path) == "PASS"


def test_step8_5_and_gate2_pass_predicates(tmp_path):
    project = tmp_path / "project"
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(project / "judge_evaluation.md", "VERDICT: REOPEN_REVISION_MODEL\n")

    from scripts.workflow_state import gate2_passed, step8_5_passed

    assert step8_5_passed(project) is True
    assert gate2_passed(project) is False


def test_precheck_pass_allows_progress_but_not_delivery(tmp_path):
    project = tmp_path / "project"
    write_file(project / "judge_evaluation.md", "VERDICT: PRECHECK_PASS\n")

    from scripts.workflow_state import (
        gate2_delivery_allowed,
        gate2_passed,
        gate2_precheck_passed,
    )

    assert gate2_precheck_passed(project) is True
    assert gate2_passed(project) is False
    assert gate2_delivery_allowed(project) is False


def test_final_audit_current_requires_final_profile(tmp_path):
    project = tmp_path / "project"
    snapshot_id = "e" * 64
    write_file(
        project / ".factory/audits/latest.json",
        '{"snapshot_id":"'
        + snapshot_id
        + '","status":"PASS","profile":"model"}\n',
    )
    write_file(
        project / "judge_outputs/final_submission.sha256", snapshot_id + "\n"
    )

    from scripts.workflow_state import final_audit_is_current

    assert final_audit_is_current(project) is False


def test_delivery_artifacts_and_step16_ready(tmp_path, monkeypatch):
    root = tmp_path
    project = root / "complete" / "demo"
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(project / "judge_evaluation.md", "VERDICT: PASS\n")
    mark_final_judge_current(project, "demo")
    publish_current(project, root)
    write_file(
        project / "gate2_delivery_override.json",
        '{"enabled": true, "scope": "continue_to_step16", "reason": "test"}\n',
    )

    from scripts.workflow_state import delivery_artifacts_ready, step16_ready

    monkeypatch.setattr(
        "scripts.judgment_receipt.verify_receipt",
        lambda *_args, **_kwargs: (True, []),
    )
    assert delivery_artifacts_ready(root, "demo", project=project) is True
    assert step16_ready(project, root, "demo") is True

    write_file(project / "demo_paper.tex", "\\begin{document}\nchanged after judging\n\\end{document}\n")
    assert step16_ready(project, root, "demo") is False


def test_no_judge_ablation_blocks_step16_even_with_delivery_override(tmp_path):
    root = tmp_path
    project = root / "ongoing" / "demo"
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(project / "judge_evaluation.md", "VERDICT: REOPEN_REVISION_MODEL\n")
    mark_final_judge_current(project, "demo")
    publish_current(project, root)
    snapshot_id = (
        project / "judge_outputs/final_submission.sha256"
    ).read_text(encoding="ascii").strip()
    from web.backend.auth_store import AuthStore

    store = AuthStore(root / "web/auth.db")
    store.initialize()
    store.bootstrap_admin("correct horse battery staple test only")
    store.issue_delivery_override(
        base_name="demo",
        scope="deliver_snapshot",
        bound_snapshot_id=snapshot_id,
        source_verdict="REOPEN_REVISION_MODEL",
        reason="test ablation fail-closed precedence",
        actor="admin",
    )
    latest_path = project / ".factory/audits/latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    latest.update(
        {
            "status": "OVERRIDDEN",
            "decision": "ABLATE_NO_JUDGE",
            "judge_completed": False,
            "delivery_allowed": False,
            "error_class": "PERMANENT_ABLATION_NO_DELIVERY",
            "returncode": 2,
        }
    )
    write_file(latest_path, json.dumps(latest) + "\n")
    write_file(
        project / "judge_outputs/final_submission.ablation.json",
        json.dumps(
            {
                "ablation": "ABLATE_NO_JUDGE",
                "judge_executed": False,
                "snapshot_id": snapshot_id,
                "quality_pass_fabricated": False,
                "delivery_allowed": False,
            }
        )
        + "\n",
    )

    from scripts.workflow_state import (
        delivered_snapshot_override,
        final_audit_is_current,
        gate2_delivery_allowed,
        gate2_delivery_override,
        gate2_passed,
        step16_ready,
    )
    from factory_core.delivery.release import resolve_current_release

    assert gate2_passed(project) is False
    assert gate2_delivery_override(project) is True
    assert delivered_snapshot_override(project) is False
    assert gate2_delivery_allowed(project) is False
    assert final_audit_is_current(project) is False
    assert step16_ready(project, root, "demo") is False
    current = resolve_current_release(
        root / "papers", "demo", project=project
    )
    assert current is not None
    assert current.release_id == snapshot_id


def _entry_gate_project(tmp_path, *, catalog: str | None, verdict: str = "PASS"):
    import os

    project = tmp_path / "ongoing" / "demo"
    if catalog is not None:
        from factory_core.stages import STAGE_SCHEDULER_GENERATION
        from factory_core.storage import SQLiteStateStore

        SQLiteStateStore(project, clock=lambda: 100).initialize(
            project_id="demo",
            project_type="modeling",
            scheduler_generation=STAGE_SCHEDULER_GENERATION,
            stage_catalog_version=catalog,
        )
    for name in ("reviewer_entry_map.md", "anchor_figure_plan.md"):
        write_file(project / name, "# gate\n")
    write_file(project / "entry_gate.md", f"VERDICT: {verdict}\n")
    write_file(project / "solve_log.md", "solved\n")
    # The reports the gate read changed after it (a review-issue recompute
    # or a Step 12 forward repair).
    later = (project / "entry_gate.md").stat().st_mtime + 60
    os.utime(project / "solve_log.md", (later, later))
    return project


def test_v2_delivery_accepts_a_passed_entry_gate_whose_inputs_changed_later(tmp_path):
    # s16 rev 856: a v2 review-issue recompute re-ran Steps 5-8 without the
    # reviewer-entry gate, so Step 16 could never become ready.
    from factory_core.stages import STAGE_CATALOG_VERSION_V2
    from scripts.workflow_state import (
        reviewer_entry_gate_satisfied_for_delivery,
        step8_5_passed,
    )

    project = _entry_gate_project(tmp_path, catalog=STAGE_CATALOG_VERSION_V2)

    assert step8_5_passed(project) is False  # stale by mtime
    assert reviewer_entry_gate_satisfied_for_delivery(project) is True


def test_v2_delivery_still_needs_a_passed_complete_entry_gate(tmp_path):
    from factory_core.stages import STAGE_CATALOG_VERSION_V2
    from scripts.workflow_state import reviewer_entry_gate_satisfied_for_delivery

    revise = _entry_gate_project(tmp_path / "a", catalog=STAGE_CATALOG_VERSION_V2, verdict="REVISE")
    assert reviewer_entry_gate_satisfied_for_delivery(revise) is False

    missing = _entry_gate_project(tmp_path / "b", catalog=STAGE_CATALOG_VERSION_V2)
    (missing / "anchor_figure_plan.md").unlink()
    assert reviewer_entry_gate_satisfied_for_delivery(missing) is False


def test_v1_delivery_still_needs_a_current_entry_gate(tmp_path):
    from factory_core.stages import STAGE_CATALOG_VERSION
    from scripts.workflow_state import reviewer_entry_gate_satisfied_for_delivery

    v1 = _entry_gate_project(tmp_path / "a", catalog=STAGE_CATALOG_VERSION)
    assert reviewer_entry_gate_satisfied_for_delivery(v1) is False
    no_state = _entry_gate_project(tmp_path / "b", catalog=None)
    assert reviewer_entry_gate_satisfied_for_delivery(no_state) is False
