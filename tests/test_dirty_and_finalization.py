from __future__ import annotations

from dataclasses import dataclass

import pytest

from factory_core.audit.domain import (
    AuditOutcome,
    AuditRecord,
    AuditSnapshot,
    AuditStatus,
)
from factory_core.current_artifact_ownership import artifact_owner_stage
from factory_core.current_dirty import (
    DirtyFlag,
    capture_artifact_manifest,
    classify_manifest_changes,
    classifier_contract_sha256,
    manifest_fingerprint,
)
from factory_core.domain import ExecutionResult, InvalidTransition, StepContext
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


def test_number_verification_is_owned_by_final_revision_validation(tmp_path):
    before = capture_artifact_manifest(tmp_path)
    (tmp_path / "number_verification.md").write_text(
        "# Number verification\n\nVERDICT: PASS\n", encoding="utf-8"
    )
    after = capture_artifact_manifest(tmp_path)

    changes = classify_manifest_changes(before, after)

    assert [(change.flag, change.owner_stage) for change in changes] == [
        (DirtyFlag.MATH, 8)
    ]


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


def test_nested_paper_source_participates_in_dirty_classification(tmp_path):
    paper = tmp_path / "paper" / "paper.tex"
    paper.parent.mkdir()
    paper.write_text("\\begin{document}$x=1$ alpha\\end{document}\n", encoding="utf-8")
    before = capture_artifact_manifest(tmp_path)
    assert "paper/paper.tex" in before

    paper.write_text("\\begin{document}$x=2$ alpha\\end{document}\n", encoding="utf-8")
    after = capture_artifact_manifest(tmp_path)

    assert DirtyFlag.MATH in _flags(before, after)


def _macro_change_flags(tmp_path, before_definition, after_definition, formula):
    paper = tmp_path / "paper" / "paper.tex"
    paper.parent.mkdir(parents=True, exist_ok=True)
    paper.write_text(
        f"{before_definition}\n\\begin{{document}}{formula}\\end{{document}}\n",
        encoding="utf-8",
    )
    before = capture_artifact_manifest(tmp_path)
    paper.write_text(
        f"{after_definition}\n\\begin{{document}}{formula}\\end{{document}}\n",
        encoding="utf-8",
    )
    return _flags(before, capture_artifact_manifest(tmp_path))


def test_newcommand_value_change_marks_math_dirty(tmp_path):
    flags = _macro_change_flags(
        tmp_path,
        r"\newcommand{\coef}{1}",
        r"\newcommand{\coef}{2}",
        r"$x=\coef$",
    )

    assert DirtyFlag.MATH in flags


def test_renewcommand_change_marks_math_dirty(tmp_path):
    flags = _macro_change_flags(
        tmp_path,
        r"\newcommand{\coef}{1}\renewcommand{\coef}{2}",
        r"\newcommand{\coef}{1}\renewcommand{\coef}{3}",
        r"$x=\coef$",
    )

    assert DirtyFlag.MATH in flags


def test_def_change_marks_math_dirty(tmp_path):
    flags = _macro_change_flags(
        tmp_path,
        r"\def\coef{1}",
        r"\def\coef{2}",
        r"$x=\coef$",
    )

    assert DirtyFlag.MATH in flags


@pytest.mark.parametrize(
    "formula",
    [
        r"\(x=1\)",
        r"\begin{math}x=1\end{math}",
        r"\begin{displaymath}x=1\end{displaymath}",
        r"\begin{alignat}{2}x&=1\end{alignat}",
        r"\begin{flalign}x&=1&&\end{flalign}",
        r"\begin{eqnarray}x&=&1\end{eqnarray}",
    ],
)
def test_additional_math_delimiters_mark_math_dirty(tmp_path, formula):
    paper = tmp_path / "paper" / "paper.tex"
    paper.parent.mkdir(parents=True)
    paper.write_text("\\begin{document}plain\\end{document}\n", encoding="utf-8")
    before = capture_artifact_manifest(tmp_path)
    paper.write_text(
        f"\\begin{{document}}{formula}\\end{{document}}\n",
        encoding="utf-8",
    )

    assert DirtyFlag.MATH in _flags(before, capture_artifact_manifest(tmp_path))


@pytest.mark.parametrize("prefix", [r"\global", r"\long", r"\outer", r"\protected"])
def test_tex_definition_prefix_change_marks_math_dirty(tmp_path, prefix):
    flags = _macro_change_flags(
        tmp_path,
        r"\def\coef{1}",
        prefix + r"\def\coef{1}",
        r"$x=\coef$",
    )

    assert DirtyFlag.MATH in flags


