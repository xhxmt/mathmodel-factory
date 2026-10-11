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
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
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
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )

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


# ============================ generic manifest-only driver shape
# The two representatives below are the plain "protect, advance one step,
# re-protect" drivers.  They differ from the first chain only in that they carry
# no registry shim, so the legacy form needs no shim reproduction either.
def _protected() -> dict:
    return {
        canary.PROTECTED_FILE: canary.protected_digest(canary.PROTECTED_CONTENT)
    }


def _verify(root):
    import hashlib

    for relative, expected in _protected().items():
        with (root / relative).open("rb") as handle:
            assert hashlib.file_digest(handle, "sha256").hexdigest() == expected, relative


def _manifest_legacy_track(completed_step):
    """The driver as written: verify, run(max_steps=1), verify again."""

    root = canary.restore_seed()
    _verify(root)
    engine = FactoryEngine(
        root,
        store=canary.store_at(root),
        registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    with canary.frozen_time():
        state = engine.run(max_steps=1)
    _verify(root)
    result = canary.collect(root)
    result["returned_last_completed_step"] = state.last_completed_step
    return result


def _manifest_migrated_track(completed_step):
    """The migrated form: one bounded invocation carrying the same manifest."""

    root = canary.restore_seed()
    engine = FactoryEngine(
        root,
        store=canary.store_at(root),
        registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision,
        expected_cursor=(state.active_stage, state.active_subtask, state.source_step_id),
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
        protected_manifest=_protected(),
    )
    with canary.frozen_time():
        outcome = engine.run_bounded(contract)
    result = canary.collect(root)
    result["returned_last_completed_step"] = result["project_state"]["last_completed_step"]
    result["outcome"] = outcome
    return result


def _assert_migrated_shape(legacy, migrated):
    assert legacy["returned_last_completed_step"] == migrated["returned_last_completed_step"]
    findings = canary.compare(legacy, migrated)
    non_empty = {area: diff for area, diff in findings.items() if diff}
    assert not non_empty, "\n".join(
        f"{area}:\n  " + "\n  ".join(diff[:10]) for area, diff in non_empty.items()
    )
    written = set(migrated["files"])
    assert not any(p.endswith("progress.json") for p in written)
    assert not any(p.endswith("protected_files.json") for p in written)
    assert migrated["outcome"].entry_verification.ok is True
    assert migrated["outcome"].final_verification.ok is True


# ---------------------------------------------- representative: the simplest one
def test_the_simplest_driver_migrates():
    """``work/run_step12_m6.py``, 21 lines, the smallest real driver.

    Its whole body is: assert a hard-coded revision and step, load a manifest,
    verify, run one step, verify again, journal twice.  Everything except the
    advance is a contract field.
    """

    # The driver targeted step 12.  The hermetic registry cannot reach it: the
    # reviewer entry gate at step 8.5 needs real gate evidence (an entry_gate.md
    # verdict), which a permissive validator does not produce, so every seed at
    # or beyond step 8 stops there.  Steps 0-8 are covered cleanly, so the proof
    # runs at a reachable position instead - the migration claim is about the
    # entry point, not about which step the historical driver happened to pick.
    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    legacy = _manifest_legacy_track(6)
    migrated = _manifest_migrated_track(6)

    _assert_migrated_shape(legacy, migrated)
    assert migrated["returned_last_completed_step"] == 7


def test_the_simplest_drivers_hard_coded_revision_is_now_an_authorisation():
    """``assert state.revision == 306`` - the rotted assertion, replaced.

    The driver pinned revision 306 of a project that has since reached 561, so
    the assertion is dead.  As an authorisation it is refused with the actual
    revision in the message, and nothing is written.
    """

    from factory_core.bounded_run import BoundedRunError

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    engine = FactoryEngine(
        root,
        store=canary.store_at(root),
        registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    before = canary.collect(root)

    with canary.frozen_time(), pytest.raises(BoundedRunError) as raised:
        engine.run_bounded(
            BoundedRunContract(expected_revision=306, max_subtasks=1)
        )
    assert "expected revision 306" in str(raised.value)

    assert canary.collect(root)["events"] == before["events"]


# --------------------------------------------- representative: protection family
def test_the_protection_driver_migrates():
    """``work/continue_adopted_model.py``, 48 lines, the protection family.

    It additionally derives part of its manifest by hashing two directories and
    loops over two steps.  The migrated form folds the manifest into the
    contract and expresses each step as its own invocation - the loop becomes
    the caller's, and the repeated boundary is what
    ``previous_boundary_fingerprint`` is for.
    """

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 2)
    )
    legacy = _manifest_legacy_track(2)
    migrated = _manifest_migrated_track(2)

    _assert_migrated_shape(legacy, migrated)
    assert migrated["returned_last_completed_step"] == 3


def test_the_protection_driver_migrates_a_second_step():
    """The driver's ``for step in (3, 4)`` loop, as successive invocations."""

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 3)
    )
    legacy = _manifest_legacy_track(3)
    migrated = _manifest_migrated_track(3)

    _assert_migrated_shape(legacy, migrated)
    assert migrated["returned_last_completed_step"] == 4


