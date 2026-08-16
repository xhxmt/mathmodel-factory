from __future__ import annotations

import re
from pathlib import Path

from factory_core.consultation_projection import write_pending_consultation_answer


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
    write_pending_consultation_answer(
        project_dir=project_path,
        gate=gate,
        step=step,
        title=title,
        answer=answer,
        timestamp=timestamp,
    )
