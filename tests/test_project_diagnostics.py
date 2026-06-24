import json
import subprocess
import sys
from pathlib import Path

from project_diagnostics import (
    EVENTS_FILE,
    STATUS_FILE,
    append_event,
    diagnostics_dir,
    load_recent_events,
    load_status,
    write_status,
)


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "project_diagnostics.py"


def test_write_status_round_trips_snapshot(tmp_path):
    write_status(
        tmp_path,
        state="waiting",
        current_step=8,
        current_action="step8_5_gate_review",
        reason_code="AWAITING_STEP8_5",
        reason_summary="Step 8.5 未通过",
        suggested_actions=["open_entry_gate", "refresh_status"],
        evidence=[{"kind": "file", "path": "entry_gate.md"}],
        since=1700000000,
    )

    status = load_status(tmp_path)
    assert status["state"] == "waiting"
    assert status["current_step"] == 8
    assert status["reason_code"] == "AWAITING_STEP8_5"
    assert status["suggested_actions"] == ["open_entry_gate", "refresh_status"]


def test_load_status_returns_none_when_missing(tmp_path):
    assert load_status(tmp_path) is None
    assert diagnostics_dir(tmp_path) == tmp_path / "diagnostics"


def test_append_event_and_tail_recent_events(tmp_path):
    for idx in range(5):
        append_event(
            tmp_path,
            step=6,
            event_type="verification_failed",
            message=f"failure-{idx}",
            reason_code="VERIFY_OUTPUT_FAILED",
            files=["solve_log.md"],
            meta={"attempt": idx + 1},
        )

    events = load_recent_events(tmp_path, limit=2)
    assert [ev["message"] for ev in events] == ["failure-3", "failure-4"]
    assert events[-1]["meta"]["attempt"] == 5


def test_load_recent_events_ignores_malformed_trailing_line(tmp_path):
    diagnostics_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    events_path = diagnostics_dir(tmp_path) / EVENTS_FILE
    events_path.write_text(
        "\n".join(
            [
                json.dumps({"message": "ok-1", "meta": {"attempt": 1}}),
                json.dumps({"message": "ok-2", "meta": {"attempt": 2}}),
                '{"message": "broken"',
            ]
        ),
        encoding="utf-8",
    )

    events = load_recent_events(tmp_path, limit=5)

    assert [ev["message"] for ev in events] == ["ok-1", "ok-2"]


def test_cli_write_status_writes_status_file(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "write-status",
            str(tmp_path),
            "--state",
            "waiting",
            "--step",
            "8",
            "--action",
            "step8_5_gate_review",
            "--reason-code",
            "AWAITING_STEP8_5",
            "--reason-summary",
            "Step 8.5 未通过",
            "--suggested-action",
            "open_entry_gate",
            "--suggested-action",
            "refresh_status",
            "--evidence",
            "file:entry_gate.md",
            "--evidence",
            "note:blocked",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    status_path = diagnostics_dir(tmp_path) / STATUS_FILE
    status = json.loads(status_path.read_text(encoding="utf-8"))

    assert payload["suggested_actions"] == ["open_entry_gate", "refresh_status"]
    assert payload["evidence"] == [
        {"kind": "file", "path": "entry_gate.md"},
        {"kind": "note", "value": "blocked"},
    ]
    assert status["reason_code"] == "AWAITING_STEP8_5"
    assert status["suggested_actions"] == ["open_entry_gate", "refresh_status"]


def test_cli_append_event_writes_events_file(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "append-event",
            str(tmp_path),
            "--step",
            "8",
            "--type",
            "gate_blocked",
            "--message",
            "Step 8.5 verdict is REVISE",
            "--reason-code",
            "AWAITING_STEP8_5",
            "--file",
            "entry_gate.md",
            "--file",
            "notes.txt",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    events_path = diagnostics_dir(tmp_path) / EVENTS_FILE
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]

    assert payload["type"] == "gate_blocked"
    assert payload["files"] == ["entry_gate.md", "notes.txt"]
    assert events[-1]["reason_code"] == "AWAITING_STEP8_5"
    assert events[-1]["files"] == ["entry_gate.md", "notes.txt"]
