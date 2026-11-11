"""MCM/ICM prompt blocks: contest detection and CUMCM byte identity."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from factory_core.stages import STAGE_CATALOG_VERSION_V2, STAGE_SCHEDULER_GENERATION
from factory_core.steps.contest_prompts import (
    CONTEST_PROMPT_DIR,
    contest_prompt_block,
    project_contest,
)
from factory_core.steps.prompting import PromptRenderer
from factory_core.storage import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]
MCM_TEMPLATES = sorted((ROOT / "prompts" / CONTEST_PROMPT_DIR).glob("*.txt"))
HEADER = "## 美赛（MCM/ICM）专用要求"


def _write(project: Path, relative: str, text: str) -> None:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _step_key(template: Path) -> str:
    return template.stem.removeprefix("step").split("_")[0]


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        # Real Step 0 wording: the line names MCM/ICM only to rule them out.
        ("- **竞赛类型 ground truth：CUMCM（全国大学生数学建模竞赛），不是 MCM/ICM。**", "CUMCM"),
        ("CUMCM 竞赛类型为 **CUMCM（全国大学生数学建模竞赛）**。", "CUMCM"),
        ("- 竞赛类型：MCM 2027 Problem B（不是 CUMCM）", "MCM"),
        ("- **Contest Type**: ICM, Problem D", "ICM"),
    ],
)
def test_step0_declaration_decides_the_contest(tmp_path, declaration, expected):
    _write(tmp_path, "problem/feasibility_constraints.md", f"## 提交格式硬约束\n{declaration}\n")
    # A contradicting source does not override the declaration.
    _write(tmp_path, "problem/source.md", "2027 MCM Problem A\n" if expected == "CUMCM" else "高教社杯\n")

    assert project_contest(tmp_path) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("2027 MCM Problem A: Heat Flow\n", "MCM"),
        ("2027 ICM\nProblem E: Sustainability\n", "ICM"),
        ("2024 高教社杯全国大学生数学建模竞赛题目\nA 题\n", "CUMCM"),
        ("A plain modeling problem.\n", "CUMCM"),
    ],
)
def test_source_decides_without_a_declaration(tmp_path, source, expected):
    _write(tmp_path, "problem/source.md", source)

    assert project_contest(tmp_path) == expected


def test_a_project_without_problem_files_is_cumcm(tmp_path):
    assert project_contest(tmp_path) == "CUMCM"
    assert contest_prompt_block(ROOT, tmp_path, "step9_paper_draft.txt", {}) == ""


def test_every_block_belongs_to_an_active_prompt_and_is_fully_rendered(tmp_path):
    assert {path.name for path in MCM_TEMPLATES} >= {
        "step8_5_reviewer_entry.txt",
        "step9_paper_draft.txt",
        "step11_constructive_review.txt",
        "step12_revision.txt",
        "step14_abstract.txt",
        "step15_polish.txt",
    }
    _write(tmp_path, "problem/source.md", "2027 MCM Problem C\n")
    renderer = PromptRenderer(ROOT)
    for path in MCM_TEMPLATES:
        assert (ROOT / "prompts" / path.name).is_file(), path.name
        text = renderer.render(path.name, tmp_path, step_key=_step_key(path), researcher_note="")
        assert text.count(HEADER) == 1
        assert not re.search(r"__[A-Z_]+__", text.split("## 共用步骤说明", 1)[0]), path.name
    assert (ROOT / "docs/guides/MCM_WRITING_BENCHMARK.md").is_file()


@pytest.mark.parametrize("template", MCM_TEMPLATES, ids=lambda path: path.name)
def test_cumcm_prompts_are_unchanged_and_mcm_prompts_only_prepend(tmp_path, template):
    renderer = PromptRenderer(ROOT)
    render = lambda project: renderer.render(  # noqa: E731
        template.name, project, step_key=_step_key(template), researcher_note=""
    )
    project = tmp_path / "demo"
    project.mkdir()
    undetermined = render(project)
    _write(project, "problem/feasibility_constraints.md", "- 竞赛类型：CUMCM\n")
    cumcm = render(project)
    _write(project, "problem/feasibility_constraints.md", "- 竞赛类型：MCM\n")
    mcm = render(project)

    assert cumcm == undetermined
    assert HEADER not in cumcm
    # The block sits between the preamble and the shared template text, which
    # is otherwise rendered exactly as for CUMCM.
    agent_line = cumcm.index("AGENT_KEY:")
    block = mcm[agent_line:].split("\n\n", 1)[1]
    assert block.startswith(HEADER)
    assert mcm.replace(block[: block.index("## 共用步骤说明\n\n") + len("## 共用步骤说明\n\n")], "", 1) == cumcm


def test_v2_overlay_still_applies_under_the_mcm_block(tmp_path):
    project = tmp_path / "v2"
    SQLiteStateStore(project).initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=-1,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
        stage_catalog_version=STAGE_CATALOG_VERSION_V2,
    )
    _write(project, "problem/source.md", "2027 ICM Problem F\n")
    renderer = PromptRenderer(ROOT)
    for path in MCM_TEMPLATES:
        text = renderer.render(path.name, project, step_key=_step_key(path), researcher_note="")
        assert HEADER in text
        assert "PRECHECK" not in text.split("## 共用步骤说明", 1)[0]
