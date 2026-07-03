from __future__ import annotations

import json
from pathlib import Path

import pytest

from web.backend.selection_service import (
    SelectionError,
    build_step3_options,
    read_selection_request,
    selection_enabled,
    write_selection_decision,
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def seed_step2_streams(project: Path) -> None:
    write(project / "selection" / "config.json", '{"enabled": true, "gates": ["step3"], "timeout_minutes": 30}\n')
    write(project / "viable_streams.md", "## Stream m1:\nMILP stream\n\n## Stream m2:\nSA stream\n")
    write(project / "m1_critique.md", "VERDICT: VALIDATED\nMAJOR warnings: none\n")
    write(project / "m2_critique.md", "VERDICT: VALIDATED\nMAJOR warnings: runtime risk\n")
    write(project / "m3_critique.md", "VERDICT: ABANDONED\ninsufficient data\n")
    write(project / "m1_spec.md", "# m1\nmethod_library/optimization/milp.md\nCovers P1 P2 P3\n")
    write(project / "m2_spec.md", "# m2\nmethod_library/metaheuristic/simulated_annealing.md\nCovers P1 P2\n")
    write(project / "m1_demo_result.json", '{"status": "OPTIMAL", "runtime_seconds": 12}\n')
    write(project / "m2_demo_result.json", '{"status": "FEASIBLE", "runtime_seconds": 55}\n')


def test_selection_enabled_defaults_to_false(tmp_path):
    project = tmp_path / "project"
    assert selection_enabled(project, "step3") is False


def test_build_step3_options_ranks_validated_streams_and_writes_files(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)

    payload = build_step3_options(project, now_epoch=1000)

    assert payload["available"] is True
    assert payload["gate"] == "step3"
    assert payload["default_option_id"] == "m1"
    assert payload["default_aux_id"] == "m2"
    assert payload["deadline_epoch"] == 2800
    assert [item["id"] for item in payload["options"]] == ["m1", "m2"]
    assert payload["options"][0]["scores"]["correctness"] >= payload["options"][1]["scores"]["correctness"]
    assert (project / "selection" / "step3_options.json").is_file()
    assert (project / "selection" / "step3_request.md").is_file()


def test_write_selection_decision_rejects_unknown_option(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)
    build_step3_options(project, now_epoch=1000)

    with pytest.raises(SelectionError):
        write_selection_decision(
            project,
            gate="step3",
            selected_option_id="m9",
            selected_aux_id="",
            source="human",
            reason="bad id",
            now_epoch=1200,
        )


def test_write_selection_decision_records_json_and_step3_human_review(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)
    build_step3_options(project, now_epoch=1000)
    write(project / "human_review.md", "# 人工审核与介入记录\n\n## Other\nkeep\n")

    decision = write_selection_decision(
        project,
        gate="step3",
        selected_option_id="m2",
        selected_aux_id="m1",
        source="human",
        reason="Prefer heuristic contrast.",
        now_epoch=1200,
    )

    saved = read_json(project / "selection" / "step3_decision.json")
    review = (project / "human_review.md").read_text(encoding="utf-8")
    assert decision["selected_option_id"] == "m2"
    assert saved["selected_aux_id"] == "m1"
    assert "## Step 3 decision:" in review
    assert "PRIMARY: m2" in review
    assert "AUXILIARY: m1" in review
    assert "SOURCE: human" in review
    assert "## Other\nkeep" in review


def test_read_selection_request_reports_existing_decision(tmp_path):
    project = tmp_path / "project"
    seed_step2_streams(project)
    build_step3_options(project, now_epoch=1000)
    write_selection_decision(
        project,
        gate="step3",
        selected_option_id="m1",
        selected_aux_id="m2",
        source="auto-timeout",
        reason="deadline",
        now_epoch=2800,
    )

    payload = read_selection_request(project, gate="step3")

    assert payload["available"] is True
    assert payload["decision"]["source"] == "auto-timeout"
    assert payload["selected_option_id"] == "m1"
