"""Pure Phase-5 shadow policy for pausing owned process scopes.

This module only maps immutable wire values to a decision. It does not inspect
or signal processes, persist state, dispatch cancellation, or participate in
the current production pause path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PausePolicyError(ValueError):
    """Raised when a pause-policy input is outside the supported wire contract."""


class PauseMode(str, Enum):
    PAUSE = "pause"
    PAUSE_AND_CANCEL_SOLVERS = "pause-and-cancel-solvers"


class ProcessScopeKind(str, Enum):
    WORKER = "worker"
    MODEL = "model"
    ATTACHED_SOLVER = "attached-solver"
    DURABLE_SOLVER = "durable-solver"


class PauseAction(str, Enum):
    TERMINATE_SCOPE = "terminate-scope"
    CONTINUE = "continue"
    REQUEST_CANCEL = "request-cancel"


@dataclass(frozen=True)
class PauseDecision:
    mode: PauseMode
    scope_kind: ProcessScopeKind
    action: PauseAction
    reason_code: str

    def as_dict(self) -> dict[str, str]:
        """Return the stable wire representation in contract field order."""

        return {
            "mode": self.mode.value,
            "scope_kind": self.scope_kind.value,
            "action": self.action.value,
            "reason_code": self.reason_code,
        }


_TERMINATION_DECISIONS: dict[
    ProcessScopeKind, tuple[PauseAction, str]
] = {
    ProcessScopeKind.WORKER: (
        PauseAction.TERMINATE_SCOPE,
        "PAUSE_STOPS_WORKER_SCOPE",
    ),
    ProcessScopeKind.MODEL: (
        PauseAction.TERMINATE_SCOPE,
        "PAUSE_STOPS_MODEL_SCOPE",
    ),
    ProcessScopeKind.ATTACHED_SOLVER: (
        PauseAction.TERMINATE_SCOPE,
        "PAUSE_STOPS_ATTACHED_SOLVER_SCOPE",
    ),
}


def _pause_mode(value: PauseMode | str) -> PauseMode:
    try:
        return PauseMode(value)
    except (TypeError, ValueError) as exc:
        raise PausePolicyError(f"unsupported pause mode: {value!r}") from exc


def _scope_kind(value: ProcessScopeKind | str) -> ProcessScopeKind:
    try:
        return ProcessScopeKind(value)
    except (TypeError, ValueError) as exc:
        raise PausePolicyError(f"unsupported process scope kind: {value!r}") from exc


def decide_pause_action(
    mode: PauseMode | str,
    scope_kind: ProcessScopeKind | str,
) -> PauseDecision:
    """Return the pure shadow decision for one supported pause/scope pair."""

    normalized_mode = _pause_mode(mode)
    normalized_scope = _scope_kind(scope_kind)
    termination = _TERMINATION_DECISIONS.get(normalized_scope)
    if termination is not None:
        action, reason_code = termination
    elif normalized_mode is PauseMode.PAUSE:
        action = PauseAction.CONTINUE
        reason_code = "DURABLE_SOLVER_CONTINUES_ON_PAUSE"
    else:
        action = PauseAction.REQUEST_CANCEL
        reason_code = "DURABLE_SOLVER_CANCEL_REQUESTED"
    return PauseDecision(
        mode=normalized_mode,
        scope_kind=normalized_scope,
        action=action,
        reason_code=reason_code,
    )