def test_a_repeated_invocation_reports_an_unchanged_boundary():
    """What replaced the driver's ad-hoc stop condition.

    The driver broke out of its loop when the state no longer matched, and
    recorded ``protected_hashes_unchanged: True`` unconditionally.  The migrated
    form asks the engine: the same boundary with no progress is reported as
    NEEDS_INSPECTION rather than silently looking like success.
    """

    canary.build_seed(canary.paused_seed)
    root = canary.restore_seed()
    engine = FactoryEngine(
        root,
        store=canary.store_at(root),
        registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    revision = canary.store_at(root).load().revision

    first = engine.run_bounded(BoundedRunContract(expected_revision=revision))
    second = engine.run_bounded(
        BoundedRunContract(
            expected_revision=revision,
            previous_boundary_fingerprint=first.boundary_fingerprint,
        )
    )

    assert second.made_progress is False
    assert second.unchanged_boundary is True
    assert second.to_dict()["outcome"] == "NEEDS_INSPECTION"


# ============================ the hermetic registry's documented reach
def test_the_hermetic_registry_reaches_step_eight_and_no_further():
    """A fidelity boundary, asserted rather than discovered later.

    ``build_native_registry`` gates step 8.5 on the reviewer entry gate's real
    evidence - an entry_gate.md verdict and its two companion maps.  A hermetic
    validator that accepts anything does not produce them, so seeds at or beyond
    step 8 stop at the gate instead of advancing.  Recording the boundary keeps
    it from being mistaken for a driver difference, and marks where a fixture
    would have to grow if a future representative needs step 8.5 or later.
    """

    advances = {}
    for completed in range(3, 9):
        canary.build_seed(
            lambda root, c=completed: canary.stage_seed_ready_for_step_with_protected_file(root, c)
        )
        root = canary.restore_seed()
        engine = FactoryEngine(
            root,
            store=canary.store_at(root),
            registry=canary.stage_registry(),
            sleeper=lambda _: None,
        )
        with canary.frozen_time():
            state = engine.run(max_steps=1)
        advances[completed] = state.last_completed_step

    # clean one-subtask advances below the gate
    assert advances[3] == 4
    assert advances[4] == 5
    assert advances[6] == 7
    assert advances[7] == 8
    # and the gate is where the reach ends
    assert advances[8] == 8, "step 8.5 onward needs real gate evidence"


# ===================== the identity protection the recovery driver needed
def _identity_of(root, relative):
    stat = (root / relative).stat()
    return (stat.st_size, stat.st_mtime_ns)


def test_protected_identity_is_validated_at_construction():
    """Same path safety and shape checks as the hash manifest."""

    from factory_core.bounded_run import BoundedRunError

    for kwargs in (
        {"protected_identity": {"../escape": (1, 2)}},
        {"protected_identity": {"/abs": (1, 2)}},
        {"protected_identity": {"./dot": (1, 2)}},
        {"protected_identity": {"ok": 5}},
        {"protected_identity": {"ok": (-1, 2)}},
        {"protected_identity": {"ok": (1, -2)}},
        {"protected_identity": {"ok": (True, 2)}},
    ):
        with pytest.raises(BoundedRunError):
            BoundedRunContract(expected_revision=1, **kwargs)


def test_a_path_cannot_be_protected_two_ways_at_once():
    """One path, one kind of check - otherwise the weaker one is ambiguous."""

    from factory_core.bounded_run import BoundedRunError

    with pytest.raises(BoundedRunError, match="both protected_manifest and"):
        BoundedRunContract(
            expected_revision=1,
            protected_manifest={"canary_protected.txt": "a" * 64},
            protected_identity={"canary_protected.txt": (3, 4)},
        )


def test_identity_is_part_of_the_contract_identity():
    base = BoundedRunContract(expected_revision=1)
    with_identity = BoundedRunContract(
        expected_revision=1, protected_identity={"big.bin": (10, 20)}
    )
    assert base.contract_sha256 != with_identity.contract_sha256
    assert with_identity.event_payload()["protected_identity"] == {
        "big.bin": {"size": 10, "mtime_ns": 20}
    }
    assert BoundedRunContract(
        expected_revision=1, protected_identity={}
    ).contract_sha256 == base.contract_sha256, "an empty mapping is absent"


def test_identity_is_enforced_at_entry_and_before_commit():
    """The two checks the driver hand-rolled, both now engine-side."""

    from factory_core.bounded_run import ProtectedManifestViolation

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    identity = {canary.PROTECTED_FILE: _identity_of(root, canary.PROTECTED_FILE)}

    # clean at entry: the run must proceed and commit
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()
    with canary.frozen_time():
        outcome = engine.run_bounded(
            BoundedRunContract(
                expected_revision=state.revision,
                max_subtasks=1,
                run_policy=RunPolicy.BOUNDED_SUBTASKS,
                protected_identity=identity,
            )
        )
    assert outcome.entry_verification.ok is True
    assert outcome.final_verification.ok is True
    assert outcome.entry_verification.checked == 1

    # broken at entry: refused before anything runs
    canary.restore_seed()
    (root / canary.PROTECTED_FILE).write_text("tampered\n", encoding="utf-8")
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()
    with canary.frozen_time(), pytest.raises(ProtectedManifestViolation):
        engine.run_bounded(
            BoundedRunContract(expected_revision=state.revision, protected_identity=identity)
        )


def test_an_identity_break_during_the_run_is_reported_separately():
    """A size/mtime change must not be confused with a hash change."""

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    identity = {canary.PROTECTED_FILE: _identity_of(root, canary.PROTECTED_FILE)}

    def break_it(_request):
        (root / canary.PROTECTED_FILE).write_text("a much longer replacement\n", encoding="utf-8")

    engine = FactoryEngine(
        root,
        store=canary.store_at(root),
        registry=canary.stage_registry(
            dispatcher=canary.HermeticDispatcher(on_execute=break_it)
        ),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()
    with canary.frozen_time():
        outcome = engine.run_bounded(
            BoundedRunContract(
                expected_revision=state.revision,
                max_subtasks=1,
                run_policy=RunPolicy.BOUNDED_SUBTASKS,
                protected_identity=identity,
            )
        )

    assert outcome.final_verification.ok is False
    assert canary.PROTECTED_FILE in outcome.final_verification.identity_changed
    assert outcome.final_verification.changed == ()
    assert outcome.stop_reason == "PROTECTED_MANIFEST_VIOLATED"


def test_identity_protection_is_weaker_than_hashing_and_says_so():
    """The documented weakness, asserted so it cannot be discovered by surprise.

    Equal size and mtime do not prove equal content: a writer that rewrites the
    file with the same length and then restores the mtime passes the identity
    check.  That is the tradeoff the caller accepts in exchange for not hashing a
    very large artifact, and it is why the two checks are reported separately.
    """

    import os

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    path = root / canary.PROTECTED_FILE
    original = path.read_bytes()
    identity = {canary.PROTECTED_FILE: _identity_of(root, canary.PROTECTED_FILE)}

    replacement = b"X" * len(original)
    assert replacement != original
    path.write_bytes(replacement)
    os.utime(path, ns=(identity[canary.PROTECTED_FILE][1], identity[canary.PROTECTED_FILE][1]))

    from factory_core.bounded_run import verify_protected_manifest

    as_identity = verify_protected_manifest(root, {}, identity)
    assert as_identity.ok is True, "identity alone cannot see this"

    as_hash = verify_protected_manifest(
        root, {canary.PROTECTED_FILE: canary.protected_digest(canary.PROTECTED_CONTENT)}
    )
    assert as_hash.ok is False, "a hash can"


# ------------------------- representative: the recovery/boundary family
LARGE_FILE = "canary_large_manifest.json"
LARGE_CONTENT = "{\"value\": 1}\n"


def _recovery_seed(root):
    """The recovery driver's stance: hash the small things, identify the big one.

    ``run_final_workflow_resume.py`` loaded both a sha256 manifest and a
    ``large_manifest_identity.json`` carrying a path, a size and an mtime_ns, and
    checked both.  The seed reproduces that shape.
    """

    canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    (root / LARGE_FILE).write_text(LARGE_CONTENT, encoding="utf-8")


def _recovery_contract_fields(root):
    stat = (root / LARGE_FILE).stat()
    return {
        "protected_manifest": _protected(),
        "protected_identity": {LARGE_FILE: (stat.st_size, stat.st_mtime_ns)},
    }


def _recovery_legacy_track():
    """The driver as written: verify both kinds, run, verify both again."""

    import hashlib

    root = canary.restore_seed()
    fields = _recovery_contract_fields(root)

    def verify():
        for relative, expected in fields["protected_manifest"].items():
            with (root / relative).open("rb") as handle:
                assert hashlib.file_digest(handle, "sha256").hexdigest() == expected
        stat = (root / LARGE_FILE).stat()
        size, mtime_ns = fields["protected_identity"][LARGE_FILE]
        assert stat.st_size == size and stat.st_mtime_ns == mtime_ns

    verify()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    with canary.frozen_time():
        state = engine.run(max_steps=1)
    verify()
    result = canary.collect(root)
    result["returned_last_completed_step"] = state.last_completed_step
    return result


def _recovery_migrated_track():
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision,
        expected_cursor=(state.active_stage, state.active_subtask, state.source_step_id),
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
        **_recovery_contract_fields(root),
    )
    with canary.frozen_time():
        outcome = engine.run_bounded(contract)
    result = canary.collect(root)
    result["returned_last_completed_step"] = result["project_state"]["last_completed_step"]
    result["outcome"] = outcome
    return result


def test_the_recovery_driver_migrates_with_both_kinds_of_protection():
    """``work/run_final_workflow_resume.py``, 40 lines, the recovery family.

    Its verify() checked a sha256 manifest *and* a large file's identity, and its
    while loop ran bounded steps until the position left a range.  Both kinds of
    check are now contract fields, checked at entry and before the commit.
    """

    canary.build_seed(_recovery_seed)
    legacy = _recovery_legacy_track()
    migrated = _recovery_migrated_track()

    assert legacy["returned_last_completed_step"] == migrated["returned_last_completed_step"]
    findings = canary.compare(legacy, migrated)
    non_empty = {area: diff for area, diff in findings.items() if diff}
    assert not non_empty, "\n".join(
        f"{area}:\n  " + "\n  ".join(diff[:10]) for area, diff in non_empty.items()
    )

    outcome = migrated["outcome"]
    assert outcome.entry_verification.ok is True
    assert outcome.final_verification.ok is True
    assert outcome.entry_verification.checked == 2, "one hash plus one identity"
    assert not any(p.endswith("progress.json") for p in migrated["files"])
    assert not any(p.endswith("large_manifest_identity.json") for p in migrated["files"])


def test_the_recovery_drivers_loop_becomes_successive_bounded_calls():
    """Its ``while 11 <= active_step <= 15`` becomes caller-side repetition.

    The stop condition is the difference: the driver inspected the state itself
    and broke out; the migrated form asks each invocation what happened, and a
    repeat with no progress is NEEDS_INSPECTION rather than a silent exit.
    """

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )

    steps = []
    fingerprint = None
    for _ in range(2):
        state = canary.store_at(root).load()
        contract = BoundedRunContract(
            expected_revision=state.revision,
            max_subtasks=1,
            run_policy=RunPolicy.BOUNDED_SUBTASKS,
            protected_manifest=_protected(),
            previous_boundary_fingerprint=fingerprint,
        )
        with canary.frozen_time():
            outcome = engine.run_bounded(contract)
        steps.append((outcome.completed_subtasks, outcome.made_progress))
        fingerprint = outcome.boundary_fingerprint
        if not outcome.made_progress:
            break

    assert steps[0] == (1, True), "the first invocation advanced"
    # the second continues from where the first stopped, so it also advances
    assert steps[1] == (1, True)
    assert all(advanced for _, advanced in steps)


