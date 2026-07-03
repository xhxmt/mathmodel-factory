from __future__ import annotations

import json
from pathlib import Path

from scripts import selection_gate
from tests.test_selection_service import seed_step2_streams


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_prepare_step3_writes_options_and_returns_pending(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)

    code = selection_gate.main(["prepare-step3", str(project), "--now-epoch", "1000"])

    assert code == 10
    assert (project / "selection" / "step3_options.json").is_file()
    assert read_json(project / "selection" / "step3_options.json")["default_option_id"] == "m1"


def test_prepare_step3_returns_ready_when_decision_exists(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)
    selection_gate.main(["prepare-step3", str(project), "--now-epoch", "1000"])
    code = selection_gate.main(["default-step3", str(project), "--now-epoch", "2800", "--no-resume"])

    assert code == 0
    assert selection_gate.main(["prepare-step3", str(project), "--now-epoch", "2801"]) == 0
    assert read_json(project / "selection" / "step3_decision.json")["source"] == "auto-timeout"


def test_prepare_step3_returns_ready_when_selection_disabled(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    assert selection_gate.main(["prepare-step3", str(project)]) == 0


def test_select_step3_records_manual_cli_decision(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)
    selection_gate.main(["prepare-step3", str(project), "--now-epoch", "1000"])

    code = selection_gate.main([
        "select-step3",
        str(project),
        "--primary",
        "m2",
        "--aux",
        "m1",
        "--reason",
        "Prefer heuristic contrast.",
        "--now-epoch",
        "1100",
        "--no-resume",
    ])

    decision = read_json(project / "selection" / "step3_decision.json")
    review = (project / "human_review.md").read_text(encoding="utf-8")
    assert code == 0
    assert decision["source"] == "manual-cli"
    assert decision["selected_option_id"] == "m2"
    assert decision["selected_aux_id"] == "m1"
    assert "PRIMARY: m2" in review
    assert "AUXILIARY: m1" in review
