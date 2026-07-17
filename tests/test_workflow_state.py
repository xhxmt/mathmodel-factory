from pathlib import Path
import zipfile


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("paper.pdf", b"pdf")


def mark_final_judge_current(project: Path, base: str) -> None:
    from scripts.submission_fingerprint import submission_fingerprint

    write_file(project / f"{base}_paper.tex", "\\begin{document}\nfinal\n\\end{document}\n")
    write_file(project / f"{base}_paper.pdf", "pdf\n")
    write_file(
        project / "judge_outputs" / "final_submission.sha256",
        submission_fingerprint(project, base) + "\n",
    )


def test_verdict_parser_uses_first_verdict_line(tmp_path):
    path = tmp_path / "judge_evaluation.md"
    write_file(path, "notes\nVERDICT: PASS\r\nVERDICT: REOPEN_REVISION_TEXT\n")

    from scripts.workflow_state import first_verdict

    assert first_verdict(path) == "PASS"


def test_step8_5_and_gate2_pass_predicates(tmp_path):
    project = tmp_path / "project"
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(project / "judge_evaluation.md", "VERDICT: REOPEN_REVISION_MODEL\n")

    from scripts.workflow_state import gate2_passed, step8_5_passed

    assert step8_5_passed(project) is True
    assert gate2_passed(project) is False


def test_delivery_artifacts_and_step16_ready(tmp_path):
    root = tmp_path
    project = root / "complete" / "demo"
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(project / "judge_evaluation.md", "VERDICT: PASS\n")
    mark_final_judge_current(project, "demo")
    write_file(root / "papers" / "demo_paper.pdf", "pdf\n")
    write_zip(root / "papers" / "demo_submission.zip")

    from scripts.workflow_state import delivery_artifacts_ready, step16_ready

    assert delivery_artifacts_ready(root, "demo") is True
    assert step16_ready(project, root, "demo") is True

    write_file(project / "demo_paper.tex", "\\begin{document}\nchanged after judging\n\\end{document}\n")
    assert step16_ready(project, root, "demo") is False


def test_gate2_delivery_override_allows_step16_without_faking_pass(tmp_path):
    root = tmp_path
    project = root / "ongoing" / "demo"
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "VERDICT: PASS\n")
    write_file(project / "judge_evaluation.md", "VERDICT: REOPEN_REVISION_MODEL\n")
    write_file(
        project / "gate2_delivery_override.json",
        '{"enabled": true, "scope": "continue_to_step16", "reason": "user_requested"}\n',
    )
    write_file(root / "papers" / "demo_paper.pdf", "pdf\n")
    mark_final_judge_current(project, "demo")
    write_zip(root / "papers" / "demo_submission.zip")

    from scripts.workflow_state import (
        gate2_delivery_allowed,
        gate2_delivery_override,
        gate2_passed,
        step16_ready,
    )

    assert gate2_passed(project) is False
    assert gate2_delivery_override(project) is True
    assert gate2_delivery_allowed(project) is True
    assert step16_ready(project, root, "demo") is True
