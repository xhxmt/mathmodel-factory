from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import inspect
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from factory_core.canonical import canonical_sha256
import factory_core.fd_ownership as fd_ownership
import factory_core.phase6_snapshot_grants as phase6
from factory_core.phase6_snapshot_grants import (
    EvaluationDecision,
    GrantScope,
    GrantStatus,
    Phase6ContractError,
    Phase6GrantConflict,
    Phase6IdempotencyConflict,
    Phase6SnapshotConflict,
    Phase6SnapshotGrantStore,
    Phase6SnapshotNotFound,
    Phase6SnapshotStale,
    Phase6SourceIneligible,
    Phase6StoreError,
    SectionAvailability,
    VerifiedSection,
    build_authority_source_binding,
    load_project_snapshot_for_web,
    run_phase6_snapshot_grants_shadow,
)


def _h(character: str) -> str:
    return character * 64


def _fd_targets() -> tuple[str, ...]:
    targets = []
    for name in os.listdir("/proc/self/fd"):
        try:
            targets.append(os.readlink(f"/proc/self/fd/{name}"))
        except FileNotFoundError:
            pass
    return tuple(targets)


def _interrupting_line_trace(
    function,
    fragment: str,
    exception_type: type[BaseException],
    *,
    runtime_hit: int = 1,
    source_hit: int = 1,
):
    """Raise at one exact Python line and report whether the seam was reached."""

    function = getattr(function, "__func__", function)
    lines, start = inspect.getsourcelines(function)
    matching_lines = [
        start + index for index, line in enumerate(lines) if fragment in line
    ]
    assert len(matching_lines) >= source_hit, (
        f"missing source seam {fragment!r} occurrence {source_hit} in "
        f"{function.__qualname__}"
    )
    target_line = matching_lines[source_hit - 1]
    state = {"hits": 0, "raised": False, "target_line": target_line}

    def trace(frame, event, argument):
        del argument
        if (
            event == "line"
            and frame.f_code is function.__code__
            and frame.f_lineno == target_line
        ):
            state["hits"] += 1
            if state["hits"] == runtime_hit:
                state["raised"] = True
                sys.settrace(None)
                raise exception_type(
                    f"line-trace:{function.__qualname__}:{target_line}"
                )
        return trace

    return trace, state


def _assert_no_phase6_creation_residue(path: Path) -> None:
    assert not path.exists()
    assert not any(
        Path(f"{path}{suffix}").exists()
        for suffix in ("-wal", "-shm", "-journal")
    )
    assert not any(str(path) in target for target in _fd_targets())
    assert list(path.parent.iterdir()) == []


def _binding(
    revision: int = 1,
    *,
    completeness: str = "COMPLETE",
    project_generation: str = "project-generation-1",
):
    coordinate = {
        "schema": "authority-workflow-coordinate-v1",
        "workflow_id": "workflow-1",
        "project_id": "project-1",
        "project_generation": project_generation,
        "run_generation": "run-generation-1",
        "runtime_generation": "runtime-generation-1",
        "scheduler_generation": "scheduler-generation-1",
        "current_revision": revision,
        "contract_pin_set_sha256": _h("a"),
        "authority_state": "active",
        "source_fence_sha256": _h("b"),
        "switch_mode": "shadow",
        "switch_epoch": 3,
    }
    source_coordinate = {
        "schema_version": "snapshot-coordinate-v0",
        "project_id": "project-1",
        "workflow_schema_version": 1,
        "project_revision": revision,
        "project_generation": project_generation,
        "run_generation": "run-generation-1",
        "runtime_generation": "runtime-generation-1",
        "scheduler_generation": "scheduler-generation-1",
        "recorded_contract_pin_set_sha256": _h("a"),
    }
    return build_authority_source_binding(
        authority_coordinate=coordinate,
        authority_coordinate_sha256=canonical_sha256(coordinate),
        authority_revision_snapshot_sha256=hashlib.sha256(
            f"revision:{revision}".encode()
        ).hexdigest(),
        authority_revision_through_revision=revision,
        source_snapshot_schema="project-snapshot-v0-source-authorized-v3",
        source_snapshot_semantic_sha256=hashlib.sha256(
            f"snapshot:{revision}:{completeness}".encode()
        ).hexdigest(),
        source_snapshot_completeness=completeness,
        source_snapshot_coordinate=source_coordinate,
        phase3_artifact_state_sha256=_h("c"),
        phase4_operation_state_sha256=_h("d"),
        phase5_supervisor_state_sha256=_h("e"),
    )


def _sections(content: str = "f") -> tuple[VerifiedSection, ...]:
    return (
        VerifiedSection(
            "action_center",
            SectionAvailability.AVAILABLE,
            _h(content),
            "action-center-section-v1",
        ),
        VerifiedSection(
            "overview",
            SectionAvailability.AVAILABLE,
            _h("1"),
            "overview-section-v1",
        ),
    )


def _store_with_snapshot(tmp_path: Path, *, valid_until: int = 100):
    path = tmp_path / "phase6.db"
    store = Phase6SnapshotGrantStore(path)
    store.initialize()
    snapshot = store.append_snapshot(
        source_binding=_binding(),
        sections=_sections(),
        captured_at=10,
        valid_until=valid_until,
        expected_previous_snapshot_id=None,
        idempotency_key="snapshot-request-1",
    ).snapshot
    return store, path, snapshot


def _issue(store: Phase6SnapshotGrantStore, snapshot_id: str, **overrides):
    values = {
        "snapshot_id": snapshot_id,
        "subject_type": "user",
        "subject_id": "alice",
        "subject_generation": "membership-generation-7",
        "scope": GrantScope.SECTION_VIEW,
        "scope_key": "overview",
        "issuer_id": "shadow-issuer",
        "issuer_generation": "issuer-generation-2",
        "issuer_evidence_schema": "synthetic-issuer-receipt-v1",
        "issuer_receipt_sha256": _h("2"),
        "issued_at": 11,
        "not_before": 12,
        "expires_at": 50,
        "expected_previous_grant_id": None,
        "idempotency_key": "grant-request-1",
    }
    values.update(overrides)
    return store.issue_grant(**values)


def _evaluate(store: Phase6SnapshotGrantStore, grant_id: str, **overrides):
    values = {
        "subject_type": "user",
        "subject_id": "alice",
        "subject_generation": "membership-generation-7",
        "requested_scope": GrantScope.SECTION_VIEW,
        "requested_scope_key": "overview",
        "evaluated_at": 12,
        "idempotency_key": "evaluation-request-1",
    }
    values.update(overrides)
    return store.evaluate_grant(grant_id, **values)


