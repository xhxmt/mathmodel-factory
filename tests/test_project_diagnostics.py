from project_diagnostics import (
    append_event,
    diagnostics_dir,
    load_recent_events,
    load_status,
    write_status,
)


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
