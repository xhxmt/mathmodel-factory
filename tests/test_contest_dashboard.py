from __future__ import annotations

import json

import pytest

from factory_core.contest import ContestPolicy
from factory_core.audit.acceptance import build_final_acceptance_receipt
from factory_core.audit.domain import AuditSnapshot
from factory_core.delivery.release import ReleasePublisher
from factory_core.domain import WorkflowStatus
from factory_core.storage import SQLiteStateStore
from tests.test_atomic_release import _package, _project
from web.backend.contest_dashboard import build_contest_dashboard


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_legacy_dashboard_does_not_invent_contest_timing(tmp_path):
    project = tmp_path / "ongoing/legacy"
    project.mkdir(parents=True)

    payload = build_contest_dashboard(project, tmp_path / "papers", now_epoch=10_000)

    assert payload["schema_version"] == "contest-dashboard-v1"
    assert payload["timing"]["configured"] is False
    assert payload["timing"]["mode"] == "legacy"
    assert payload["delivery"]["ready"] is False


def test_contest_dashboard_forecast_switches_to_repair_only_when_slack_is_negative(tmp_path):
    project = tmp_path / "ongoing/demo"
    project.mkdir(parents=True)
    policy = ContestPolicy.for_deadline(started_at=1_000, deadline_at=100_000)
    SQLiteStateStore(project, clock=lambda: 2_000).initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=1,
        contest_policy=policy.to_dict(),
    )

    payload = build_contest_dashboard(
        project,
        tmp_path / "papers",
        now_epoch=policy.content_freeze_at - 5_000,
    )

    assert payload["timing"]["risk_level"] == "critical"
    assert payload["timing"]["mode"] == "repair_only"
    assert payload["timing"]["content_slack_seconds"] < 0
    assert any(item["id"] == "contest-clock" for item in payload["actions"])


def test_dashboard_fail_closes_missing_contracts_and_surfaces_pending_human_gate(tmp_path):
    project = tmp_path / "ongoing/demo"
    project.mkdir(parents=True)
    policy = ContestPolicy.default(started_at=1_000)
    SQLiteStateStore(project, clock=lambda: 2_000).initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=2,
        status=WorkflowStatus.AWAITING_SELECTION,
        active_step=3,
        pending_action={"type": "step3_selection", "gate": "step3"},
        contest_policy=policy.to_dict(),
    )

    payload = build_contest_dashboard(project, tmp_path / "papers", now_epoch=2_000)
    checks = {item["id"]: item for item in payload["delivery"]["checks"]}

    assert checks["attachments"]["status"] == "pending"
    assert checks["final_audit"]["status"] == "pending"
    assert payload["delivery"]["ready"] is False
    assert payload["actions"][0]["id"] == "gate:step3"
    assert payload["actions"][0]["tab"] == "selection"


def test_attachment_contract_cannot_probe_outside_project(tmp_path):
    project = tmp_path / "ongoing/demo"
    project.mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("present", encoding="utf-8")
    _write_json(
        project / "problem/deliverables.json",
        {"attachments": [{"file": "../../outside.txt"}]},
    )

    payload = build_contest_dashboard(project, tmp_path / "papers", now_epoch=2_000)
    checks = {item["id"]: item for item in payload["delivery"]["checks"]}

    assert checks["attachments"]["status"] == "fail"


def test_verified_current_release_is_download_ready_and_zero_required_attachments_pass(tmp_path):
    snapshot_id = "f" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    store = SQLiteStateStore(project, clock=lambda: 2_000)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=16,
        status=WorkflowStatus.COMPLETED,
    )
    _write_json(project / "problem/deliverables.json", {"attachments": []})
    _write_json(
        project / "results/canonical_results.json",
        {"primary_method": "m1", "auxiliary_method": "m2", "p1": {"status": "ok", "value": 42}},
    )
    _write_json(
        project / "judge_outputs/final_paper_checks.json",
        {"checks": [{"severity": "hard", "passed": True}]},
    )
    _write_json(project / "judge_outputs/visual_gate.json", {"blocking_findings": 0})
    _write_json(
        project / "judge_outputs/aggregate.json",
        {"role_statuses": {"math": "PASS", "execution": "PASS", "paper": "PASS"}},
    )
    snapshot = AuditSnapshot(
        **json.loads(
            (project / f".factory/audits/{snapshot_id}/snapshot.json").read_text(
                encoding="utf-8"
            )
        )
    )
    build_final_acceptance_receipt(project, snapshot, status="PASS")
    ReleasePublisher(tmp_path / "papers").publish(
        project,
        snapshot_id,
        status="PASS",
        package_builder=_package(project, "demo"),
    )

    payload = build_contest_dashboard(project, tmp_path / "papers", now_epoch=2_000)
    checks = {item["id"]: item for item in payload["delivery"]["checks"]}

    assert checks["attachments"]["status"] == "pass"
    assert checks["attachments"]["detail"] == "题目未要求额外交付附件"
    assert checks["content_freeze"]["status"] == "pass"
    assert checks["content_freeze"]["detail"].startswith("Legacy 项目")
    assert payload["delivery"]["ready"] is True
    assert payload["delivery"]["release"]["submission_available"] is True
    assert payload["evidence"]["canonical"]["items"][0]["headline"] == 42
