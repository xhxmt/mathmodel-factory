"""Phase 3a: a spent semantic-reopen budget pauses the run with an explanation.

Limits (decision 4, 2026-11-06): an initiating Stage may rewind an earlier
Stage at most twice, and a run at most five times, under the current classifier
identity.  Either limit pauses instead of failing; the operator either reverts
the upstream-owned edits and resumes (the change set is rechecked through
recovery) or grants one more reopen.
"""

from __future__ import annotations

import pytest

from factory_core.domain import InvalidTransition, RecoveryDecision, RecoveryDisposition, WorkflowStatus
from factory_core.engine import (
    RUN_SEMANTIC_REOPEN_LIMIT,
    SEMANTIC_REOPEN_GRANTED,
    SEMANTIC_REOPEN_PAUSE_SUBCODE,
    STAGE_SEMANTIC_REOPEN_LIMIT,
    FactoryEngine,
)
from factory_core.stages import STAGE_SCHEDULER_GENERATION
from factory_core.storage import SQLiteStateStore
from factory_core.workflow_events import project_runtime_diagnostics
from tests.test_stage_scheduler import (
    Lifecycle,
    stage_registry,
    trust_seeded_reviewer_gate,
)

UPSTREAM = "viable_streams.md"  # owned by Stage 1


class RewriteUpstreamThenRecover(Lifecycle):
    """Step 14 (Stage 9) edits a Stage-1 artifact once; recovery completes."""

    def __init__(self):
        super().__init__()
        self.recover_calls = 0

    def execute(self, context):
        self.calls.append(context.attempt)
        if len(self.calls) == 1:
            (context.project_dir / UPSTREAM).write_text("changed by Stage 9\n", encoding="utf-8")
        from factory_core.domain import ExecutionResult

        return ExecutionResult.succeeded()

    def recover(self, _context, _error):
        self.recover_calls += 1
        return RecoveryDecision(RecoveryDisposition.COMPLETE)


def _project(tmp_path, prior_reopens):
    (tmp_path / UPSTREAM).write_text("original\n", encoding="utf-8")
    step14 = RewriteUpstreamThenRecover()
    registry, _ = stage_registry(overrides={14: step14})
    store = SQLiteStateStore(tmp_path)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=13,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    trust_seeded_reviewer_gate(store, tmp_path)
    engine = FactoryEngine(tmp_path, store=store, registry=registry)
    classifier = engine._classifier_sha(store.load())
    for stage in prior_reopens:
        state = store.load()
        store.transition(
            expected_revision=state.revision,
            event_type="STAGE_SEMANTIC_REOPENED",
            changes={},
            payload={"stage": stage, "semantic_owner_stage": 1, "resume_after_step": -1,
                     "classifier_contract_sha256": classifier},
        )
    return engine, store, step14


def _reopens(store):
    return [event for event in store.events() if event.type == "STAGE_SEMANTIC_REOPENED"]


def test_a_spent_stage_budget_pauses_with_an_explanation(tmp_path):
    engine, store, _ = _project(tmp_path, prior_reopens=[9] * STAGE_SEMANTIC_REOPEN_LIMIT)

    state = engine.run(max_steps=1)

    assert state.status is WorkflowStatus.PAUSED  # not FAILED
    assert (state.active_stage, state.active_subtask) == (9, "abstract")  # cursor kept
    pause = engine.semantic_reopen_pause(state)
    assert pause is not None
    assert pause.payload["reason"]["subcode"] == SEMANTIC_REOPEN_PAUSE_SUBCODE
    assert pause.payload["reason"]["actor"] == "engine"
    assert pause.payload["reason"]["evidence"] == [UPSTREAM]
    budget = pause.payload["reopen_budget"]
    assert budget["exhausted"] == "stage"
    assert (budget["stage_count"], budget["stage_limit"]) == (2, 2)
    assert (budget["run_count"], budget["run_limit"]) == (2, RUN_SEMANTIC_REOPEN_LIMIT)
    assert [item["stage"] for item in budget["history"]] == [9, 9]
    assert budget["causes"] == [{"flag": budget["causes"][0]["flag"], "owner_stage": 1, "cause_artifact": UPSTREAM}]
    assert len(_reopens(store)) == 2  # nothing reopened
    assert any(flag["owner_stage"] == 1 for flag in store.dirty_flags())

    diagnostics = project_runtime_diagnostics(store.events(), state)["status"]
    assert diagnostics["reason_code"] == SEMANTIC_REOPEN_PAUSE_SUBCODE
    assert diagnostics["suggested_actions"][:2] == ["revert_upstream_edits_and_resume", "grant_semantic_reopen"]
    assert diagnostics["evidence"] == [{"kind": "file", "path": UPSTREAM}]
    assert diagnostics["reopen_budget"]["exhausted"] == "stage"