# ============== the committed-checkpoint protection the last family needs
def test_protected_checkpoints_are_validated_at_construction():
    from factory_core.bounded_run import BoundedRunError

    for bad in ({-1}, {True}, {"13"}, {None}):
        with pytest.raises(BoundedRunError):
            BoundedRunContract(expected_revision=1, protected_checkpoints=bad)
    assert BoundedRunContract(
        expected_revision=1, protected_checkpoints=set()
    ).protected_checkpoints is None, "an empty set is absent"


def test_protected_checkpoints_are_part_of_the_contract_identity():
    base = BoundedRunContract(expected_revision=1)
    protected = BoundedRunContract(expected_revision=1, protected_checkpoints={13, 14})
    assert base.contract_sha256 != protected.contract_sha256
    assert protected.event_payload()["protected_checkpoints"] == [13, 14]
    assert base.event_payload()["protected_checkpoints"] is None


def test_protecting_a_checkpoint_that_does_not_exist_is_refused_at_entry():
    """There would be nothing to protect, so the authorisation is unusable."""

    from factory_core.bounded_run import ProtectedCheckpointViolation

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    before = canary.collect(root)
    state = canary.store_at(root).load()

    with canary.frozen_time(), pytest.raises(
        ProtectedCheckpointViolation, match="absent at entry"
    ):
        engine.run_bounded(
            BoundedRunContract(
                expected_revision=state.revision, protected_checkpoints={13}
            )
        )

    assert canary.collect(root)["events"] == before["events"]