def test_included_macro_file_change_marks_math_dirty(tmp_path):
    paper = tmp_path / "paper" / "paper.tex"
    macros = tmp_path / "paper" / "macros.tex"
    paper.parent.mkdir(parents=True)
    paper.write_text(
        r"\input{macros}\begin{document}$x=\coef$\end{document}" + "\n",
        encoding="utf-8",
    )
    macros.write_text(r"\newcommand{\coef}{1}" + "\n", encoding="utf-8")
    before = capture_artifact_manifest(tmp_path)

    macros.write_text(r"\newcommand{\coef}{2}" + "\n", encoding="utf-8")
    after = capture_artifact_manifest(tmp_path)

    assert DirtyFlag.MATH in _flags(before, after)


def test_unused_macro_change_classification_fails_closed(tmp_path):
    flags = _macro_change_flags(
        tmp_path,
        r"\providecommand{\unused}{1}",
        r"\providecommand{\unused}{2}",
        r"$x=1$",
    )

    assert DirtyFlag.MATH in flags


@pytest.mark.parametrize(
    ("before_definition", "after_definition"),
    [
        (
            r"\NewDocumentCommand{\coef}{}{1}",
            r"\NewDocumentCommand{\coef}{}{2}",
        ),
        (
            r"\RenewDocumentCommand{\coef}{}{1}",
            r"\RenewDocumentCommand{\coef}{}{2}",
        ),
        (
            r"\ProvideDocumentCommand{\coef}{}{1}",
            r"\ProvideDocumentCommand{\coef}{}{2}",
        ),
        (
            r"\DeclareDocumentCommand{\coef}{}{1}",
            r"\DeclareDocumentCommand{\coef}{}{2}",
        ),
    ],
)
def test_document_command_definition_change_marks_math_dirty(
    tmp_path, before_definition, after_definition
):
    flags = _macro_change_flags(
        tmp_path, before_definition, after_definition, r"$x=\coef$"
    )

    assert DirtyFlag.MATH in flags


def test_new_environment_change_marks_math_dirty(tmp_path):
    flags = _macro_change_flags(
        tmp_path,
        r"\newenvironment{scaled}{\def\coef{1}}{}",
        r"\newenvironment{scaled}{\def\coef{2}}{}",
        r"$x=\coef$",
    )

    assert DirtyFlag.MATH in flags


def test_expl3_command_definition_change_marks_math_dirty(tmp_path):
    flags = _macro_change_flags(
        tmp_path,
        r"\cs_new:Npn \coef { 1 }",
        r"\cs_set:Npn \coef { 2 }",
        r"$x=\coef$",
    )

    assert DirtyFlag.MATH in flags


def test_pgfmath_macro_change_marks_math_dirty(tmp_path):
    flags = _macro_change_flags(
        tmp_path,
        r"\pgfmathsetmacro{\coef}{1}",
        r"\pgfmathsetmacro{\coef}{2}",
        r"$x=\coef$",
    )

    assert DirtyFlag.MATH in flags


def test_included_xparse_macro_change_marks_math_dirty(tmp_path):
    paper = tmp_path / "paper" / "paper.tex"
    macros = tmp_path / "paper" / "macros.tex"
    paper.parent.mkdir(parents=True)
    paper.write_text(
        r"\input{macros}\begin{document}$x=\coef$\end{document}" + "\n",
        encoding="utf-8",
    )
    macros.write_text(
        r"\NewDocumentCommand{\coef}{}{1}" + "\n", encoding="utf-8"
    )
    before = capture_artifact_manifest(tmp_path)
    macros.write_text(
        r"\NewDocumentCommand{\coef}{}{2}" + "\n", encoding="utf-8"
    )

    assert DirtyFlag.MATH in _flags(before, capture_artifact_manifest(tmp_path))


