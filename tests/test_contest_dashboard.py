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


def _remaining_steps(payload, now):
    timing = payload["timing"]
    return (timing["projected_content_finish_at"] - now) // timing["recent_step_average_seconds"]


@pytest.mark.parametrize("catalog, last_completed, expected", [
    ("factory-stage-catalog-v2", 12, 4),  # 14, 15, 13, 16
    ("factory-stage-catalog-v2", 15, 2),  # 13, 16
    ("factory-stage-catalog-v2", 13, 1),  # 16
    ("factory-stage-catalog-v1", 12, 4),  # 16 - step, unchanged
    ("factory-stage-catalog-v1", 15, 1),
])
def test_dashboard_forecast_counts_the_steps_left_in_the_project_catalog(
    tmp_path, catalog, last_completed, expected
):
    from factory_core.stages import STAGE_SCHEDULER_GENERATION

    project = tmp_path / "ongoing/demo"
    project.mkdir(parents=True)
    policy = ContestPolicy.for_deadline(started_at=1_000, deadline_at=1_000_000)
    SQLiteStateStore(project, clock=lambda: 2_000).initialize(
        project_id="demo", project_type="modeling", last_completed_step=last_completed,
        scheduler_generation=STAGE_SCHEDULER_GENERATION, stage_catalog_version=catalog,
        contest_policy=policy.to_dict(),
    )

    payload = build_contest_dashboard(project, tmp_path / "papers", now_epoch=10_000)

    assert _remaining_steps(payload, 10_000) == expected


@pytest.mark.parametrize("active_subtask, expected", [
    ("content_freeze_guard", 2),  # the gate has no Step position: 13 and 16 remain
    ("final_judge", 1),
    ("no_such_subtask", 2),  # unknown cursor: fall back to the Step (15)
])
def test_v2_forecast_follows_the_active_subtask(tmp_path, active_subtask, expected):
    from factory_core.stages import STAGE_CATALOG_VERSION_V2, STAGE_SCHEDULER_GENERATION
    from web.backend.contest_dashboard import _timing

    project = tmp_path / "demo"
    policy = ContestPolicy.for_deadline(started_at=1_000, deadline_at=1_000_000)
    store = SQLiteStateStore(project, clock=lambda: 2_000)
    store.initialize(
        project_id="demo", project_type="modeling", last_completed_step=15,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        stage_catalog_version=STAGE_CATALOG_VERSION_V2, contest_policy=policy.to_dict(),
    )

    timing = _timing(store, current_step=15, now_epoch=10_000,
                     catalog_version=STAGE_CATALOG_VERSION_V2, active_subtask=active_subtask)

    assert (timing["projected_content_finish_at"] - 10_000) // timing["recent_step_average_seconds"] == expected


def test_dashboard_splits_contest_time_into_work_and_external_waits(tmp_path):
    from factory_core.domain import WorkflowStatus
    from factory_core.stages import STAGE_SCHEDULER_GENERATION

    clock = {"now": 1_000}
    project = tmp_path / "ongoing/demo"
    project.mkdir(parents=True)
    policy = ContestPolicy.for_deadline(started_at=1_000, deadline_at=1_000_000)
    store = SQLiteStateStore(project, clock=lambda: clock["now"])
    store.initialize(project_id="demo", project_type="modeling", last_completed_step=3,
                     scheduler_generation=STAGE_SCHEDULER_GENERATION, contest_policy=policy.to_dict())

    def at(when, event_type, status, **payload):
        clock["now"] = when
        state = store.load()
        store.transition(expected_revision=state.revision, event_type=event_type,
                         changes={"status": status}, payload=payload)

    at(1_000, "RUN_STARTED", WorkflowStatus.RUNNING)
    at(1_600, "PAUSED", WorkflowStatus.PAUSED,
       reason={"code": "PAUSED", "subcode": "PROVIDER_UNAVAILABLE", "actor": "engine"})
    at(2_000, "RESUMED", WorkflowStatus.READY)
    at(2_100, "RUN_STARTED", WorkflowStatus.RUNNING)
    at(2_500, "AWAITING_ACTION", WorkflowStatus.AWAITING_SELECTION)
    at(2_700, "PAUSED", WorkflowStatus.PAUSED,
       reason={"code": "PAUSED", "subcode": "OPERATOR", "actor": "operator"})

    split = build_contest_dashboard(project, tmp_path / "papers", now_epoch=3_000)["timing"]["time_split"]

    assert split["working_seconds"] == 600 + 400
    assert split["waiting_provider_seconds"] == 400
    assert split["waiting_human_seconds"] == 200
    assert split["stopped_seconds"] == 100 + 300  # idle between runs, operator pause
    assert split["waiting_external_seconds"] == 600
    assert sum(split[key] for key in ("working_seconds", "waiting_human_seconds",
                                      "waiting_provider_seconds", "stopped_seconds")) == 3_000 - 1_000


def test_time_before_the_first_event_counts_as_stopped(tmp_path):
    """A contest that began before the project existed still adds up."""

    from factory_core.stages import STAGE_SCHEDULER_GENERATION

    clock = {"now": 1_500}
    project = tmp_path / "ongoing/demo"
    project.mkdir(parents=True)
    policy = ContestPolicy.for_deadline(started_at=1_000, deadline_at=1_000_000)
    store = SQLiteStateStore(project, clock=lambda: clock["now"])
    store.initialize(project_id="demo", project_type="modeling", last_completed_step=3,
                     scheduler_generation=STAGE_SCHEDULER_GENERATION, contest_policy=policy.to_dict())
    clock["now"] = 1_700
    state = store.load()
    store.transition(expected_revision=state.revision, event_type="RUN_STARTED",
                     changes={"status": WorkflowStatus.RUNNING}, payload={})

    timing = build_contest_dashboard(project, tmp_path / "papers", now_epoch=2_000)["timing"]
    split = timing["time_split"]

    assert split["working_seconds"] == 300
    assert split["stopped_seconds"] == 500 + 200  # before creation, then ready until the run
    assert sum(split[key] for key in ("working_seconds", "waiting_human_seconds",
                                      "waiting_provider_seconds", "stopped_seconds")) == timing["elapsed_seconds"]


def test_timing_reads_the_event_table_once(tmp_path, monkeypatch):
    from factory_core.stages import STAGE_SCHEDULER_GENERATION
    from web.backend.contest_dashboard import _timing

    project = tmp_path / "ongoing/demo"
    project.mkdir(parents=True)
    policy = ContestPolicy.for_deadline(started_at=1_000, deadline_at=1_000_000)
    store = SQLiteStateStore(project, clock=lambda: 1_000)
    store.initialize(project_id="demo", project_type="modeling", last_completed_step=3,
                     scheduler_generation=STAGE_SCHEDULER_GENERATION, contest_policy=policy.to_dict())
    reads = []
    original = store.events
    monkeypatch.setattr(store, "events", lambda *a, **k: reads.append(1) or original(*a, **k))

    timing = _timing(store, current_step=4, now_epoch=2_000)

    assert timing["configured"] is True and "time_split" in timing
    assert len(reads) == 1