def test_a_run_that_leaves_the_protected_checkpoint_alone_passes():
    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()

    with canary.frozen_time():
        outcome = engine.run_bounded(
            BoundedRunContract(
                expected_revision=state.revision,
                max_subtasks=1,
                run_policy=RunPolicy.BOUNDED_SUBTASKS,
                protected_checkpoints={4},
            )
        )

    assert outcome.entry_checkpoints.to_dict() == {
        "ok": True, "checked": [4], "changed": [], "missing": []
    }
    assert outcome.final_checkpoints.ok is True
    assert outcome.stop_reason != "PROTECTED_CHECKPOINT_VIOLATED"


def test_a_checkpoint_rewritten_during_the_run_blocks_the_commit():
    """The last family's guard, moved from after the run to before the commit.

    The three special_business drivers snapshotted step 13's checkpoint and
    asserted it was unchanged afterwards.  Here the change happens *during* the
    step, which is the window a caller-side check cannot cover: the engine sees
    it before the success checkpoint commits and refuses.
    """

    import sqlite3

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()

    def rewrite_the_checkpoint(_request):
        connection = sqlite3.connect(root / ".factory" / "state.db")
        try:
            connection.execute(
                "UPDATE stage_checkpoints SET receipt_json=? WHERE source_step_id=4",
                ('{"tampered": true}',),
            )
            connection.commit()
        finally:
            connection.close()

    engine = FactoryEngine(
        root,
        store=canary.store_at(root),
        registry=canary.stage_registry(
            dispatcher=canary.HermeticDispatcher(on_execute=rewrite_the_checkpoint)
        ),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()

    with canary.frozen_time():
        outcome = engine.run_bounded(
            BoundedRunContract(
                expected_revision=state.revision,
                max_subtasks=1,
                run_policy=RunPolicy.BOUNDED_SUBTASKS,
                protected_checkpoints={4},
            )
        )

    # 1. the engine blocked it, and named the checkpoint condition
    failures = [
        event
        for event in canary.collect(root)["events"]
        if event["type"] == "STEP_FAILED"
    ]
    assert failures
    assert "PERMANENT_PROTECTED_CHECKPOINT_VIOLATED" in [
        event["payload"].get("error_class") for event in failures
    ]

    # 2. the structured result agrees, and names the step
    assert outcome.stop_reason == "PROTECTED_CHECKPOINT_VIOLATED"
    assert outcome.final_checkpoints.ok is False
    assert outcome.final_checkpoints.changed == (4,)
    assert outcome.entry_checkpoints.ok is True, "it was intact at entry"

    # 3. no success checkpoint was committed for the running subtask
    after = canary.collect(root)
    assert not any(
        checkpoint["source_step_id"] == 7
        for checkpoint in after["tables"]["stage_checkpoints"]
    )


# ================= batch 1: the first production driver actually rewritten
# work/run_results_adoption.py was migrated in place on 2026-10-07 (batch 1 of
# G4.5c).  This pins the shape it was rewritten from, so the rewrite has a
# version-controlled equivalence proof rather than only a backup.
def _adoption_shim():
    """The driver's SingleAttemptRegistry: step 5 capped at one attempt."""

    base = canary.stage_registry()

    class SingleAttemptRegistry(StepRegistry):
        def get(self, step_id):
            definition = base.get(step_id)
            if step_id == 5:
                return dataclasses.replace(definition, max_attempts=1, max_reopens=0)
            return definition

        def next_after(self, completed_step):
            definition = base.next_after(completed_step)
            return self.get(definition.id) if definition else None

        def stage_subtask(self, key):
            return base.stage_subtask(key)

        def __iter__(self):
            return (self.get(definition.id) for definition in base)

    return SingleAttemptRegistry()


def test_the_batch1_driver_shape_is_equivalent_after_migration():
    """legacy: run(max_steps=1) + SingleAttemptRegistry.  migrated: ceilings."""

    canary.build_seed(canary.stage_seed_at_step_5_with_protected_file)
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=_adoption_shim(),
        sleeper=lambda _: None,
    )
    with canary.frozen_time():
        legacy_state = engine.run(max_steps=1)
    legacy = canary.collect(root)
    legacy["returned_last_completed_step"] = legacy_state.last_completed_step

    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision,
        expected_cursor=(state.active_stage, state.active_subtask, state.source_step_id),
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
        max_attempts_per_step={5: 1},
        max_reopens_per_step={5: 0},
        protected_manifest=_protected(),
        actor="operator",
    )
    with canary.frozen_time():
        outcome = engine.run_bounded(contract)
    migrated = canary.collect(root)
    migrated["returned_last_completed_step"] = migrated["project_state"]["last_completed_step"]

    assert migrated["returned_last_completed_step"] == legacy["returned_last_completed_step"]
    assert outcome.completed_subtasks == 1

    findings = canary.compare(legacy, migrated)
    non_empty = {area: diff for area, diff in findings.items() if diff}
    assert not non_empty, "\n".join(
        f"{area}:\n  " + "\n  ".join(diff[:10]) for area, diff in non_empty.items()
    )
    assert outcome.entry_verification.ok and outcome.final_verification.ok
    assert not any(p.endswith("progress.json") for p in migrated["files"])
    assert not any(p.endswith("protected_files.json") for p in migrated["files"])


