from pathlib import Path

from web.backend.diagnostics_service import build_project_diagnostics, summarize_project_diagnostics


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
