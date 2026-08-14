from __future__ import annotations

from dataclasses import dataclass

import pytest

from factory_core.audit.domain import (
    AuditOutcome,
    AuditRecord,
    AuditSnapshot,
    AuditStatus,
)
from factory_core.dirty import (
    DirtyFlag,
    capture_artifact_manifest,
    classify_manifest_changes,
)
from factory_core.domain import ExecutionResult, StepContext
from factory_core.finalization import (
    FinalizationSnapshotChanged,
    build_final_input_manifest,
    reopen_after_for_changed_paths,
    verify_final_input_snapshot,
)
from factory_core.steps.catalog import contract_for
from factory_core.steps.specialized import DeliveryStep
from factory_core.storage import SQLiteStateStore


def _flags(before, after):
    return {change.flag for change in classify_manifest_changes(before, after)}


def test_dirty_classifier_separates_prose_math_results_and_unknown(tmp_path):
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("\\begin{document}\n$x=1$ alpha\n\\end{document}\n", encoding="utf-8")
    before = capture_artifact_manifest(tmp_path)

    paper.write_text("\\begin{document}\n$x=1$ beta\n\\end{document}\n", encoding="utf-8")
    prose = capture_artifact_manifest(tmp_path)
    assert DirtyFlag.PROSE in _flags(before, prose)
    assert DirtyFlag.MATH not in _flags(before, prose)

    paper.write_text("\\begin{document}\n$x=2$ beta\n\\end{document}\n", encoding="utf-8")
    math = capture_artifact_manifest(tmp_path)
    assert DirtyFlag.MATH in _flags(prose, math)

    results = tmp_path / "results"
    results.mkdir()
    (results / "canonical_results.json").write_text('{"x": 2}\n', encoding="utf-8")
    canonical = capture_artifact_manifest(tmp_path)
    assert DirtyFlag.RESULT in _flags(math, canonical)

    (tmp_path / "unclassified.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    unknown = capture_artifact_manifest(tmp_path)
    unknown_flags = _flags(canonical, unknown)
    assert {DirtyFlag.MATH, DirtyFlag.RESULT} <= unknown_flags


def test_derived_result_projection_does_not_reopen_canonical_solve(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    before = capture_artifact_manifest(tmp_path)
    (results / "plot_metadata.json").write_text('{"color": "blue"}\n', encoding="utf-8")
    after = capture_artifact_manifest(tmp_path)

    flags = _flags(before, after)

    assert DirtyFlag.FORMAT in flags
    assert DirtyFlag.RESULT not in flags


def test_problem_plan_change_is_owned_by_understand_stage(tmp_path):
    plan = tmp_path / "problem" / "problem_plan.json"
    plan.parent.mkdir()
    before = capture_artifact_manifest(tmp_path)
    plan.write_text('{"schema_version":"problem-plan-v1"}\n', encoding="utf-8")
    after = capture_artifact_manifest(tmp_path)

    changes = classify_manifest_changes(before, after)

    assert [(change.flag, change.owner_stage) for change in changes] == [
        (DirtyFlag.MODEL, 1)
    ]


def test_dirty_flag_clear_requires_owner_stage_receipt_in_same_revision(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")
    dirty = store.transition(
        expected_revision=state.revision,
        event_type="DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": "RESULT_DIRTY",
                "owner_stage": 4,
                "cause_artifact": "results/canonical_results.json",
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": "c" * 64,
            }
        ],
    )
    assert store.dirty_flags()[0]["cause_revision"] == dirty.revision

    cleared = store.transition(
        expected_revision=dirty.revision,
        event_type="STAGE_SUCCEEDED_FOR_TEST",
        changes={},
        clear_dirty_stage={
            "owner_stage": 4,
            "cleared_fingerprint": "d" * 64,
            "classifier_contract_sha256": "c" * 64,
            "success_receipt": {"status": "PASS"},
        },
    )

    assert store.dirty_flags() == []
    receipt = store.dirty_clear_receipts()[0]
    assert receipt["revision"] == cleared.revision
    assert receipt["receipt"]["success_receipt"] == {"status": "PASS"}


def test_final_input_manifest_detects_mutation_and_routes_owner(tmp_path):
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("\\begin{document}\nhello\n\\end{document}\n", encoding="utf-8")
    snapshot = build_final_input_manifest(tmp_path)
    verify_final_input_snapshot(tmp_path, snapshot)

    paper.write_text("\\begin{document}\nchanged\n\\end{document}\n", encoding="utf-8")

    with pytest.raises(FinalizationSnapshotChanged) as raised:
        verify_final_input_snapshot(tmp_path, snapshot)
    assert paper.name in raised.value.changed_paths
    assert reopen_after_for_changed_paths(raised.value.changed_paths) == 10


@dataclass
class MutatingAudit:
    project: object

    def run(self, _context):
        paper = self.project / "demo_paper.tex"
        paper.write_text(
            paper.read_text(encoding="utf-8") + "% mutation\n",
            encoding="utf-8",
        )
        snapshot = AuditSnapshot("a" * 64, "demo", "final", "now", {})
        record = AuditRecord(
            "a" * 64,
            "demo",
            "final",
            AuditStatus.PASS,
            "PASS",
            True,
            True,
            "now",
        )
        return AuditOutcome(ExecutionResult.succeeded(), record, snapshot)


class NoopRunner:
    def python(self, *args, **kwargs):
        class Result:
            accepted = True
            returncode = 0

        return Result()


class Unused:
    pass


def test_delivery_returns_events_without_writing_workflow_state_when_input_changes(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "demo_paper.tex").write_text(
        "\\begin{document}\nhello\n\\end{document}\n", encoding="utf-8"
    )
    store = SQLiteStateStore(project)
    store.initialize(project_id="demo", project_type="modeling")
    step = DeliveryStep(
        contract_for(16),
        tmp_path,
        Unused(),
        Unused(),
        NoopRunner(),
        audit_service=MutatingAudit(project),
    )

    result = step.execute(StepContext(project, "demo", 16, 1, 300, 1))

    assert result.metadata["finalization_aborted"] is True
    assert result.metadata["resume_after_step"] == 10
    assert [event.type for event in store.events()] == ["PROJECT_CREATED"]
    assert [event["type"] for event in result.metadata["_workflow_events"]] == [
        "FINAL_SNAPSHOT_CREATED",
        "FINALIZATION_ABORTED_SNAPSHOT_CHANGED",
    ]
