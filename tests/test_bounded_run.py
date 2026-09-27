"""S6: the bounded-advance execution contract.

The contract exists because A's ``work/`` grew 128 hand-written driver scripts -
24 of them calling ``engine.run(max_steps=1)`` - each re-implementing the
invariants the engine should have guaranteed: an expected revision, an expected
cursor, a protected-file hash check, a progress journal, and a repeated-boundary
guard.  This suite asserts the supported replacement for each of those.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from factory_core.bounded_run import (
    BOUNDED_RUN_SCHEMA,
    BoundedRunContract,
    BoundedRunError,
    ProtectedManifestViolation,
    RunPolicy,
    boundary_fingerprint,
    check_cursor,
    classify_stop_reason,
    verify_protected_manifest,
)
from factory_core.domain import ExecutionResult, ValidationResult
from factory_core.engine import FactoryEngine
from factory_core.registry import StepDefinition, StepRegistry
from factory_core.stages import STAGE_SCHEDULER_GENERATION
from factory_core.storage import SQLiteStateStore


class _Handler:
    """A step that succeeds, so the loop actually commits a checkpoint.

    Real handlers need prompt templates from the code root; the established
    pattern for exercising the loop end to end is a fake registry
    (tests/test_factory_engine.py), and it is used here for the same reason.
    """

    def __init__(self):
        self.calls: list[int] = []

    def execute(self, context):
        self.calls.append(context.attempt)
        return ExecutionResult.succeeded()


class _Validator:
    def validate(self, context):
        return ValidationResult.valid()


def _project(tmp_path) -> Path:
    """A project plus an engine whose registry can actually run a step."""

    root = tmp_path / "s6"
    root.mkdir()
    store = SQLiteStateStore(root)
    store.initialize(project_id="s6", project_type="modeling")
    return root


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _engine(root: Path, *, handler=None, validator=None, max_attempts=3) -> FactoryEngine:
    """An engine with a fake registry that can complete a step."""

    registry = StepRegistry()
    for step_id in (1, 2):
        registry.register(
            StepDefinition(
                id=step_id,
                name=f"step{step_id}",
                timeout_seconds=30,
                max_attempts=max_attempts,
                handler=handler or _Handler(),
                validator=validator or _Validator(),
            )
        )
    return FactoryEngine(root, registry=registry, sleeper=lambda _: None)


# =========================================================== contract validation
def test_contract_pins_the_authorising_content_only():
    contract = BoundedRunContract(
        expected_revision=301,
        expected_cursor=(8, "revision", 12),
        allowed_source_steps=frozenset({16}),
        max_subtasks=1,
        protected_manifest={"paper/main.tex": "a" * 64},
        actor="operator",
    )
    payload = contract.canonical_payload()
    assert payload["schema"] == BOUNDED_RUN_SCHEMA
    assert payload["expected_revision"] == 301
    assert payload["expected_cursor"] == [8, "revision", 12]
    assert payload["allowed_source_steps"] == [16]
    assert payload["protected_manifest"] == {"paper/main.tex": "a" * 64}
    # the caller's bookkeeping is NOT part of what was authorised
    assert "previous_boundary_fingerprint" not in payload


def test_contract_identity_is_stable_and_content_addressed():
    a = BoundedRunContract(expected_revision=5, max_subtasks=2, actor="operator")
    b = BoundedRunContract(expected_revision=5, max_subtasks=2, actor="operator")
    c = BoundedRunContract(expected_revision=5, max_subtasks=3, actor="operator")
    assert a.contract_sha256 == b.contract_sha256, "same authorisation, same identity"
    assert a.contract_sha256 != c.contract_sha256, "a different bound is a different authorisation"
    assert a.run_id == a.contract_sha256[:32]


def test_protected_manifest_rejects_escaping_and_unsafe_paths():
    for bad in ("/etc/passwd", "C:/windows/system32", "../outside.txt", "a/../../b.txt", "", "./x.txt"):
        with pytest.raises(BoundedRunError):
            BoundedRunContract(expected_revision=1, protected_manifest={bad: "a" * 64})


def test_protected_manifest_requires_a_lowercase_sha256():
    with pytest.raises(BoundedRunError, match="lowercase sha256"):
        BoundedRunContract(expected_revision=1, protected_manifest={"x": "NOTAHASH"})
    with pytest.raises(BoundedRunError, match="lowercase sha256"):
        BoundedRunContract(expected_revision=1, protected_manifest={"x": "A" * 64})


def test_bounded_subtasks_policy_requires_an_explicit_bound():
    with pytest.raises(BoundedRunError, match="requires an explicit max_subtasks"):
        BoundedRunContract(
            expected_revision=1, run_policy=RunPolicy.BOUNDED_SUBTASKS
        )
    BoundedRunContract(
        expected_revision=1, run_policy=RunPolicy.BOUNDED_SUBTASKS, max_subtasks=3
    )


def test_unknown_run_policy_and_bad_revision_are_refused():
    with pytest.raises(BoundedRunError, match="unknown run_policy"):
        BoundedRunContract(expected_revision=1, run_policy="anything_goes")
    with pytest.raises(BoundedRunError, match="expected_revision"):
        BoundedRunContract(expected_revision=-1)
    with pytest.raises(BoundedRunError, match="max_subtasks"):
        BoundedRunContract(expected_revision=1, max_subtasks=0)


# ====================================================== protected manifest checks
def test_verification_detects_missing_changed_and_unsafe(tmp_path):
    root = _project(tmp_path)
    good = root / "keep.txt"
    good.write_text("original\n", encoding="utf-8")
    manifest = {"keep.txt": _sha(good)}

    assert verify_protected_manifest(root, manifest).ok

    good.write_text("tampered\n", encoding="utf-8")
    changed = verify_protected_manifest(root, manifest)
    assert changed.ok is False and changed.changed == ("keep.txt",)

    (root / "symlink.txt").symlink_to(good)
    unsafe = verify_protected_manifest(root, {"symlink.txt": "a" * 64})
    assert unsafe.ok is False and unsafe.unsafe == ("symlink.txt",)

    missing = verify_protected_manifest(root, {"absent.txt": "a" * 64})
    assert missing.ok is False and missing.missing == ("absent.txt",)


def test_a_parent_symlink_resolving_outside_the_project_is_unsafe(tmp_path):
    """A path that stays inside by spelling but escapes by resolution."""

    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret\n", encoding="utf-8")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)

    result = verify_protected_manifest(root, {"linkdir/secret.txt": _sha(target)})
    assert result.ok is False
    assert result.unsafe == ("linkdir/secret.txt",), (
        "an escaping path must never be reported as unchanged"
    )


def test_verification_reports_every_category_at_once(tmp_path):
    root = _project(tmp_path)
    kept = root / "kept.txt"
    kept.write_text("x\n", encoding="utf-8")
    manifest = {
        "kept.txt": _sha(kept),
        "gone.txt": "a" * 64,
        "link.txt": "b" * 64,
    }
    (root / "link.txt").symlink_to(kept)
    result = verify_protected_manifest(root, manifest)
    assert result.checked == 3
    assert result.missing == ("gone.txt",)
    assert result.unsafe == ("link.txt",)
    assert result.ok is False


# ================================================================= CAS and cursor
def test_stale_revision_is_refused_before_anything_is_written(tmp_path):
    root = _project(tmp_path)
    store = SQLiteStateStore(root)
    state = store.load()
    before = store.status_snapshot()

    engine = _engine(root)
    contract = BoundedRunContract(expected_revision=state.revision + 99)
    with pytest.raises(BoundedRunError, match="expected revision"):
        engine.run(contract=contract)

    after = store.status_snapshot()
    assert after["state"].revision == before["state"].revision
    assert len(after["events"]) == len(before["events"]), "a refused CAS writes nothing"


def test_cursor_mismatch_is_refused_even_when_the_revision_matches(tmp_path):
    """The case a revision-only check would let through."""

    root = _project(tmp_path)
    state = SQLiteStateStore(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision,
        expected_cursor=(99, "not_the_current_subtask", None),
    )
    with pytest.raises(BoundedRunError, match="cursor mismatch"):
        _engine(root).run(contract=contract)


def test_check_cursor_accepts_a_matching_position():
    class _State:
        active_stage = 8
        active_subtask = "revision"
        source_step_id = 12

    check_cursor(_State(), (8, "revision", 12))
    check_cursor(_State(), None)
    with pytest.raises(BoundedRunError):
        check_cursor(_State(), (8, "revision", 11))


def test_contract_bound_kwargs_must_not_disagree(tmp_path):
    root = _project(tmp_path)
    state = SQLiteStateStore(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision, max_subtasks=2, allowed_source_steps=frozenset({16})
    )
    with pytest.raises(BoundedRunError, match="disagrees"):
        _engine(root).run(contract=contract, max_steps=3)
    with pytest.raises(BoundedRunError, match="disagrees"):
        _engine(root).run(contract=contract, allowed_source_steps=frozenset({12}))


# ==================================================================== run_bounded
def test_run_bounded_returns_a_structured_outcome(tmp_path):
    root = _project(tmp_path)
    state = SQLiteStateStore(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision, actor="operator"
    )
    result = _engine(root).run_bounded(contract)

    payload = result.to_dict()
    assert payload["run_id"] == contract.run_id
    assert payload["contract_sha256"] == contract.contract_sha256
    assert payload["start_revision"] == state.revision
    assert payload["end_revision"] >= state.revision
    assert payload["actor"] == "operator"
    assert isinstance(payload["completed_subtasks"], int)
    assert payload["entry_verification"]["ok"] is True
    assert payload["final_verification"]["ok"] is True
    assert json.loads(json.dumps(payload)) == payload


def test_run_bounded_refuses_when_the_manifest_is_already_violated(tmp_path):
    root = _project(tmp_path)
    target = root / "guarded.txt"
    target.write_text("expected\n", encoding="utf-8")
    expected = _sha(target)
    target.write_text("already broken\n", encoding="utf-8")

    state = SQLiteStateStore(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision,
        protected_manifest={"guarded.txt": expected},
    )
    with pytest.raises(ProtectedManifestViolation, match="already violated at entry"):
        _engine(root).run_bounded(contract)


def test_run_bounded_records_the_authorisation_on_run_started(tmp_path):
    """Capability 10: the event stream must answer *which* authorisation ran."""

    root = _project(tmp_path)
    state = SQLiteStateStore(root).load()
    contract = BoundedRunContract(
        expected_revision=state.revision, max_subtasks=1, actor="operator"
    )
    _engine(root).run_bounded(contract)

    events = SQLiteStateStore(root).events()
    started = next(e for e in reversed(events) if e.type == "RUN_STARTED")
    bound = started.payload["bounded_run"]
    assert bound["bounded_run_schema"] == BOUNDED_RUN_SCHEMA
    assert bound["bounded_run_contract_sha256"] == contract.contract_sha256
    assert bound["bounded_run_id"] == contract.run_id
    assert bound["run_policy"] == contract.run_policy
    assert bound["actor"] == "operator"
    assert bound["max_subtasks"] == 1
    assert bound["expected_revision"] == state.revision
    assert len(bound["protected_manifest_sha256"]) == 64


def test_the_contract_does_not_leak_into_a_later_plain_run(tmp_path):
    """The instance attribute must be cleared even though run_bounded returns early."""

    root = _project(tmp_path)
    engine = _engine(root)
    state = SQLiteStateStore(root).load()
    # bounded to one subtask so the project is left with work for the plain run
    engine.run_bounded(
        BoundedRunContract(expected_revision=state.revision, max_subtasks=1)
    )
    assert engine._bounded_contract is None

    # a plain run must then write its own RUN_STARTED, and it must not carry the
    # contract: the contract is per-invocation authorisation, never ambient state
    before = len([e for e in SQLiteStateStore(root).events() if e.type == "RUN_STARTED"])
    engine.run()
    events = SQLiteStateStore(root).events()
    started = [e for e in events if e.type == "RUN_STARTED"]
    assert len(started) == before + 1, "the plain run must have started on its own"
    assert "bounded_run" not in started[-1].payload


def test_advance_bounded_is_reachable_through_the_service(tmp_path):
    from factory_core.service import FactoryService

    root = tmp_path / "factory"
    root.mkdir()
    (root / "ongoing").mkdir()
    project = root / "ongoing" / "demo"
    project.mkdir()
    SQLiteStateStore(project).initialize(
        project_id="demo",
        project_type="modeling",
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    state = SQLiteStateStore(project).load()

    service = FactoryService(root)
    result = service.advance_bounded(
        project, BoundedRunContract(expected_revision=state.revision)
    )
    assert result.contract_sha256
    assert result.run_id


# ======================================================= boundary fingerprinting
def test_boundary_fingerprint_changes_with_the_stopping_position():
    class _State:
        revision = 10
        status = "paused"
        active_stage = 8
        active_subtask = "revision"
        source_step_id = 12
        last_completed_stage = 7
        last_completed_step = 11

    base = boundary_fingerprint(_State())
    assert boundary_fingerprint(_State()) == base, "deterministic"

    class _Elsewhere(_State):
        active_subtask = "constructive_review"

    assert boundary_fingerprint(_Elsewhere()) != base


def test_boundary_fingerprint_includes_the_dependency_not_just_its_identity():
    """A solver job with a new receipt is a different boundary.

    Recording only the job id would report "unchanged" for a real change, which
    is exactly the failure the repeated-boundary guard must not have.
    """

    class _State:
        revision = 10
        status = "blocked"
        active_stage = 4
        active_subtask = "solve"
        source_step_id = 5
        last_completed_stage = 3
        last_completed_step = 4

    without = boundary_fingerprint(_State(), dependency_fingerprint="job:abc")
    with_new_receipt = boundary_fingerprint(
        _State(), dependency_fingerprint="job:abc|receipt:def"
    )
    assert without != with_new_receipt


def test_repeated_boundary_is_reported_rather_than_raised(tmp_path):
    root = _project(tmp_path)
    state = SQLiteStateStore(root).load()
    first = _engine(root).run_bounded(
        BoundedRunContract(expected_revision=state.revision)
    )
    second = _engine(root).run_bounded(
        BoundedRunContract(
            expected_revision=first.end_revision,
            previous_boundary_fingerprint=first.boundary_fingerprint,
        )
    )
    if second.boundary_fingerprint == first.boundary_fingerprint and not second.made_progress:
        assert second.unchanged_boundary is True
        assert second.needs_inspection is True
        assert second.to_dict()["outcome"] == "NEEDS_INSPECTION"
    else:
        assert second.unchanged_boundary is False


def test_stop_reason_classification_is_machine_readable():
    class _State:
        def __init__(self, status):
            self.status = status

    assert classify_stop_reason(_State("completed"), previous_status="running", completed=1, bounded=None) == "PROJECT_COMPLETED"
    assert classify_stop_reason(_State("paused"), previous_status="running", completed=1, bounded=None) == "BOUNDARY_OR_SCOPE"
    assert classify_stop_reason(_State("failed"), previous_status="running", completed=0, bounded=None) == "FAILED"
    assert classify_stop_reason(_State("ready"), previous_status="ready", completed=0, bounded=None) == "UNCHANGED"
    assert classify_stop_reason(_State("ready"), previous_status="running", completed=2, bounded=2) == "MAX_SUBTASKS"


# ================================================================== the outcome
def test_a_successful_advance_does_not_require_a_progress_journal(tmp_path):
    """The point of the whole stage.

    The hand-written drivers wrote ``progress.json`` and a
    ``protected_files.json`` and asserted hard-coded revisions.  The contract
    returns all of that information instead, and leaves nothing behind.
    """

    root = _project(tmp_path)
    state = SQLiteStateStore(root).load()
    result = _engine(root).run_bounded(
        BoundedRunContract(expected_revision=state.revision, actor="operator", max_subtasks=1)
    )
    payload = result.to_dict()

    # everything the old journals carried is now in the result
    assert payload["start_revision"] == state.revision
    assert payload["end_revision"] >= state.revision
    assert payload["stop_reason"]
    assert payload["completed_subtasks"] >= 0
    assert payload["boundary_fingerprint"]
    assert payload["entry_verification"]["checked"] >= 0

    # and nothing was written outside the workflow's own state
    assert not (root / "progress.json").exists()
    assert not (root / "protected_files.json").exists()