def test_default_off_returns_before_path_construction(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled runner touched the path")

    monkeypatch.setattr(phase6, "Path", forbidden)
    result = run_phase6_snapshot_grants_shadow(
        enabled=False, database="/definitely/not/a/phase6/database"
    )
    assert result.enabled is False
    assert result.store_verified is False
    assert result.authoritative is False
    assert result.authority_transferred is False
    assert result.dispatch_performed is False
    assert len(result.run_sha256) == 64


def test_source_binding_is_exact_and_binds_phase3_phase4_phase5():
    binding = _binding()
    assert binding.eligible is True
    assert binding.as_dict()["phase3_artifact_state_sha256"] == _h("c")
    assert binding.as_dict()["phase4_operation_state_sha256"] == _h("d")
    assert binding.as_dict()["phase5_supervisor_state_sha256"] == _h("e")
    assert canonical_sha256(
        {key: value for key, value in binding.as_dict().items() if key != "binding_sha256"}
    ) == binding.binding_sha256

    coordinate = dict(binding.authority_coordinate)
    coordinate["unexpected"] = "value"
    with pytest.raises(Phase6ContractError, match="exact v1 keys"):
        build_authority_source_binding(
            authority_coordinate=coordinate,
            authority_coordinate_sha256=canonical_sha256(coordinate),
            authority_revision_snapshot_sha256=_h("1"),
            authority_revision_through_revision=1,
            source_snapshot_schema=binding.source_snapshot_schema,
            source_snapshot_semantic_sha256=binding.source_snapshot_semantic_sha256,
            source_snapshot_completeness="COMPLETE",
            source_snapshot_coordinate=binding.source_snapshot_coordinate,
            phase3_artifact_state_sha256=_h("c"),
            phase4_operation_state_sha256=_h("d"),
            phase5_supervisor_state_sha256=_h("e"),
        )


@pytest.mark.parametrize("invalid_revision", [True, -1, 2**63])
def test_logical_integer_domain_rejects_bool_negative_and_overflow(invalid_revision):
    binding = _binding()
    coordinate = dict(binding.authority_coordinate)
    coordinate["current_revision"] = invalid_revision
    source = dict(binding.source_snapshot_coordinate)
    source["project_revision"] = invalid_revision
    with pytest.raises(Phase6ContractError, match="signed 64-bit"):
        build_authority_source_binding(
            authority_coordinate=coordinate,
            authority_coordinate_sha256=canonical_sha256(coordinate),
            authority_revision_snapshot_sha256=_h("1"),
            authority_revision_through_revision=invalid_revision,
            source_snapshot_schema=binding.source_snapshot_schema,
            source_snapshot_semantic_sha256=binding.source_snapshot_semantic_sha256,
            source_snapshot_completeness="COMPLETE",
            source_snapshot_coordinate=source,
            phase3_artifact_state_sha256=_h("c"),
            phase4_operation_state_sha256=_h("d"),
            phase5_supervisor_state_sha256=_h("e"),
        )


@pytest.mark.parametrize("mismatch", ["revision", "generation", "pin"])
def test_source_binding_rejects_coordinate_crossover(mismatch):
    binding = _binding()
    source = dict(binding.source_snapshot_coordinate)
    if mismatch == "revision":
        source["project_revision"] = 0
    elif mismatch == "generation":
        source["run_generation"] = "another-run"
    else:
        source["recorded_contract_pin_set_sha256"] = _h("9")
    with pytest.raises(Phase6ContractError, match="differs"):
        build_authority_source_binding(
            authority_coordinate=dict(binding.authority_coordinate),
            authority_coordinate_sha256=binding.authority_coordinate_sha256,
            authority_revision_snapshot_sha256=binding.authority_revision_snapshot_sha256,
            authority_revision_through_revision=1,
            source_snapshot_schema=binding.source_snapshot_schema,
            source_snapshot_semantic_sha256=binding.source_snapshot_semantic_sha256,
            source_snapshot_completeness="COMPLETE",
            source_snapshot_coordinate=source,
            phase3_artifact_state_sha256=_h("c"),
            phase4_operation_state_sha256=_h("d"),
            phase5_supervisor_state_sha256=_h("e"),
        )


def test_store_fresh_reopen_mode_and_no_sidecars(tmp_path):
    path = tmp_path / "phase6.db"
    store = Phase6SnapshotGrantStore(path)
    store.initialize()
    store.initialize()
    assert stat_mode(path) == 0o600
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))
    result = run_phase6_snapshot_grants_shadow(enabled=True, database=path)
    assert result.store_verified is True
    assert result.snapshot_count == result.grant_count == 0


def test_fresh_creation_is_parent_anchored_and_directory_fsynced(
    tmp_path, monkeypatch
):
    path = tmp_path / "phase6.db"
    original_open = os.open
    original_fsync = os.fsync
    exclusive_calls = []
    fsynced_directories = []

    def recording_open(file, flags, mode=0o777, *, dir_fd=None):
        if flags & os.O_EXCL:
            exclusive_calls.append((file, flags, mode, dir_fd))
        return original_open(file, flags, mode, dir_fd=dir_fd)

    def recording_fsync(descriptor):
        if phase6.stat.S_ISDIR(os.fstat(descriptor).st_mode):
            fsynced_directories.append(descriptor)
        return original_fsync(descriptor)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    Phase6SnapshotGrantStore(path).initialize()

    assert len(exclusive_calls) == 1
    name, flags, mode, parent_fd = exclusive_calls[0]
    assert name == path.name
    assert flags & os.O_CREAT and flags & os.O_EXCL
    assert flags & getattr(os, "O_NOFOLLOW", 0)
    assert mode == 0o600
    assert isinstance(parent_fd, int) and parent_fd >= 0
    assert fsynced_directories


