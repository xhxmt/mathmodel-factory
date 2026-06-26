import os
import subprocess
import sys
from pathlib import Path

from conftest import REPO_ROOT


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_infer_step_stays_at_8_with_step8_5_artifacts(tmp_path):
    project = tmp_path / "demo"
    write_file(project / "checkpoint.md", "- **Last completed step**: 8\n")
    write_file(project / "problem" / "problem_brief.md", "# brief\n")
    write_file(project / "visualization_log.md", "\n".join(["viz"] * 20) + "\n")
    write_file(project / "figures" / "demo.pdf", "fake\n")
    write_file(project / "reviewer_entry_map.md", "# map\n")
    write_file(project / "anchor_figure_plan.md", "# anchors\n")
    write_file(project / "entry_gate.md", "# gate\n\nVERDICT: REVISE\n")

    out = subprocess.run(
        [os.path.join(REPO_ROOT, "run_paper.sh"), "--infer-step", str(project)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "8"
