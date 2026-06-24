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
