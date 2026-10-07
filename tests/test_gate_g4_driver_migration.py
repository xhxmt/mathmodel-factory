"""Gate 4: the first hand-written driver chain, migrated and proved equivalent.

The chain is ``ongoing/cumcm_2026_a_fable_pro_20260910/work/run_bounded_evidence_repair.py``
(74 lines).  Its name already claimed ``run_bounded`` while its body still called
``engine.run(max_steps=1)``, and around that call it hand-rolled every guarantee
the bounded contract exists to provide:

    line 19/45  assert state.status.value == 'ready' and state.active_step == 5
    line 20-33  its own protected_files.json, built from another directory's copy
    line 27-30  its own verify(), called before and after
    line 37-38  its own progress.json journal
    line 43-70  its own two-pass loop and stop condition
    line 49-62  a private StepRegistry subclass capping max_attempts/max_reopens
    line 67     records "protected_files_unchanged: True" without deriving it

The migration is proved here rather than asserted in prose, by running the
driver's *own* mechanism side by side with the contract's on the same seed:

    legacy track   engine.run(max_steps=1)  + the driver's registry shim
    migrated track engine.run_bounded(...)  + the contract's ceilings

Both start from a byte-identical restoration of one seed and must land on the
same semantic closure - the same comparison Gate 1 uses.  The legacy track is
reproduced faithfully because the shim is what the migration has to replace; a
comparison against a bare ``run(max_steps=1)`` would have been comparing against
something the driver never did.

The original project has since completed (revision 561), so the driver's
hard-coded precondition no longer holds and it cannot be run at all.  That is
the rot S6 described, and it is why the seed below exists.
"""
from __future__ import annotations

import dataclasses

import pytest

import _g1_canary as canary
from factory_core.bounded_run import BoundedRunContract, RunPolicy
from factory_core.engine import FactoryEngine
from factory_core.registry import StepRegistry

#: The steps the driver capped, and the caps it used.
_SHIM_STEPS = (4, 5)


def _driver_shim(limit):
    """The driver's private registry subclass, reproduced exactly.

    ``limit`` is the driver's ``before.attempt + 1`` for step 5; step 4 was
    always capped at 1 and both had reopens disabled.
    """

    base = canary.stage_registry()

    class BoundedRegistry(StepRegistry):
        def get(self, step_id):
            definition = base.get(step_id)
            if step_id in _SHIM_STEPS:
                return dataclasses.replace(
                    definition,
                    max_attempts=limit if step_id == 5 else 1,
                    max_reopens=0,
                )
            return definition

        def next_after(self, completed_step):
            definition = base.next_after(completed_step)
            return self.get(definition.id) if definition else None

        def stage_subtask(self, key):
            return base.stage_subtask(key)

        def __iter__(self):
            return (self.get(definition.id) for definition in base)

    return BoundedRegistry()


def _legacy_track(limit) -> dict:
    """The driver as written: bare run plus its own shim."""

    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=_driver_shim(limit)
    )
    with canary.frozen_time():
        state = engine.run(max_steps=1)
    result = canary.collect(root)
    result["returned_last_completed_step"] = state.last_completed_step
    return result


def _migrated_track(limit, *, actor="operator") -> dict:
    """The driver migrated: one bounded invocation, no shim, no journals."""

    root = canary.restore_seed()
    engine = FactoryEngine(root, store=canary.store_at(root), registry=canary.stage_registry())
    state = canary.store_at(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision,
        # the driver's hard-coded assert, expressed as an authorisation
        expected_cursor=(state.active_stage, state.active_subtask, state.source_step_id),
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
        protected_manifest={
            canary.PROTECTED_FILE: canary.protected_digest(canary.PROTECTED_CONTENT)
        },
        max_attempts_per_step={5: limit, 4: 1},
        max_reopens_per_step={5: 0, 4: 0},
        actor=actor,
    )
    with canary.frozen_time():
        outcome = engine.run_bounded(contract)
    result = canary.collect(root)
    result["returned_last_completed_step"] = result["project_state"]["last_completed_step"]
    result["outcome"] = outcome
    return result


def _seed():
    return canary.build_seed(canary.stage_seed_at_step_5_with_protected_file)


# ==================================================== equivalence with the shim
def test_the_migrated_form_matches_the_driver_it_replaces():
    """The core migration claim, asserted rather than stated.

    The only permitted difference is the bounded authorisation block in
    RUN_STARTED, which is the point of the migration.  Everything else - the
    checkpoints, the cursor, the events, the state hashes, the domain root and
    the file manifest - must be identical.
    """

    evidence = _seed()
    limit = evidence["project_state"]["attempt"] + 1

    legacy = _legacy_track(limit)
    migrated = _migrated_track(limit)

    assert legacy["returned_last_completed_step"] == migrated["returned_last_completed_step"]
    assert migrated["outcome"].completed_subtasks == 1

    findings = canary.compare(legacy, migrated)
    non_empty = {area: diff for area, diff in findings.items() if diff}
    assert not non_empty, "\n".join(
        f"{area}:\n  " + "\n  ".join(diff[:10]) for area, diff in non_empty.items()
    )


