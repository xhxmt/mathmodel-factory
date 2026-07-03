import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from conftest import REPO_ROOT


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("paper.pdf", "fake")


def make_project_with_delivery(factory: Path, base: str, verdict: str = "PASS") -> Path:
    project = factory / "ongoing" / base
    paper = project / f"{base}_paper.tex"
    write_file(project / "problem" / "problem_brief.md", "# brief\n")
    write_file(project / "checkpoint.md", "- **Last completed step**: 15\n")
    write_file(project / "judge_evaluation.md", f"VERDICT: {verdict}\n" + "\n".join(["judge"] * 30) + "\n")
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(paper, "\\begin{document}\n" + "\n".join(["paper"] * 220) + "\n\\end{document}\n")
    write_file(factory / "papers" / f"{base}_paper.pdf", "%PDF fake\n")
    write_zip(factory / "papers" / f"{base}_submission.zip")
    return project


def test_infer_step_does_not_report_16_when_gate2_is_not_pass(tmp_path):
    project = make_project_with_delivery(tmp_path, "demo_reopen", "REOPEN_REVISION_TEXT")

    out = subprocess.run(
        [os.path.join(REPO_ROOT, "run_paper.sh"), "--infer-step", str(project)],
        env={**os.environ, "FACTORY": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() != "16"


def test_verify_step_output_rejects_step9_without_step8_5_pass(tmp_path):
    project = tmp_path / "ongoing" / "demo_step9"
    write_file(project / "problem" / "problem_brief.md", "# brief\n")
    write_file(project / "checkpoint.md", "- **Last completed step**: 8\n")
    write_file(project / "visualization_log.md", "\n".join(["viz"] * 20) + "\n")
    write_file(project / "figures" / "anchor.pdf", "fake\n")
    write_file(
        project / "demo_step9_paper.tex",
        "\\begin{document}\nABSTRACT_PLACEHOLDER\n" + "\n".join(["paper"] * 220) + "\n\\end{document}\n",
    )

    out = subprocess.run(
        [os.path.join(REPO_ROOT, "run_paper.sh"), "--infer-step", str(project)],
        env={**os.environ, "FACTORY": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "8"


def test_evaluator_rejects_incomplete_canonical_results(tmp_path, monkeypatch):
    project = tmp_path / "ongoing" / "demo"
    from test_evaluate_modeling_project_step8_5 import make_complete_project

    make_complete_project(project)
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(project / "results" / "p1" / "values.json", '{"problem": 1, "status": "CONVERGED", "objective": 1.0}\n')
    write_file(project / "results" / "p2" / "values.json", '{"problem": 2, "status": "RUNNING"}\n')

    from scripts import evaluate_modeling_project as mod

    monkeypatch.setattr(mod, "infer_step", lambda root, project: (16, "16"))
    monkeypatch.setattr(mod, "zip_ok", lambda path: (True, "ok"))

    ev = mod.evaluate(project, tmp_path)
    checks = {check.name: check for check in ev.checks}

    assert checks["canonical_results"].ok is False
    assert "p2" in checks["canonical_results"].detail


def test_verify_numbers_manifest_includes_nested_json_and_xlsx(tmp_path):
    project = tmp_path / "proj"
    write_file(project / "results" / "p1" / "values.json", json.dumps({
        "problem": 1,
        "objective": 4.9,
        "decision": {"theta_deg": 8.7, "v_mps": 140.0},
        "intervals": [[1.5, 6.4]],
    }))
    try:
        from openpyxl import Workbook
    except ImportError:
        return
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "duration"
    ws["B1"] = 14.804
    wb.save(project / "result3.xlsx")

    from verify_numbers import scan_results_directory

    manifest = scan_results_directory(project)
    flat_keys = set(manifest["results/p1/values.json"].keys())

    assert "decision.theta_deg" in flat_keys
    assert "intervals[0][1]" in flat_keys
    assert manifest["result3.xlsx"]["Sheet!B1"]["value"] == 14.804


def test_runner_invokes_step3_selection_before_step3_dispatch():
    runner = Path(REPO_ROOT) / "run_paper.sh"
    text = runner.read_text(encoding="utf-8")

    assert "maybe_select_option step3 3" in text
    assert text.index("maybe_select_option step3 3") < text.index("3)  run_step_3")