@pytest.mark.parametrize(
    ("relative", "expected_flag", "expected_owner"),
    [
        ("problem/problem_brief.md", DirtyFlag.MODEL, 1),
        ("viable_streams.md", DirtyFlag.MODEL, 1),
        ("m1_spec.md", DirtyFlag.MODEL, 2),
        ("m1_demo_result.json", DirtyFlag.MODEL, 2),
        ("method_decision.md", DirtyFlag.MODEL, 2),
        ("chosen_method.md", DirtyFlag.MODEL, 2),
        ("results/values.json", DirtyFlag.RESULT, 4),
        ("results/run_sensitivity.json", DirtyFlag.RESULT, 5),
        ("sensitivity_report.md", DirtyFlag.RESULT, 5),
        ("evaluation.md", DirtyFlag.RESULT, 5),
        ("reviewer_entry_map.md", DirtyFlag.VISUAL, 6),
        ("anchor_figure_plan.md", DirtyFlag.VISUAL, 6),
        ("entry_gate.md", DirtyFlag.VISUAL, 6),
    ],
)
def test_business_artifacts_use_the_shared_owner_registry(
    tmp_path, relative, expected_flag, expected_owner
):
    before = capture_artifact_manifest(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n", encoding="utf-8")
    changes = classify_manifest_changes(
        before, capture_artifact_manifest(tmp_path)
    )

    assert artifact_owner_stage(relative) == expected_owner
    assert {(item.flag, item.owner_stage) for item in changes} == {
        (expected_flag, expected_owner)
    }


def test_finalization_and_dirty_classifier_share_owner_registry(tmp_path):
    before = capture_artifact_manifest(tmp_path)
    chosen = tmp_path / "chosen_method.md"
    chosen.write_text("PRIMARY: m1\n", encoding="utf-8")
    change = classify_manifest_changes(
        before, capture_artifact_manifest(tmp_path)
    )[0]

    assert change.owner_stage == artifact_owner_stage("chosen_method.md") == 2
    assert reopen_after_for_changed_paths(["chosen_method.md"]) == 1
    assert reopen_after_for_changed_paths(["entry_gate.md"]) == 7


def test_dirty_classifier_preserves_each_recursive_source_cause(tmp_path):
    paper = tmp_path / "paper" / "paper.tex"
    first = tmp_path / "paper" / "sections" / "first.tex"
    second = tmp_path / "paper" / "sections" / "second.tex"
    paper.parent.mkdir(parents=True)
    first.parent.mkdir(parents=True)
    paper.write_text(
        "\\begin{document}\\input{sections/first}\\input{sections/second}"
        "\\end{document}\n",
        encoding="utf-8",
    )
    first.write_text("$x=1$\n", encoding="utf-8")
    second.write_text("$y=1$\n", encoding="utf-8")
    before = capture_artifact_manifest(tmp_path)

    first.write_text("$x=2$\n", encoding="utf-8")
    second.write_text("$y=2$\n", encoding="utf-8")
    after = capture_artifact_manifest(tmp_path)
    math_causes = {
        change.cause_artifact
        for change in classify_manifest_changes(before, after)
        if change.flag is DirtyFlag.MATH
    }

    assert math_causes == {
        "paper/sections/first.tex",
        "paper/sections/second.tex",
    }


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
                "classifier_contract_sha256": classifier_contract_sha256(),
            }
        ],
    )
    assert store.dirty_flags()[0]["cause_revision"] == dirty.revision

    output_fingerprint = manifest_fingerprint(capture_artifact_manifest(tmp_path))
    success_receipt = {
        "schema_version": "factory-stage-checkpoint-v1",
        "status": "PASS",
        "stage": 4,
        "output_fingerprint": output_fingerprint,
        "classifier_contract_sha256": classifier_contract_sha256(),
    }
    cleared = store.transition(
        expected_revision=dirty.revision,
        event_type="STAGE_SUCCEEDED_FOR_TEST",
        changes={},
        stage_checkpoint={
            "stage_id": 4,
            "subtask": "canonical_solve",
            "source_step_id": 7,
            "completed_step_id": 7,
            "input_fingerprint": output_fingerprint,
            "output_fingerprint": output_fingerprint,
            "receipt": success_receipt,
        },
        clear_dirty_stage={
            "owner_stage": 4,
            "cleared_fingerprint": output_fingerprint,
            "classifier_contract_sha256": classifier_contract_sha256(),
            "success_receipt": success_receipt,
        },
    )

    assert store.dirty_flags() == []
    receipt = store.dirty_clear_receipts()[0]
    assert receipt["revision"] == cleared.revision
    assert receipt["receipt"]["success_receipt"] == success_receipt


