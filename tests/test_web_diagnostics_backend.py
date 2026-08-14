from pathlib import Path
import json

from web.backend.diagnostics_service import build_project_diagnostics, summarize_project_diagnostics
from factory_core.domain import WorkflowStatus
from factory_core.storage import SQLiteStateStore


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_runner_status_beats_fallback(tmp_path):
    write_file(
        tmp_path / "diagnostics" / "status.json",
        """{
  "version": 2,
  "state": "waiting",
  "current_step": 8,
  "current_action": "step8_5_gate_review",
  "reason_code": "AWAITING_STEP8_5",
  "reason_summary": "Step 8.5 未通过",
  "display_status": "step8_5_gate_review",
  "since": 1700000000,
  "last_event_at": 1700000000,
  "updated_at": 1700000000,
  "suggested_actions": ["open_entry_gate"],
  "evidence": [{"kind": "file", "path": "entry_gate.md"}]
}
""",
    )
    diag = build_project_diagnostics(tmp_path, "demo", is_running=True, consultation_pending=False, consultation_gate=None)
    assert diag["source"] == "runner"
    assert diag["status"]["reason_code"] == "AWAITING_STEP8_5"


def test_fallback_detects_step8_5_gate_wait(tmp_path):
    write_file(tmp_path / ".heartbeat", "AWAITING_STEP8_5:8 1700000000\n")
    write_file(tmp_path / "entry_gate.md", "# gate\n\nVERDICT: REVISE\n")
    diag = build_project_diagnostics(tmp_path, "demo", is_running=False, consultation_pending=False, consultation_gate=None)
    assert diag["source"] == "fallback"
    assert diag["status"]["reason_code"] == "AWAITING_STEP8_5"


def test_summary_exposes_badge_and_priority(tmp_path):
    write_file(tmp_path / ".heartbeat", "CONSULT:6 1700000000\n")
    diag = build_project_diagnostics(tmp_path, "demo", is_running=False, consultation_pending=True, consultation_gate="dynamic")
    summary = summarize_project_diagnostics(diag)
    assert summary["diagnostic_badge"] == "等待人工"
    assert summary["diagnostic_priority"] == 1


def test_native_workflow_event_projection_beats_runner_files(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    store.transition(
        expected_revision=initial.revision,
        event_type="STEP_FAILED",
        changes={"status": WorkflowStatus.FAILED, "active_step": 8},
        payload={
            "error_class": "PERMANENT_GATE_BLOCKED",
            "reason": "reviewer evidence is incomplete",
            "evidence": ["entry_gate.md"],
        },
    )
    write_file(
        tmp_path / "diagnostics" / "status.json",
        '{"state":"running","reason_code":"STALE"}\n',
    )

    diag = build_project_diagnostics(
        tmp_path,
        "demo",
        is_running=False,
        consultation_pending=False,
        consultation_gate=None,
    )

    assert diag["source"] == "workflow_events"
    assert diag["status"]["reason_code"] == "PERMANENT_GATE_BLOCKED"
    assert diag["recovery"]["latest"]["canonical_type"] == "GATE_BLOCKED"


def test_native_diagnostics_reports_orphaned_decision_projection(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    initial = store.initialize(project_id="demo", project_type="modeling")
    store.transition(
        expected_revision=initial.revision,
        event_type="AWAITING_ACTION",
        changes={
            "status": WorkflowStatus.AWAITING_SELECTION,
            "pending_action": {"type": "step3_selection", "gate": "step3"},
        },
    )
    write_file(
        tmp_path / "selection" / "step3_decision.json",
        '{"gate":"step3","selected_option_id":"m1"}\n',
    )

    diag = build_project_diagnostics(
        tmp_path,
        "demo",
        is_running=False,
        consultation_pending=False,
        consultation_gate=None,
    )

    assert diag["orphaned_artifacts"] == [
        {"gate": "step3", "path": "selection/step3_decision.json"}
    ]
    assert {action["id"] for action in diag["actions"]} >= {
        "retry_human_decision_commit"
    }


def test_native_diagnostics_fails_closed_on_replay_hash_mismatch(tmp_path):
    store = SQLiteStateStore(tmp_path, clock=lambda: 100)
    store.initialize(project_id="demo", project_type="modeling")
    with store._session() as connection:
        connection.execute("DROP TRIGGER events_append_only_update")
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM events WHERE revision=1"
            ).fetchone()[0]
        )
        payload["_workflow"]["state_patch"]["status"] = "completed"
        connection.execute(
            "UPDATE events SET payload_json=? WHERE revision=1",
            (json.dumps(payload),),
        )

    diag = build_project_diagnostics(
        tmp_path,
        "demo",
        is_running=False,
        consultation_pending=False,
        consultation_gate=None,
    )

    assert diag["source"] == "workflow_events"
    assert diag["status"]["reason_code"] == "WORKFLOW_REPLAY_MISMATCH"
