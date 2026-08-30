from __future__ import annotations

import pytest

from factory_core.adapters.infrastructure.pause_policy import (
    PauseAction,
    PauseMode,
    PausePolicyError,
    ProcessScopeKind,
    decide_pause_action,
)


@pytest.mark.parametrize(
    ("mode", "scope_kind", "action", "reason_code"),
    (
        (
            PauseMode.PAUSE,
            ProcessScopeKind.WORKER,
            PauseAction.TERMINATE_SCOPE,
            "PAUSE_STOPS_WORKER_SCOPE",
        ),
        (
            PauseMode.PAUSE_AND_CANCEL_SOLVERS,
            ProcessScopeKind.WORKER,
            PauseAction.TERMINATE_SCOPE,
            "PAUSE_STOPS_WORKER_SCOPE",
        ),
        (
            PauseMode.PAUSE,
            ProcessScopeKind.MODEL,
            PauseAction.TERMINATE_SCOPE,
            "PAUSE_STOPS_MODEL_SCOPE",
        ),
        (
            PauseMode.PAUSE_AND_CANCEL_SOLVERS,
            ProcessScopeKind.MODEL,
            PauseAction.TERMINATE_SCOPE,
            "PAUSE_STOPS_MODEL_SCOPE",
        ),
        (
            PauseMode.PAUSE,
            ProcessScopeKind.ATTACHED_SOLVER,
            PauseAction.TERMINATE_SCOPE,
            "PAUSE_STOPS_ATTACHED_SOLVER_SCOPE",
        ),
        (
            PauseMode.PAUSE_AND_CANCEL_SOLVERS,
            ProcessScopeKind.ATTACHED_SOLVER,
            PauseAction.TERMINATE_SCOPE,
            "PAUSE_STOPS_ATTACHED_SOLVER_SCOPE",
        ),
        (
            PauseMode.PAUSE,
            ProcessScopeKind.DURABLE_SOLVER,
            PauseAction.CONTINUE,
            "DURABLE_SOLVER_CONTINUES_ON_PAUSE",
        ),
        (
            PauseMode.PAUSE_AND_CANCEL_SOLVERS,
            ProcessScopeKind.DURABLE_SOLVER,
            PauseAction.REQUEST_CANCEL,
            "DURABLE_SOLVER_CANCEL_REQUESTED",
        ),
    ),
)
def test_pause_decision_matrix(mode, scope_kind, action, reason_code):
    decision = decide_pause_action(mode, scope_kind)

    assert decision.mode is mode
    assert decision.scope_kind is scope_kind
    assert decision.action is action
    assert decision.reason_code == reason_code


def test_wire_strings_and_stable_as_dict_serialization():
    decision = decide_pause_action(
        "pause-and-cancel-solvers", "durable-solver"
    )

    assert decision.as_dict() == {
        "mode": "pause-and-cancel-solvers",
        "scope_kind": "durable-solver",
        "action": "request-cancel",
        "reason_code": "DURABLE_SOLVER_CANCEL_REQUESTED",
    }


def test_unknown_pause_mode_fails_closed():
    with pytest.raises(PausePolicyError, match="unsupported pause mode"):
        decide_pause_action("stop-everything", ProcessScopeKind.WORKER)


def test_unknown_process_scope_fails_closed():
    with pytest.raises(PausePolicyError, match="unsupported process scope kind"):
        decide_pause_action(PauseMode.PAUSE, "external-process")