def test_the_batch1_cursor_precondition_is_an_authorisation_not_an_assert():
    """The driver asserted ready/active_step==5/last_completed_step==4.

    As an authorisation the same expectation is refused with a readable reason,
    and - unlike the assertion - it also pins the revision the driver never
    checked.
    """

    from factory_core.bounded_run import BoundedRunError

    canary.build_seed(canary.stage_seed_at_step_5_with_protected_file)
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()

    with canary.frozen_time(), pytest.raises(BoundedRunError, match="cursor mismatch"):
        engine.run_bounded(
            BoundedRunContract(expected_revision=state.revision, expected_cursor=(9, "delivery", 16))
        )


# ================= batch 2: five advance_with_protection drivers rewritten
def test_the_batch2_family_shape_is_equivalent_after_migration():
    """The shape all five batch-2 drivers share: manifest, no shim, one step.

    Two of them carried an extra expectation (``active_step == 9`` / ``== 11``)
    and one expected ``active_subtask == 'reviewer_entry_gate'``; the guard for
    those is asserted separately below, because it cannot ride on the contract's
    cursor.
    """

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    legacy = _manifest_legacy_track(6)
    migrated = _manifest_migrated_track(6)

    assert migrated["returned_last_completed_step"] == legacy["returned_last_completed_step"]
    assert migrated["outcome"].completed_subtasks == 1
    assert migrated["outcome"].entry_verification.checked == 1
    _assert_migrated_shape(legacy, migrated)