def test_active_dirty_flags_preserve_same_domain_across_multiple_owners(tmp_path):
    store = SQLiteStateStore(tmp_path)
    state = store.initialize(project_id="demo", project_type="modeling")
    dirty = store.transition(
        expected_revision=state.revision,
        event_type="MULTI_OWNER_DIRTY_FOR_TEST",
        changes={},
        dirty_changes=[
            {
                "flag": flag,
                "owner_stage": owner,
                "cause_artifact": artifact,
                "baseline_fingerprint": "a" * 64,
                "current_fingerprint": "b" * 64,
                "classifier_contract_sha256": classifier_contract_sha256(),
            }
            for flag, owner, artifact in (
                ("MODEL_DIRTY", 1, "problem/problem_brief.md"),
                ("MODEL_DIRTY", 3, "model.md"),
                ("RESULT_DIRTY", 4, "results/canonical_results.json"),
                ("RESULT_DIRTY", 5, "sensitivity_report.md"),
            )
        ],
    )

    assert {(row["flag"], row["owner_stage"]) for row in store.dirty_flags()} == {
        ("MODEL_DIRTY", 1),
        ("MODEL_DIRTY", 3),
        ("RESULT_DIRTY", 4),
        ("RESULT_DIRTY", 5),
    }
    assert all(row["cause_revision"] == dirty.revision for row in store.dirty_flags())

    output = manifest_fingerprint(capture_artifact_manifest(tmp_path))
    receipt = {
        "schema_version": "factory-stage-checkpoint-v1",
        "status": "PASS",
        "stage": 1,
        "output_fingerprint": output,
        "classifier_contract_sha256": classifier_contract_sha256(),
    }
    store.transition(
        expected_revision=dirty.revision,
        event_type="CLEAR_ONE_DIRTY_OWNER_FOR_TEST",
        changes={},
        stage_checkpoint={
            "stage_id": 1,
            "subtask": "problem_setup",
            "source_step_id": 0,
            "completed_step_id": 0,
            "input_fingerprint": output,
            "output_fingerprint": output,
            "receipt": receipt,
        },
        clear_dirty_stage={
            "owner_stage": 1,
            "cleared_fingerprint": output,
            "classifier_contract_sha256": classifier_contract_sha256(),
            "success_receipt": receipt,
        },
    )

    assert {(row["flag"], row["owner_stage"]) for row in store.dirty_flags()} == {
        ("MODEL_DIRTY", 3),
        ("RESULT_DIRTY", 4),
        ("RESULT_DIRTY", 5),
    }


@pytest.mark.parametrize(
    ("checkpoint_stage", "fingerprint", "message"),
    [
        (5, None, "owner does not match"),
        (4, "d" * 64, "fingerprint is stale"),
    ],
)
def test_dirty_clear_rejects_wrong_owner_or_stale_fingerprint(
    tmp_path, checkpoint_stage, fingerprint, message
):
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
                "classifier_contract_sha256": classifier_contract_sha256(),
            }
        ],
    )
    current = manifest_fingerprint(capture_artifact_manifest(tmp_path))
    output = fingerprint or current
    receipt = {
        "schema_version": "factory-stage-checkpoint-v1",
        "status": "PASS",
        "stage": checkpoint_stage,
        "output_fingerprint": output,
        "classifier_contract_sha256": classifier_contract_sha256(),
    }

    with pytest.raises(InvalidTransition, match=message):
        store.transition(
            expected_revision=dirty.revision,
            event_type="INVALID_CLEAR_FOR_TEST",
            changes={},
            stage_checkpoint={
                "stage_id": checkpoint_stage,
                "subtask": "test",
                "source_step_id": 7,
                "completed_step_id": 7,
                "input_fingerprint": current,
                "output_fingerprint": output,
                "receipt": receipt,
            },
            clear_dirty_stage={
                "owner_stage": 4,
                "cleared_fingerprint": output,
                "classifier_contract_sha256": classifier_contract_sha256(),
                "success_receipt": receipt,
            },
        )
    assert store.load().revision == dirty.revision


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


def test_final_input_manifest_excludes_final_audit_output(tmp_path):
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("\\begin{document}\nhello\n\\end{document}\n", encoding="utf-8")
    judge = tmp_path / "judge_evaluation.md"
    judge.write_text("VERDICT: OLD\n", encoding="utf-8")
    snapshot = build_final_input_manifest(tmp_path)

    judge.write_text("VERDICT: PASS\n", encoding="utf-8")

    verify_final_input_snapshot(tmp_path, snapshot)