def test_exclusive_creation_parent_swap_never_creates_in_replacement_directory(
    tmp_path, monkeypatch
):
    parent = tmp_path / "phase6-parent"
    parent.mkdir()
    displaced = tmp_path / "phase6-parent-displaced"
    path = (parent / "phase6.db").resolve()
    original_open = os.open
    swapped = False

    def swapping_open(file, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and flags & os.O_CREAT and flags & os.O_EXCL:
            swapped = True
            parent.replace(displaced)
            parent.mkdir()
        return original_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises(Phase6StoreError):
        Phase6SnapshotGrantStore(path).initialize()
    assert swapped is True
    assert list(parent.iterdir()) == []
    assert list(displaced.iterdir()) == []
    assert not any(str(path) in target for target in _fd_targets())


def test_concurrent_exclusive_creation_has_only_valid_outcomes_and_restarts(tmp_path):
    path = tmp_path / "phase6-concurrent.db"
    barrier = Barrier(2)

    def initialize_once():
        barrier.wait(timeout=5)
        try:
            Phase6SnapshotGrantStore(path).initialize()
        except Phase6StoreError:
            return "exclusive-loser"
        return "initialized"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: initialize_once(), range(2)))
    assert "initialized" in results
    assert set(results) <= {"initialized", "exclusive-loser"}
    Phase6SnapshotGrantStore(path).initialize()
    assert run_phase6_snapshot_grants_shadow(
        enabled=True, database=path
    ).store_verified is True
    assert not any(str(path) in target for target in _fd_targets())


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.parametrize("error_number", [errno.EMFILE, errno.ENFILE])
def test_first_dup_failure_preserves_oserror_and_removes_empty_database(
    tmp_path, monkeypatch, error_number
):
    path = tmp_path / "phase6.db"
    before = set(os.listdir("/proc/self/fd"))

    def fail_dup(descriptor):
        raise OSError(error_number, "injected dup exhaustion")

    monkeypatch.setattr(phase6.os, "dup", fail_dup)
    with pytest.raises(OSError) as caught:
        Phase6SnapshotGrantStore(path).initialize()
    assert caught.value.errno == error_number
    assert str(caught.value).endswith("injected dup exhaustion")
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
    assert len(os.listdir("/proc/self/fd")) <= len(before)
    assert not any(str(path) in target for target in _fd_targets())


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "stage,retained",
    [
        ("after_exclusive_create", False),
        ("after_cleanup_dup", False),
        ("after_sqlite_connect_before_transfer", False),
        ("before_initialize_commit", False),
        ("before_parent_fsync", True),
        ("after_parent_fsync", True),
        ("after_initialize_commit", True),
    ],
)
def test_initialization_baseexception_narrow_windows_are_atomic(
    tmp_path, monkeypatch, exception_type, stage, retained
):
    path = tmp_path / "phase6.db"
    before = set(os.listdir("/proc/self/fd"))

    def inject(current):
        if current == stage:
            raise exception_type(f"injected:{stage}")

    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(exception_type, match=f"injected:{stage}"):
        Phase6SnapshotGrantStore(path).initialize()
    assert path.exists() is retained
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))
    if retained:
        monkeypatch.setattr(phase6, "_phase6_failure_point", lambda stage: None)
        Phase6SnapshotGrantStore(path).initialize()
    else:
        assert list(tmp_path.iterdir()) == []
    assert len(os.listdir("/proc/self/fd")) <= len(before)
    assert not any(str(path) in target for target in _fd_targets())


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "fragment",
    [
        "creator = OwnedDescriptor(",
        "fcntl.flock(creator.fileno",
        "created = self._assert_path",
        "cleanup = creator.duplicate(",
    ],
)
def test_line_interrupt_during_creator_publication_cleans_exact_inode(
    tmp_path, exception_type, fragment
):
    path = tmp_path / "phase6.db"
    before = len(os.listdir("/proc/self/fd"))
    trace, state = _interrupting_line_trace(
        Phase6SnapshotGrantStore._initialize_new,
        fragment,
        exception_type,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(exception_type, match="line-trace"):
            Phase6SnapshotGrantStore(path).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    _assert_no_phase6_creation_residue(path)
    assert len(os.listdir("/proc/self/fd")) <= before
    Phase6SnapshotGrantStore(path).initialize()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "fragment",
    [
        "self._adopted_descriptor = descriptor",
        "self._descriptor: int | None = descriptor",
        "self._owner = owner",
        "self.label = label",
        "self._managed = not _defer_finalizer",
    ],
)
def test_line_interrupt_inside_creator_lease_constructor_uses_raw_fallback(
    tmp_path, exception_type, fragment
):
    path = tmp_path / "phase6.db"
    before = len(os.listdir("/proc/self/fd"))
    trace, state = _interrupting_line_trace(
        fd_ownership.OwnedDescriptor.__init__,
        fragment,
        exception_type,
        runtime_hit=2,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(exception_type, match="line-trace"):
            Phase6SnapshotGrantStore(path).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    _assert_no_phase6_creation_residue(path)
    assert len(os.listdir("/proc/self/fd")) <= before
    Phase6SnapshotGrantStore(path).initialize()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "fragment",
    [
        "lease = cls(",
        "lease._managed = True",
        "return lease",
    ],
)
def test_line_interrupt_during_cleanup_dup_publication_has_no_raw_fd_leak(
    tmp_path, exception_type, fragment
):
    path = tmp_path / "phase6.db"
    before = len(os.listdir("/proc/self/fd"))
    trace, state = _interrupting_line_trace(
        fd_ownership.OwnedDescriptor.from_opener,
        fragment,
        exception_type,
        runtime_hit=2,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(exception_type, match="line-trace"):
            Phase6SnapshotGrantStore(path).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    _assert_no_phase6_creation_residue(path)
    assert len(os.listdir("/proc/self/fd")) <= before
    Phase6SnapshotGrantStore(path).initialize()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_line_interrupt_at_duplicate_return_uses_unpublished_lease_finalizer(
    tmp_path, exception_type
):
    path = tmp_path / "phase6.db"
    before = len(os.listdir("/proc/self/fd"))
    trace, state = _interrupting_line_trace(
        fd_ownership.OwnedDescriptor.duplicate,
        "return duplicate",
        exception_type,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(exception_type, match="line-trace"):
            Phase6SnapshotGrantStore(path).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    _assert_no_phase6_creation_residue(path)
    assert len(os.listdir("/proc/self/fd")) <= before
    Phase6SnapshotGrantStore(path).initialize()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "fragment",
    [
        "self._anchor = database",
        "self._parent_anchor = parent",
        "database.transfer(",
        "parent.transfer(",
    ],
)
def test_line_interrupt_during_two_anchor_transfer_closes_each_fd_once(
    tmp_path, exception_type, fragment
):
    path = tmp_path / "phase6.db"
    before = len(os.listdir("/proc/self/fd"))
    trace, state = _interrupting_line_trace(
        phase6._AnchoredConnection._adopt_anchors,
        fragment,
        exception_type,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(exception_type, match="line-trace"):
            Phase6SnapshotGrantStore(path).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    _assert_no_phase6_creation_residue(path)
    assert len(os.listdir("/proc/self/fd")) <= before
    Phase6SnapshotGrantStore(path).initialize()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_line_interrupt_after_anchor_transfer_before_metadata_publish_is_atomic(
    tmp_path, exception_type
):
    path = tmp_path / "phase6.db"
    before = len(os.listdir("/proc/self/fd"))
    trace, state = _interrupting_line_trace(
        Phase6SnapshotGrantStore._initialize_new,
        "connection._identity = created",
        exception_type,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(exception_type, match="line-trace"):
            Phase6SnapshotGrantStore(path).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    _assert_no_phase6_creation_residue(path)
    assert len(os.listdir("/proc/self/fd")) <= before
    Phase6SnapshotGrantStore(path).initialize()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "fragment",
    [
        "callbacks = self._anchor_cleanup_callbacks()",
        "sqlite3.Connection.close(self)",
        "run_cleanup(callbacks)",
    ],
)
def test_line_interrupt_inside_connection_close_releases_both_anchors(
    tmp_path, exception_type, fragment
):
    store, path, _ = _store_with_snapshot(tmp_path)
    connection = store._connect()
    before = len(os.listdir("/proc/self/fd"))
    trace, state = _interrupting_line_trace(
        phase6._AnchoredConnection.close,
        fragment,
        exception_type,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(exception_type, match="line-trace"):
            connection.close()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert connection._anchor is None
    assert connection._parent_anchor is None
    assert len(os.listdir("/proc/self/fd")) <= before - 2
    assert not any(str(path) in target for target in _fd_targets())
    store.initialize()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "function,fragment",
    [
        (phase6._AnchoredConnection._anchor_cleanup_callbacks, "callbacks = []"),
        (phase6._AnchoredConnection._close_anchor_attribute, "lease = getattr"),
    ],
)
def test_line_interrupt_during_connection_anchor_cleanup_is_reconciled(
    tmp_path, exception_type, function, fragment
):
    store, path, _ = _store_with_snapshot(tmp_path)
    connection = store._connect()
    before = len(os.listdir("/proc/self/fd"))
    trace, state = _interrupting_line_trace(
        function,
        fragment,
        exception_type,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(exception_type, match="line-trace"):
            connection.close()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert connection._anchor is None
    assert connection._parent_anchor is None
    assert len(os.listdir("/proc/self/fd")) <= before - 2
    assert not any(str(path) in target for target in _fd_targets())
    store.initialize()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_line_interrupt_at_success_close_callsite_enters_failure_cleanup(
    tmp_path, exception_type
):
    store, path, snapshot = _store_with_snapshot(tmp_path)
    before = len(os.listdir("/proc/self/fd"))
    trace, state = _interrupting_line_trace(
        Phase6SnapshotGrantStore.load_snapshot,
        "connection.close()",
        exception_type,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(exception_type, match="line-trace") as caught:
            store.load_snapshot(snapshot.snapshot_id)
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert str(caught.value).startswith("line-trace:")
    assert len(os.listdir("/proc/self/fd")) <= before
    assert not any(str(path) in target for target in _fd_targets())
    assert store.load_snapshot(snapshot.snapshot_id) == snapshot


@pytest.mark.parametrize("fault_stage", ["before", "after"])
def test_primary_exception_retains_close_cleanup_failure(
    tmp_path, monkeypatch, fault_stage
):
    path = tmp_path / "phase6.db"
    primary = KeyboardInterrupt("primary interrupt")
    original_raw_close = fd_ownership._attempt_raw_close
    closed_descriptors = []

    def recording_raw_close(descriptor, state):
        closed_descriptors.append(descriptor)
        original_raw_close(descriptor, state)

    def close_failure(stage, descriptor):
        del descriptor
        if stage == fault_stage:
            raise OSError(errno.EIO, f"injected {stage}-close diagnostic")

    def inject(stage):
        if stage == "after_exclusive_create":
            raise primary

    monkeypatch.setattr(
        fd_ownership,
        "_descriptor_close_failure_point",
        close_failure,
    )
    monkeypatch.setattr(fd_ownership, "_attempt_raw_close", recording_raw_close)
    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(KeyboardInterrupt, match="primary interrupt") as caught:
        Phase6SnapshotGrantStore(path).initialize()
    assert caught.value is primary
    failures = getattr(caught.value, "__factory_cleanup_failures__", ())
    assert failures
    assert any("close" in failure.label for failure in failures)
    assert len(closed_descriptors) == len(set(closed_descriptors)) == 2
    _assert_no_phase6_creation_residue(path)


@pytest.mark.parametrize("fault_stage", ["before", "after"])
def test_primary_exception_retains_unlink_failure_around_unique_unlink(
    tmp_path, monkeypatch, fault_stage
):
    path = tmp_path / "phase6.db"
    primary = SystemExit("primary exit")
    original_raw_unlink = fd_ownership._raw_unlinkat
    unlinked_names = []
    failure_injected = False

    def recording_raw_unlink(parent_fd, name):
        unlinked_names.append(name)
        original_raw_unlink(parent_fd, name)

    def unlink_failure(stage, parent_fd, name):
        nonlocal failure_injected
        del parent_fd, name
        if stage == fault_stage and not failure_injected:
            failure_injected = True
            raise OSError(errno.EIO, f"injected {stage}-unlink diagnostic")

    def inject(stage):
        if stage == "after_exclusive_create":
            raise primary

    monkeypatch.setattr(fd_ownership, "_unlink_failure_point", unlink_failure)
    monkeypatch.setattr(fd_ownership, "_raw_unlinkat", recording_raw_unlink)
    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(SystemExit, match="primary exit") as caught:
        Phase6SnapshotGrantStore(path).initialize()
    assert caught.value is primary
    failures = getattr(caught.value, "__factory_cleanup_failures__", ())
    assert any("unlink" in failure.label for failure in failures)
    assert failure_injected is True
    assert len(unlinked_names) == 1
    assert ".phase6-cleanup-" in unlinked_names[0]
    _assert_no_phase6_creation_residue(path)


@pytest.mark.parametrize("fault_stage", ["before", "after"])
def test_primary_exception_retains_cleanup_rename_failure_and_reconciles(
    tmp_path, monkeypatch, fault_stage
):
    path = tmp_path / "phase6.db"
    primary = KeyboardInterrupt("primary rename interrupt")
    rename_failure = OSError(errno.EIO, f"injected {fault_stage}-rename diagnostic")
    original_rename = phase6._rename_noreplace
    injected = False

    def failing_rename(source_parent_fd, source_name, target_parent_fd, target_name):
        nonlocal injected
        if not injected:
            injected = True
            if fault_stage == "before":
                raise rename_failure
            original_rename(
                source_parent_fd,
                source_name,
                target_parent_fd,
                target_name,
            )
            raise rename_failure
        return original_rename(
            source_parent_fd,
            source_name,
            target_parent_fd,
            target_name,
        )

    def inject(stage):
        if stage == "after_exclusive_create":
            raise primary

    monkeypatch.setattr(phase6, "_rename_noreplace", failing_rename)
    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(KeyboardInterrupt) as caught:
        Phase6SnapshotGrantStore(path).initialize()
    assert caught.value is primary
    assert injected is True
    failures = getattr(primary, "__factory_cleanup_failures__", ())
    assert rename_failure in [failure.error for failure in failures]
    _assert_no_phase6_creation_residue(path)


def test_snapshot_commit_replay_chain_and_stale_web_adapter(tmp_path):
    store, path, first = _store_with_snapshot(tmp_path)
    replay = store.append_snapshot(
        source_binding=_binding(),
        sections=_sections(),
        captured_at=10,
        valid_until=100,
        expected_previous_snapshot_id=None,
        idempotency_key="same-bytes-another-key",
    )
    assert replay.replayed is True
    assert replay.snapshot.snapshot_id == first.snapshot_id

    second = store.append_snapshot(
        source_binding=_binding(2),
        sections=_sections("7"),
        captured_at=20,
        valid_until=120,
        expected_previous_snapshot_id=first.snapshot_id,
        idempotency_key="snapshot-request-2",
    ).snapshot
    assert second.snapshot_sequence == 2
    assert second.previous_snapshot_id == first.snapshot_id
    ready = load_project_snapshot_for_web(
        db_path=path, project_id="project-1", expected_revision=2
    )
    assert ready["state"] == "ready"
    assert ready["coordinate"] == {
        "snapshot_id": second.snapshot_id,
        "revision": 2,
    }
    assert ready["actions"] == []
    assert all(ready[field] is False for field in (
        "authoritative", "authority_transferred", "dispatch_performed"
    ))
    with pytest.raises(Phase6SnapshotStale) as caught:
        load_project_snapshot_for_web(
            db_path=path, project_id="project-1", expected_revision=1
        )
    assert caught.value.server_revision == 2
    assert caught.value.code == "PHASE6_SNAPSHOT_STALE"
    with pytest.raises(Phase6SnapshotNotFound) as missing:
        load_project_snapshot_for_web(
            db_path=path, project_id="project-missing", expected_revision=None
        )
    assert missing.value.code == "PHASE6_SNAPSHOT_NOT_FOUND"


def test_snapshot_conflicts_on_same_revision_bytes_and_generation_crossover(tmp_path):
    store, _, first = _store_with_snapshot(tmp_path)
    with pytest.raises(Phase6SnapshotConflict, match="different canonical"):
        store.append_snapshot(
            source_binding=_binding(),
            sections=_sections("8"),
            captured_at=10,
            valid_until=100,
            expected_previous_snapshot_id=None,
            idempotency_key="same-revision-different",
        )
    with pytest.raises(Phase6SnapshotConflict, match="generation crossover"):
        store.append_snapshot(
            source_binding=_binding(2, project_generation="project-generation-2"),
            sections=_sections(),
            captured_at=20,
            valid_until=120,
            expected_previous_snapshot_id=first.snapshot_id,
            idempotency_key="generation-crossover",
        )


def test_same_revision_replay_requires_the_original_predecessor_fence(tmp_path):
    store, _, first = _store_with_snapshot(tmp_path)
    with pytest.raises(Phase6SnapshotConflict, match="replay predecessor differs"):
        store.append_snapshot(
            source_binding=_binding(),
            sections=_sections(),
            captured_at=10,
            valid_until=100,
            expected_previous_snapshot_id=_h("9"),
            idempotency_key="wrong-same-revision-predecessor",
        )
    assert store.current_snapshot(
        workflow_id="workflow-1", project_id="project-1"
    ) == first


def test_issue_evaluate_scope_subject_revoke_and_historical_replay(tmp_path):
    store, _, snapshot = _store_with_snapshot(tmp_path)
    issued = _issue(store, snapshot.snapshot_id)
    assert issued.lifecycle_receipt.after_status is GrantStatus.ACTIVE
    replay = _issue(store, snapshot.snapshot_id)
    assert replay.replayed is True
    assert replay.grant == issued.grant

    not_yet = _evaluate(
        store,
        issued.grant.grant_id,
        evaluated_at=11,
        idempotency_key="evaluation-not-yet",
    )
    assert not_yet.receipt.decision is EvaluationDecision.DENIED_NOT_YET_VALID
    wrong_subject = _evaluate(
        store,
        issued.grant.grant_id,
        subject_generation="membership-generation-8",
        evaluated_at=12,
        idempotency_key="evaluation-wrong-subject",
    )
    assert wrong_subject.receipt.decision is EvaluationDecision.DENIED_SUBJECT
    wrong_scope = _evaluate(
        store,
        issued.grant.grant_id,
        requested_scope=GrantScope.SNAPSHOT_VIEW,
        requested_scope_key=None,
        evaluated_at=13,
        idempotency_key="evaluation-wrong-scope",
    )
    assert wrong_scope.receipt.decision is EvaluationDecision.DENIED_SCOPE
    allowed = _evaluate(
        store,
        issued.grant.grant_id,
        evaluated_at=14,
        idempotency_key="evaluation-allowed",
    )
    assert allowed.receipt.decision is EvaluationDecision.ALLOWED_SHADOW
    assert allowed.current is allowed.shadow_allowed is True
    assert allowed.access_proof is not None
    proof = allowed.access_proof.as_dict()
    assert proof["dispatch_performed"] is False
    assert canonical_sha256(
        {key: value for key, value in proof.items() if key != "proof_sha256"}
    ) == proof["proof_sha256"]
    same_fact = _evaluate(
        store,
        issued.grant.grant_id,
        evaluated_at=14,
        idempotency_key="same-evaluation-another-key",
    )
    assert same_fact.receipt == allowed.receipt
    assert same_fact.shadow_allowed is True
    with pytest.raises(Phase6GrantConflict, match="precedes its latest evaluation"):
        store.revoke_grant(
            issued.grant.grant_id,
            actor_id="shadow-issuer",
            actor_generation="issuer-generation-2",
            reason_code="BACKDATED_REVOKE",
            effective_at=13,
            idempotency_key="backdated-revoke",
        )

    revoked = store.revoke_grant(
        issued.grant.grant_id,
        actor_id="shadow-issuer",
        actor_generation="issuer-generation-2",
        reason_code="USER_REQUEST",
        effective_at=15,
        idempotency_key="revoke-request",
    )
    assert revoked.lifecycle_receipt.after_status is GrantStatus.REVOKED
    historical = _evaluate(
        store,
        issued.grant.grant_id,
        evaluated_at=14,
        idempotency_key="evaluation-allowed",
    )
    assert historical.replayed is True
    assert historical.receipt == allowed.receipt
    assert historical.current is historical.shadow_allowed is False
    assert historical.access_proof is None
    after = _evaluate(
        store,
        issued.grant.grant_id,
        evaluated_at=16,
        idempotency_key="evaluation-after-revoke",
    )
    assert after.receipt.decision is EvaluationDecision.DENIED_REVOKED


def test_grant_cannot_be_issued_before_its_snapshot_capture(tmp_path):
    store, _, snapshot = _store_with_snapshot(tmp_path)
    with pytest.raises(Phase6ContractError, match="within its verified snapshot"):
        _issue(
            store,
            snapshot.snapshot_id,
            issued_at=9,
            not_before=9,
            idempotency_key="grant-before-snapshot",
        )
    assert run_phase6_snapshot_grants_shadow(
        enabled=True, database=store.path
    ).grant_count == 0


def test_expiry_is_exclusive_materialized_once_and_reopen_safe(tmp_path):
    store, path, snapshot = _store_with_snapshot(tmp_path)
    issued = _issue(store, snapshot.snapshot_id)
    before = _evaluate(
        store,
        issued.grant.grant_id,
        evaluated_at=49,
        idempotency_key="before-expiry",
    )
    assert before.receipt.decision is EvaluationDecision.ALLOWED_SHADOW
    boundary = _evaluate(
        store,
        issued.grant.grant_id,
        evaluated_at=50,
        idempotency_key="at-expiry",
    )
    assert boundary.receipt.decision is EvaluationDecision.DENIED_GRANT_EXPIRED
    assert store.load_grant(issued.grant.grant_id).lifecycle_receipt.after_status is GrantStatus.EXPIRED
    reopened = Phase6SnapshotGrantStore(path)
    again = _evaluate(
        reopened,
        issued.grant.grant_id,
        evaluated_at=51,
        idempotency_key="after-expiry",
    )
    assert again.receipt.decision is EvaluationDecision.DENIED_GRANT_EXPIRED
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM phase6_shadow_grant_lifecycle_facts WHERE grant_id=?",
            (issued.grant.grant_id,),
        ).fetchone()[0] == 2


def test_snapshot_advance_stales_old_grant_and_renewal_links_new_grant(tmp_path):
    store, _, first = _store_with_snapshot(tmp_path)
    old = _issue(store, first.snapshot_id)
    second = store.append_snapshot(
        source_binding=_binding(2),
        sections=_sections("7"),
        captured_at=20,
        valid_until=120,
        expected_previous_snapshot_id=first.snapshot_id,
        idempotency_key="snapshot-advance",
    ).snapshot
    stale = _evaluate(
        store,
        old.grant.grant_id,
        evaluated_at=20,
        idempotency_key="old-grant-after-advance",
    )
    assert stale.receipt.decision is EvaluationDecision.DENIED_SNAPSHOT_STALE
    renewed = _issue(
        store,
        second.snapshot_id,
        issued_at=21,
        not_before=21,
        expires_at=80,
        expected_previous_grant_id=old.grant.grant_id,
        idempotency_key="grant-renewal",
        issuer_receipt_sha256=_h("3"),
    )
    assert renewed.grant.grant_sequence == 2
    assert renewed.grant.previous_grant_id == old.grant.grant_id
    allowed = _evaluate(
        store,
        renewed.grant.grant_id,
        evaluated_at=21,
        idempotency_key="renewed-evaluation",
    )
    assert allowed.receipt.decision is EvaluationDecision.ALLOWED_SHADOW


def test_snapshot_expiry_precedes_grant_expiry_at_shared_boundary(tmp_path):
    store, _, snapshot = _store_with_snapshot(tmp_path, valid_until=30)
    issued = _issue(store, snapshot.snapshot_id, expires_at=30)
    boundary = _evaluate(
        store,
        issued.grant.grant_id,
        evaluated_at=30,
        idempotency_key="snapshot-expiry-boundary",
    )
    assert boundary.receipt.decision is EvaluationDecision.DENIED_SNAPSHOT_EXPIRED


def test_partial_source_snapshot_persists_but_cannot_issue_grant(tmp_path):
    path = tmp_path / "partial.db"
    store = Phase6SnapshotGrantStore(path)
    store.initialize()
    snapshot = store.append_snapshot(
        source_binding=_binding(completeness="PARTIAL"),
        sections=_sections(),
        captured_at=10,
        valid_until=100,
        expected_previous_snapshot_id=None,
        idempotency_key="partial-snapshot",
    ).snapshot
    assert snapshot.source_binding.eligible is False
    with pytest.raises(Phase6SourceIneligible) as rejected:
        load_project_snapshot_for_web(
            db_path=path,
            project_id="project-1",
            expected_revision=1,
        )
    assert rejected.value.code == "PHASE6_SOURCE_INELIGIBLE"
    with pytest.raises(Phase6GrantConflict, match="ineligible"):
        _issue(store, snapshot.snapshot_id)


def test_idempotency_conflicts_across_bytes_and_domains(tmp_path):
    store, _, snapshot = _store_with_snapshot(tmp_path)
    with pytest.raises(Phase6IdempotencyConflict):
        store.append_snapshot(
            source_binding=_binding(),
            sections=_sections("8"),
            captured_at=10,
            valid_until=100,
            expected_previous_snapshot_id=None,
            idempotency_key="snapshot-request-1",
        )
    with pytest.raises(Phase6IdempotencyConflict):
        _issue(store, snapshot.snapshot_id, idempotency_key="snapshot-request-1")


@pytest.mark.parametrize(
    "stage",
    ["after_snapshot_fact", "after_snapshot_current", "after_snapshot_idempotency", "before_snapshot_commit"],
)
@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_snapshot_fault_injection_rolls_back_every_stage(
    tmp_path, monkeypatch, stage, exception_type
):
    path = tmp_path / "fault.db"
    store = Phase6SnapshotGrantStore(path)
    store.initialize()

    def inject(current):
        if current == stage:
            raise exception_type(f"injected:{stage}")

    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(exception_type, match=f"injected:{stage}"):
        store.append_snapshot(
            source_binding=_binding(),
            sections=_sections(),
            captured_at=10,
            valid_until=100,
            expected_previous_snapshot_id=None,
            idempotency_key="faulted-snapshot",
        )
    monkeypatch.setattr(phase6, "_phase6_failure_point", lambda stage: None)
    run = run_phase6_snapshot_grants_shadow(enabled=True, database=path)
    assert run.snapshot_count == 0
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def test_snapshot_postcommit_interrupt_is_recoverable_as_committed(tmp_path, monkeypatch):
    path = tmp_path / "postcommit.db"
    store = Phase6SnapshotGrantStore(path)
    store.initialize()

    def inject(stage):
        if stage == "after_snapshot_commit":
            raise KeyboardInterrupt("after durable commit")

    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(KeyboardInterrupt, match="after durable commit"):
        store.append_snapshot(
            source_binding=_binding(),
            sections=_sections(),
            captured_at=10,
            valid_until=100,
            expected_previous_snapshot_id=None,
            idempotency_key="postcommit-snapshot",
        )
    monkeypatch.setattr(phase6, "_phase6_failure_point", lambda stage: None)
    reopened = Phase6SnapshotGrantStore(path)
    assert reopened.current_snapshot(
        workflow_id="workflow-1", project_id="project-1"
    ).snapshot_sequence == 1
    replay = reopened.append_snapshot(
        source_binding=_binding(),
        sections=_sections(),
        captured_at=10,
        valid_until=100,
        expected_previous_snapshot_id=None,
        idempotency_key="postcommit-snapshot",
    )
    assert replay.replayed is True


@pytest.mark.parametrize(
    "stage",
    [
        "after_grant_fact",
        "after_grant_lifecycle",
        "after_grant_current",
        "after_grant_idempotency",
        "before_grant_commit",
    ],
)
def test_grant_issue_failure_rolls_back_all_companion_rows(tmp_path, monkeypatch, stage):
    store, path, snapshot = _store_with_snapshot(tmp_path)

    def inject(current):
        if current == stage:
            raise SystemExit(f"injected:{stage}")

    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(SystemExit, match=f"injected:{stage}"):
        _issue(store, snapshot.snapshot_id)
    monkeypatch.setattr(phase6, "_phase6_failure_point", lambda stage: None)
    reopened = Phase6SnapshotGrantStore(path)
    run = run_phase6_snapshot_grants_shadow(enabled=True, database=path)
    assert run.grant_count == 0
    issued = _issue(reopened, snapshot.snapshot_id)
    assert issued.grant.grant_sequence == 1


@pytest.mark.parametrize(
    "stage",
    [
        "after_revoke_lifecycle",
        "after_revoke_current",
        "after_lifecycle_idempotency",
        "before_revoke_commit",
    ],
)
def test_revoke_failure_rolls_back_lifecycle_and_projection(tmp_path, monkeypatch, stage):
    store, path, snapshot = _store_with_snapshot(tmp_path)
    issued = _issue(store, snapshot.snapshot_id)

    def inject(current):
        if current == stage:
            raise KeyboardInterrupt(f"injected:{stage}")

    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(KeyboardInterrupt, match=f"injected:{stage}"):
        store.revoke_grant(
            issued.grant.grant_id,
            actor_id="shadow-issuer",
            actor_generation="issuer-generation-2",
            reason_code="USER_REQUEST",
            effective_at=15,
            idempotency_key="faulted-revoke",
        )
    monkeypatch.setattr(phase6, "_phase6_failure_point", lambda stage: None)
    reopened = Phase6SnapshotGrantStore(path)
    assert reopened.load_grant(
        issued.grant.grant_id
    ).lifecycle_receipt.after_status is GrantStatus.ACTIVE


@pytest.mark.parametrize(
    "stage",
    [
        "after_evaluation_receipt",
        "after_evaluation_current",
        "after_evaluation_idempotency",
        "before_evaluation_commit",
    ],
)
def test_evaluation_failure_rolls_back_receipt_and_time_projection(
    tmp_path, monkeypatch, stage
):
    store, path, snapshot = _store_with_snapshot(tmp_path)
    issued = _issue(store, snapshot.snapshot_id)

    def inject(current):
        if current == stage:
            raise SystemExit(f"injected:{stage}")

    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(SystemExit, match=f"injected:{stage}"):
        _evaluate(store, issued.grant.grant_id)
    monkeypatch.setattr(phase6, "_phase6_failure_point", lambda stage: None)
    reopened = Phase6SnapshotGrantStore(path)
    allowed = _evaluate(reopened, issued.grant.grant_id)
    assert allowed.receipt.decision is EvaluationDecision.ALLOWED_SHADOW


@pytest.mark.parametrize(
    "stage",
    [
        "after_expiry_lifecycle",
        "after_lifecycle_idempotency",
        "after_expiry_current",
        "after_evaluation_receipt",
        "after_evaluation_current",
        "after_evaluation_idempotency",
        "before_evaluation_commit",
    ],
)
def test_expiry_evaluation_failure_rolls_back_one_atomic_transaction(
    tmp_path, monkeypatch, stage
):
    store, path, snapshot = _store_with_snapshot(tmp_path)
    issued = _issue(store, snapshot.snapshot_id)

    def inject(current):
        if current == stage:
            raise KeyboardInterrupt(f"injected:{stage}")

    monkeypatch.setattr(phase6, "_phase6_failure_point", inject)
    with pytest.raises(KeyboardInterrupt, match=f"injected:{stage}"):
        _evaluate(
            store,
            issued.grant.grant_id,
            evaluated_at=50,
            idempotency_key="faulted-expiry",
        )
    monkeypatch.setattr(phase6, "_phase6_failure_point", lambda stage: None)
    reopened = Phase6SnapshotGrantStore(path)
    assert reopened.load_grant(
        issued.grant.grant_id
    ).lifecycle_receipt.after_status is GrantStatus.ACTIVE
    expired = _evaluate(
        reopened,
        issued.grant.grant_id,
        evaluated_at=50,
        idempotency_key="faulted-expiry",
    )
    assert expired.receipt.decision is EvaluationDecision.DENIED_GRANT_EXPIRED


def test_foreign_database_refused_without_mutation(tmp_path):
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE foreign_table(value TEXT)")
    before = path.read_bytes()
    before_stat = path.stat()
    with pytest.raises(Phase6StoreError):
        Phase6SnapshotGrantStore(path).initialize()
    after_stat = path.stat()
    assert path.read_bytes() == before
    assert (after_stat.st_size, after_stat.st_mode) == (
        before_stat.st_size,
        before_stat.st_mode,
    )
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def test_tamper_fails_closed_and_is_not_self_healed(tmp_path):
    store, path, _ = _store_with_snapshot(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DROP TRIGGER phase6_shadow_snapshot_facts_immutable_update"
        )
        connection.execute(
            "UPDATE phase6_shadow_snapshot_facts SET snapshot_json='{}'"
        )
    before = path.read_bytes()
    with pytest.raises(Phase6StoreError):
        store.initialize()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "json_column,sha_column,match",
    [
        ("request_json", "request_sha256", "idempotency request differs"),
        ("result_json", "result_sha256", "idempotency result differs"),
    ],
)
def test_coherently_rehashed_idempotency_payload_tamper_fails_closed(
    tmp_path, json_column, sha_column, match
):
    store, path, _ = _store_with_snapshot(tmp_path)
    trigger_name = "phase6_shadow_idempotency_immutable_update"
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()[0]
        row = connection.execute(
            f"SELECT {json_column} FROM phase6_shadow_idempotency "
            "WHERE idempotency_key='snapshot-request-1'"
        ).fetchone()
        payload = phase6.json.loads(row[0])
        payload["tampered"] = True
        connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(
            f"UPDATE phase6_shadow_idempotency SET {json_column}=?,{sha_column}=? "
            "WHERE idempotency_key='snapshot-request-1'",
            (phase6._canonical_json(payload), canonical_sha256(payload)),
        )
        connection.execute(trigger_sql)
    before = path.read_bytes()
    with pytest.raises(Phase6StoreError, match=match):
        store.initialize()
    assert path.read_bytes() == before


def test_store_rejects_relaxed_mode_without_repairing_it(tmp_path):
    store, path, _ = _store_with_snapshot(tmp_path)
    path.chmod(0o640)
    before = path.read_bytes()
    with pytest.raises(Phase6StoreError, match="mode must be exactly 0600"):
        store.initialize()
    assert path.read_bytes() == before
    assert stat_mode(path) == 0o640


def test_two_concurrent_first_snapshots_have_one_winner(tmp_path):
    path = tmp_path / "concurrent.db"
    Phase6SnapshotGrantStore(path).initialize()

    def submit(content: str):
        try:
            value = Phase6SnapshotGrantStore(path).append_snapshot(
                source_binding=_binding(),
                sections=_sections(content),
                captured_at=10,
                valid_until=100,
                expected_previous_snapshot_id=None,
                idempotency_key=f"concurrent-{content}",
            )
            return value.snapshot.snapshot_id
        except (Phase6SnapshotConflict, Phase6StoreError) as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ("6", "7")))
    winners = [value for value in results if len(value) == 64]
    assert len(winners) == 1
    run = run_phase6_snapshot_grants_shadow(enabled=True, database=path)
    assert run.snapshot_count == 1