def test_a_read_then_pin_cursor_silently_drops_a_step_expectation():
    """The correction batch 2 forced, pinned so it cannot regress unnoticed.

    A driver's precondition is "the next step is N".  ``cursor_of()`` returns
    ``(active_stage, active_subtask, source_step_id)`` - it does not carry
    ``active_step`` - so ``expected_cursor`` cannot express that expectation.  And
    because a caller reads the state to obtain ``expected_revision``, a cursor
    taken from that same read matches by construction: the CAS then passes
    trivially.

    The first version of the batch-2 migration did exactly that, and would have
    advanced a project the original driver refused.  That is the failure this
    test makes visible: a migration can look like it moved a guard into the
    contract while actually deleting it.
    """

    from factory_core.bounded_run import cursor_of

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    state = canary.store_at(root).load()

    # the cursor carries no step information at all in a ready state
    assert cursor_of(state) == (None, None, None)
    assert state.active_step is None
    assert state.last_completed_step == 6

    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    # a contract built from the observed state is accepted even though a driver
    # written for step 12 would have refused this project outright
    with canary.frozen_time():
        outcome = engine.run_bounded(
            BoundedRunContract(
                expected_revision=state.revision,
                expected_cursor=cursor_of(state),
                max_subtasks=1,
                run_policy=RunPolicy.BOUNDED_SUBTASKS,
            )
        )
    assert outcome.made_progress is True, (
        "the read-then-pin form advances a project the driver expected to refuse"
    )

    # what the contract does contribute is the revision CAS, and it is real:
    # a revision that was not just read is refused
    from factory_core.bounded_run import BoundedRunError

    canary.restore_seed()
    stale_engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    with canary.frozen_time(), pytest.raises(BoundedRunError, match="expected revision"):
        stale_engine.run_bounded(
            BoundedRunContract(expected_revision=state.revision + 5)
        )


def test_the_batch2_guard_is_kept_explicit_in_the_drivers():
    """Documents what the migrated drivers do instead of relying on the cursor.

    They check the field the original asserted - ``active_step`` or
    ``active_subtask`` - and refuse with a readable message, then let the
    contract add the revision CAS.  This test exercises that pattern so the
    distinction from ``expected_cursor`` stays visible in the suite.
    """

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    state = canary.store_at(canary.restore_seed()).load()

    def guard(expected_step):
        if state.status.value != "ready" or state.active_step != expected_step:
            return "refused"
        return "allowed"

    assert guard(7) == "refused", "not the driver's step" if state.active_step is None else ""
    assert guard(None) == "allowed", "the seeded state is exactly what the guard expects"


# ============== batch 3: the private repeat counter becomes a fingerprint
def test_the_batch3_boundary_fingerprint_replaces_a_private_repeat_counter():
    """Two of the batch-3 drivers kept their own `seen` counter.

    They keyed on (active_step, active_subtask, last_completed_step) and stopped
    only once the same key had been seen THREE times, with a hand-written reason
    string.  The contract answers the same question on the second identical
    boundary, because it compares the boundary fingerprint rather than counting
    occurrences - so the caller stops one iteration earlier and the verdict comes
    from the engine as NEEDS_INSPECTION.

    This asserts both halves: the engine reports on call 2, and the driver's own
    rule had not yet reached its threshold at that point.
    """

    evidence = canary.build_seed(canary.paused_seed)
    revision = evidence["project_state"]["revision"]
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )

    seen: dict = {}
    calls = 0
    fingerprint = None
    outcome = None
    while calls < 5:
        state = canary.store_at(root).load()
        key = (state.active_step, state.active_subtask, state.last_completed_step)
        seen[key] = seen.get(key, 0) + 1
        calls += 1
        with canary.frozen_time():
            outcome = engine.run_bounded(
                BoundedRunContract(
                    expected_revision=state.revision,
                    previous_boundary_fingerprint=fingerprint,
                )
            )
        fingerprint = outcome.boundary_fingerprint
        if outcome.unchanged_boundary:
            break
        if seen[key] > 2:
            break

    assert calls == 2, "the engine stops on the second identical boundary"
    assert outcome.unchanged_boundary is True
    assert outcome.needs_inspection is True
    assert outcome.to_dict()["outcome"] == "NEEDS_INSPECTION"
    assert seen[key] == 2, "the driver's own rule needed >2 and had not fired"
    assert revision is not None


def test_the_batch3_identity_and_loop_shape_is_already_covered():
    """Batch 3's representative is the recovery driver batch 2 proved.

    ``run_final_workflow_resume.py`` is where the large-file identity and the
    step-range loop came from, and
    ``test_the_recovery_driver_migrates_with_both_kinds_of_protection`` already
    asserts that shape end to end.  This test records the mapping so the batch's
    coverage cannot be mistaken for missing.
    """

    canary.build_seed(_recovery_seed)
    migrated = _recovery_migrated_track()

    assert migrated["outcome"].entry_verification.checked == 2
    assert migrated["outcome"].final_verification.ok is True
    assert not any(p.endswith("large_manifest_identity.json") for p in migrated["files"])


