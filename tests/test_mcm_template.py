"""The vendored MCM/ICM template compiles through compile_paper.sh as-is."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from factory_core.paper_sources import count_abstract_placeholders

REPOSITORY = Path(__file__).resolve().parents[1]
TEMPLATE = REPOSITORY / "latex_templates/mcm_paper_template.tex"


def _page_counter():
    spec = importlib.util.spec_from_file_location(
        "check_page_count", REPOSITORY / "scripts/check_page_count.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_template_carries_the_step_contract_markers():
    text = TEMPLATE.read_text(encoding="utf-8")

    assert re.search(r"^\\documentclass\{mcmthesis\}", text, re.MULTILINE)
    assert (REPOSITORY / "latex_templates/mcmthesis.cls").is_file()
    # One placeholder line for Step 14, counted the way the Step 9/14
    # validators count it.
    assert count_abstract_placeholders(text) == 1
    # The Summary Sheet year is pinned, not the compile date.
    assert re.search(r"\\renewcommand\{\\headset\}\{\d{4}\\\\", text)
    assert "\\AImatter" in text and "\\begin{ReportAiUse}" in text
    prompt = (REPOSITORY / "prompts/mcm/step9_paper_draft.txt").read_text(encoding="utf-8")
    assert "latex_templates/mcm_paper_template.tex" in prompt


@pytest.mark.latex
@pytest.mark.skipif(
    shutil.which("xelatex") is None or shutil.which("pdftotext") is None,
    reason="xelatex/pdftotext unavailable",
)
def test_template_compiles_and_the_ai_report_is_not_counted(tmp_path):
    project = tmp_path / "mcmdemo"
    project.mkdir()
    text = TEMPLATE.read_text(encoding="utf-8").replace(
        "\\subsection{Problem Background}\n",
        "\\subsection{Problem Background}\nSee \\cite{giordano2014}.\n",
        1,
    )
    (project / "mcmdemo_paper.tex").write_text(text, encoding="utf-8")
    (project / "references.bib").write_text(
        "@book{giordano2014, author={Giordano, Frank R.}, "
        "title={A First Course in Mathematical Modeling}, "
        "publisher={Cengage}, year={2014}}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(REPOSITORY / "compile_paper.sh"), str(project), "mcmdemo"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    pdf = project / "mcmdemo_paper.pdf"
    first_page = subprocess.run(
        ["pdftotext", "-f", "1", "-l", "1", "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Summary Sheet" in first_page and "2027" in first_page
    assert "Keywords" in first_page

    counter = _page_counter()
    counted, total = counter.counted_page_count(pdf)
    assert counted == total - 1
    assert counter.first_ai_report_page(pdf) == total
