"""Acceptance A follow-ups: visual-gate routing and early contract checks.

Item 1 -- the PDF visual gate ran only in the final audit, its findings were
routed to the revision Step although figures belong to Step 8 (F19(b)), and
every audit overwrote the routing evidence (F19(d)).  Item 2 -- Step 4 declared
result paths Step 5 never wrote (F1), and a ``model`` review row was left "to
Step 4", which a v2 recompute never re-runs (C4).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from factory_core.adapters.infrastructure.commands import CommandResult
from factory_core.audit import AuditStatus, IncrementalAuditService
from factory_core.audit.incremental import declared_result_path_issues
from factory_core.audit.service import FinalAuditService
from factory_core.audit.visual_routing import (
    figure_owner_steps,
    figure_rework_plan,
    finding_owner,
)
from factory_core.engine import VISUAL_FIGURE_REWORK_RULE, FactoryEngine
from factory_core.stages import CATALOG_V1, CATALOG_V2, STAGE_SCHEDULER_GENERATION
from factory_core.storage import SQLiteStateStore
from tests.test_incremental_audit import FakeRunner, lines, valid_model, write
from tests.test_stage_catalog_v2 import (
    Lifecycle,
    _checkpoint_steps,
    _judge_reopen,
    _render,
    succeeded_sequence,
    v2_registry,
    v2_store,
)

# The blocking findings of acceptance A's last final Judge (rev 806).
ACCEPTANCE_A_VISUAL = {
    "status": "FAIL",
    "blocking_findings": 3,
    "findings": [
        {"code": "CONTENT_OUTSIDE_MEDIA_BOX", "severity": "blocking", "page": 8,
         "message": "PDF content extends outside the physical page box",
         "evidence": {"content_bbox": [94.7, 70.3, 500.6, 1160.1]}},
        {"code": "UNREADABLY_SMALL_TEXT", "severity": "blocking", "page": 5,
         "message": "text is rendered at 3pt", "evidence": {"font_size": 3, "text": "end"}},
        {"code": "BLANK_RENDERED_PAGE", "severity": "blocking", "page": 7,
         "message": "rendered page is effectively blank", "evidence": {"nonwhite_fraction": 0.0001}},
        {"code": "VERY_SMALL_TEXT", "severity": "warning", "page": 5,
         "message": "text is rendered at 5pt", "evidence": {"font_size": 5, "text": "附录"}},
    ],
}
LAYOUT_ONLY_VISUAL = {
    "status": "FAIL",
    "findings": [item for item in ACCEPTANCE_A_VISUAL["findings"]
                 if item["code"] != "UNREADABLY_SMALL_TEXT"],
}


def _write_visual(project: Path, report: dict) -> None:
    (project / "judge_outputs").mkdir(parents=True, exist_ok=True)
    (project / "judge_outputs" / "visual_gate.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )


# -- F19(b): which Step owns a visual finding --------------------------------

def test_figure_findings_are_owned_by_the_figure_step_of_the_registry():
    assert finding_owner("UNREADABLY_SMALL_TEXT") == "figure"
    assert finding_owner("LOW_IMAGE_RESOLUTION") == "figure"
    for code in ("CONTENT_OUTSIDE_MEDIA_BOX", "BLANK_RENDERED_PAGE", "OVERFULL_HBOX",
                 "PAGE_LIMIT_EXCEEDED", "CONTENT_TOUCHES_RENDER_EDGE"):
        assert finding_owner(code) == "layout", code
    # figures/** belongs to the visualization Stage in both catalogs.
    assert figure_owner_steps(CATALOG_V1) == [8]
    assert figure_owner_steps(CATALOG_V2) == [8]


def test_only_blocking_figure_findings_ask_for_a_figure_recompute():
    plan = figure_rework_plan(ACCEPTANCE_A_VISUAL, CATALOG_V2)
    assert plan == {"steps": [8], "codes": ["UNREADABLY_SMALL_TEXT"],
                    "artifact": "figures/visual_gate_finding.pdf"}
    assert figure_rework_plan(LAYOUT_ONLY_VISUAL, CATALOG_V2) is None
    assert figure_rework_plan({**ACCEPTANCE_A_VISUAL, "status": "PASS"}, CATALOG_V2) is None
    assert figure_rework_plan({**ACCEPTANCE_A_VISUAL, "status": "INDETERMINATE"}, CATALOG_V2) is None
    assert figure_rework_plan(None, CATALOG_V2) is None
    # A malformed FAIL report (findings not a list) asks for nothing, not a crash.
    for findings in (3, {"code": "UNREADABLY_SMALL_TEXT"}, "blocking", None):
        assert figure_rework_plan({"status": "FAIL", "findings": findings}, CATALOG_V2) is None


def test_the_final_audit_asks_for_a_figure_recompute_only_in_v2(tmp_path):
    v2 = tmp_path / "v2"
    v2_store(v2, last_completed_step=15)
    _write_visual(v2, ACCEPTANCE_A_VISUAL)
    assert FinalAuditService._figure_rework(v2)["steps"] == [8]
    _write_visual(v2, LAYOUT_ONLY_VISUAL)
    assert FinalAuditService._figure_rework(v2) is None

    v1 = tmp_path / "v1"
    SQLiteStateStore(v1).initialize(
        project_id="v1", project_type="modeling", scheduler_generation=STAGE_SCHEDULER_GENERATION
    )
    _write_visual(v1, ACCEPTANCE_A_VISUAL)
    assert FinalAuditService._figure_rework(v1) is None
    assert FinalAuditService._figure_rework(tmp_path / "no-state") is None


def test_the_figure_rework_reads_the_catalog_without_touching_the_live_database(tmp_path):
    # Analysis-only audits reach _figure_rework; load() would upgrade the
    # schema and switch the live database to WAL.
    v2_store(tmp_path, last_completed_step=15)
    _write_visual(tmp_path, ACCEPTANCE_A_VISUAL)
    database = tmp_path / ".factory" / "state.db"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    connection.close()
    before = database.read_bytes()

    assert FinalAuditService._figure_rework(tmp_path)["steps"] == [8]

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    connection.close()
    assert database.read_bytes() == before
    assert not Path(f"{database}-wal").exists()


def test_an_unreadable_database_records_the_skipped_figure_recompute(tmp_path, monkeypatch):
    v2_store(tmp_path, last_completed_step=15)

    def unreadable(self):
        raise ValueError("workflow catalog version cannot be read without mutation")

    monkeypatch.setattr(SQLiteStateStore, "read_stage_catalog_version", unreadable)
    _write_visual(tmp_path, ACCEPTANCE_A_VISUAL)
    record = FinalAuditService._figure_rework(tmp_path)
    assert record == {
        "skipped": "catalog_unreadable",
        "reason": "workflow catalog version cannot be read without mutation",
        "codes": ["UNREADABLY_SMALL_TEXT"],
    }
    assert "steps" not in record
    # Without figure findings there is nothing to skip.
    _write_visual(tmp_path, LAYOUT_ONLY_VISUAL)
    assert FinalAuditService._figure_rework(tmp_path) is None


def _figure_reopen_judge():
    return Lifecycle(result={
        "resume_after_step": 11,
        "final_decision": "REOPEN_REVISION_TEXT",
        "visual_recompute_steps": [8],
        "visual_recompute_codes": ["UNREADABLY_SMALL_TEXT"],
    })


def test_v2_figure_reopen_redraws_figures_then_returns_to_the_revision(tmp_path):
    judge = _figure_reopen_judge()
    registry, lifecycles = v2_registry(final_judge=judge)
    store = v2_store(tmp_path, last_completed_step=15)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)

    engine.run(max_steps=4)  # abstract, polish, content-freeze guard, then the Judge reopens

    [reopened] = [e for e in store.events() if e.type == "STEP_REOPENED"]
    assert reopened.payload["subtask"] == "final_judge"
    assert reopened.payload["rework_rule"] == VISUAL_FIGURE_REWORK_RULE
    assert reopened.payload["resume_after_step"] == 7
    assert reopened.payload["text_resume_after_step"] == 11
    assert reopened.payload["recompute_steps"] == [8]
    assert reopened.payload["return_subtask"] == "revision"
    assert reopened.payload["recompute_reasons"] == [
        "PDF visual gate UNREADABLY_SMALL_TEXT: figures are owned by Step 8"
    ]
    # Visualization and everything after the revision lost their checkpoints;
    # the draft, the numbers gate and the review kept theirs.  The reopen is
    # not a counted step, so the same run already re-drew the figures.
    assert lifecycles[8].calls == [1]
    assert _checkpoint_steps(store) == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert store.load().last_completed_step == 8

    state = engine.run()

    assert succeeded_sequence(store, after_revision=reopened.revision) == [
        (6, "visualization"), (8, "revision"), (9, "abstract"), (9, "polish"),
        (10, "content_freeze_guard"), (10, "final_judge"), (10, "delivery"),
    ]
    for step in (9, 10, 11):
        assert lifecycles[step].calls == [], step
    assert lifecycles[8].calls == [1]
    assert judge.calls == [1, 1]
    assert state.last_completed_step == 16


def test_v2_layout_only_reopen_still_resumes_at_the_revision(tmp_path):
    judge = Lifecycle(result={"resume_after_step": 11, "final_decision": "REOPEN_REVISION_TEXT"})
    registry, lifecycles = v2_registry(final_judge=judge)
    store = v2_store(tmp_path, last_completed_step=15)

    FactoryEngine(tmp_path, store=store, registry=registry).run()

    [reopened] = [e for e in store.events() if e.type == "STEP_REOPENED"]
    assert reopened.payload["resume_after_step"] == 11
    assert "rework_rule" not in reopened.payload
    assert lifecycles[8].calls == []


def test_a_model_reopen_ignores_the_figure_plan(tmp_path):
    # Resuming after Step 3 re-runs visualization anyway.
    judge = Lifecycle(result={
        "resume_after_step": 3, "final_decision": "REOPEN_REVISION_MODEL",
        "visual_recompute_steps": [8],
    })
    registry, _ = v2_registry(final_judge=judge)
    store = v2_store(tmp_path, last_completed_step=15)

    FactoryEngine(tmp_path, store=store, registry=registry).run(max_steps=4)

    [reopened] = [e for e in store.events() if e.type == "STEP_REOPENED"]
    assert reopened.payload["resume_after_step"] == 3
    assert "rework_rule" not in reopened.payload


def test_the_figure_reopen_block_reaches_visualization_and_names_the_route(tmp_path):
    store = v2_store(tmp_path, last_completed_step=15)
    _judge_reopen(store, tmp_path, verdict="REOPEN_REVISION_TEXT", target=11,
                  aggregate_verdict="PASS")
    (tmp_path / "judge_outputs" / "decision_route.json").write_text(json.dumps(
        {"effective_decision": "REOPEN_REVISION_TEXT"}))
    _write_visual(tmp_path, ACCEPTANCE_A_VISUAL)
    # Replace the plain reopen with the engine's figure route.
    state = store.load()
    store.transition(
        expected_revision=state.revision, event_type="STEP_REOPENED", changes={},
        payload={"stage": 10, "subtask": "final_judge", "source_step": 13,
                 "final_decision": "REOPEN_REVISION_TEXT", "resume_after_step": 7,
                 "text_resume_after_step": 11, "rework_rule": "visual_figure",
                 "recompute_steps": [8], "return_subtask": "revision"},
    )

    step8 = _render(tmp_path, "step8_visualization.txt", "8")
    assert "【最终评审返工" in step8
    assert "先重跑 Step 8" in step8 and "回到 Step 11 之后" in step8
    assert "[visual UNREADABLY_SMALL_TEXT，blocking，PDF 第 5 页]" in step8
    assert "图的问题：在 Step 8 的绘图脚本里修" in step8
    assert "版面问题：在 paper.tex 里修" in step8
    assert "【最终评审返工" in _render(tmp_path, "step12_revision.txt", "12")
    assert "【最终评审返工" not in _render(tmp_path, "step7_model_eval.txt", "7")


def test_visual_findings_travel_with_a_content_verdict_too(tmp_path):
    store = v2_store(tmp_path, last_completed_step=15)
    _judge_reopen(store, tmp_path, verdict="REOPEN_REVISION_MODEL", target=3)
    _write_visual(tmp_path, ACCEPTANCE_A_VISUAL)

    step4 = _render(tmp_path, "step4_model_construction.txt", "4")

    assert "math-e1" in step4
    assert "[visual BLANK_RENDERED_PAGE，blocking，PDF 第 7 页]" in step4


# -- F19(d): every attempt keeps the routing evidence it saw ----------------

def test_each_final_audit_attempt_archives_its_routing_evidence(tmp_path):
    project = tmp_path / "demo"
    audit_dir = project / ".factory/audits/" / ("a" * 64)
    attempts = audit_dir / "attempts"
    attempts.mkdir(parents=True)
    _write_visual(project, ACCEPTANCE_A_VISUAL)
    (project / "judge_outputs/aggregate.json").write_text('{"verdict": "PASS"}')

    first = attempts / "20261108T121700.000000Z.json"
    FinalAuditService._archive_judge_outputs(project, audit_dir, first)
    _write_visual(project, LAYOUT_ONLY_VISUAL)
    second = attempts / "20261108T130000.000000Z.json"
    FinalAuditService._archive_judge_outputs(project, audit_dir, second)

    one = json.loads(first.with_name(first.stem + ".judge_outputs.json").read_text())
    two = json.loads(second.with_name(second.stem + ".judge_outputs.json").read_text())
    assert set(one["files"]) == {"visual_gate.json", "aggregate.json"}
    assert one["files"]["aggregate.json"] == two["files"]["aggregate.json"]  # stored once
    assert one["files"]["visual_gate.json"] != two["files"]["visual_gate.json"]
    archived = project / one["files"]["visual_gate.json"]["archived"]
    assert archived.name.endswith(".visual_gate.json.archive")
    assert json.loads(archived.read_text())["findings"][1]["code"] == "UNREADABLY_SMALL_TEXT"
    # The earlier Judge's visual gate is recoverable after the overwrite.
    assert len(list((audit_dir / "judge_outputs").iterdir())) == 3
    assert not list((audit_dir / "judge_outputs").glob("*.json"))


def test_an_attempt_without_judge_outputs_writes_no_sidecar(tmp_path):
    project = tmp_path / "demo"
    audit_dir = project / ".factory/audits/snap"
    (audit_dir / "attempts").mkdir(parents=True)
    attempt = audit_dir / "attempts" / "x.json"
    FinalAuditService._archive_judge_outputs(project, audit_dir, attempt)
    assert list((audit_dir / "attempts").iterdir()) == []


# -- item 6: the paper profile runs the visual gate early, as advice --------

class VisualRunner(FakeRunner):
    def __init__(self, report: dict | None, *, exit_code: int = 1) -> None:
        super().__init__()
        self.report = report
        self.exit_code = exit_code

    def python(self, root, project, script, args, *, label, log_path, **kwargs):
        if label != "audit_paper_visual_gate":
            return super().python(root, project, script, args, label=label, log_path=log_path, **kwargs)
        self.calls.append(label)
        output = Path(args[args.index("--output") + 1])
        if self.report is not None:
            write(output, json.dumps(self.report))
        write(Path(log_path), "visual gate\n")
        return CommandResult(self.exit_code, True, Path(log_path))


def _paper_project(tmp_path: Path, *, pdf: bool = True) -> Path:
    project = tmp_path / "factory" / "ongoing" / "demo"
    valid_model(project)
    write(project / "demo_paper.tex", lines(30))
    write(project / "code_review.md", lines(20))
    if pdf:
        (project / "demo_paper.pdf").write_bytes(b"%PDF-1.5\n")
    return project


def test_paper_profile_reports_visual_findings_without_failing_or_touching_the_ledger(tmp_path):
    project = _paper_project(tmp_path)
    runner = VisualRunner(ACCEPTANCE_A_VISUAL)

    outcome = IncrementalAuditService(tmp_path / "factory", runner=runner).run_project(
        project, "paper"
    )

    assert outcome.record.status is AuditStatus.PASS
    assert "audit_paper_visual_gate" in runner.calls
    [check] = [c for c in outcome.record.evidence["checks"] if c["name"] == "visual_gate"]
    assert check["severity"] == "advisory" and check["passed"] is False
    assert check["detail"] == "status=FAIL blocking=3"
    assert check["report"] == ".factory/audits/profiles/paper/visual_gate.latest.json"
    assert outcome.record.evidence["warnings"] == []
    assert outcome.record.evidence["hard_failures"] == []
    # No ledger row: a v1 Stage 8 exit blocks on any open MAJOR row.
    ledger = project / "audit_issue_ledger.md"
    assert "VISUAL" not in (ledger.read_text(encoding="utf-8") if ledger.is_file() else "")
    assert json.loads((project / check["report"]).read_text())["status"] == "FAIL"


def test_paper_profile_without_a_pdf_or_verdict_says_so(tmp_path):
    project = _paper_project(tmp_path, pdf=False)
    # A report left by an earlier run must not reach this run's review Steps.
    stale = project / ".factory/audits/profiles/paper/visual_gate.latest.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps(ACCEPTANCE_A_VISUAL), encoding="utf-8")
    outcome = IncrementalAuditService(
        tmp_path / "factory", runner=VisualRunner(ACCEPTANCE_A_VISUAL)
    ).run_project(project, "paper")
    [check] = [c for c in outcome.record.evidence["checks"] if c["name"] == "visual_gate"]
    assert outcome.record.status is AuditStatus.PASS
    assert check["indeterminate"] is True and "no compiled demo_paper.pdf" in check["detail"]
    assert not stale.exists()

    project = _paper_project(tmp_path / "second")
    outcome = IncrementalAuditService(
        tmp_path / "second" / "factory",
        runner=VisualRunner({"status": "INDETERMINATE", "error": "mutool missing"}, exit_code=2),
    ).run_project(project, "paper")
    [check] = [c for c in outcome.record.evidence["checks"] if c["name"] == "visual_gate"]
    assert outcome.record.status is AuditStatus.PASS
    assert check["indeterminate"] is True and check["detail"] == "no verdict: mutool missing"


def test_the_final_audit_paper_suite_does_not_run_the_early_gate(tmp_path):
    project = _paper_project(tmp_path)
    runner = VisualRunner(ACCEPTANCE_A_VISUAL)
    checks = IncrementalAuditService(tmp_path / "factory", runner=runner).paper_checks(project)
    assert "visual_gate" not in {check.name for check in checks}
    assert "audit_paper_visual_gate" not in runner.calls


def test_v2_review_and_revision_prompts_use_the_early_visual_gate(tmp_path):
    v2_store(tmp_path, last_completed_step=10)
    step11 = _render(tmp_path, "step11_constructive_review.txt", "11")
    assert ".factory/audits/profiles/paper/visual_gate.latest.json" in step11
    assert "Category 写 `paper`" in step11
    step12 = _render(tmp_path, "step12_revision.txt", "12")
    assert "pdf_visual_gate.py" in step12 and "${TMPDIR:-/tmp}" in step12


# -- F1: declared result paths must be where Step 5 writes -----------------

def test_declared_result_paths_must_use_the_step5_subproblem_directories():
    registry = {"claims": [{"artifacts": [
        {"path": "results/q1/values.json"},
        {"path": "results/problem2/values.json"},
        {"path": "results/problem4a/bound.json"},
        {"path": "results/canonical_results.json"},
        {"path": "results/sensitivity/summary.json"},
        {"path": "model.md"},
    ]}], "delivery_requirements": [{"artifacts": [{"path": "./results/Q3/values.json"}]}]}
    contract = {"competitiveness_checks": [{
        "result": {"path": "results/problem1/values.json"},
        "bound": {"path": "results/p1/bound.json"},
        "ladder": {"path": "results/problem1/convergence.json"},
    }]}

    assert declared_result_path_issues(registry, contract) == [
        "results/Q3/values.json", "results/p1/bound.json", "results/q1/values.json",
    ]
    assert declared_result_path_issues(None, None) == []


def _declare(project: Path, path: str) -> None:
    registry = json.loads((project / "claim_registry.json").read_text())
    registry["claims"][0]["artifacts"].append({"path": path, "roles": ["execution"]})
    (project / "claim_registry.json").write_text(json.dumps(registry))


def test_the_model_snapshot_binds_the_owner_rule_of_claim_result_paths(tmp_path):
    from factory_core.audit.domain import AuditProfile

    files = IncrementalAuditService(tmp_path)._checker_files(AuditProfile.MODEL)
    assert "factory_core/artifact_ownership.py" in files
    assert "factory_core/artifact_policy.py" in files


def test_model_audit_fails_at_step4_on_a_path_step5_will_not_write(tmp_path):
    project = tmp_path / "factory" / "ongoing" / "demo"
    valid_model(project)
    _declare(project, "results/q1/values.json")

    outcome = IncrementalAuditService(tmp_path / "factory", runner=FakeRunner()).run_project(
        project, "model"
    )

    assert outcome.record.status is AuditStatus.FAIL
    assert outcome.record.evidence["hard_failures"] == ["claim_result_paths"]
    assert outcome.record.evidence["repair_step"] == 4
    [check] = [c for c in outcome.record.evidence["checks"] if c["name"] == "claim_result_paths"]
    assert "results/q1/values.json" in check["detail"]
    assert "AUDIT-MODEL-CLAIM-RESULT-PATHS" in (project / "audit_issue_ledger.md").read_text()

    registry = json.loads((project / "claim_registry.json").read_text())
    registry["claims"][0]["artifacts"][-1]["path"] = "results/problem1/values.json"
    (project / "claim_registry.json").write_text(json.dumps(registry))
    outcome = IncrementalAuditService(tmp_path / "factory", runner=FakeRunner()).run_project(
        project, "model"
    )
    assert outcome.record.status is AuditStatus.PASS


def test_v2_solve_prompt_binds_the_declared_result_paths(tmp_path):
    v2_store(tmp_path, last_completed_step=4)
    step5 = _render(tmp_path, "step5_full_solve.txt", "5")
    assert "这些路径就是本步骤必须写出的文件" in step5


# -- C4: a recompute cannot leave a model row to Step 4 ---------------------

def _review_recompute(store, project, rows):
    ledger = ["| Issue ID | Raised In | Category | Severity | Blocking Until | Status | Notes |",
              "|---|---|---|---|---|---|---|"]
    ledger += [f"| {issue} | Step 11 | {category} | MAJOR | Step 12 | OPEN | {notes} |"
               for issue, category, notes in rows]
    (project / "audit_issue_ledger.md").write_text("\n".join(ledger) + "\n", encoding="utf-8")
    state = store.load()
    store.transition(
        expected_revision=state.revision, event_type="STAGE_SEMANTIC_REOPENED", changes={},
        payload={"rework_rule": "review_issue", "recompute_steps": [5, 6, 7, 8],
                 "return_subtask": "revision",
                 "review_issues": [f"{issue} ({category}, MAJOR)" for issue, category, _ in rows]},
    )


def test_a_model_review_row_is_decided_by_the_first_recomputed_step(tmp_path):
    store = v2_store(tmp_path, last_completed_step=12)
    _review_recompute(store, tmp_path, [
        ("S11R-J8", "model", "温度方程收缩项的 (a)/(b) 建模决定留给了 Step 4"),
        ("S11R-M2", "results", "solve_log.md 仍写着“未完成/前置条件”"),
    ])

    step5 = _render(tmp_path, "step5_full_solve.txt", "5")
    assert "Step 4 在这次重算中不会重跑" in step5
    assert "不得以“属于 Step 4”为由不改" in step5
    assert "本步骤自己的报告（`solve_log.md`）" in step5
    step6 = _render(tmp_path, "step6_sensitivity.txt", "6")
    assert "由 Step 5 作出决定" in step6
    assert "本步骤自己的报告（`sensitivity_report.md`）" in step6


def test_a_results_only_recompute_does_not_mention_step4(tmp_path):
    store = v2_store(tmp_path, last_completed_step=12)
    _review_recompute(store, tmp_path, [("S11R-M2", "results", "solve_log.md 过时")])

    step5 = _render(tmp_path, "step5_full_solve.txt", "5")
    assert "【审稿返工" in step5 and "S11R-M2" in step5
    assert "Step 4 在这次重算中不会重跑" not in step5
    assert "就地改写" in step5