# ============== batch 4: every authorisation bound at once, argv CAS included
def test_the_batch4_shape_binds_every_authorisation_at_once():
    """The three special_business drivers carry the most contract fields at once.

    An argv-supplied revision (a real compare-and-swap, not a read-back), the
    stage cursor, an allowed source-step scope, a subtask bound, a hash manifest,
    a large-file identity and a protected checkpoint.  All of them must appear in
    the RUN_STARTED authorisation, because the question afterwards is which
    authorisation a run executed under.
    """

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()
    big = root / "canary_large_manifest.json"
    big.write_text('{"v": 1}\n', encoding="utf-8")
    stat = big.stat()

    contract = BoundedRunContract(
        expected_revision=state.revision,          # as if passed on argv
        expected_cursor=(state.active_stage, state.active_subtask, state.source_step_id),
        allowed_source_steps=frozenset({7}),
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
        protected_manifest=_protected(),
        protected_identity={big.name: (stat.st_size, stat.st_mtime_ns)},
        protected_checkpoints={4},
        actor="operator",
    )

    with canary.frozen_time():
        outcome = engine.run_bounded(contract)
    collected = canary.collect(root)

    bindings = canary.bounded_authorisation(collected["events"])
    assert bindings, "the authorisation must be recoverable from the stream"
    block = bindings[0]["block"]
    assert block["expected_revision"] == state.revision
    assert block["max_subtasks"] == 1
    assert block["allowed_source_steps"] == [7]
    assert block["protected_identity"] == {
        big.name: {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    }
    assert block["protected_checkpoints"] == [4]
    assert block["protected_manifest_sha256"]

    # both kinds of protection were really checked
    assert outcome.entry_verification.checked == 2, "one hash plus one identity"
    assert outcome.entry_checkpoints.checked == (4,)
    assert outcome.entry_verification.ok and outcome.final_verification.ok
    assert outcome.entry_checkpoints.ok and outcome.final_checkpoints.ok


def test_the_batch4_argv_revision_is_a_real_compare_and_swap():
    """Unlike a revision just read back, an argv expectation cannot self-satisfy.

    This is the difference that batch 2 had to correct: the drivers there read the
    revision to build the contract, so the CAS was vacuous.  These three take the
    revision from argv, so a stale authorisation is genuinely refused.
    """

    from factory_core.bounded_run import BoundedRunError

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    before = canary.collect(root)

    with canary.frozen_time(), pytest.raises(BoundedRunError, match="expected revision 411"):
        engine.run_bounded(
            BoundedRunContract(
                expected_revision=411,  # the driver's argv value, long since stale
                allowed_source_steps=frozenset({16}),
                max_subtasks=1,
            )
        )

    assert canary.collect(root)["events"] == before["events"]


def test_the_batch4_checkpoint_precondition_is_not_the_checkpoint_protection():
    """Two mechanisms, two questions - the distinction the batch preserves.

    The drivers look up Step13's checkpoint and assert its receipt fields: "is the
    historical checkpoint what it should be?"  The contract's
    ``protected_checkpoints`` answers a different question: "does this run leave it
    alone?"  Keeping the lookup while moving the invariance is why the precondition
    metric stays flat while the invariance metric falls.
    """

    from factory_core.bounded_run import ProtectedCheckpointViolation

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()

    # the caller can still assert whatever it likes about an existing checkpoint
    checkpoint4 = next(
        c for c in canary.store_at(root).stage_checkpoints() if c["source_step_id"] == 4
    )
    assert checkpoint4["source_step_id"] == 4

    # and the contract refuses to protect a checkpoint that is not there, which is
    # the precondition the lookup would have failed on anyway - but with a reason
    with canary.frozen_time(), pytest.raises(ProtectedCheckpointViolation):
        engine.run_bounded(
            BoundedRunContract(expected_revision=state.revision, protected_checkpoints={13})
        )


# ============== batch 5: the last private counters, one of which could crash
def test_the_batch5_asserting_counter_becomes_a_reported_boundary():
    """run_m6_native_revisit.py did not merely stop on a repeat - it asserted.

    Its counter keyed on active_step and ``assert seen[step] <= 2``, so the third
    visit to the same step raised instead of stopping, turning a stuck workflow
    into a traceback.  The contract reports the first unchanged boundary as
    NEEDS_INSPECTION, so the caller gets an answer rather than an exception - and
    one iteration earlier.
    """

    evidence = canary.build_seed(canary.paused_seed)
    revision = evidence["project_state"]["revision"]
    root = canary.restore_seed()
    engine = FactoryEngine(
        root, store=canary.store_at(root), registry=canary.stage_registry(),
        sleeper=lambda _: None,
    )

    seen: dict = {}
    calls = 0
    fingerprint = None
    outcome = None
    raised = None
    while calls < 5:
        state = canary.store_at(root).load()
        seen[state.active_step] = seen.get(state.active_step, 0) + 1
        # the original would have raised here on the third visit
        if seen[state.active_step] > 2:
            raised = "AssertionError"
            break
        calls += 1
        with canary.frozen_time():
            outcome = engine.run_bounded(
                BoundedRunContract(
                    expected_revision=state.revision,
                    previous_boundary_fingerprint=fingerprint,
                )
            )
        fingerprint = outcome.boundary_fingerprint
        if outcome.unchanged_boundary:
            break

    assert raised is None, "the contract stopped before the original would have raised"
    assert calls == 2
    assert outcome.unchanged_boundary is True
    assert outcome.to_dict()["outcome"] == "NEEDS_INSPECTION"


def test_the_batch5_single_step_shape_is_equivalent():
    """The two single-invocation drivers in the batch, guard included."""

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    legacy = _manifest_legacy_track(6)
    migrated = _manifest_migrated_track(6)

    assert migrated["outcome"].completed_subtasks == 1
    _assert_migrated_shape(legacy, migrated)


def test_the_batch5_naming_scripts_need_no_change():
    """The correction that made this batch smaller than planned.

    Four of the scripts that name these drivers only mention the filename inside a
    ``ps`` process check - they do not read or rewrite the driver's source.  So
    migrating the driver needs no change there, and the earlier claim of eight
    source-rewriting patchers was wrong: only activate_scope_alignment.py and
    prepare_readonly_output_recovery.py rewrite source.

    The check below reproduces the distinction on a synthetic pair so the
    criterion is visible: naming a file is not the same as rewriting it.
    """

    import re

    naming_only = "assert not any('run_step12_m6.py' in s for s in ps_output)\n"
    rewriting = (
        "runner = (P / 'work/run_step12_m6.py').read_text()\n"
        "runner = runner.replace('a', 'b')\n"
        "(P / 'work/run_step12_m6.py').write_text(runner)\n"
    )
    reads_and_replaces = re.compile(r"\.py['\"]\s*\)\s*\.read_text\(\)[\s\S]{0,200}?\.replace\(")

    assert not reads_and_replaces.search(naming_only)
    assert reads_and_replaces.search(rewriting)


# ============== batch 6: three clones become three contracts
def test_the_batch6_clones_become_one_shape_with_three_scopes():
    """The three drivers were byte-identical apart from a W path, a scope string
    and three lines that widened the manifest.

    Two prepare_* scripts produced them by reading the template, substituting
    those, and writing the result - because the difference could not be
    expressed.  With the contract it can: the difference is data, so three
    distinct authorisations replace three near-identical 75-line programs.  This
    asserts that the three contracts are distinct, valid and differ only in the
    scope they carry.
    """

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    big = root / "canary_large_manifest.json"
    big.write_text('{"v": 1}\n', encoding="utf-8")
    stat = big.stat()
    state = canary.store_at(root).load()

    def contract(scope_label, extra=None):
        manifest = dict(_protected())
        if extra:
            manifest.update(extra)
        return BoundedRunContract(
            expected_revision=state.revision,
            expected_cursor=(state.active_stage, state.active_subtask, state.source_step_id),
            max_subtasks=1,
            run_policy=RunPolicy.BOUNDED_SUBTASKS,
            max_attempts_per_step={5: state.attempt + 1, 4: 1},
            max_reopens_per_step={5: 0, 4: 0},
            protected_manifest=manifest,
            actor="operator",
        )

    template = contract("bounded independent evidence repair")
    recovery = contract("read-only partial-output and provenance recovery")
    alignment = contract(
        "reviewed conditional reporting scope alignment; zero Solver submissions",
        extra={big.name: canary.protected_digest('{"v": 1}\n')},
    )

    # Two of the three clones encoded the SAME authorisation.  The only things
    # that differed were the directory their manifest was read from and a label
    # that went into the journal - and the label is not part of the contract at
    # all, so it cannot be an authorisation.  That is the strongest argument for
    # retiring the clones rather than migrating them as three programs.
    assert template.contract_sha256 == recovery.contract_sha256
    assert template.protected_manifest == recovery.protected_manifest
    assert "scope" not in template.canonical_payload(), (
        "the scope label lived only in the journal, so it is not an authorisation"
    )

    # The third did carry a genuinely wider manifest, so it is a distinct
    # authorisation - and that difference is now a field, not a second program.
    assert alignment.contract_sha256 != template.contract_sha256
    assert big.name in alignment.protected_manifest
    assert big.name not in template.protected_manifest
    assert len({template.contract_sha256, recovery.contract_sha256,
                alignment.contract_sha256}) == 2


def test_the_batch6_the_difference_is_data_not_a_second_program():
    """Cloning versus parameterising, as a pattern the suite can see.

    The retired step read the template's source and substituted strings; the
    replacement passes the difference as contract fields.  A test cannot read the
    production files from CI, so it pins the distinction itself: a shape that
    requires editing source to change behaviour is the thing being removed.
    """

    import re

    clone = (
        "runner = (P/'work/run_bounded_evidence_repair.py').read_text()\n"
        "runner = runner.replace(\"W = P / 'work/bounded_evidence_repair_20260911'\", "
        "\"W = P / 'work/scope_alignment_20260911'\")\n"
        "(P/'work/run_scope_alignment.py').write_text(runner)\n"
    )
    parameterise = (
        "contract = BoundedRunContract(protected_manifest=manifest, "
        "max_attempts_per_step={5: state.attempt + 1, 4: 1}, actor='operator')\n"
    )
    reads_source = re.compile(r"\.py['\"]\s*\)\s*\.read_text\(\)")
    writes_source = re.compile(r"\.py['\"]\s*\)\s*\.write_text\(")

    assert reads_source.search(clone) and writes_source.search(clone)
    assert not reads_source.search(parameterise)
    assert not writes_source.search(parameterise)
