from pathlib import Path


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_complete_project(project: Path) -> None:
    base = project.name
    root = project.parents[1]

    for name, text in {
        "problem/source.md": "# source\n",
        "problem/problem_brief.md": "# brief\n",
        "problem/terminology_table.md": "\n".join(["term"] * 5) + "\n",
        "problem/data_inventory.md": "\n".join(["data"] * 5) + "\n",
        "problem/feasibility_constraints.md": "\n".join(["limit"] * 5) + "\n",
        "problem/candidate_methods.md": "method_library/demo.md\n",
        "problem/method_retrieval.md": "# retrieval\n",
        "research_brief.md": "\n".join(["research"] * 30) + "\n",
        "viable_streams.md": "## Stream m1:\nmethod_library/demo.md\n## Stream m2:\nmethod_library/demo.md\n",
        "viability_gate.md": "VERDICT: PASS\n" + "\n".join(["ok"] * 10) + "\n",
        "method_decision.md": "\n".join(["decision"] * 30) + "\n",
        "chosen_method.md": "PRIMARY: m1\n" + "\n".join(["chosen"] * 10) + "\n",
        "model.md": "\n".join(["model"] * 100) + "\n",
        "symbol_table.md": "\n".join(["symbol"] * 10) + "\n",
        "assumption_ledger.md": "\n".join(["assumption"] * 10) + "\n",
        "solve_log.md": "\n".join(["solve"] * 20) + "\n",
        "sensitivity_report.md": "\n".join(["sensitivity"] * 20) + "\n",
        "evaluation.md": "\n".join(["evaluation"] * 30) + "\n",
        "visualization_log.md": "\n".join(["viz"] * 20) + "\n",
        "code_review.md": "\n".join(["clean"] * 20) + "\n",
        "review_comments.md": "\n".join(["review"] * 30) + "\n",
        "revision_summary.md": "\n".join(["revision"] * 10) + "\n",
        "judge_evaluation.md": "VERDICT: PASS\n" + "\n".join(["judge"] * 30) + "\n",
        "abstract_draft.md": "\n".join(["abstract"] * 20) + "\n",
        "citation_audit.md": "\n".join(["cite"] * 10) + "\n",
        "derobotification.md": "\n".join(["polish"] * 10) + "\n",
        f"{base}_paper.tex": "\\begin{document}\nfinal text\n\\end{document}\n",
        "results/p1/values.json": "{\"ok\": true}\n",
        "logs/solver.log": "ok\n",
        "checkpoint.md": "- **Last completed step**: 16\n",
    }.items():
        write_file(project / name, text)

    write_file(project / "m1_critique.md", "VERDICT: VALIDATED\n")
    write_file(project / "m2_critique.md", "VERDICT: VALIDATED\n")
    write_file(project / "figures" / "figure.pdf", "fake\n")
    write_file(project / "paper" / "archive" / "pre_step12" / "note.txt", "archive\n")
    write_file(root / "papers" / f"{base}_paper.pdf", "pdf\n")
    write_file(root / "papers" / f"{base}_submission.zip", "not a real zip\n")


def test_evaluate_flags_missing_step_8_5_artifacts(tmp_path, monkeypatch):
    project = tmp_path / "ongoing" / "demo"
    make_complete_project(project)

    from scripts import evaluate_modeling_project as mod

    monkeypatch.setattr(mod, "infer_step", lambda root, project: (16, "16"))
    monkeypatch.setattr(mod, "zip_ok", lambda path: (True, "ok"))

    ev = mod.evaluate(project, tmp_path)
    checks = {check.name: check for check in ev.checks}

    assert checks["artifact:reviewer_entry_map.md"].ok is False
    assert checks["artifact:anchor_figure_plan.md"].ok is False
    assert checks["artifact:entry_gate.md"].ok is False
    assert checks["entry_gate_verdict"].ok is False


def test_evaluate_accepts_step_8_5_pass_artifacts(tmp_path, monkeypatch):
    project = tmp_path / "ongoing" / "demo"
    make_complete_project(project)
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "# gate\n\nVERDICT: PASS\n")

    from scripts import evaluate_modeling_project as mod

    monkeypatch.setattr(mod, "infer_step", lambda root, project: (16, "16"))
    monkeypatch.setattr(mod, "zip_ok", lambda path: (True, "ok"))

    ev = mod.evaluate(project, tmp_path)
    checks = {check.name: check for check in ev.checks}

    assert checks["artifact:reviewer_entry_map.md"].ok is True
    assert checks["artifact:anchor_figure_plan.md"].ok is True
    assert checks["artifact:entry_gate.md"].ok is True
    assert checks["entry_gate_verdict"].ok is True