def test_the_run_limit_pauses_even_when_the_stage_has_budget(tmp_path):
    engine, store, _ = _project(tmp_path, prior_reopens=[2, 3, 4, 5, 7][:RUN_SEMANTIC_REOPEN_LIMIT])

    state = engine.run(max_steps=1)

    assert state.status is WorkflowStatus.PAUSED
    budget = engine.semantic_reopen_pause(state).payload["reopen_budget"]
    assert budget["exhausted"] == "run"
    assert (budget["stage_count"], budget["run_count"]) == (0, RUN_SEMANTIC_REOPEN_LIMIT)


def test_a_grant_lets_the_paused_reopen_happen_on_resume(tmp_path):
    engine, store, step14 = _project(tmp_path, prior_reopens=[9, 9])
    paused = engine.run(max_steps=1)

    with pytest.raises(InvalidTransition, match="needs a reason"):
        engine.grant_semantic_reopen(expected_revision=paused.revision, note=" ")
    granted = engine.grant_semantic_reopen(expected_revision=paused.revision, note="upstream fix is real")
    assert granted.status is WorkflowStatus.PAUSED
    grant = store.events()[-1]
    assert grant.type == SEMANTIC_REOPEN_GRANTED
    assert (grant.payload["stage"], grant.payload["reason"]["message"]) == (9, "upstream fix is real")
    with pytest.raises(InvalidTransition, match="already has a grant"):
        engine.grant_semantic_reopen(expected_revision=granted.revision, note="again")

    resumed = engine.resume(expected_revision=granted.revision)
    state = engine.run(max_steps=1)

    assert step14.recover_calls == 1 and len(step14.calls) == 1  # rechecked, not re-run
    assert len(_reopens(store)) == 3
    assert _reopens(store)[-1].payload["stage"] == 9
    assert state.status is not WorkflowStatus.FAILED
    assert state.active_stage in (None, 1) and state.last_completed_step == 0
    assert resumed.revision < state.revision
    # The grant was used up: the same Stage is out of budget again.
    budget = engine._semantic_reopen_budget(9, state)
    assert (budget["stage_count"], budget["stage_limit"], budget["allowed"]) == (3, 3, False)


def test_reverting_the_upstream_edit_lets_the_stage_finish_on_resume(tmp_path):
    engine, store, step14 = _project(tmp_path, prior_reopens=[9, 9])
    paused = engine.run(max_steps=1)

    (tmp_path / UPSTREAM).write_text("original\n", encoding="utf-8")
    engine.resume(expected_revision=paused.revision)
    state = engine.run(max_steps=1)

    assert step14.recover_calls == 1 and len(step14.calls) == 1
    assert len(_reopens(store)) == 2
    assert state.status is not WorkflowStatus.PAUSED
    assert state.last_completed_step >= 14  # Step 14 finished; the run may go on


def test_a_grant_needs_a_budget_pause(tmp_path):
    engine, store, _ = _project(tmp_path, prior_reopens=[])
    state = store.load()
    with pytest.raises(InvalidTransition, match="not paused at a spent semantic reopen budget"):
        engine.grant_semantic_reopen(expected_revision=state.revision, note="why")
    paused = engine.pause(expected_revision=state.revision)
    with pytest.raises(InvalidTransition, match="not paused at a spent semantic reopen budget"):
        engine.grant_semantic_reopen(expected_revision=paused.revision, note="why")


def test_grants_from_another_classifier_do_not_count(tmp_path):
    engine, store, _ = _project(tmp_path, prior_reopens=[9, 9])
    state = store.load()
    store.transition(
        expected_revision=state.revision, event_type=SEMANTIC_REOPEN_GRANTED, changes={},
        payload={"stage": 9, "classifier_contract_sha256": "0" * 64},
    )
    assert engine._semantic_reopen_budget(9, store.load())["allowed"] is False


def test_cli_grant_reopen_needs_a_reason_and_records_the_grant(tmp_path, monkeypatch):
    from factory_core import cli

    engine, store, _ = _project(tmp_path, prior_reopens=[9, 9])
    paused = engine.run(max_steps=1)

    parser = cli.build_parser()
    args = parser.parse_args(["action", "grant-reopen", str(tmp_path), "--reason", "real fix", "--no-start"])
    assert (args.name, args.reason, args.no_start) == ("grant-reopen", "real fix", True)

    calls = []

    class Service:
        def __init__(self, *_a, **_k):
            pass

        def inspect(self, _project):
            return paused

        def grant_semantic_reopen(self, project, *, reason, expected_revision=None):
            calls.append((reason, expected_revision))
            return engine.grant_semantic_reopen(expected_revision=expected_revision, note=reason)

    monkeypatch.setattr(cli, "FactoryService", Service)
    assert cli.main(["action", "grant-reopen", str(tmp_path), "--reason", "real fix", "--no-start"]) == 0
    assert calls == [("real fix", paused.revision)]
    assert store.events()[-1].type == SEMANTIC_REOPEN_GRANTED
    assert cli.main(["action", "grant-reopen", str(tmp_path), "--no-start"]) != 0