def test_identical_concurrent_grant_issue_converges_to_one_fact(tmp_path):
    store, path, snapshot = _store_with_snapshot(tmp_path)

    def submit():
        return _issue(
            Phase6SnapshotGrantStore(path),
            snapshot.snapshot_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert results[0].grant.grant_id == results[1].grant.grant_id
    assert sorted(result.replayed for result in results) == [False, True]
    assert run_phase6_snapshot_grants_shadow(
        enabled=True, database=path
    ).grant_count == 1


def test_concurrent_revoke_vs_expire_has_irreversible_revoked_final_state(tmp_path):
    store, path, snapshot = _store_with_snapshot(tmp_path)
    issued = _issue(store, snapshot.snapshot_id)

    def expire():
        return _evaluate(
            Phase6SnapshotGrantStore(path),
            issued.grant.grant_id,
            evaluated_at=50,
            idempotency_key="concurrent-expire",
        ).receipt.decision

    def revoke():
        return Phase6SnapshotGrantStore(path).revoke_grant(
            issued.grant.grant_id,
            actor_id="shadow-issuer",
            actor_generation="issuer-generation-2",
            reason_code="CONCURRENT_REVOKE",
            effective_at=50,
            idempotency_key="concurrent-revoke",
        ).lifecycle_receipt.after_status

    with ThreadPoolExecutor(max_workers=2) as executor:
        expire_future = executor.submit(expire)
        revoke_future = executor.submit(revoke)
        decision = expire_future.result()
        status = revoke_future.result()
    assert decision in {
        EvaluationDecision.DENIED_GRANT_EXPIRED,
        EvaluationDecision.DENIED_REVOKED,
    }
    assert status is GrantStatus.REVOKED
    assert Phase6SnapshotGrantStore(path).load_grant(
        issued.grant.grant_id
    ).lifecycle_receipt.after_status is GrantStatus.REVOKED


def test_repeated_reopen_does_not_leak_or_double_close_descriptors(tmp_path, monkeypatch):
    store, _, snapshot = _store_with_snapshot(tmp_path)
    before = set(os.listdir("/proc/self/fd"))
    original_close = fd_ownership.OwnedDescriptor.close
    closed_objects = []

    def recording_close(descriptor, owner):
        result = original_close(descriptor, owner)
        if result:
            assert all(descriptor is not item for item in closed_objects)
            closed_objects.append(descriptor)
        return result

    monkeypatch.setattr(fd_ownership.OwnedDescriptor, "close", recording_close)
    for _ in range(20):
        assert store.load_snapshot(snapshot.snapshot_id) == snapshot
    assert closed_objects
    assert len(os.listdir("/proc/self/fd")) <= len(before)
    assert not any(str(store.path) in target for target in _fd_targets())


def test_contract_has_no_effect_dispatch_or_wall_clock_surface():
    source = Path(phase6.__file__).read_text(encoding="utf-8")
    forbidden = (
        "import socket", "import subprocess", "time.time", "datetime.now",
        "connect_authority", "authority_production_writer",
        "dispatch_callback", "effect_callback",
    )
    assert all(token not in source for token in forbidden)


def test_canonical_binding_hash_is_stable_under_optimized_and_hash_seed_modes():
    script = r'''
from factory_core.canonical import canonical_sha256
from factory_core.phase6_snapshot_grants import build_authority_source_binding
h=lambda c:c*64
c={'schema':'authority-workflow-coordinate-v1','workflow_id':'workflow-1','project_id':'project-1','project_generation':'project-generation-1','run_generation':'run-generation-1','runtime_generation':'runtime-generation-1','scheduler_generation':'scheduler-generation-1','current_revision':1,'contract_pin_set_sha256':h('a'),'authority_state':'active','source_fence_sha256':h('b'),'switch_mode':'shadow','switch_epoch':3}
s={'schema_version':'snapshot-coordinate-v0','project_id':'project-1','workflow_schema_version':1,'project_revision':1,'project_generation':'project-generation-1','run_generation':'run-generation-1','runtime_generation':'runtime-generation-1','scheduler_generation':'scheduler-generation-1','recorded_contract_pin_set_sha256':h('a')}
print(build_authority_source_binding(authority_coordinate=c,authority_coordinate_sha256=canonical_sha256(c),authority_revision_snapshot_sha256=h('1'),authority_revision_through_revision=1,source_snapshot_schema='project-snapshot-v0-source-authorized-v3',source_snapshot_semantic_sha256=h('2'),source_snapshot_completeness='COMPLETE',source_snapshot_coordinate=s,phase3_artifact_state_sha256=h('3'),phase4_operation_state_sha256=h('4'),phase5_supervisor_state_sha256=h('5')).binding_sha256)
'''
    results = set()
    for optimized in (False, True):
        for seed in ("0", "7", "321"):
            command = [sys.executable]
            if optimized:
                command.append("-O")
            command.extend(("-c", script))
            environment = dict(os.environ)
            environment["PYTHONHASHSEED"] = seed
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                command,
                check=True,
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
            )
            results.add(completed.stdout.strip())
    assert len(results) == 1
    assert len(results.pop()) == 64
