import json
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from test_evaluate_modeling_project_step8_5 import make_complete_project, write_file
from tests.phase9_delivery_test_support import nonformal_delivery_fence


@pytest.fixture(autouse=True)
def _nonformal_delivery_mechanics(monkeypatch):
    monkeypatch.setattr(
        "factory_core.phase9_delivery_fence.require_phase9_delivery_authority",
        nonformal_delivery_fence,
    )


def make_valid_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("paper.pdf", b"pdf")


def make_current_contract_project(project: Path, *, overridden: bool = False) -> None:
    base = project.name
    root = project.parents[1]
    for name in ("reviewer_entry_map.md", "anchor_figure_plan.md"):
        write_file(project / name, "# ok\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(project / "numbers_manifest.json", "{}\n")
    write_file(project / "results" / "p1" / "values.json", "{\"status\":\"OPTIMAL\",\"objective\":1.0}\n")
    write_file(project / f"{base}_paper.pdf", "pdf\n")
    write_file(
        project / "judge_outputs/final_paper_checks.json",
        '{"schema_version":"final-paper-checks-v1","checks":[],"hard_failures":[]}\n',
    )
    write_file(project / "judge_outputs/visual_gate.json", '{"status":"PASS"}\n')
    write_file(
        project / "judge_outputs/decision_route.json",
        (
            '{"effective_decision":"CONTINUE_TO_STEP16",'
            '"quality_pass_fabricated":false}\n'
            if overridden
            else '{"effective_decision":"PASS","quality_pass_fabricated":false}\n'
        ),
    )
    write_file(
        project / "judge_outputs/judgment_receipt.json",
        '{"status":"VALID"}\n',
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
    if overridden:
        write_file(
            project / "judge_evaluation.md",
            "VERDICT: REOPEN_REVISION_MODEL\n" + "\n".join(["judge"] * 30) + "\n",
        )
    from factory_core.audit.acceptance import build_final_acceptance_receipt
    from factory_core.audit.domain import AuditSnapshot
    from factory_core.delivery.release import ReleasePublisher
    from scripts.submission_fingerprint import (
        submission_fingerprint,
        submission_fingerprint_payload,
    )

    snapshot_id = submission_fingerprint(project, base, policy_mode="enforce")
    identity = submission_fingerprint_payload(project, base, policy_mode="enforce")
    write_file(
        project / "judge_outputs" / "final_submission.sha256",
        snapshot_id + "\n",
    )
    snapshot = AuditSnapshot(
        snapshot_id=snapshot_id,
        base=base,
        profile="final",
        created_at=datetime.now(UTC).isoformat(),
        identity=identity,
    )
    override_id = None
    if overridden:
        from factory_core.governance.overrides import SQLiteOverrideProvider
        from web.backend.auth_store import AuthStore

        store = AuthStore(root / "web/auth.db")
        store.initialize()
        store.bootstrap_admin("correct horse battery staple test only")
        authorization = store.issue_delivery_override(
            base_name=base,
            scope="deliver_snapshot",
            bound_snapshot_id=snapshot_id,
            source_verdict="REOPEN_REVISION_MODEL",
            reason="test exact snapshot authorization",
            actor="admin",
        )
        override_id = authorization.override_id
        provider = SQLiteOverrideProvider(root / "web/auth.db")
        assert provider.consume(override_id) is True
        write_file(
            project / "judge_outputs/delivery_override_receipt.json",
            json.dumps(
                {
                    "schema_version": "delivery-override-receipt-v1",
                    "snapshot_id": snapshot_id,
                    "base": base,
                    "scope": "deliver_snapshot",
                    "source_verdict": "REOPEN_REVISION_MODEL",
                    "quality_pass_fabricated": False,
                    "authorization": asdict(authorization),
                }
            )
            + "\n",
        )
    build_final_acceptance_receipt(
        project,
        snapshot,
        status="OVERRIDDEN" if overridden else "PASS",
        override_receipt=(
            "judge_outputs/delivery_override_receipt.json" if overridden else None
        ),
    )
    write_file(
        project / ".factory" / "audits" / "latest.json",
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "base": base,
                "status": "OVERRIDDEN" if overridden else "PASS",
                "profile": "final",
                "decision": "REOPEN_REVISION_MODEL" if overridden else "PASS",
                "judge_completed": not overridden,
                "delivery_allowed": True,
                "override": overridden,
                "evidence": {"override_id": override_id} if overridden else {},
            }
        )
        + "\n",
    )
    write_file(
        project / ".factory" / "audits" / snapshot_id / "snapshot.json",
        json.dumps(snapshot.to_dict()) + "\n",
    )
    write_file(root / "method_library" / "demo.md", "# demo\n")

    def package(output: Path) -> bool:
        from factory_core.submission_bundle import submission_bundle_manifest

        bundle = submission_bundle_manifest(project, base)
        with zipfile.ZipFile(output, "w") as archive:
            for item in bundle["members"]:
                archive.write(project / item["source_path"], item["archive_path"])
        return True

    ReleasePublisher(root / "papers").publish(
        project,
        snapshot_id,
        status="OVERRIDDEN" if overridden else "PASS",
        package_builder=package,
    )


