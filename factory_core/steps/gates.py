from __future__ import annotations

from pathlib import Path

from ..domain import PendingAction, PrepareResult
from scripts.selection_gate import PENDING_EXIT, prepare_step3
from web.backend.selection_service import build_content_freeze_options


def _consult_enabled(project: Path, gate: str) -> bool:
    enabled = project / "consultation" / "enabled"
    if enabled.is_symlink():
        raise ValueError("consultation/enabled must not be a symlink")
    if not enabled.exists():
        return False
    if not enabled.is_file():
        raise ValueError("consultation/enabled must be a regular file")
    body = enabled.read_text(encoding="utf-8", errors="replace").replace(",", " ")
    gates = set(body.split())
    return not gates or gate in gates


def _consult_ready(project: Path, gate: str) -> bool:
    from ..consultation_projection import (
        current_consultation_decision,
        ensure_consultation_projection,
    )

    if current_consultation_decision(project, gate) is None:
        return False
    ensure_consultation_projection(project, gate)
    return True


def _consultation_request(
    project: Path, gate: str, step_id: int
) -> Path:
    request = (
        project / "consultation" / "REQUEST.md"
        if gate == "dynamic"
        else project / "consultation" / f"{gate}_request.md"
    )
    if request.is_symlink():
        raise ValueError(f"consultation request must not be a symlink: {request.name}")
    request.parent.mkdir(parents=True, exist_ok=True)
    if request.exists() and not request.is_file():
        raise ValueError(f"consultation request must be a regular file: {request.name}")
    if not request.exists():
        request.write_text(
            f"# Consultation request\n\ngate: {gate}\nstep: {step_id}\n"
            f"project: {project.name}\n",
            encoding="utf-8",
        )
    return request


def _consultation_gate(
    project: Path,
    gate: str,
    step_id: int,
    *,
    reason: str,
    owner_stage: int,
) -> PrepareResult | None:
    review = project / "human_review.md"
    if review.is_symlink():
        return PrepareResult(
            ready=False, reason="human_review.md must not be a symlink"
        )
    try:
        enabled = _consult_enabled(project, gate)
    except (OSError, ValueError) as exc:
        return PrepareResult(ready=False, reason=str(exc))
    if not enabled:
        return None
    try:
        if _consult_ready(project, gate):
            return None
        request = _consultation_request(project, gate, step_id)
    except (OSError, ValueError) as exc:
        return PrepareResult(ready=False, reason=str(exc))
    return PrepareResult.awaiting(
        PendingAction(
            type="human_consultation",
            gate=gate,
            metadata={"consultation_owner_stage": int(owner_stage)},
        ),
        str(request.relative_to(project)),
        reason=reason,
    )


def prepare_human_gates(project: Path, step_id: int) -> PrepareResult:
    from ..stages import gate_policy, native_consultation_policy_for_step

    consultation_policy = native_consultation_policy_for_step(step_id)
    if consultation_policy is not None:
        assert consultation_policy.stage_id is not None
        consultation = _consultation_gate(
            project,
            consultation_policy.gate,
            step_id,
            reason=(
                f"consultation gate {consultation_policy.gate} is awaiting "
                "an immutable decision"
            ),
            owner_stage=consultation_policy.stage_id,
        )
        if consultation is not None:
            return consultation

    contest_required = False
    if step_id in {3, 16}:
        from ..storage import SQLiteStateStore

        contest_store = SQLiteStateStore(project)
        contest_required = (
            contest_store.exists and contest_store.contest_policy() is not None
        )
    if (
        step_id == 3
        and prepare_step3(project, None, required=contest_required) == PENDING_EXIT
    ):
        options = project / "selection" / "step3_options.json"
        return PrepareResult.awaiting(
            PendingAction(type="step3_selection", gate="step3"),
            str(options.relative_to(project)),
            reason="Step 3 selection is awaiting input",
        )
    if step_id == 16:
        from ..storage import SQLiteStateStore

        store = SQLiteStateStore(project)
        if store.exists and store.contest_policy() is not None:
            content_freeze = store.decision("content_freeze")
            if not (content_freeze and content_freeze.get("approved") is True):
                options = project / "selection" / "content_freeze_options.json"
                if options.is_symlink():
                    return PrepareResult(
                        ready=False,
                        reason="content freeze options must not be a symlink",
                    )
                if not options.is_file():
                    build_content_freeze_options(project)
                return PrepareResult.awaiting(
                    PendingAction(
                        type="content_freeze_selection",
                        gate="content_freeze",
                    ),
                    str(options.relative_to(project)),
                    reason="content freeze approval is awaiting human review",
                )

    from ..storage import SQLiteStateStore

    dynamic = project / "consultation" / "REQUEST.md"
    if dynamic.exists() or dynamic.is_symlink():
        dynamic_policy = gate_policy("dynamic")
        consultation = _consultation_gate(
            project,
            dynamic_policy.gate,
            step_id,
            reason="dynamic consultation is awaiting an immutable decision",
            owner_stage=(
                SQLiteStateStore(project).load().active_stage
                if SQLiteStateStore(project).exists
                and SQLiteStateStore(project).load().active_stage is not None
                else 1
            ),
        )
        if consultation is not None:
            return consultation
    return PrepareResult.prepared()
