from pathlib import Path
import os


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_missing_gate_files_returns_missing(tmp_path):
    from step8_5_gate import collect_step8_5_state

    state = collect_step8_5_state(tmp_path)
    assert state["status"] == "missing"
    assert state["verdict"] is None
    assert state["ready"] is False
    assert state["artifacts_complete"] is False


def test_revise_gate_is_not_ready(tmp_path):
    from step8_5_gate import collect_step8_5_state

    write_file(tmp_path / "reviewer_entry_map.md", "# map\n")
    write_file(tmp_path / "anchor_figure_plan.md", "# anchors\n")
    write_file(tmp_path / "entry_gate.md", "# Step 8.5 Entry Gate\n\nVERDICT: REVISE\n")

    state = collect_step8_5_state(tmp_path)
    assert state["status"] == "revise"
    assert state["verdict"] == "REVISE"
    assert state["ready"] is False
    assert state["artifacts_complete"] is True


def test_pass_gate_is_ready(tmp_path):
    from step8_5_gate import collect_step8_5_state

    write_file(tmp_path / "reviewer_entry_map.md", "# map\n")
    write_file(tmp_path / "anchor_figure_plan.md", "# anchors\n")
    write_file(tmp_path / "entry_gate.md", "# Step 8.5 Entry Gate\n\nVERDICT: PASS\n")

    state = collect_step8_5_state(tmp_path)
    assert state["status"] == "pass"
    assert state["verdict"] == "PASS"
    assert state["ready"] is True
    assert state["artifacts_complete"] is True


def test_pass_gate_is_stale_when_canonical_results_are_newer(tmp_path):
    from step8_5_gate import collect_step8_5_state

    write_file(tmp_path / "reviewer_entry_map.md", "# map\n")
    write_file(tmp_path / "anchor_figure_plan.md", "# anchors\n")
    write_file(tmp_path / "entry_gate.md", "# Step 8.5 Entry Gate\n\nVERDICT: PASS\n")
    write_file(tmp_path / "results" / "canonical_results.json", "{}\n")

    gate_time = 1_700_000_000
    for name in ("reviewer_entry_map.md", "anchor_figure_plan.md", "entry_gate.md"):
        os.utime(tmp_path / name, (gate_time, gate_time))
    os.utime(
        tmp_path / "results" / "canonical_results.json",
        (gate_time + 10, gate_time + 10),
    )

    state = collect_step8_5_state(tmp_path)
    assert state["status"] == "stale"
    assert state["verdict"] == "PASS"
    assert state["effective_verdict"] == "STALE"
    assert state["ready"] is False
    assert state["fresh"] is False
