"""Gate 1: entry equivalence between ``run(max_steps=1)`` and ``run_bounded()``.

Gate 1 answers exactly one question: holding the code, the state and the
environment fixed, does swapping the entry point change the workflow's
semantics?  The harness in ``tests/_g1_canary.py`` holds them fixed - one
canonical absolute path, one constant clock, one byte-identical seed restored
for each track - and the tests below compare the two tracks' full semantic
closure.

Two layers, deliberately:

**Layer 1 - smoke.**  A step-scheduler project with a two-Step registry.  Fast,
and it pins the mechanics of the bounded entry (CAS, contract identity, the
structured result, the event-stream delta).  It is *not* sufficient to close
Gate 1: the Stage scheduler's ``stage_checkpoints``, Stage cursor and Stage
dirty ownership are not on this path at all.

**Layer 2 - the Stage v1 canary.**  A ``stage_v1`` project driving the real
Stage scheduler, so the checkpoints, the cursor and the Stage dirty ownership
that the simplification actually touches are exercised.  This is the layer that
closes Gate 1.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import _g1_canary as canary
from factory_core.bounded_run import (
    BoundedRunContract,
    RunPolicy,
    boundary_fingerprint,
)
from factory_core.domain import ExecutionResult, ValidationResult, WorkflowStatus
from factory_core.engine import FactoryEngine
from factory_core.registry import StepDefinition, StepRegistry
from factory_core.stages import STAGE_SCHEDULER_GENERATION
from factory_core.steps import build_native_registry
from factory_core.steps.catalog import STEP_CONTRACTS


# --------------------------------------------------------------- registries
class _Handler:
    """A Step that succeeds, so the loop actually commits a checkpoint."""

    def __init__(self):
        self.calls: list[int] = []

    def execute(self, context):
        self.calls.append(context.attempt)
        return ExecutionResult.succeeded()


class _Validator:
    def validate(self, context):
        return ValidationResult.valid()


def _two_step_registry() -> StepRegistry:
    registry = StepRegistry()
    for step_id in (1, 2):
        registry.register(
            StepDefinition(
                id=step_id,
                name=f"step{step_id}",
                timeout_seconds=30,
                max_attempts=3,
                handler=_Handler(),
                validator=_Validator(),
            )
        )
    return registry


# ------------------------------------------------------------------- tracks
def _legacy_track(registry_factory) -> dict:
    root = canary.restore_seed()
    engine = FactoryEngine(root, store=canary.store_at(root), registry=registry_factory())
    with canary.frozen_time():
        state = engine.run(max_steps=1)
    result = canary.collect(root)
    result["returned_status"] = str(getattr(state.status, "value", state.status))
    result["returned_revision"] = int(state.revision)
    result["returned_last_completed_step"] = state.last_completed_step
    return result


def _bounded_on(root: Path, registry_factory, *, expected_revision: int, **contract_kwargs) -> dict:
    """One bounded invocation against an already-prepared project."""

    engine = FactoryEngine(root, store=canary.store_at(root), registry=registry_factory())
    contract = BoundedRunContract(expected_revision=expected_revision, **contract_kwargs)
    with canary.frozen_time():
        outcome = engine.run_bounded(contract)
    result = canary.collect(root)
    result["returned_status"] = outcome.status
    result["returned_revision"] = outcome.end_revision
    result["returned_last_completed_step"] = result["project_state"]["last_completed_step"]
    result["outcome"] = outcome
    return result


def _bounded_track(registry_factory, *, expected_revision: int, **contract_kwargs) -> dict:
    """One bounded invocation starting from a fresh restoration of the seed."""

    return _bounded_on(
        canary.restore_seed(),
        registry_factory,
        expected_revision=expected_revision,
        **contract_kwargs,
    )


def _assert_equivalent(legacy: dict, bounded: dict) -> None:
    # Coherence first: agreement between two broken tracks is not equivalence.
    canary.assert_integrity(legacy)
    canary.assert_integrity(bounded)
    findings = canary.compare(legacy, bounded)
    non_empty = {area: diff for area, diff in findings.items() if diff}
    assert not non_empty, "\n".join(
        f"{area}:\n  " + "\n  ".join(diff[:12]) for area, diff in non_empty.items()
    )


# =========================================================== Layer 1: smoke
def _smoke_seed(root: Path) -> None:
    canary.store_at(root).initialize(project_id="g1-canary", project_type="modeling")


def test_smoke_seed_is_reproducible_and_restorable():
    """The seed must be pinned before its determinism can be relied on."""

    evidence = canary.build_seed(_smoke_seed)

    assert evidence["state_db_sha256"], "seed must be hashed"
    assert evidence["counts"]["events"] == 1, "initialize writes exactly one event"
    assert evidence["triple"]["physical_schema"] == evidence["triple"]["project_state_schema"]

    # restoring twice must yield the same bytes, or the tracks start differently
    canary.restore_seed()
    first = canary.describe_seed(canary.CANARY_PROJECT)
    canary.restore_seed()
    second = canary.describe_seed(canary.CANARY_PROJECT)

    assert first["state_db_sha256"] == evidence["state_db_sha256"]
    assert second["state_db_sha256"] == evidence["state_db_sha256"]
    assert first["files"] == second["files"]
    # the constant clock makes the seed time-independent
    assert canary.CONSTANT_EPOCH == 1_700_000_000


def test_smoke_step_scheduler_advances_one_step_on_both_tracks():
    evidence = canary.build_seed(_smoke_seed)
    revision = evidence["project_state"]["revision"]

    legacy = _legacy_track(_two_step_registry)
    bounded = _bounded_track(
        _two_step_registry,
        expected_revision=revision,
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    # both advanced, and to the same place
    assert legacy["returned_last_completed_step"] == 1
    assert bounded["outcome"].made_progress is True
    assert bounded["outcome"].completed_subtasks == 1
    assert bounded["returned_status"] == legacy["returned_status"]

    _assert_equivalent(legacy, bounded)


def test_smoke_bounded_track_binds_its_authorisation():
    """The one expected difference: RUN_STARTED carries the contract."""

    evidence = canary.build_seed(_smoke_seed)
    bounded = _bounded_track(
        _two_step_registry,
        expected_revision=evidence["project_state"]["revision"],
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    bindings = canary.bounded_authorisation(bounded["events"])
    assert len(bindings) == 1
    assert bindings[0]["type"] == "RUN_STARTED"
    block = bindings[0]["block"]
    assert block["run_policy"] == RunPolicy.BOUNDED_SUBTASKS
    assert block["max_subtasks"] == 1
    assert block["expected_revision"] == evidence["project_state"]["revision"]
    # identity is derived from the contract, so it is reproducible
    assert block["bounded_run_id"] == block["bounded_run_contract_sha256"][:32]

    outcome = bounded["outcome"]
    assert outcome.run_id == block["bounded_run_id"]
    assert outcome.contract_sha256 == block["bounded_run_contract_sha256"]


def test_smoke_legacy_track_carries_no_authorisation():
    evidence = canary.build_seed(_smoke_seed)
    legacy = _legacy_track(_two_step_registry)
    assert canary.bounded_authorisation(legacy["events"]) == []
    assert evidence["project_state"]["revision"] >= 1


def test_smoke_stale_revision_is_refused_before_any_write():
    """The CAS assertion, on the same seed the equivalence test uses."""

    evidence = canary.build_seed(_smoke_seed)
    root = canary.restore_seed()
    engine = FactoryEngine(root, store=canary.store_at(root), registry=_two_step_registry())

    before = canary.collect(root)
    contract = BoundedRunContract(
        expected_revision=evidence["project_state"]["revision"] + 1,
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    from factory_core.bounded_run import BoundedRunError

    with canary.frozen_time(), pytest.raises(BoundedRunError):
        engine.run_bounded(contract)

    after = canary.collect(root)
    assert after["events"] == before["events"], "no business event may be written"
    assert after["project_state"]["revision"] == before["project_state"]["revision"]


_paused_seed = canary.paused_seed


def test_smoke_repeated_boundary_reports_needs_inspection():
    """A real no-progress boundary, so the reporting can be asserted outright.

    ``boundary_fingerprint`` covers the revision, so a run that advances and a
    run that repeats a boundary cannot be compared by fingerprint - which is why
    the S6 suite hedges this case.  A paused project is the honest construction:
    neither entry writes anything, so two invocations see an identical boundary
    and the repeat is detectable unconditionally.

    Note ``stop_reason`` stays ``BOUNDARY_OR_SCOPE``: the boundary verdict is
    carried by ``unchanged_boundary`` / ``outcome``, not by ``stop_reason``.
    """

    evidence = canary.build_seed(_paused_seed)
    revision = evidence["project_state"]["revision"]
    root = canary.restore_seed()

    first = _bounded_on(root, _two_step_registry, expected_revision=revision)
    fingerprint = first["outcome"].boundary_fingerprint

    second = _bounded_on(
        root,
        _two_step_registry,
        expected_revision=revision,
        previous_boundary_fingerprint=fingerprint,
    )

    outcome = second["outcome"]
    assert outcome.boundary_fingerprint == fingerprint
    assert outcome.made_progress is False
    assert outcome.unchanged_boundary is True
    assert outcome.needs_inspection is True
    assert outcome.to_dict()["outcome"] == "NEEDS_INSPECTION"
    assert outcome.stop_reason == "BOUNDARY_OR_SCOPE"


def test_smoke_both_entries_leave_a_paused_project_alone():
    """The two entries agree on a boundary neither may cross."""

    evidence = canary.build_seed(_paused_seed)
    revision = evidence["project_state"]["revision"]

    legacy = _legacy_track(_two_step_registry)
    bounded = _bounded_track(_two_step_registry, expected_revision=revision)

    assert legacy["returned_last_completed_step"] == evidence["project_state"]["last_completed_step"]
    assert bounded["outcome"].made_progress is False
    assert bounded["project_state"]["revision"] == revision

    _assert_equivalent(legacy, bounded)


# ============================================== Layer 2: the Stage v1 canary
# The hermetic Stage fixtures live in the shared harness so the Gate 2 failure
# scenarios reuse exactly the same registry and seeds rather than a second copy
# that could drift from this one.
_stage_registry = canary.stage_registry
_stage_seed = canary.stage_seed
_stage_seed_with_solver = canary.stage_seed_with_solver
_stage_seed_with_dirty = canary.stage_seed_with_dirty
SOLVER_JOB_ID = canary.SOLVER_JOB_ID


def test_stage_v1_advances_one_subtask_equivalently_on_both_tracks():
    """The layer that closes Gate 1: real Stage routing, real checkpoints."""

    evidence = canary.build_seed(_stage_seed)
    revision = evidence["project_state"]["revision"]

    legacy = _legacy_track(_stage_registry)
    bounded = _bounded_track(
        _stage_registry,
        expected_revision=revision,
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    assert legacy["returned_last_completed_step"] == 0
    assert bounded["returned_last_completed_step"] == legacy["returned_last_completed_step"]
    assert bounded["outcome"].completed_subtasks == 1
    assert bounded["outcome"].made_progress is True

    # the Stage machinery really was on the path, in both tracks
    event_types = [event["type"] for event in legacy["events"]]
    assert "STAGE_SUBTASK_SELECTED" in event_types
    assert "PROMPT_INPUT_BOUND" in event_types
    assert legacy["tables"]["stage_checkpoints"], "no checkpoint was committed"
    assert legacy["tables"]["stage_checkpoint_history"], "no checkpoint history"

    _assert_equivalent(legacy, bounded)


def test_stage_v1_checkpoint_and_cursor_are_identical():
    """G1.2 / G1.3 asserted directly, not only via the aggregate digest."""

    evidence = canary.build_seed(_stage_seed)
    legacy = _legacy_track(_stage_registry)
    bounded = _bounded_track(
        _stage_registry,
        expected_revision=evidence["project_state"]["revision"],
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    assert legacy["tables"]["stage_checkpoints"] == bounded["tables"]["stage_checkpoints"]
    assert legacy["tables"]["stage_checkpoint_history"] == bounded["tables"]["stage_checkpoint_history"]

    legacy_cursor = (
        legacy["project_state"]["last_completed_stage"],
        legacy["project_state"]["active_stage"],
        legacy["project_state"]["active_subtask"],
        legacy["project_state"]["source_step_id"],
    )
    bounded_cursor = (
        bounded["project_state"]["last_completed_stage"],
        bounded["project_state"]["active_stage"],
        bounded["project_state"]["active_subtask"],
        bounded["project_state"]["source_step_id"],
    )
    assert legacy_cursor == bounded_cursor


def test_stage_v1_solver_ownership_is_carried_equivalently():
    """G1.5 with data: the job, its slot and its status must be untouched."""

    evidence = canary.build_seed(_stage_seed_with_solver)

    legacy = _legacy_track(_stage_registry)
    bounded = _bounded_track(
        _stage_registry,
        expected_revision=evidence["project_state"]["revision"],
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    # the table is genuinely populated, so this cannot pass on empty lists
    assert len(legacy["tables"]["solver_jobs"]) == 1
    job = legacy["tables"]["solver_jobs"][0]
    assert job["job_id"] == SOLVER_JOB_ID
    assert job["owner_stage"] == 4
    assert job["status"] == "completed", "the engine must not rewrite solver status"

    assert legacy["tables"]["solver_jobs"] == bounded["tables"]["solver_jobs"]

    _assert_equivalent(legacy, bounded)


# ==================================================== the comparator's teeth
def test_the_comparator_detects_a_real_divergence():
    """A negative control, so "all green" cannot be vacuous.

    The harness holds the entry point as the only variable.  Here the variable
    is changed deliberately - the bounded track is allowed two subtasks instead
    of one - and the comparator must say so.  Without this, a comparator that
    compared nothing would look exactly like a passing Gate 1.
    """

    evidence = canary.build_seed(_stage_seed)
    revision = evidence["project_state"]["revision"]

    one_step = _bounded_track(
        _stage_registry,
        expected_revision=revision,
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )
    two_steps = _bounded_track(
        _stage_registry,
        expected_revision=revision,
        max_subtasks=2,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    findings = canary.compare(one_step, two_steps)
    non_empty = {area: diff for area, diff in findings.items() if diff}

    assert non_empty, "the comparator reported no difference between 1 and 2 subtasks"
    # and it localised the divergence rather than returning a bare flag
    assert "project_state" in non_empty or "events" in non_empty
    assert one_step["outcome"].end_revision != two_steps["outcome"].end_revision


def test_the_comparator_is_silent_on_the_same_inputs():
    """The other half of the control: identical runs compare equal."""

    evidence = canary.build_seed(_stage_seed)
    revision = evidence["project_state"]["revision"]

    first = _bounded_track(
        _stage_registry,
        expected_revision=revision,
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )
    second = _bounded_track(
        _stage_registry,
        expected_revision=revision,
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    assert first["state_hashes"] == second["state_hashes"], "replay hashes must reproduce"
    assert first["aggregate_domain_root"] == second["aggregate_domain_root"]
    assert not {area: d for area, d in canary.compare(first, second).items() if d}


# ================================================ G1.4 with obligations present
def test_stage_v1_dirty_obligations_are_carried_equivalently():
    """G1.4 asserted on a non-empty obligation set."""

    evidence = canary.build_seed(_stage_seed_with_dirty)

    # the seed itself must carry the obligation, or the test below is vacuous
    assert evidence["counts"]["dirty_flags"] == 1

    legacy = _legacy_track(_stage_registry)
    bounded = _bounded_track(
        _stage_registry,
        expected_revision=evidence["project_state"]["revision"],
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    assert len(legacy["tables"]["dirty_flags"]) >= 1, "the obligation vanished from the legacy track"
    assert legacy["tables"]["dirty_flags"] == bounded["tables"]["dirty_flags"]
    assert legacy["tables"]["dirty_causes"] == bounded["tables"]["dirty_causes"]
    assert (
        legacy["tables"]["dirty_flag_clear_receipts"]
        == bounded["tables"]["dirty_flag_clear_receipts"]
    )

    _assert_equivalent(legacy, bounded)


# ==================================== the determinism the harness depends on
def test_every_event_is_stamped_with_the_pinned_clock():
    """No time source may bypass the harness, or equality becomes luck.

    ``prompt_step.py`` builds its own ``SQLiteStateStore(context.project_dir)``
    with the default clock, so without ``frozen_time()`` the PROMPT_INPUT_BOUND
    event is stamped from the real wall clock.  Two tracks then agree only when
    they happen to fall inside the same second - passing on an idle machine and
    failing on a loaded one, which is the worst possible failure mode.

    This asserts the property directly, so a future un-injected time source
    (in this path or a new one) fails here - plainly - instead of surfacing as
    an unexplained event_id mismatch somewhere else.
    """

    evidence = canary.build_seed(_stage_seed)
    legacy = _legacy_track(_stage_registry)
    bounded = _bounded_track(
        _stage_registry,
        expected_revision=evidence["project_state"]["revision"],
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    for label, track in (("legacy", legacy), ("bounded", bounded)):
        assert track["event_created_at"], label
        off_clock = [
            (event["revision"], event["type"])
            for event, stamp in zip(track["events"], track["event_created_at"])
            if stamp != canary.CONSTANT_EPOCH
        ]
        assert off_clock == [], f"{label} events stamped off the pinned clock: {off_clock}"

    # and the two tracks agree on the stamps, which is what the comparison uses
    assert legacy["event_created_at"] == bounded["event_created_at"]


# ==================== the clock is inherited, not monkeypatched around
def test_prompt_step_inherits_the_store_clock_without_monkeypatching():
    """Gate 1 section 3.5, fixed at the source rather than in the harness.

    ``prompt_step.py`` opens its own store to bind the prompt-input receipt, and
    that store used to be built with the default clock, so ``PROMPT_INPUT_BOUND``
    was stamped from the real wall clock: its timestamp, and the event id hashed
    from it, were not reproducible, and two runs of the same work stopped being
    comparable.  ``frozen_time()`` worked around it by also replacing the storage
    class, which is a harness compensating for a product gap.

    The engine now puts its store's clock on the StepContext and the internal
    store inherits it, so this test deliberately calls **no** monkeypatch: the
    constant has to arrive on its own.
    """

    evidence = canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision,
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
    )

    # no frozen_time() on purpose
    outcome = engine.run_bounded(contract)
    collected = canary.collect(root)

    types = [event["type"] for event in collected["events"]]
    assert "PROMPT_INPUT_BOUND" in types, "the prompt path must be exercised"
    assert outcome.completed_subtasks == 1

    off_clock = [
        (kind, stamp)
        for kind, stamp in zip(types, collected["event_created_at"])
        if stamp != canary.CONSTANT_EPOCH
    ]
    assert off_clock == [], f"events stamped off the injected clock: {off_clock}"


def test_the_context_carries_the_clock_the_engine_was_built_with():
    """The mechanism, asserted directly: no store is opened with a stray clock."""

    from factory_core.storage import SQLiteStateStore

    canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    store = canary.store_at(root)
    assert store.clock is canary.constant_clock
    assert SQLiteStateStore(root).clock is not canary.constant_clock, (
        "a store built without a clock still uses the default, which is why the "
        "context has to carry it"
    )
