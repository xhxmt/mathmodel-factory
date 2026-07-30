from __future__ import annotations

import re
from pathlib import Path

from ..domain import PendingAction, PrepareResult
from scripts.selection_gate import PENDING_EXIT, prepare_step3


def _consult_enabled(project: Path, gate: str) -> bool:
    enabled = project / "consultation" / "enabled"
    if not enabled.is_file():
        return False
    body = enabled.read_text(encoding="utf-8", errors="replace").replace(",", " ")
    gates = set(body.split())
    return not gates or gate in gates


def _consult_ready(project: Path, gate: str) -> bool:
    review = project / "human_review.md"
    if not review.is_file():
        return False
    pattern = rf"^##\s+CONSULT\s+{re.escape(gate)}(?:\s|\().*STATUS:\s*READY"
    return re.search(pattern, review.read_text(encoding="utf-8", errors="replace"), re.MULTILINE | re.IGNORECASE) is not None


def prepare_human_gates(project: Path, step_id: int) -> PrepareResult:
    gate = "preflight" if step_id == 1 else "step4" if step_id == 4 else ""
    if gate and _consult_enabled(project, gate) and not _consult_ready(project, gate):
        request = project / "consultation" / f"{gate}_request.md"
        request.parent.mkdir(parents=True, exist_ok=True)
        if not request.is_file():
            request.write_text(
                f"# Consultation request\n\ngate: {gate}\nstep: {step_id}\nproject: {project.name}\n",
                encoding="utf-8",
            )
        return PrepareResult.awaiting(
            PendingAction(type="human_consultation", gate=gate),
            str(request.relative_to(project)),
            reason=f"consultation gate {gate} is awaiting input",
        )
    if step_id == 3 and prepare_step3(project, None) == PENDING_EXIT:
        options = project / "selection" / "step3_options.json"
        return PrepareResult.awaiting(
            PendingAction(type="step3_selection", gate="step3"),
            str(options.relative_to(project)),
            reason="Step 3 selection is awaiting input",
        )
    dynamic = project / "consultation" / "REQUEST.md"
    if dynamic.is_file() and _consult_enabled(project, "dynamic") and not _consult_ready(project, "dynamic"):
        return PrepareResult.awaiting(
            PendingAction(type="human_consultation", gate="dynamic"),
            str(dynamic.relative_to(project)),
            reason="dynamic consultation is awaiting input",
        )
    return PrepareResult.prepared()