def test_the_migration_writes_none_of_the_drivers_four_artefacts():
    """What the driver had to write, and the contract does not."""

    evidence = _seed()
    limit = evidence["project_state"]["attempt"] + 1
    migrated = _migrated_track(limit)

    relative = set(migrated["files"])
    assert "progress.json" not in relative
    assert "protected_files.json" not in relative
    assert not any(path.endswith("progress.json") for path in relative)
    assert not any(path.endswith("protected_files.json") for path in relative)


def test_the_structured_result_carries_what_the_driver_journalled():
    """Every field the hand-written progress.json recorded, derived instead.

    The driver recorded before/after state, an unconditional
    ``protected_files_unchanged: True`` and its own stop reason.  All of it is
    now reported by the run itself, and the manifest claim is derived from a
    verification rather than asserted.
    """

    evidence = _seed()
    limit = evidence["project_state"]["attempt"] + 1
    migrated = _migrated_track(limit)
    outcome = migrated["outcome"]
    payload = outcome.to_dict()

    assert payload["schema"] == "factory-bounded-run-result-v1"
    assert payload["start_revision"] == evidence["project_state"]["revision"]
    assert payload["end_revision"] == outcome.end_revision
    assert payload["completed_subtasks"] == 1
    assert payload["stop_reason"]
    assert payload["boundary_fingerprint"]

    # the driver's "protected_files_unchanged: True" is now a verification
    assert outcome.entry_verification.ok is True
    assert outcome.entry_verification.checked == 1
    assert outcome.final_verification.ok is True

    # and the authorisation, including the ceilings, is on the event stream
    bindings = canary.bounded_authorisation(migrated["events"])
    assert bindings, "the bounded authorisation must be recoverable afterwards"
    block = bindings[0]["block"]
    assert block["max_attempts_per_step"] == {"4": 1, "5": limit}
    assert block["max_reopens_per_step"] == {"4": 0, "5": 0}


# ========================================= the migration cannot loosen anything
def test_a_manifest_broken_during_the_migrated_run_is_refused():
    """The driver checked this itself, unconditionally, after the fact.

    Same scenario as the driver's ``verify()`` after the run, except the refusal
    now happens before the checkpoint commits rather than being discovered
    afterwards by the caller.
    """

    evidence = _seed()
    root = canary.restore_seed()
    limit = evidence["project_state"]["attempt"] + 1

    def break_it(_request):
        (root / canary.PROTECTED_FILE).write_text("tampered\n", encoding="utf-8")

    engine = FactoryEngine(
        root,
        store=canary.store_at(root),
        registry=canary.stage_registry(
            dispatcher=canary.HermeticDispatcher(on_execute=break_it)
        ),
    )
    state = canary.store_at(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision,
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
        protected_manifest={
            canary.PROTECTED_FILE: canary.protected_digest(canary.PROTECTED_CONTENT)
        },
        max_attempts_per_step={4: 1, 5: limit},
        max_reopens_per_step={4: 0, 5: 0},
    )

    with canary.frozen_time():
        outcome = engine.run_bounded(contract)

    assert outcome.stop_reason == "PROTECTED_MANIFEST_VIOLATED"
    assert outcome.entry_verification.ok is True
    assert outcome.final_verification.ok is False

    # the seed already carries checkpoints for the steps completed before it, so
    # the assertion is that no *new* one was committed for the blocked subtask
    after = canary.collect(root)
    assert len(after["tables"]["stage_checkpoints"]) == evidence["counts"]["stage_checkpoints"]
    assert not any(
        checkpoint["source_step_id"] == 5
        for checkpoint in after["tables"]["stage_checkpoints"]
    ), "the blocked subtask must not have committed a checkpoint"


def test_the_drivers_hard_coded_precondition_is_no_longer_needed():
    """The rot S6 described, expressed as a test.

    The driver asserted ``status == 'ready' and active_step == 5``.  Project A is
    now completed at revision 561, so that assertion is dead.  The contract
    expresses the same expectation as an authorisation, and a stale one is
    refused with a reason instead of an AssertionError inside a script.
    """

    from factory_core.bounded_run import BoundedRunError

    evidence = _seed()
    root = canary.restore_seed()
    engine = FactoryEngine(root, store=canary.store_at(root), registry=canary.stage_registry())

    stale = BoundedRunContract(
        expected_revision=evidence["project_state"]["revision"] + 100,
        expected_cursor=(4, "solve", 5),
    )
    with canary.frozen_time(), pytest.raises(BoundedRunError, match="expected revision"):
        engine.run_bounded(stale)

    wrong_position = BoundedRunContract(
        expected_revision=evidence["project_state"]["revision"],
        expected_cursor=(9, "delivery", 16),
    )
    with canary.frozen_time(), pytest.raises(BoundedRunError, match="cursor mismatch"):
        engine.run_bounded(wrong_position)
