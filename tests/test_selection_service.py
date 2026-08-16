from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory_core.contest import ContestPolicy
from factory_core.domain import StepContext
from factory_core.selection_projection import (
    rebuild_step3_projections,
    verify_step3_projections,
)
from factory_core.steps.catalog import contract_for
from factory_core.steps.prompt_step import PromptStep
from factory_core.steps.validators import NativeArtifactValidator
from factory_core.storage import SQLiteStateStore
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


def seed_authoritative_step3_decision(project: Path) -> dict:
    seed_step2_streams(project)
    SQLiteStateStore(project, clock=lambda: 1_100).initialize(
        project_id=project.name,
        project_type="modeling",
        contest_policy=ContestPolicy.default(started_at=1_000).to_dict(),
    )
    build_step3_options(project, now_epoch=1_000)
    decision = write_selection_decision(
        project,
        gate="step3",
        selected_option_id="m1",
        selected_aux_id="m2",
        source="human",
        reason="Bound test decision.",
        now_epoch=1_100,
    )
    method = project / "method_decision.md"
    method.write_text(
        method.read_text(encoding="utf-8")
        + "\n".join(f"Decision rationale line {index}" for index in range(35))
        + "\n",
        encoding="utf-8",
    )
    rebuild_step3_projections(project)
    return decision


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
    assert payload["options"][0]["demo_runtime_seconds"] == 12
    assert "小样 12.0s" in payload["options"][0]["estimated_time"]
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
        confirmations=["evidence_reviewed"],
        now_epoch=1200,
    )

    saved = read_json(project / "selection" / "step3_decision.json")
    review = (project / "human_review.md").read_text(encoding="utf-8")
    assert decision["selected_option_id"] == "m2"
    assert saved["selected_aux_id"] == "m1"
    assert saved["candidate_evidence"] == ["m2_spec.md", "m2_critique.md", "m2_demo_result.json"]
    assert saved["confirmations"] == ["evidence_reviewed"]
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


def test_step3_rejects_projection_primary_mismatch(tmp_path):
    project = tmp_path / "project"
    seed_authoritative_step3_decision(project)
    chosen = project / "chosen_method.md"
    chosen.write_text(
        chosen.read_text(encoding="utf-8").replace("PRIMARY: m1", "PRIMARY: m2", 1),
        encoding="utf-8",
    )

    ok, reason, _evidence, _metadata = NativeArtifactValidator(
        tmp_path, 3
    )._step_3(project)

    assert ok is False
    assert "PRIMARY does not match SQLite decision" in reason


def test_step3_rejects_projection_auxiliary_mismatch(tmp_path):
    project = tmp_path / "project"
    seed_authoritative_step3_decision(project)
    chosen = project / "chosen_method.md"
    chosen.write_text(
        chosen.read_text(encoding="utf-8").replace(
            "AUXILIARY: m2", "AUXILIARY: NONE", 1
        ),
        encoding="utf-8",
    )

    verification = verify_step3_projections(project)

    assert verification.valid is False
    assert any("AUXILIARY" in error for error in verification.errors)


def test_method_projection_rejects_spoofed_fields_before_machine_header(tmp_path):
    project = tmp_path / "project"
    seed_authoritative_step3_decision(project)
    method = project / "method_decision.md"
    method.write_text(
        "PRIMARY: m1\nAUXILIARY: m2\n" + method.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    verification = verify_step3_projections(project)

    assert verification.valid is False
    assert "method_decision.md machine header is not canonical" in verification.errors


def test_step4_refuses_selection_projection_drift(tmp_path):
    project = tmp_path / "project"
    seed_authoritative_step3_decision(project)
    chosen = project / "chosen_method.md"
    chosen.write_text(
        chosen.read_text(encoding="utf-8").replace("DECISION_ID:", "DECISION_ID: tampered-", 1),
        encoding="utf-8",
    )
    step = PromptStep(
        contract_for(4),
        renderer=None,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        validator=NativeArtifactValidator(tmp_path, 4),
    )

    prepared = step.prepare(StepContext(project, project.name, 4, 1, 300, 1))

    assert prepared.ready is False
    assert "selection projection drift" in prepared.reason


def test_tampered_human_review_cannot_override_sqlite_selection(tmp_path):
    project = tmp_path / "project"
    seed_authoritative_step3_decision(project)
    write(
        project / "human_review.md",
        "# Tampered projection\n\n## Step 3 decision:\nPRIMARY: m2\nAUXILIARY: NONE\n",
    )

    verification = verify_step3_projections(project)

    assert verification.valid is True
    assert verification.decision["selected_option_id"] == "m1"


def test_chosen_method_projection_can_be_rebuilt_from_decision(tmp_path):
    project = tmp_path / "project"
    decision = seed_authoritative_step3_decision(project)
    write(project / "chosen_method.md", "PRIMARY: m9\n")
    method = project / "method_decision.md"
    method.write_text(
        method.read_text(encoding="utf-8").replace(
            str(decision["request_id"]), "tampered-request", 1
        ),
        encoding="utf-8",
    )

    rebuilt = rebuild_step3_projections(project)

    assert rebuilt["decision_id"] == decision["decision_id"]
    assert verify_step3_projections(project).valid is True
    assert (project / "chosen_method.md").read_text(encoding="utf-8").startswith(
        "PRIMARY: m1\nAUXILIARY: m2"
    )


def test_candidate_spec_change_invalidates_step3_decision(tmp_path):
    project = tmp_path / "project"
    seed_authoritative_step3_decision(project)
    write(project / "m1_spec.md", "# changed after human selection\n")

    verification = verify_step3_projections(project)

    assert verification.valid is False
    assert any("candidate evidence fingerprint" in error for error in verification.errors)
