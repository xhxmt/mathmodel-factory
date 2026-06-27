from __future__ import annotations

import re
from pathlib import Path


def gate_ready(human_review: Path, gate: str) -> bool:
    if not human_review.is_file():
        return False
    pattern = rf"(?im)^##[ \t]+CONSULT[ \t]+{re.escape(gate)}([ \t(].*)?STATUS:[ \t]*READY"
    content = human_review.read_text(encoding="utf-8", errors="replace")
    return re.search(pattern, content) is not None


def write_consultation_answer(
    *,
    project_path: Path,
    gate: str,
    step: int,
    title: str,
    answer: str,
    timestamp: str,
) -> None:
    human_review = project_path / "human_review.md"
    heading = f"## CONSULT {gate} (Step {step}) — STATUS: READY"
    section = (
        f"{heading}\n"
        f"咨询点：{title}\n"
        f"提交时间: {timestamp}\n\n"
        f"{answer.strip()}\n"
    )

    if human_review.is_file():
        content = human_review.read_text(encoding="utf-8", errors="replace")
        pattern = rf"(?ims)^##[ \t]+CONSULT[ \t]+{re.escape(gate)}.*?(?=^##[ \t]|\Z)"
        if re.search(pattern, content):
            content = re.sub(pattern, section + "\n", content)
        else:
            content = content.rstrip() + "\n\n" + section + "\n"
    else:
        content = "# 人工审核与介入记录\n\n" + section + "\n"

    human_review.write_text(content, encoding="utf-8")
