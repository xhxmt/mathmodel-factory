"""Deterministic F1 tests for Phase-4/5 descriptor ownership transfer.

These tests deliberately assert the lifecycle, exception identity and disk
postconditions.  A passing return value alone is not sufficient evidence for
the creator/cleanup/connection ownership contract.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import inspect
import os
from pathlib import Path
import stat
import sys
import threading

import pytest

import factory_core.fd_ownership as ownership
import factory_core.phase4_shadow_runtime as phase4
import factory_core.phase5_shadow_supervisor as phase5


PHASE4_PRECOMMIT_STAGES = (
    "after_creator_open",
    "before_cleanup_dup",
    "after_cleanup_dup",
    "before_connect",
    "after_connect_before_transfer",
    "after_connection_transfer",
    "before_schema",
    "after_schema_before_commit",
    "before_commit",
)
PHASE4_COMMITTED_STAGES = (
    "before_parent_fsync",
    "after_parent_fsync",
    "after_commit",
    "before_final_preflight",
    "after_final_preflight",
)
PHASE5_PRECOMMIT_STAGES = (
    "after_database_create",
    "before_database_cleanup_dup",
    "after_database_cleanup_dup",
    "before_marker_create",
    "after_marker_create",
    "before_marker_cleanup_dup",
    "after_marker_cleanup_dup",
    "before_marker_write",
    "after_marker_write",
    "before_parent_dup",
    "after_parent_dup",
    "before_connect",
    "after_connect_before_transfer",
    "after_connection_transfer",
    "before_schema",
    "after_schema_before_commit",
    "before_commit",
)
PHASE5_COMMITTED_STAGES = (
    "before_parent_fsync",
    "after_parent_fsync",
    "after_commit",
    "before_final_preflight",
    "after_final_preflight",
)


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _interrupting_line_trace(
    function,
    fragment: str,
    exception: BaseException,
    *,
    runtime_hit: int = 1,
    source_hit: int = 1,
):
    """Raise one exact exception at a selected Python source line."""

    function = getattr(function, "__func__", function)
    lines, start = inspect.getsourcelines(function)
    matches = [
        start + offset for offset, line in enumerate(lines) if fragment in line
    ]
    assert len(matches) >= source_hit
    target = matches[source_hit - 1]
    state = {"hits": 0, "raised": False}

    def trace(frame, event, argument):
        del argument
        if event == "line" and frame.f_code is function.__code__ and frame.f_lineno == target:
            state["hits"] += 1
            if state["hits"] == runtime_hit:
                state["raised"] = True
                sys.settrace(None)
                raise exception
        return trace

    return trace, state


def _assert_no_phase4_residue(database: Path) -> None:
    assert not database.exists()
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(f"{database}{suffix}").exists()
    assert list(database.parent.glob(f".{database.name}.phase4-cleanup-*")) == []


def _assert_no_phase5_residue(database: Path) -> None:
    assert not database.exists()
    assert not Path(f"{database}.phase5-owner.json").exists()
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(f"{database}{suffix}").exists()
    assert list(database.parent.glob(".*.phase5-cleanup-*")) == []


def _assert_phase4_restartable(database: Path) -> None:
    assert database.exists() and database.stat().st_size > 0
    store = phase4.Phase4ShadowStore(database)
    store.initialize()
    assert set(store.table_counts().values()) == {0}


def _assert_phase5_restartable(database: Path) -> None:
    marker = Path(f"{database}.phase5-owner.json")
    assert database.exists() and database.stat().st_size > 0
    assert marker.exists() and marker.stat().st_size > 0
    store = phase5.Phase5SupervisorStore(database)
    store.initialize()
    assert set(store.table_counts().values()) == {0}


@pytest.mark.parametrize("error_number", (errno.EMFILE, errno.ENFILE))
def test_phase4_first_dup_failure_has_no_leak_or_empty_database(
    tmp_path, monkeypatch, error_number
):
    database = (tmp_path / f"phase4-first-dup-{error_number}.sqlite").resolve()
    before = _fd_count()
    injected = OSError(error_number, "injected first dup failure")

    def fail_dup(_descriptor):
        raise injected

    monkeypatch.setattr(phase4.os, "dup", fail_dup)
    with pytest.raises(phase4.Phase4ShadowStoreError) as captured:
        phase4.Phase4ShadowStore(database).initialize()
    assert captured.value.__cause__ is injected
    assert _fd_count() == before
    _assert_no_phase4_residue(database)


@pytest.mark.parametrize(
    ("dup_index", "error_number"),
    ((1, errno.EMFILE), (2, errno.ENFILE), (3, errno.EMFILE)),
)
def test_phase5_each_dup_failure_has_no_leak_database_or_orphan_marker(
    tmp_path, monkeypatch, dup_index, error_number
):
    database = (tmp_path / f"phase5-dup-{dup_index}.sqlite").resolve()
    before = _fd_count()
    real_dup = os.dup
    calls = 0
    injected = OSError(error_number, f"injected dup {dup_index} failure")

    def fail_selected(descriptor):
        nonlocal calls
        calls += 1
        if calls == dup_index:
            raise injected
        return real_dup(descriptor)

    monkeypatch.setattr(phase5.os, "dup", fail_selected)
    with pytest.raises(phase5.Phase5SupervisorStoreError) as captured:
        phase5.Phase5SupervisorStore(database).initialize()
    assert captured.value.__cause__ is injected
    assert calls == dup_index
    assert _fd_count() == before
    _assert_no_phase5_residue(database)


@pytest.mark.parametrize("stage", PHASE4_PRECOMMIT_STAGES + PHASE4_COMMITTED_STAGES)
@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_phase4_baseexception_at_every_initialization_window_is_atomic(
    tmp_path, monkeypatch, stage, kind
):
    database = (tmp_path / f"phase4-{kind.__name__}-{stage}.sqlite").resolve()
    before = _fd_count()
    injected = kind(f"phase4 injected at {stage}")

    def fail(selected):
        if selected == stage:
            raise injected

    monkeypatch.setattr(phase4, "_phase4_initialization_failure_point", fail)
    with pytest.raises(kind) as captured:
        phase4.Phase4ShadowStore(database).initialize()
    assert captured.value is injected
    assert str(captured.value) == f"phase4 injected at {stage}"
    assert _fd_count() == before
    if stage in PHASE4_COMMITTED_STAGES:
        _assert_phase4_restartable(database)
    else:
        _assert_no_phase4_residue(database)


@pytest.mark.parametrize("stage", PHASE5_PRECOMMIT_STAGES + PHASE5_COMMITTED_STAGES)
@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_phase5_baseexception_at_every_initialization_window_is_atomic(
    tmp_path, monkeypatch, stage, kind
):
    database = (tmp_path / f"phase5-{kind.__name__}-{stage}.sqlite").resolve()
    before = _fd_count()
    injected = kind(f"phase5 injected at {stage}")

    def fail(selected):
        if selected == stage:
            raise injected

    monkeypatch.setattr(phase5, "_phase5_initialization_failure_point", fail)
    with pytest.raises(kind) as captured:
        phase5.Phase5SupervisorStore(database).initialize()
    assert captured.value is injected
    assert str(captured.value) == f"phase5 injected at {stage}"
    assert _fd_count() == before
    if stage in PHASE5_COMMITTED_STAGES:
        _assert_phase5_restartable(database)
    else:
        _assert_no_phase5_residue(database)


def _install_close_lifecycle_recorder(monkeypatch, root: Path):
    real_open = os.open
    real_dup = os.dup
    active: dict[int, int] = {}
    ever: set[int] = set()
    duplicate_close_attempts: list[int] = []
    generation = 0

    def is_scoped(descriptor: int) -> bool:
        try:
            target = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            return False
        return target == str(root) or target.startswith(f"{root}/")

    def tracked_open(*args, **kwargs):
        nonlocal generation
        descriptor = real_open(*args, **kwargs)
        if is_scoped(descriptor):
            generation += 1
            active[descriptor] = generation
            ever.add(descriptor)
        return descriptor

    def tracked_dup(descriptor):
        nonlocal generation
        duplicate = real_dup(descriptor)
        if descriptor in active or is_scoped(duplicate):
            generation += 1
            active[duplicate] = generation
            ever.add(duplicate)
        return duplicate

    def tracked_close(stage, descriptor):
        if stage != "before":
            return
        if descriptor in ever and descriptor not in active:
            duplicate_close_attempts.append(descriptor)
        active.pop(descriptor, None)

    monkeypatch.setattr(ownership.os, "open", tracked_open)
    monkeypatch.setattr(ownership.os, "dup", tracked_dup)
    monkeypatch.setattr(ownership, "_descriptor_close_failure_point", tracked_close)
    return active, duplicate_close_attempts


@pytest.mark.parametrize("phase", (4, 5))
def test_failure_after_connection_transfer_closes_each_acquisition_once(
    tmp_path, monkeypatch, phase
):
    active, duplicate_close_attempts = _install_close_lifecycle_recorder(
        monkeypatch, tmp_path
    )
    injected = KeyboardInterrupt(f"phase{phase} transfer interruption")
    if phase == 4:
        database = (tmp_path / "phase4-close-once.sqlite").resolve()

        def fail(stage):
            if stage == "after_connection_transfer":
                raise injected

        monkeypatch.setattr(phase4, "_phase4_initialization_failure_point", fail)
        with pytest.raises(KeyboardInterrupt) as captured:
            phase4.Phase4ShadowStore(database).initialize()
        _assert_no_phase4_residue(database)
    else:
        database = (tmp_path / "phase5-close-once.sqlite").resolve()

        def fail(stage):
            if stage == "after_connection_transfer":
                raise injected

        monkeypatch.setattr(phase5, "_phase5_initialization_failure_point", fail)
        with pytest.raises(KeyboardInterrupt) as captured:
            phase5.Phase5SupervisorStore(database).initialize()
        _assert_no_phase5_residue(database)
    assert captured.value is injected
    assert duplicate_close_attempts == []
    assert active == {}


@pytest.mark.parametrize(
    ("phase", "transfer_index"), ((4, 1), (5, 1), (5, 2), (5, 3))
)
@pytest.mark.parametrize("timing", ("before", "after"))
@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_baseexception_during_each_owner_token_transfer_is_atomic(
    tmp_path, monkeypatch, phase, transfer_index, timing, kind
):
    database = (tmp_path / f"phase{phase}-transfer-{transfer_index}-{timing}.sqlite").resolve()
    before = _fd_count()
    original_transfer = ownership.OwnedDescriptor.transfer
    injected = kind(f"phase{phase} transfer {transfer_index} {timing}")
    calls = 0

    def fail_selected(self, *, owner, new_owner):
        nonlocal calls
        if new_owner.startswith(f"phase{phase}-connection"):
            calls += 1
            if calls == transfer_index and timing == "before":
                raise injected
            original_transfer(self, owner=owner, new_owner=new_owner)
            if calls == transfer_index and timing == "after":
                raise injected
            return None
        return original_transfer(self, owner=owner, new_owner=new_owner)

    monkeypatch.setattr(ownership.OwnedDescriptor, "transfer", fail_selected)
    with pytest.raises(kind) as captured:
        if phase == 4:
            phase4.Phase4ShadowStore(database).initialize()
        else:
            phase5.Phase5SupervisorStore(database).initialize()
    assert captured.value is injected
    assert calls == transfer_index
    assert _fd_count() == before
    if phase == 4:
        _assert_no_phase4_residue(database)
    else:
        _assert_no_phase5_residue(database)


@pytest.mark.parametrize("phase", (4, 5))
@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_database_connect_baseexception_cleans_all_creator_state(
    tmp_path, monkeypatch, phase, kind
):
    database = (tmp_path / f"phase{phase}-connect-{kind.__name__}.sqlite").resolve()
    before = _fd_count()
    module = phase4 if phase == 4 else phase5
    real_connect = module.sqlite3.connect
    injected = kind(f"phase{phase} connect interruption")

    def fail_rw_connect(target, *args, **kwargs):
        if "mode=rw" in str(target) and kwargs.get("factory") is not None:
            raise injected
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", fail_rw_connect)
    with pytest.raises(kind) as captured:
        if phase == 4:
            phase4.Phase4ShadowStore(database).initialize()
        else:
            phase5.Phase5SupervisorStore(database).initialize()
    assert captured.value is injected
    assert _fd_count() == before
    if phase == 4:
        _assert_no_phase4_residue(database)
    else:
        _assert_no_phase5_residue(database)


def test_phase5_marker_open_failure_removes_exclusive_database(tmp_path, monkeypatch):
    database = (tmp_path / "phase5-marker-open-failure.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")
    before = _fd_count()
    real_open = os.open
    injected = OSError(errno.ENOSPC, "injected marker creation failure")

    def fail_marker(path, flags, *args, **kwargs):
        if (
            flags & os.O_CREAT
            and os.fspath(path) in {os.fspath(marker), marker.name}
        ):
            raise injected
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(phase5.os, "open", fail_marker)
    with pytest.raises(phase5.Phase5SupervisorStoreError) as captured:
        phase5.Phase5SupervisorStore(database).initialize()
    assert captured.value.__cause__ is injected
    assert _fd_count() == before
    _assert_no_phase5_residue(database)


@pytest.mark.parametrize("phase", (4, 5))
@pytest.mark.parametrize("timing", ("before", "after"))
def test_cleanup_rename_failure_is_retained_but_does_not_leave_partial_state(
    tmp_path, monkeypatch, phase, timing
):
    database = (tmp_path / f"phase{phase}-rename-{timing}.sqlite").resolve()
    before = _fd_count()
    module = phase4 if phase == 4 else phase5
    original_rename = module._rename_noreplace
    rename_failure = OSError(errno.EIO, f"phase{phase} rename {timing} failure")
    primary = KeyboardInterrupt(f"phase{phase} schema interruption")
    calls = 0

    def fail_first_rename(*args):
        nonlocal calls
        calls += 1
        if calls == 1 and timing == "before":
            raise rename_failure
        original_rename(*args)
        if calls == 1 and timing == "after":
            raise rename_failure

    monkeypatch.setattr(module, "_rename_noreplace", fail_first_rename)
    if phase == 4:
        def fail(stage):
            if stage == "before_schema":
                raise primary

        monkeypatch.setattr(phase4, "_phase4_initialization_failure_point", fail)
        with pytest.raises(KeyboardInterrupt) as captured:
            phase4.Phase4ShadowStore(database).initialize()
        _assert_no_phase4_residue(database)
    else:
        def fail(stage):
            if stage == "before_schema":
                raise primary

        monkeypatch.setattr(phase5, "_phase5_initialization_failure_point", fail)
        with pytest.raises(KeyboardInterrupt) as captured:
            phase5.Phase5SupervisorStore(database).initialize()
        _assert_no_phase5_residue(database)
    assert captured.value is primary
    assert calls >= 1
    retained = getattr(primary, "__factory_cleanup_failures__")
    assert rename_failure in [failure.error for failure in retained]
    assert _fd_count() == before


@pytest.mark.parametrize("phase", (4, 5))
def test_primary_and_close_unlink_failures_are_all_retained_and_cleanup_completes(
    tmp_path, monkeypatch, phase
):
    database = (tmp_path / f"phase{phase}-cleanup-failures.sqlite").resolve()
    before = _fd_count()
    primary = KeyboardInterrupt(f"phase{phase} primary")
    close_failure = OSError(errno.EIO, f"phase{phase} close cleanup failure")
    unlink_failure = OSError(errno.EACCES, f"phase{phase} unlink cleanup failure")
    close_injected = False
    unlink_injected = False

    def fail_one_close(stage, descriptor):
        nonlocal close_injected
        try:
            target = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            target = ""
        if stage == "before" and not close_injected and str(database) in target:
            close_injected = True
            raise close_failure

    def fail_one_unlink(stage, _parent_fd, name):
        nonlocal unlink_injected
        if (
            stage == "before"
            and not unlink_injected
            and "cleanup-" in os.fspath(name)
        ):
            unlink_injected = True
            raise unlink_failure

    monkeypatch.setattr(ownership, "_descriptor_close_failure_point", fail_one_close)
    monkeypatch.setattr(ownership, "_unlink_failure_point", fail_one_unlink)
    if phase == 4:
        def fail(stage):
            if stage == "after_connection_transfer":
                raise primary

        monkeypatch.setattr(phase4, "_phase4_initialization_failure_point", fail)
        with pytest.raises(KeyboardInterrupt) as captured:
            phase4.Phase4ShadowStore(database).initialize()
        _assert_no_phase4_residue(database)
    else:
        def fail(stage):
            if stage == "after_connection_transfer":
                raise primary

        monkeypatch.setattr(phase5, "_phase5_initialization_failure_point", fail)
        with pytest.raises(KeyboardInterrupt) as captured:
            phase5.Phase5SupervisorStore(database).initialize()
        _assert_no_phase5_residue(database)
    assert captured.value is primary
    assert close_injected is True
    assert unlink_injected is True
    failures = getattr(primary, "__factory_cleanup_failures__")
    assert close_failure in [failure.error for failure in failures]
    assert unlink_failure in [failure.error for failure in failures]
    assert _fd_count() == before


def test_owned_descriptor_transfer_has_exactly_one_current_owner(monkeypatch, tmp_path):
    path = tmp_path / "lease.txt"
    path.write_bytes(b"lease")
    descriptor = os.open(path, os.O_RDONLY)
    close_calls = []
    def record_close(stage, value):
        if stage == "before":
            close_calls.append(value)

    monkeypatch.setattr(ownership, "_descriptor_close_failure_point", record_close)
    lease = ownership.OwnedDescriptor(descriptor, owner="creator", label="test")
    lease.transfer(owner="creator", new_owner="connection")
    assert lease.close("creator") is False
    assert lease.close("connection") is True
    assert lease.close("connection") is False
    assert close_calls == [descriptor]


@pytest.mark.parametrize("phase", (4, 5))
def test_concurrent_exclusive_creation_has_only_valid_outcomes_and_restarts(
    tmp_path, phase
):
    database = (tmp_path / f"phase{phase}-concurrent.sqlite").resolve()
    barrier = threading.Barrier(2)

    def initialize_once():
        barrier.wait(timeout=5)
        store = (
            phase4.Phase4ShadowStore(database)
            if phase == 4
            else phase5.Phase5SupervisorStore(database)
        )
        try:
            store.initialize()
        except (phase4.Phase4ShadowStoreError, phase5.Phase5SupervisorStoreError):
            return "exclusive-loser"
        return "initialized"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _value: initialize_once(), range(2)))
    assert "initialized" in results
    assert set(results) <= {"initialized", "exclusive-loser"}
    if phase == 4:
        _assert_phase4_restartable(database)
    else:
        _assert_phase5_restartable(database)


@pytest.mark.parametrize("phase", (4, 5))
@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_creator_constructor_midpoint_interrupt_closes_once_and_unlinks(
    tmp_path, monkeypatch, phase, kind
):
    database = (tmp_path / f"phase{phase}-constructor-{kind.__name__}.sqlite").resolve()
    before = _fd_count()
    injected = kind(f"phase{phase} constructor midpoint")
    trace, state = _interrupting_line_trace(
        ownership.OwnedDescriptor.__init__,
        "self._owner = owner",
        injected,
        runtime_hit=2,
    )
    sys.settrace(trace)
    try:
        with pytest.raises(kind) as captured:
            if phase == 4:
                phase4.Phase4ShadowStore(database).initialize()
            else:
                phase5.Phase5SupervisorStore(database).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert captured.value is injected
    assert _fd_count() == before
    if phase == 4:
        _assert_no_phase4_residue(database)
    else:
        _assert_no_phase5_residue(database)


@pytest.mark.parametrize(
    ("phase", "fragment", "source_hit"),
    (
        (4, "creator_raw = -1", 2),
        (5, "database_raw = -1", 2),
        (5, "marker_raw = -1", 2),
    ),
)
def test_raw_alias_release_line_interrupt_never_double_closes_or_leaves_state(
    tmp_path, monkeypatch, phase, fragment, source_hit
):
    database = (tmp_path / f"phase{phase}-{fragment.split()[0]}.sqlite").resolve()
    before = _fd_count()
    injected = KeyboardInterrupt(f"interrupt before {fragment}")
    function = (
        phase4.Phase4ShadowStore._initialize_new
        if phase == 4
        else phase5.Phase5SupervisorStore._initialize_new
    )
    trace, state = _interrupting_line_trace(
        function, fragment, injected, source_hit=source_hit
    )
    close_calls: list[int] = []
    def recording_close(stage, descriptor):
        if stage == "before":
            close_calls.append(descriptor)

    monkeypatch.setattr(
        ownership, "_descriptor_close_failure_point", recording_close
    )
    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt) as captured:
            if phase == 4:
                phase4.Phase4ShadowStore(database).initialize()
            else:
                phase5.Phase5SupervisorStore(database).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert captured.value is injected
    assert len(close_calls) == len(set(close_calls))
    assert _fd_count() == before
    if phase == 4:
        _assert_no_phase4_residue(database)
    else:
        _assert_no_phase5_residue(database)


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_owned_descriptor_close_line_interrupt_is_retry_safe(tmp_path, kind):
    path = tmp_path / f"close-line-{kind.__name__}.txt"
    path.write_bytes(b"owned")
    descriptor = os.open(path, os.O_RDONLY)
    lease = ownership.OwnedDescriptor(descriptor, owner="owner", label="line close")
    injected = kind("close line interrupt")
    trace, state = _interrupting_line_trace(
        ownership.OwnedDescriptor.close,
        "close_descriptor_once(descriptor, _state=close_state)",
        injected,
    )
    sys.settrace(trace)
    try:
        with pytest.raises(kind) as captured:
            lease.close("owner")
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert captured.value is injected
    assert lease.closed is True
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert lease.close("owner") is False


@pytest.mark.parametrize("phase", (4, 5))
@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_connection_close_line_interrupt_cleans_anchors_and_allows_retry(
    tmp_path, phase, kind
):
    database = (tmp_path / f"phase{phase}-connection-close.sqlite").resolve()
    store = (
        phase4.Phase4ShadowStore(database)
        if phase == 4
        else phase5.Phase5SupervisorStore(database)
    )
    store.initialize()
    before = _fd_count()
    connection = store._connect()
    function = type(connection).close
    injected = kind(f"phase{phase} connection close line")
    trace, state = _interrupting_line_trace(function, "super().close()", injected)
    sys.settrace(trace)
    try:
        with pytest.raises(kind) as captured:
            connection.close()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert captured.value is injected
    if phase == 4:
        assert connection._anchor_lease is None
    else:
        assert connection._database_lease is None
        assert connection._marker_lease is None
        assert connection._parent_lease is None
    connection.close()
    assert _fd_count() == before


def test_completed_close_never_closes_a_reused_foreign_descriptor(
    tmp_path, monkeypatch
):
    owned_path = tmp_path / "owned-close.txt"
    foreign_path = tmp_path / "foreign-close.txt"
    owned_path.write_bytes(b"owned")
    foreign_path.write_bytes(b"foreign")
    descriptor = os.open(owned_path, os.O_RDONLY)
    lease = ownership.OwnedDescriptor(descriptor, owner="owner", label="owned")
    injected = OSError(errno.EIO, "after close diagnostic")
    foreign_descriptor = -1

    def after_close(stage, closed_descriptor):
        nonlocal foreign_descriptor
        if stage == "after":
            foreign_descriptor = os.open(foreign_path, os.O_RDONLY)
            assert foreign_descriptor == closed_descriptor
            raise injected

    monkeypatch.setattr(ownership, "_descriptor_close_failure_point", after_close)
    with pytest.raises(OSError) as captured:
        lease.close("owner")
    assert captured.value is injected
    assert lease.closed is True
    assert lease.close("owner") is False
    assert os.read(foreign_descriptor, 7) == b"foreign"
    os.close(foreign_descriptor)


def test_completed_unlink_never_deletes_same_name_foreign_replacement(
    tmp_path, monkeypatch
):
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    name = "owned-entry"
    (tmp_path / name).write_bytes(b"owned")
    injected = OSError(errno.EIO, "after unlink diagnostic")

    def after_unlink(stage, _parent_fd, entry):
        if stage == "after":
            (tmp_path / entry).write_bytes(b"foreign")
            raise injected

    monkeypatch.setattr(ownership, "_unlink_failure_point", after_unlink)
    try:
        with pytest.raises(OSError) as captured:
            ownership.resilient_unlink_at(parent_fd, name)
        assert captured.value is injected
        assert (tmp_path / name).read_bytes() == b"foreign"
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("phase", (4, 5))
def test_pre_unlink_failure_never_deletes_foreign_quarantine_replacement(
    tmp_path, monkeypatch, phase
):
    database = (tmp_path / f"phase{phase}-unlink-replacement.sqlite").resolve()
    primary = KeyboardInterrupt("primary initialization interruption")
    secondary = SystemExit("pre-unlink replacement interruption")
    foreign_bytes = b"foreign quarantine replacement"
    replaced_name: str | None = None

    def replace_before_unlink(stage, parent_fd, name):
        nonlocal replaced_name
        if stage != "before" or replaced_name is not None or "cleanup-" not in name:
            return
        os.unlink(name, dir_fd=parent_fd)
        foreign_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(foreign_fd, foreign_bytes)
        finally:
            os.close(foreign_fd)
        replaced_name = name
        raise secondary

    def fail(stage):
        if stage == "before_schema":
            raise primary

    module = phase4 if phase == 4 else phase5
    hook = (
        "_phase4_initialization_failure_point"
        if phase == 4
        else "_phase5_initialization_failure_point"
    )
    monkeypatch.setattr(module, hook, fail)
    monkeypatch.setattr(ownership, "_unlink_failure_point", replace_before_unlink)
    with pytest.raises(KeyboardInterrupt) as captured:
        if phase == 4:
            phase4.Phase4ShadowStore(database).initialize()
        else:
            phase5.Phase5SupervisorStore(database).initialize()
    assert captured.value is primary
    assert replaced_name is not None
    replacement = tmp_path / replaced_name
    assert replacement.read_bytes() == foreign_bytes
    retained = getattr(primary, "__factory_cleanup_failures__")
    assert secondary in [failure.error for failure in retained]
    assert not database.exists()
    assert not Path(f"{database}.phase5-owner.json").exists()
    replacement.unlink()


@pytest.mark.parametrize("phase", (4, 5))
def test_exclusive_creation_is_anchored_across_parent_swap(
    tmp_path, monkeypatch, phase
):
    parent = (tmp_path / f"phase{phase}-parent").resolve()
    parent.mkdir()
    displaced = (tmp_path / f"phase{phase}-displaced").resolve()
    database = parent / "owned.sqlite"
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and flags & os.O_CREAT and flags & os.O_EXCL:
            swapped = True
            parent.replace(displaced)
            parent.mkdir()
        return real_open(path, flags, *args, **kwargs)

    module = phase4 if phase == 4 else phase5
    monkeypatch.setattr(module.os, "open", swapping_open)
    error_type = (
        phase4.Phase4ShadowStoreError
        if phase == 4
        else phase5.Phase5SupervisorStoreError
    )
    with pytest.raises(error_type):
        if phase == 4:
            phase4.Phase4ShadowStore(database).initialize()
        else:
            phase5.Phase5SupervisorStore(database).initialize()
    assert swapped is True
    assert list(parent.iterdir()) == []
    assert list(displaced.iterdir()) == []


@pytest.mark.parametrize("phase", (4, 5))
def test_cleanup_uses_old_parent_anchor_and_preserves_replacement_entry(
    tmp_path, monkeypatch, phase
):
    parent = (tmp_path / f"phase{phase}-post-parent").resolve()
    parent.mkdir()
    displaced = (tmp_path / f"phase{phase}-post-displaced").resolve()
    database = parent / "owned.sqlite"
    foreign_bytes = b"foreign replacement"
    primary = KeyboardInterrupt("post-create parent swap")

    def fail(stage):
        target = "after_creator_open" if phase == 4 else "after_database_create"
        if stage != target:
            return
        parent.replace(displaced)
        parent.mkdir()
        database.write_bytes(foreign_bytes)
        raise primary

    module = phase4 if phase == 4 else phase5
    hook = (
        "_phase4_initialization_failure_point"
        if phase == 4
        else "_phase5_initialization_failure_point"
    )
    monkeypatch.setattr(module, hook, fail)
    with pytest.raises(KeyboardInterrupt) as captured:
        if phase == 4:
            phase4.Phase4ShadowStore(database).initialize()
        else:
            phase5.Phase5SupervisorStore(database).initialize()
    assert captured.value is primary
    assert database.read_bytes() == foreign_bytes
    assert not (displaced / database.name).exists()
    if phase == 5:
        assert not (displaced / f"{database.name}.phase5-owner.json").exists()


@pytest.mark.parametrize("phase", (4, 5))
def test_reconcile_secondary_baseexception_is_attached_to_original(
    tmp_path, monkeypatch, phase
):
    database = (tmp_path / f"phase{phase}-reconcile-secondary.sqlite").resolve()
    module = phase4 if phase == 4 else phase5
    primary = KeyboardInterrupt("primary after commit")
    secondary = SystemExit("secondary reconcile connect")
    original_connect = module.sqlite3.connect
    committed = False

    def fail(stage):
        nonlocal committed
        if stage == "after_commit":
            committed = True
            raise primary

    def connect(target, *args, **kwargs):
        if committed and "mode=ro&immutable=1" in str(target):
            raise secondary
        return original_connect(target, *args, **kwargs)

    hook = (
        "_phase4_initialization_failure_point"
        if phase == 4
        else "_phase5_initialization_failure_point"
    )
    monkeypatch.setattr(module, hook, fail)
    monkeypatch.setattr(module.sqlite3, "connect", connect)
    with pytest.raises(KeyboardInterrupt) as captured:
        if phase == 4:
            phase4.Phase4ShadowStore(database).initialize()
        else:
            phase5.Phase5SupervisorStore(database).initialize()
    assert captured.value is primary
    retained = getattr(primary, "__factory_cleanup_failures__")
    assert secondary in [failure.error for failure in retained]


@pytest.mark.parametrize("phase", (4, 5))
def test_success_and_committed_recovery_fsync_parent_directory(
    tmp_path, monkeypatch, phase
):
    database = (tmp_path / f"phase{phase}-directory-fsync.sqlite").resolve()
    module = phase4 if phase == 4 else phase5
    original_fsync = module.os.fsync
    directory_calls = 0

    def recording_fsync(descriptor):
        nonlocal directory_calls
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_calls += 1
        return original_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", recording_fsync)
    if phase == 4:
        phase4.Phase4ShadowStore(database).initialize()
    else:
        phase5.Phase5SupervisorStore(database).initialize()
    assert directory_calls >= 1


def test_cleanup_logger_cannot_replace_primary_exception(monkeypatch):
    primary = KeyboardInterrupt("primary")
    cleanup = OSError(errno.EIO, "cleanup")

    def bad_cleanup():
        raise cleanup

    def bad_logger(*_args, **_kwargs):
        raise SystemExit("logger failure")

    monkeypatch.setattr(ownership.LOGGER, "error", bad_logger)
    try:
        try:
            raise primary
        except BaseException as caught:
            ownership.run_cleanup([("bad cleanup", bad_cleanup)], primary=caught)
            raise
    except BaseException as captured:
        assert captured is primary
    retained = getattr(primary, "__factory_cleanup_failures__")
    assert cleanup in [failure.error for failure in retained]
