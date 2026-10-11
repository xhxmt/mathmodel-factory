"""Gate 2: the bounded contract's failure semantics.

Gate 1 asked whether the bounded entry *agrees* with the legacy one on the happy
path.  Gate 2 asks the complementary question: when the bounded entry refuses,
or when it must stop rather than commit, is the refusal the one the contract
promised?

Each assertion here is a promise the contract makes, so a regression in any of
them turns an explicit authorisation into a guess:

* G2.1 a stale authorisation is refused before any business write
* G2.2 a matching revision with a different position is still refused
* G2.3 an already-violated protected manifest is refused at entry
* G2.4 a manifest broken *during* the run blocks the checkpoint commit, and the
  engine's internal protection and the caller's structured result agree
* G2.5 a repeated boundary with no progress is reported, not raised, and not
  looped over
* G2.6 the contract and the low-level kwargs may not disagree
* G2.7 a live runner is not silently taken over
* G2.8 an unsafe manifest path is refused, and an escaping symlink is reported
  unsafe rather than compared

The scenarios reuse the Gate 1 harness for the same reason Gate 1 uses it: the
"nothing was written" assertions are only meaningful against a project whose
exact prior state is known and restorable.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import _g1_canary as canary
from factory_core.bounded_run import (
    BoundedRunContract,
    BoundedRunError,
    ProtectedManifestViolation,
    RunPolicy,
    classify_stop_reason,
    verify_protected_manifest,
)
from factory_core.domain import RunnerBusy, WorkflowStatus
from factory_core.engine import FactoryEngine


def _engine(root: Path, *, registry=None):
    return FactoryEngine(
        root,
        store=canary.store_at(root),
        registry=registry if registry is not None else canary.stage_registry(),
    )


def _bounded(root: Path, *, expected_revision: int, **kwargs):
    engine = _engine(root)
    contract = BoundedRunContract(expected_revision=expected_revision, **kwargs)
    with canary.frozen_time():
        return engine.run_bounded(contract)


# ============================================ G2.8 manifest safety (pure)
@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("/etc/passwd", id="posix-absolute"),
        pytest.param("\\\\server\\share\\x", id="windows-unc"),
        pytest.param("C:\\\\Windows\\\\x", id="windows-drive"),
        pytest.param("../escape.txt", id="parent-traversal"),
        pytest.param("a/../../escape.txt", id="nested-traversal"),
        pytest.param("./relative.txt", id="leading-dot-slash"),
        pytest.param("a/./b.txt", id="interior-dot"),
        pytest.param("", id="empty"),
    ],
)
def test_an_unsafe_manifest_path_is_refused_at_construction(relative):
    """A manifest that escapes the project protects nothing the caller named."""

    with pytest.raises(BoundedRunError):
        BoundedRunContract(
            expected_revision=1,
            protected_manifest={relative: "a" * 64},
        )


@pytest.mark.parametrize(
    "digest",
    [
        pytest.param("A" * 64, id="uppercase"),
        pytest.param("a" * 63, id="too-short"),
        pytest.param("a" * 65, id="too-long"),
        pytest.param("g" * 64, id="non-hex"),
        pytest.param("", id="empty"),
    ],
)
def test_a_non_lowercase_sha256_digest_is_refused(digest):
    with pytest.raises(BoundedRunError):
        BoundedRunContract(
            expected_revision=1,
            protected_manifest={"ok.txt": digest},
        )


def test_an_escaping_symlink_is_reported_unsafe_rather_than_compared(tmp_path):
    """Treating an unreadable escaped path as "unchanged" is the worst answer."""

    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("not ours\\n", encoding="utf-8")

    # a direct symlink
    (project / "link.txt").symlink_to(outside)
    # and a parent component that is a symlink resolving elsewhere
    (project / "sub" / "escaped").symlink_to(tmp_path, target_is_directory=True)

    verification = verify_protected_manifest(
        project,
        {
            "link.txt": canary.protected_digest("not ours\\n"),
            "sub/escaped/outside.txt": canary.protected_digest("not ours\\n"),
        },
    )

    assert verification.ok is False
    assert set(verification.unsafe) == {"link.txt", "sub/escaped/outside.txt"}
    assert verification.changed == ()


# ===================================================== G2.1 stale revision
def test_a_stale_authorisation_is_refused_before_any_write():
    """No business event, and no revision move.

    Note this is deliberately *not* "no SQLite bytes written": on a physical-v9
    database the store's own read path migrates ``schema_info`` first.  What the
    contract promises is that the authorisation is not acted on.
    """

    evidence = canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    before = canary.collect(root)

    with pytest.raises(BoundedRunError, match="expected revision"):
        _bounded(root, expected_revision=evidence["project_state"]["revision"] + 1)

    after = canary.collect(root)
    assert after["events"] == before["events"], "a refused run must not write events"
    assert after["project_state"]["revision"] == before["project_state"]["revision"]
    assert after["state_hashes"] == before["state_hashes"]


# ====================================================== G2.2 cursor mismatch
def test_a_matching_revision_with_the_wrong_position_is_refused():
    """The revision is right; the cursor is not.  The message must say which."""

    evidence = canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    before = canary.collect(root)

    actual = (
        evidence["project_state"]["active_stage"],
        evidence["project_state"]["active_subtask"],
        evidence["project_state"]["source_step_id"],
    )
    wrong = (actual[0] or 0) + 5, "not-a-real-subtask", (actual[2] or 0) + 5

    with pytest.raises(BoundedRunError) as raised:
        _bounded(
            root,
            expected_revision=evidence["project_state"]["revision"],
            expected_cursor=wrong,
        )

    message = str(raised.value)
    assert "cursor mismatch" in message
    assert "expected revision" not in message, "must be distinguishable from a stale CAS"

    after = canary.collect(root)
    assert after["events"] == before["events"]


# ================================================ G2.3 manifest already broken
def test_an_already_violated_manifest_is_refused_at_entry():
    evidence = canary.build_seed(canary.stage_seed_with_protected_file)
    root = canary.restore_seed()

    # break the file before the run, so entry verification is what refuses
    (root / canary.PROTECTED_FILE).write_text("tampered before the run\\n", encoding="utf-8")
    before = canary.collect(root)

    with pytest.raises(ProtectedManifestViolation, match="already violated at entry"):
        _bounded(
            root,
            expected_revision=evidence["project_state"]["revision"],
            max_subtasks=1,
            run_policy=RunPolicy.BOUNDED_SUBTASKS,
            protected_manifest={
                canary.PROTECTED_FILE: canary.protected_digest(canary.PROTECTED_CONTENT)
            },
        )

    after = canary.collect(root)
    assert after["events"] == before["events"], "refusal at entry must not write"


# ============================================= G2.4 manifest broken during run
def test_a_manifest_broken_during_the_run_blocks_the_checkpoint():
    """The strongest protection assertion: engine and caller must agree.

    The manifest is clean at entry, so the run proceeds and the Step succeeds.
    The dispatcher then breaks the protected file *while the Step is executing*,
    which is exactly the window a post-hoc caller-side check cannot cover.
    """

    evidence = canary.build_seed(canary.stage_seed_with_protected_file)
    root = canary.restore_seed()
    digest = canary.protected_digest(canary.PROTECTED_CONTENT)

    def break_it(_request):
        (root / canary.PROTECTED_FILE).write_text(
            "tampered during the run\\n", encoding="utf-8"
        )

    engine = _engine(root, registry=canary.stage_registry(
        dispatcher=canary.HermeticDispatcher(on_execute=break_it)
    ))
    contract = BoundedRunContract(
        expected_revision=evidence["project_state"]["revision"],
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
        protected_manifest={canary.PROTECTED_FILE: digest},
    )

    with canary.frozen_time():
        outcome = engine.run_bounded(contract)

    # 1. the engine's own protection fired, and named itself
    failures = [
        event
        for event in canary.collect(root)["events"]
        if event["type"] == "STEP_FAILED"
    ]
    assert failures, "the blocked checkpoint must be recorded as a failure"
    error_classes = [event["payload"].get("error_class") for event in failures]
    assert "PERMANENT_PROTECTED_MANIFEST_VIOLATED" in error_classes

    # 2. the caller's structured result says the same thing
    assert outcome.stop_reason == "PROTECTED_MANIFEST_VIOLATED"
    assert outcome.entry_verification.ok is True, "entry was clean; the run broke it"
    assert outcome.final_verification.ok is False
    assert canary.PROTECTED_FILE in outcome.final_verification.changed

    # 3. no success checkpoint was committed for the blocked subtask
    after = canary.collect(root)
    assert after["tables"]["stage_checkpoints"] == []
    assert after["tables"]["stage_checkpoint_history"] == []

    # and the cursor did not move past the subtask whose checkpoint was blocked.
    # (made_progress is not the right field here: it tracks whether the revision
    # moved, and the failure events legitimately move it.)
    assert (
        after["project_state"]["last_completed_step"]
        == evidence["project_state"]["last_completed_step"]
    )
    assert after["project_state"]["status"] == WorkflowStatus.FAILED.value


# ================================================= G2.5 repeated boundary
def test_a_repeated_boundary_is_reported_and_needs_inspection():
    """Reported, not raised - and not turned into a hand-written loop.

    ``stop_reason`` is asserted to be the underlying boundary reason, because the
    boundary verdict is carried by ``unchanged_boundary``/``outcome`` and not by
    ``stop_reason``; requiring a particular ``stop_reason`` here would be
    asserting the wrong field.
    """

    evidence = canary.build_seed(canary.paused_seed)
    revision = evidence["project_state"]["revision"]
    root = canary.restore_seed()

    first = _bounded(root, expected_revision=revision)
    fingerprint = first.boundary_fingerprint

    second = _bounded(
        root,
        expected_revision=revision,
        previous_boundary_fingerprint=fingerprint,
    )

    assert second.boundary_fingerprint == fingerprint
    assert second.made_progress is False
    assert second.unchanged_boundary is True
    assert second.needs_inspection is True
    assert second.to_dict()["outcome"] == "NEEDS_INSPECTION"
    # the underlying boundary reason is preserved rather than overwritten
    assert second.stop_reason == "BOUNDARY_OR_SCOPE"


def test_unchanged_takes_precedence_over_no_further_work():
    """The pure classifier rule, isolated from any project.

    A 'ready' project that did not advance used to report NO_FURTHER_WORK, which
    would hide a repeated boundary behind a benign-looking reason.
    """

    def state(status):
        return SimpleNamespace(status=status)

    assert (
        classify_stop_reason(state("ready"), previous_status="ready", completed=0, bounded=None)
        == "UNCHANGED"
    )
    # the same position, but the run did advance: this is not a repeat
    assert (
        classify_stop_reason(state("ready"), previous_status="ready", completed=1, bounded=None)
        == "NO_FURTHER_WORK"
    )
    assert (
        classify_stop_reason(state("ready"), previous_status="running", completed=0, bounded=None)
        == "NO_FURTHER_WORK"
    )


# ============================================ G2.6 contract / kwarg disagreement
def test_max_steps_may_not_disagree_with_the_contract():
    evidence = canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    engine = _engine(root)
    contract = BoundedRunContract(
        expected_revision=evidence["project_state"]["revision"], max_subtasks=1
    )

    with pytest.raises(BoundedRunError, match="max_steps disagrees"):
        with canary.frozen_time():
            engine.run(max_steps=2, contract=contract)


def test_allowed_source_steps_may_not_disagree_with_the_contract():
    evidence = canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    engine = _engine(root)
    contract = BoundedRunContract(
        expected_revision=evidence["project_state"]["revision"],
        allowed_source_steps=frozenset({2}),
    )

    with pytest.raises(BoundedRunError, match="allowed_source_steps disagrees"):
        with canary.frozen_time():
            engine.run(allowed_source_steps=frozenset({1}), contract=contract)


# ===================================================== G2.7 a live runner
def test_a_live_foreign_runner_is_not_taken_over():
    """A real same-user child process, because liveness is permission-scoped.

    PID 1 is live but not usable here: ``_pid_is_live`` is ``os.kill(pid, 0)``
    with ``except OSError: return False``, and that call raises ``PermissionError``
    for a process owned by another user - so PID 1 reads as *dead* and the engine
    would correctly treat the record as an interrupted runner rather than a live
    one.  A child of this process is live and ours, which is the case that must
    actually raise.
    """

    evidence = canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    assert evidence["project_state"]["revision"] >= 1

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    try:
        store = canary.store_at(root)
        state = store.load()
        store.transition(
            expected_revision=state.revision,
            event_type="RUNNER_ATTACHED_FOR_TEST",
            changes={"runner_pid": child.pid, "runner_lease_id": "foreign-lease"},
        )

        before = canary.collect(root)
        with pytest.raises(RunnerBusy):
            _bounded(root, expected_revision=before["project_state"]["revision"])

        # read the raw row: collect() normalises runner_pid/lease_id on purpose,
        # so it can prove nothing changed but cannot report what the values are
        raw = canary.store_at(root).load()
        assert raw.runner_pid == child.pid, "the foreign runner record is untouched"
        assert raw.runner_lease_id == "foreign-lease"

        after = canary.collect(root)
        assert after["events"] == before["events"], "a refused takeover must not write"
        assert after["project_state"]["revision"] == before["project_state"]["revision"]
    finally:
        child.terminate()
        child.wait(timeout=10)


# ============================ contract ceilings (the Gate 4 enabler)
def test_step_ceilings_are_validated_at_construction():
    """A ceiling is an authorisation, so a malformed one must not be accepted."""

    for kwargs in (
        {"max_attempts_per_step": {"5": 1}},
        {"max_attempts_per_step": {5: 0}},
        {"max_attempts_per_step": {5: -1}},
        {"max_attempts_per_step": {-1: 1}},
        {"max_attempts_per_step": {5: True}},
        {"max_reopens_per_step": {5: -1}},
        {"max_reopens_per_step": {"x": 1}},
    ):
        with pytest.raises(BoundedRunError):
            BoundedRunContract(expected_revision=1, **kwargs)


def test_an_empty_ceiling_is_absent_so_the_identity_is_stable():
    empty = BoundedRunContract(expected_revision=1, max_attempts_per_step={})
    absent = BoundedRunContract(expected_revision=1)
    assert empty.max_attempts_per_step is None
    assert empty.contract_sha256 == absent.contract_sha256


def test_the_ceilings_are_part_of_the_contract_identity():
    """Two authorisations that differ in scope must not share a run id."""

    base = BoundedRunContract(expected_revision=1)
    narrowed = BoundedRunContract(expected_revision=1, max_attempts_per_step={5: 1})
    reopened = BoundedRunContract(expected_revision=1, max_reopens_per_step={5: 0})

    assert base.contract_sha256 != narrowed.contract_sha256
    assert base.contract_sha256 != reopened.contract_sha256

    payload = narrowed.event_payload()
    assert payload["max_attempts_per_step"] == {"5": 1}
    assert payload["max_reopens_per_step"] is None


def _definitions():
    from factory_core.registry import StepDefinition

    return {
        step: StepDefinition(
            id=step, name=f"step{step}", timeout_seconds=30, max_attempts=5,
            max_reopens=2, step=type("L", (), {
                "prepare": lambda self, c: None,
                "execute": lambda self, c: None,
                "validate": lambda self, c: None,
                "recover": lambda self, c, e: None,
            })(),
        )
        for step in (4, 5, 6)
    }


def test_the_scoped_registry_tightens_and_never_widens():
    """Strictest-wins: a contract cannot grant what the catalogue did not."""

    from factory_core.registry import StepRegistry
    from factory_core.bounded_run import ScopedRegistry

    registry = StepRegistry()
    for definition in _definitions().values():
        registry.register(definition)

    scoped = ScopedRegistry(
        registry,
        BoundedRunContract(
            expected_revision=1,
            # 5 is tightened, 4 is asked to *loosen* (99 > 5), 6 is untouched
            max_attempts_per_step={5: 1, 4: 99},
            max_reopens_per_step={5: 0},
        ),
    )

    assert scoped.get(5).max_attempts == 1 and scoped.get(5).max_reopens == 0
    assert scoped.get(4).max_attempts == 5, "a looser ceiling must be ignored"
    assert scoped.get(4).max_reopens == 2
    assert scoped.get(6).max_attempts == 5 and scoped.get(6).max_reopens == 2

    # every resolution path is scoped, not just get()
    assert scoped.next_after(4).max_attempts == 1
    assert {d.id: d.max_attempts for d in scoped} == {4: 5, 5: 1, 6: 5}


def test_the_scoped_registry_delegates_what_it_does_not_scope():
    from factory_core.registry import StepRegistry
    from factory_core.bounded_run import ScopedRegistry

    registry = StepRegistry()
    for definition in _definitions().values():
        registry.register(definition)

    scoped = ScopedRegistry(registry, BoundedRunContract(expected_revision=1))
    # nothing scoped, so definitions pass through unchanged
    assert scoped.get(5) is registry.get(5)
    assert list(scoped) == list(registry)


def test_the_engine_restores_its_registry_after_a_scoped_run():
    """The swap must not leak, exactly like the contract reference does not."""

    evidence = canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    engine = _engine(root)
    original = engine.registry

    contract = BoundedRunContract(
        expected_revision=evidence["project_state"]["revision"],
        max_subtasks=1,
        max_attempts_per_step={5: 1},
    )
    with canary.frozen_time():
        engine.run_bounded(contract)

    assert engine.registry is original, "the scoped registry leaked past the run"


def test_the_engine_does_not_swap_the_registry_without_a_ceiling():
    """An unscoped bounded run must be exactly what it was."""

    evidence = canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    engine = _engine(root)
    original = engine.registry

    contract = BoundedRunContract(
        expected_revision=evidence["project_state"]["revision"], max_subtasks=1
    )
    with canary.frozen_time():
        engine.run_bounded(contract)

    assert engine.registry is original


# ==================== the ceilings actually reach the engine's retry decision
class _TransientFailure:
    """Fails transiently, so ``max_attempts`` is what decides the outcome."""

    def __init__(self):
        self.calls = 0

    def execute(self, _context):
        from factory_core.domain import ExecutionResult

        self.calls += 1
        return ExecutionResult.failed("TRANSIENT_TEST_FAILURE")


class _Accept:
    def validate(self, _context):
        from factory_core.domain import ValidationResult

        return ValidationResult.valid()


def _retrying_engine(root, handler, registry_max_attempts=3):
    from factory_core.engine import FactoryEngine
    from factory_core.registry import StepDefinition, StepRegistry

    registry = StepRegistry()
    for step_id in (1, 2):
        registry.register(
            StepDefinition(
                id=step_id,
                name=f"step{step_id}",
                timeout_seconds=30,
                max_attempts=registry_max_attempts,
                handler=handler,
                validator=_Accept(),
            )
        )
    return FactoryEngine(root, store=canary.store_at(root), registry=registry, sleeper=lambda _: None)


def _run_with_ceiling(ceiling):
    evidence = canary.build_seed(
        lambda root: canary.store_at(root).initialize(
            project_id="g1-canary", project_type="modeling"
        )
    )
    root = canary.restore_seed()
    handler = _TransientFailure()
    engine = _retrying_engine(root, handler)

    contract = BoundedRunContract(
        expected_revision=evidence["project_state"]["revision"],
        max_subtasks=1,
        run_policy=RunPolicy.BOUNDED_SUBTASKS,
        max_attempts_per_step=ceiling,
    )
    with canary.frozen_time():
        engine.run_bounded(contract)
    return handler.calls, [event["type"] for event in canary.collect(root)["events"]]


def test_a_ceiling_reaches_the_engines_retry_decision():
    """The behavioural proof that this is not just a payload field.

    Without a ceiling the registry's budget governs and the run is retried; with
    a ceiling of one attempt the retry never happens.  Asserting merely that the
    field round-trips into RUN_STARTED would not show that.
    """

    calls, events = _run_with_ceiling(None)
    assert calls == 3, "the registry's max_attempts governs when there is no ceiling"
    assert "RETRY_SCHEDULED" in events

    calls, events = _run_with_ceiling({1: 1})
    assert calls == 1, "the ceiling cut the retries off after one attempt"
    assert "RETRY_SCHEDULED" not in events


def test_a_ceiling_looser_than_the_registry_changes_nothing():
    """Strictest-wins, asserted through behaviour rather than through the field.

    A contract must not be able to buy extra attempts by naming a larger number
    than the Step catalogue allows.
    """

    calls, events = _run_with_ceiling({1: 99})
    assert calls == 3, "the registry's cap still governs"
    assert "RETRY_SCHEDULED" in events



# ============ the arming must precede recovery, because recovery commits too
def test_the_contract_is_armed_before_recovery_runs():
    """All three protections have to be installed before ``recover()``.

    ``recover()`` is not a read-only prelude.  On its COMPLETE disposition it
    calls ``_complete_stage_task`` - the function holding both pre-commit checks -
    and it resolves a Step through the registry to decide whether a reopen is
    allowed, which reads ``max_reopens``.  The contract, the checkpoint snapshot
    and the scoped registry used to be installed only around the advance loop, so
    all three were invisible to recovery:

      * a recovered commit skipped the manifest and checkpoint checks entirely,
        leaving ``run_bounded``'s final verification to report the violation
        after the commit had landed;
      * recovery's reopen decision read the untightened Step ceilings, so a
        contract that set ``max_reopens`` to zero did not bind it.

    The state is asserted at the moment ``recover()`` is entered, because that is
    the ordering under test.  Whether a particular recovery then commits depends
    on the Step's own receipt state, which would mask the ordering.
    """

    canary.build_seed(
        lambda root: canary.stage_seed_ready_for_step_with_protected_file(root, 6)
    )
    root = canary.restore_seed()
    assert canary.store_at(root).stage_checkpoints(), (
        "the seed must commit checkpoints for the arming to be observable"
    )

    seen: dict = {}
    original_recover = FactoryEngine.recover

    def spy(self, **kwargs):
        seen["contract"] = self._bounded_contract
        seen["checkpoints"] = dict(self._protected_checkpoints)
        seen["registry"] = type(self.registry).__name__
        return original_recover(self, **kwargs)

    FactoryEngine.recover = spy
    try:
        # leave a Step selected and uncommitted, so the next run recovers
        def fail_the_step(request):
            raise RuntimeError("deliberate step failure")

        first = FactoryEngine(
            root, store=canary.store_at(root),
            registry=canary.stage_registry(
                dispatcher=canary.HermeticDispatcher(on_execute=fail_the_step)
            ),
            sleeper=lambda _: None,
        )
        state = canary.store_at(root).load()
        with pytest.raises(RuntimeError):
            with canary.frozen_time():
                first.run_bounded(
                    BoundedRunContract(
                        expected_revision=state.revision,
                        max_subtasks=1,
                        run_policy=RunPolicy.BOUNDED_SUBTASKS,
                    )
                )

        interrupted = canary.store_at(root).load()
        assert interrupted.active_step is not None, "recovery needs a selected Step"

        second = FactoryEngine(
            root, store=canary.store_at(root), registry=canary.stage_registry(),
            sleeper=lambda _: None,
        )
        second.run_bounded(
            BoundedRunContract(
                expected_revision=interrupted.revision,
                max_subtasks=1,
                run_policy=RunPolicy.BOUNDED_SUBTASKS,
                max_reopens_per_step={0: 0},
                protected_checkpoints={4},
            )
        )
    finally:
        FactoryEngine.recover = original_recover

    assert seen, "recovery must have run for this test to mean anything"
    assert seen["contract"] is not None, (
        "the contract was not armed when recovery ran, so a recovered commit "
        "would skip the pre-commit protection checks"
    )
    assert seen["checkpoints"], (
        "the protected-checkpoint snapshot was not armed when recovery ran"
    )
    assert seen["registry"] == "ScopedRegistry", (
        "recovery resolved its Step through the untightened registry"
    )


def test_recovery_cannot_commit_a_step_the_contract_protects():
    """No commit lands when the contract's manifest is violated during recovery.

    Weaker than the ordering test above, and deliberately so: recovery also has to
    satisfy its own prompt-input receipt before the manifest is consulted, so this
    asserts what is observable - the recovery hook really rewrote the protected
    file, no ``STEP_SUCCEEDED`` was written, and the run reports the violation
    rather than advancing on top of it.
    """

    import dataclasses

    from factory_core.domain import RecoveryDecision, RecoveryDisposition

    canary.build_seed(canary.stage_seed)
    root = canary.restore_seed()
    target = root / canary.PROTECTED_FILE
    target.write_text(canary.PROTECTED_CONTENT, encoding="utf-8")

    class RecoveryRewritesProtectedFile:
        def __init__(self, inner, path):
            self._inner = inner
            self._path = path

        def recover(self, context, error):
            self._path.write_text("rewritten by recovery\n", encoding="utf-8")
            return RecoveryDecision(
                disposition=RecoveryDisposition.COMPLETE,
                reason="hermetic recovery that rewrites a protected file",
                completed_through_step=context.step_id,
            )

        def __getattr__(self, name):
            return getattr(self._inner, name)

    registry = canary.stage_registry()
    definition = registry.get(0)
    wrapped = dataclasses.replace(
        definition, step=RecoveryRewritesProtectedFile(definition.step, target)
    )
    registry._steps[0] = wrapped
    for key, value in list(registry._stage_subtasks.items()):
        if value.id == 0:
            registry._stage_subtasks[key] = wrapped

    def fail_the_step(request):
        raise RuntimeError("deliberate step failure")

    first = FactoryEngine(
        root, store=canary.store_at(root),
        registry=canary.stage_registry(
            dispatcher=canary.HermeticDispatcher(on_execute=fail_the_step)
        ),
        sleeper=lambda _: None,
    )
    state = canary.store_at(root).load()
    with pytest.raises(RuntimeError):
        with canary.frozen_time():
            first.run_bounded(
                BoundedRunContract(
                    expected_revision=state.revision,
                    max_subtasks=1,
                    run_policy=RunPolicy.BOUNDED_SUBTASKS,
                )
            )

    interrupted = canary.store_at(root).load()
    second = FactoryEngine(
        root, store=canary.store_at(root), registry=registry, sleeper=lambda _: None
    )
    before = canary.collect(root)
    outcome = second.run_bounded(
        BoundedRunContract(
            expected_revision=interrupted.revision,
            max_subtasks=1,
            run_policy=RunPolicy.BOUNDED_SUBTASKS,
            protected_manifest={
                canary.PROTECTED_FILE: canary.protected_digest(canary.PROTECTED_CONTENT)
            },
        )
    )
    new_events = canary.collect(root)["events"][len(before["events"]):]

    assert target.read_text(encoding="utf-8") == "rewritten by recovery\n", (
        "the recovery hook must have run for this test to mean anything"
    )
    assert "STEP_SUCCEEDED" not in [event["type"] for event in new_events], (
        "a step committed while the contract's protected file was violated"
    )
    assert outcome.final_verification.ok is False
    assert outcome.stop_reason == "PROTECTED_MANIFEST_VIOLATED"
