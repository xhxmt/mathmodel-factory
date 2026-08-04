import json
import zipfile
from pathlib import Path

from test_evaluate_modeling_project_step8_5 import make_complete_project, write_file


def make_valid_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("paper.pdf", b"pdf")


def make_current_contract_project(project: Path) -> None:
    base = project.name
    root = project.parents[1]
    for name in ("reviewer_entry_map.md", "anchor_figure_plan.md"):
        write_file(project / name, "# ok\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(project / "numbers_manifest.json", "{}\n")
    write_file(project / "results" / "p1" / "values.json", "{\"status\":\"OPTIMAL\",\"objective\":1.0}\n")
    write_file(project / f"{base}_paper.pdf", "pdf\n")
    from scripts.submission_fingerprint import submission_fingerprint

    write_file(
        project / "judge_outputs" / "final_submission.sha256",
        submission_fingerprint(project, base) + "\n",
    )
    snapshot_id = (project / "judge_outputs/final_submission.sha256").read_text(
        encoding="utf-8"
    ).strip()
    write_file(
        project / ".factory" / "audits" / "latest.json",
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "status": "PASS",
                "profile": "final",
                "decision": "PASS",
                "judge_completed": True,
                "delivery_allowed": True,
            }
        )
        + "\n",
    )
    write_file(
        project / ".factory" / "audits" / snapshot_id / "snapshot.json",
        json.dumps({"snapshot_id": snapshot_id}) + "\n",
    )
    write_file(root / "method_library" / "demo.md", "# demo\n")
    make_valid_zip(root / "papers" / f"{base}_submission.zip")


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
    assert manifest["contract_version"].endswith(".incremental_audit_v6")
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
    make_current_contract_project(project)
    write_file(
        project / "judge_evaluation.md",
        "VERDICT: REOPEN_REVISION_MODEL\n" + "\n".join(["judge"] * 30) + "\n",
    )
    write_file(
        project / "gate2_delivery_override.json",
        '{"enabled": true, "scope": "continue_to_step16", "reason": "user_requested"}\n',
    )
    audit_path = project / ".factory" / "audits" / "latest.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["status"] = "OVERRIDDEN"
    audit["decision"] = "REOPEN_REVISION_MODEL"
    audit_path.write_text(json.dumps(audit) + "\n", encoding="utf-8")
    write_file(
        project / "judge_outputs" / "decision_route.json",
        '{"effective_decision":"CONTINUE_TO_STEP16",'
        '"quality_pass_fabricated":false}\n',
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


def test_current_artifacts_without_snapshot_audit_are_legacy(tmp_path, monkeypatch):
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
    assert delivery_contract.classify_evaluation(ev, project) == "LEGACY_DELIVERED"


def test_audit_complete_projects_classifies_current_legacy_and_invalid(tmp_path, monkeypatch):
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
    assert statuses["legacy"] == "LEGACY_DELIVERED"
    assert statuses["invalid"] == "INVALID_OR_INCOMPLETE"
    assert (current / "delivery_manifest.json").is_file()
    assert (legacy / "delivery_manifest.json").is_file()
    assert (invalid / "delivery_manifest.json").is_file()
    assert result["summary"] == {
        "CURRENT_PASS": 1,
        "GATE2_OVERRIDE_DELIVERED": 0,
        "LEGACY_DELIVERED": 1,
        "INVALID_OR_INCOMPLETE": 1,
    }