def _v2_state(project: Path) -> None:
    from factory_core.stages import STAGE_CATALOG_VERSION_V2, STAGE_SCHEDULER_GENERATION
    from factory_core.storage import SQLiteStateStore

    SQLiteStateStore(project, clock=lambda: 100).initialize(
        project_id=project.name,
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        stage_catalog_version=STAGE_CATALOG_VERSION_V2,
    )


def _evaluate(tmp_path, monkeypatch, project):
    from scripts import evaluate_modeling_project as mod

    monkeypatch.setattr(mod, "infer_step", lambda root, project: (16, "16"))
    monkeypatch.setattr(mod, "zip_ok", lambda path: (True, "ok"))
    return {check.name: check for check in mod.evaluate(project, tmp_path).checks}


def _stale_gate_project(tmp_path, *, v2: bool) -> Path:
    import os

    project = tmp_path / "ongoing" / "demo"
    make_complete_project(project)
    if v2:
        _v2_state(project)
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "# gate\n\nVERDICT: PASS\n")
    later = (project / "entry_gate.md").stat().st_mtime + 60
    os.utime(project / "solve_log.md", (later, later))
    return project


def test_v2_delivery_evaluation_accepts_a_passed_entry_gate_with_later_inputs(tmp_path, monkeypatch):
    # s16 rev 863: the same v2 rule as step16_ready (F23).
    v2 = _stale_gate_project(tmp_path / "v2", v2=True)
    assert _evaluate(tmp_path / "v2", monkeypatch, v2)["entry_gate_verdict"].ok is True

    v1 = _stale_gate_project(tmp_path / "v1", v2=False)
    assert _evaluate(tmp_path / "v1", monkeypatch, v1)["entry_gate_verdict"].ok is False


def _ledger(project: Path, status: str) -> None:
    write_file(
        project / "audit_issue_ledger.md",
        "| ID | Category | Severity | Status | Blocking Until | Notes |\n"
        "|---|---|---|---|---|---|\n"
        f"| S11R-B1 | paper | BLOCKING | {status} | Human (交付前) | cover fields |\n",
    )


def test_v2_code_review_markers_defer_to_the_ledger(tmp_path, monkeypatch):
    # s16 rev 863: code_review.md still names a human-owned blocking row that
    # the ledger no longer counts; delivery evaluation failed on the word alone.
    project = tmp_path / "ongoing" / "demo"
    make_complete_project(project)
    _v2_state(project)
    write_file(project / "code_review.md", "D-J5 [paper-issue-1, blocking] cover fields need the team\n")

    _ledger(project, "RESOLVED")
    assert _evaluate(tmp_path, monkeypatch, project)["gate1_numeric_trace"].ok is True

    _ledger(project, "OPEN")
    assert _evaluate(tmp_path, monkeypatch, project)["gate1_numeric_trace"].ok is False


def test_v1_code_review_markers_still_fail(tmp_path, monkeypatch):
    project = tmp_path / "ongoing" / "demo"
    make_complete_project(project)
    write_file(project / "code_review.md", "UNRESOLVED: numeric trace for table 3\n")
    _ledger(project, "RESOLVED")

    assert _evaluate(tmp_path, monkeypatch, project)["gate1_numeric_trace"].ok is False


def test_v2_code_review_markers_honour_a_gate2_acknowledgement(tmp_path, monkeypatch):
    # The row stays OPEN; only the content-freeze approval's acknowledgement
    # (read through ledger.acknowledged_human_issues) stops it counting.
    import factory_core.audit.ledger as ledger

    project = tmp_path / "ongoing" / "demo"
    make_complete_project(project)
    _v2_state(project)
    write_file(project / "code_review.md", "D-J5 [paper-issue-1, blocking] cover fields need the team\n")
    _ledger(project, "OPEN")

    monkeypatch.setattr(ledger, "acknowledged_human_issues", lambda _project: frozenset({"S11R-B1"}))
    assert _evaluate(tmp_path, monkeypatch, project)["gate1_numeric_trace"].ok is True

    monkeypatch.setattr(ledger, "acknowledged_human_issues", lambda _project: frozenset({"S11R-B9"}))
    assert _evaluate(tmp_path, monkeypatch, project)["gate1_numeric_trace"].ok is False
