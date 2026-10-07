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
