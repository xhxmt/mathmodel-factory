"""MCM/ICM wording for the shared paper-segment prompts.

The paper prompts are written for CUMCM.  A CUMCM project must keep
byte-identical prompts (benchmark comparability; ``prompt_template_sha256`` and
``effective_prompt_sha256`` stay unchanged), so the templates are not edited.
A project whose problem is an MCM/ICM problem instead gets the matching
``prompts/mcm/<template>`` block prepended to the rendered prompt.  The block
states which CUMCM-only rules it replaces; the shared contract below it (output
files, validators, number provenance, derived artifacts) still applies.

The contest is read from the project, never guessed from the model: Step 0's
declaration in ``problem/feasibility_constraints.md`` (the ground truth Step 9
already uses), else the competition named in ``problem/source.md``.  Before
Step 0 has written either, the project is CUMCM, as Step 9 defaults.
"""

from __future__ import annotations

import re
from pathlib import Path

CUMCM = "CUMCM"
MCM = "MCM"
ICM = "ICM"
US_CONTESTS = frozenset({MCM, ICM})

CONTEST_PROMPT_DIR = "mcm"

# A contest name on its own: CUMCM is not read as MCM.
_CONTEST_TOKEN = re.compile(r"(?<![A-Za-z])(CUMCM|MCM|ICM)(?![A-Za-z])")
_DECLARATION_LABEL = re.compile(r"竞赛类型|Contest\s+Type", re.IGNORECASE)
_CUMCM_MARKERS = re.compile(r"CUMCM|全国大学生数学建模|高教社杯")
_SOURCE_CHARS = 5000


def _read(path: Path, limit: int | None = None) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if limit is None else text[:limit]


def _declared_contest(text: str) -> str | None:
    """The first contest named after the first contest-type label."""

    for line in text.splitlines():
        label = _DECLARATION_LABEL.search(line)
        if label is None:
            continue
        # "竞赛类型：CUMCM，不是 MCM/ICM" declares CUMCM: take the first name
        # after the label, not any name on the line.
        token = _CONTEST_TOKEN.search(line, label.end())
        if token is not None:
            return token.group(1)
    return None


def _source_contest(text: str) -> str:
    if _CUMCM_MARKERS.search(text):
        return CUMCM
    for match in _CONTEST_TOKEN.finditer(text):
        if match.group(1) in US_CONTESTS:
            return match.group(1)
    if "Interdisciplinary Contest in Modeling" in text:
        return ICM
    if "Mathematical Contest in Modeling" in text:
        return MCM
    return CUMCM


def project_contest(project: Path) -> str:
    """``CUMCM``, ``MCM`` or ``ICM`` for a project, CUMCM when undetermined."""

    declared = _declared_contest(_read(project / "problem/feasibility_constraints.md"))
    if declared is not None:
        return declared
    return _source_contest(_read(project / "problem/source.md", _SOURCE_CHARS))


def contest_prompt_path(factory_root: Path, template: str) -> Path:
    return factory_root / "prompts" / CONTEST_PROMPT_DIR / Path(template).name


def contest_prompt_block(
    factory_root: Path,
    project: Path,
    template: str,
    replacements: dict[str, str],
) -> str:
    """The MCM/ICM block for a rendered prompt, or ``""`` for CUMCM."""

    path = contest_prompt_path(factory_root, template)
    if not path.is_file():
        return ""
    contest = project_contest(project)
    if contest not in US_CONTESTS:
        return ""
    body = path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        body = body.replace(key, value)
    return (
        f"## 美赛（MCM/ICM）专用要求 — 本项目是 {contest} 题\n\n"
        "竞赛类型取自 `problem/feasibility_constraints.md` 的声明（无声明时取自 `problem/source.md`）。"
        "下文是国赛与美赛共用的步骤说明：本段优先于下文所有只适用于 CUMCM 的规则；"
        "输出文件、校验命令、数值溯源和派生物等通用契约仍按下文执行。\n\n"
        f"{body.rstrip()}\n\n"
        "## 共用步骤说明\n\n"
    )