def test_unregistered_authored_artifact_blocks_finalization(tmp_path):
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("\\begin{document}hello\\end{document}\n", encoding="utf-8")
    (tmp_path / "calibration.json").write_text('{"scale": 2}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="ownership coverage.*calibration.json"):
        build_final_input_manifest(tmp_path)


def test_unregistered_inactive_paper_artifact_blocks_finalization(tmp_path):
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("\\begin{document}hello\\end{document}\n", encoding="utf-8")
    draft_data = tmp_path / "paper" / "calibration.json"
    draft_data.parent.mkdir()
    draft_data.write_text('{"scale": 2}\n', encoding="utf-8")

    with pytest.raises(
        ValueError, match="ownership coverage.*paper/calibration.json"
    ):
        build_final_input_manifest(tmp_path)


def test_declared_unowned_attachment_is_covered_and_included(tmp_path):
    paper = tmp_path / f"{tmp_path.name}_paper.tex"
    paper.write_text("\\begin{document}hello\\end{document}\n", encoding="utf-8")
    attachment = tmp_path / "calibration.json"
    attachment.write_text('{"scale": 2}\n', encoding="utf-8")
    deliverables = tmp_path / "problem" / "deliverables.json"
    deliverables.parent.mkdir(parents=True)
    deliverables.write_text(
        '{"attachments": [{"file": "calibration.json"}]}\n',
        encoding="utf-8",
    )

    snapshot = build_final_input_manifest(tmp_path)

    paths = {item["path"] for item in snapshot.manifest["files"]}
    assert "calibration.json" in paths


@dataclass
class MutatingAudit:
    project: object

    def run(self, _context, *, analysis_only=True):
        assert analysis_only is False
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


def test_delivery_returns_events_without_writing_workflow_state_when_input_changes(
    tmp_path,
):
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



def _tex_control_change(tmp_path, before_control, after_control, body):
    paper = tmp_path / "paper" / "paper.tex"
    paper.parent.mkdir(parents=True, exist_ok=True)
    paper.write_text(
        f"{before_control}\n\\begin{{document}}{body}\\end{{document}}\n",
        encoding="utf-8",
    )
    before = capture_artifact_manifest(tmp_path)
    paper.write_text(
        f"{after_control}\n\\begin{{document}}{body}\\end{{document}}\n",
        encoding="utf-8",
    )
    return classify_manifest_changes(before, capture_artifact_manifest(tmp_path))


def test_newif_toggle_change_marks_math_dirty(tmp_path):
    changes = _tex_control_change(
        tmp_path,
        r"\newif\ifshow\showtrue",
        r"\newif\ifshow\showfalse",
        r"\ifshow \(x=1\)\else \(x=2\)\fi",
    )
    assert any(item.flag is DirtyFlag.MATH and item.owner_stage == 8 for item in changes)


def test_ifthen_boolean_change_marks_math_dirty(tmp_path):
    changes = _tex_control_change(
        tmp_path,
        r"\newboolean{show}\setboolean{show}{true}",
        r"\newboolean{show}\setboolean{show}{false}",
        r"\ifthenelse{\boolean{show}}{\(x=1\)}{\(x=2\)}",
    )
    assert any(item.flag is DirtyFlag.MATH for item in changes)


def test_etoolbox_toggle_change_marks_math_dirty(tmp_path):
    changes = _tex_control_change(
        tmp_path,
        r"\newtoggle{show}\toggletrue{show}",
        r"\newtoggle{show}\togglefalse{show}",
        r"\iftoggle{show}{\(x=1\)}{\(x=2\)}",
    )
    assert any(item.flag is DirtyFlag.MATH for item in changes)


def test_expl3_boolean_change_marks_math_dirty(tmp_path):
    changes = _tex_control_change(
        tmp_path,
        r"\bool_new:N \l_show_bool \bool_set_true:N \l_show_bool",
        r"\bool_new:N \l_show_bool \bool_set_false:N \l_show_bool",
        r"\bool_if:NTF \l_show_bool {\(x=1\)} {\(x=2\)}",
    )
    assert any(item.flag is DirtyFlag.MATH for item in changes)


def test_ifcase_branch_control_change_marks_math_dirty(tmp_path):
    changes = _tex_control_change(
        tmp_path,
        r"\newcount\choice\choice=0",
        r"\newcount\choice\choice=1",
        r"\ifcase\choice \(x=1\)\or \(x=2\)\fi",
    )
    assert any(item.flag is DirtyFlag.MATH for item in changes)


def test_counter_control_change_marks_math_dirty(tmp_path):
    changes = _tex_control_change(
        tmp_path,
        r"\newcounter{choice}\setcounter{choice}{0}",
        r"\newcounter{choice}\setcounter{choice}{1}",
        r"\ifcase\value{choice} \(x=1\)\or \(x=2\)\fi",
    )
    assert any(item.flag is DirtyFlag.MATH for item in changes)


def test_stage9_conditional_formula_change_reopens_stage8_and_invalidates_precheck(tmp_path):
    changes = _tex_control_change(
        tmp_path,
        r"\newif\ifshow\showtrue",
        r"\newif\ifshow\showfalse",
        r"\ifshow \(x=1\)\else \(x=2\)\fi",
    )
    math = [item for item in changes if item.flag is DirtyFlag.MATH]
    assert math
    assert {item.owner_stage for item in math} == {8}
    assert reopen_after_for_changed_paths([item.cause_artifact for item in math]) <= 12