def test_delivery_manifest_records_contract_and_artifact_hashes(tmp_path, monkeypatch):
    project = tmp_path / "complete" / "demo"
    make_complete_project(project)
    make_current_contract_project(project)

    from scripts import evaluate_modeling_project as evaluator
    from scripts import delivery_contract

    monkeypatch.setattr(evaluator, "infer_step", lambda root, project: (16, "16"))
    monkeypatch.setattr(evaluator, "run_python_check", lambda root, args, timeout=60: (True, "ok"))
    monkeypatch.setattr(evaluator, "symbol_check_ok", lambda root, project, base: (True, "ok"))
    monkeypatch.setattr(
        evaluator.workflow_state,
        "final_judge_is_current",
        lambda project, base=None: True,
    )

    ev = evaluator.evaluate(project, tmp_path)
    manifest = delivery_contract.build_delivery_manifest(project, tmp_path, ev)

    assert manifest["contract_version"] == delivery_contract.CURRENT_CONTRACT_VERSION
    assert manifest["contract_version"].endswith(".atomic_release_v7")
    assert manifest["status"] == "CURRENT_PASS"
    assert manifest["project"]["base"] == "demo"
    assert manifest["evaluation"]["passed"] is True
    assert manifest["artifacts"]["papers_pdf"]["sha256"]
    assert manifest["artifacts"]["submission_zip"]["sha256"]
    assert manifest["artifacts"]["audit_result"]["sha256"]
    assert manifest["evaluation"]["audit_status"] == "PASS"
    assert any(
        check.name == "final_audit_current" and check.ok for check in ev.checks
    )
    assert manifest["evaluation"]["award_prediction"] == "UNAVAILABLE_WITHOUT_HUMAN_CALIBRATION"


def test_delivery_manifest_does_not_mark_gate2_override_as_current_pass(tmp_path, monkeypatch):
    project = tmp_path / "complete" / "demo_override"
    make_complete_project(project)
    make_current_contract_project(project, overridden=True)
    write_file(
        project / "gate2_delivery_override.json",
        '{"enabled": true, "scope": "continue_to_step16", "reason": "user_requested"}\n',
    )

    from scripts import delivery_contract
    from scripts import evaluate_modeling_project as evaluator

    monkeypatch.setattr(evaluator, "infer_step", lambda root, project: (16, "16"))
    monkeypatch.setattr(evaluator, "run_python_check", lambda root, args, timeout=60: (True, "ok"))
    monkeypatch.setattr(evaluator, "symbol_check_ok", lambda root, project, base: (True, "ok"))

    ev = evaluator.evaluate(project, tmp_path)
    manifest = delivery_contract.build_delivery_manifest(project, tmp_path, ev)

    assert ev.passed is True
    assert manifest["status"] == "GATE2_OVERRIDE_DELIVERED"
    assert manifest["evaluation"]["gate2_verdict"] == "REOPEN_REVISION_MODEL"
    assert manifest["evaluation"]["gate2_delivery_override"] is True


def test_current_artifacts_without_snapshot_audit_are_not_deliverable(
    tmp_path, monkeypatch
):
    project = tmp_path / "complete" / "pre_split"
    make_complete_project(project)
    make_current_contract_project(project)
    (project / ".factory" / "audits" / "latest.json").unlink()

    from scripts import delivery_contract
    from scripts import evaluate_modeling_project as evaluator

    monkeypatch.setattr(evaluator, "infer_step", lambda root, project: (16, "16"))
    monkeypatch.setattr(
        evaluator,
        "run_python_check",
        lambda root, args, timeout=60: (True, "ok"),
    )
    monkeypatch.setattr(
        evaluator,
        "symbol_check_ok",
        lambda root, project, base: (True, "ok"),
    )
    monkeypatch.setattr(
        evaluator.workflow_state,
        "final_judge_is_current",
        lambda project, base=None: True,
    )

    ev = evaluator.evaluate(project, tmp_path)

    checks = {check.name: check for check in ev.checks}
    assert checks["final_audit_current"].ok is False
    assert delivery_contract.classify_evaluation(ev, project) == "INVALID_OR_INCOMPLETE"


def test_audit_complete_projects_rejects_legacy_and_invalid(tmp_path, monkeypatch):
    current = tmp_path / "complete" / "current"
    legacy = tmp_path / "complete" / "legacy"
    invalid = tmp_path / "complete" / "invalid"
    for project in (current, legacy, invalid):
        make_complete_project(project)

    make_current_contract_project(current)
    make_valid_zip(tmp_path / "papers" / "legacy_submission.zip")
    (tmp_path / "papers" / "invalid_submission.zip").unlink()

    from scripts import audit_complete_projects
    from scripts import evaluate_modeling_project as evaluator

    def fake_infer(root, project):
        return (16 if project.name == "current" else 15, "fake")

    monkeypatch.setattr(evaluator, "infer_step", fake_infer)
    monkeypatch.setattr(evaluator, "run_python_check", lambda root, args, timeout=60: (True, "ok"))
    monkeypatch.setattr(evaluator, "symbol_check_ok", lambda root, project, base: (True, "ok"))
    monkeypatch.setattr(
        evaluator.workflow_state,
        "final_judge_is_current",
        lambda project, base=None: project.name == "current",
    )

    result = audit_complete_projects.audit_complete_projects(tmp_path / "complete", tmp_path, write_manifests=True)
    statuses = {entry["base"]: entry["status"] for entry in result["projects"]}

    assert statuses["current"] == "CURRENT_PASS"
    assert statuses["legacy"] == "INVALID_OR_INCOMPLETE"
    assert statuses["invalid"] == "INVALID_OR_INCOMPLETE"
    assert (current / "delivery_manifest.json").is_file()
    assert (legacy / "delivery_manifest.json").is_file()
    assert (invalid / "delivery_manifest.json").is_file()
    assert result["summary"] == {
        "CURRENT_PASS": 1,
        "GATE2_OVERRIDE_DELIVERED": 0,
        "LEGACY_DELIVERED": 0,
        "INVALID_OR_INCOMPLETE": 2,
    }
