"""Replay the five historical F1 counterexamples on the release candidate.

This module is intentionally self-contained evidence.  Each test names the
historical failure, injects at the repaired boundary, and asserts descriptor,
exception, and filesystem state.  The formal audit records the exact pytest
commands and environment used to run each probe from this frozen source.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import inspect
import os
from pathlib import Path
import sqlite3
import sys

import pytest

import factory_core.fd_ownership as ownership
import factory_core.phase4_shadow_runtime as phase4
import factory_core.phase5_shadow_supervisor as phase5


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _one_line_interrupt(
    function,
    fragment: str,
    error: BaseException,
    *,
    source_hit: int = 1,
):
    """Return a trace that raises ``error`` once at one exact source line."""

    function = getattr(function, "__func__", function)
    lines, start = inspect.getsourcelines(function)
    matches = [
        start + offset for offset, line in enumerate(lines) if fragment in line
    ]
    assert len(matches) >= source_hit, (function, fragment, matches, source_hit)
    target = matches[source_hit - 1]
    state = {"raised": False, "line": target}

    def trace(frame, event, argument):
        del argument
        if (
            event == "line"
            and frame.f_code is function.__code__
            and frame.f_lineno == target
            and not state["raised"]
        ):
            state["raised"] = True
            sys.settrace(None)
            raise error
        return trace

    return trace, state


def _cleanup_errors(primary: BaseException) -> list[BaseException]:
    return [
        failure.error
        for failure in getattr(primary, "__factory_cleanup_failures__", ())
    ]


def _assert_no_residue(database: Path, phase: int) -> None:
    assert not database.exists()
    assert not Path(f"{database}.phase5-owner.json").exists()
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(f"{database}{suffix}").exists()
    pattern = (
        f".{database.name}.phase4-cleanup-*"
        if phase == 4
        else ".*.phase5-cleanup-*"
    )
    assert list(database.parent.glob(pattern)) == []


def _store(database: Path, phase: int):
    if phase == 4:
        return phase4.Phase4ShadowStore(database)
    return phase5.Phase5SupervisorStore(database)


def _assert_restartable(database: Path, phase: int) -> None:
    store = _store(database, phase)
    store.initialize()
    assert database.exists() and database.stat().st_size > 0
    assert set(store.table_counts().values()) == {0}
    if phase == 5:
        marker = Path(f"{database}.phase5-owner.json")
        assert marker.exists() and marker.stat().st_size > 0


@pytest.mark.parametrize("phase", (4, 5))
def test_historical_foreign_quarantine_replacement_is_not_unlinked(
    tmp_path, monkeypatch, phase
):
    """Probe 1: a foreign replacement survives pre-unlink interruption."""

    database = (tmp_path / f"phase{phase}-foreign.sqlite").resolve()
    primary = KeyboardInterrupt("historical primary initialization failure")
    secondary = SystemExit("historical pre-unlink replacement failure")
    foreign = b"foreign quarantine replacement"
    replacement_name: str | None = None

    def initialization_failure(stage: str) -> None:
        if stage == "before_schema":
            raise primary

    def replace_quarantine(stage: str, parent_fd: int, name: str) -> None:
        nonlocal replacement_name
        if stage != "before" or replacement_name is not None or "cleanup-" not in name:
            return
        os.unlink(name, dir_fd=parent_fd)
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(descriptor, foreign)
        finally:
            os.close(descriptor)
        replacement_name = name
        raise secondary

    module = phase4 if phase == 4 else phase5
    hook = (
        "_phase4_initialization_failure_point"
        if phase == 4
        else "_phase5_initialization_failure_point"
    )
    monkeypatch.setattr(module, hook, initialization_failure)
    monkeypatch.setattr(ownership, "_unlink_failure_point", replace_quarantine)

    with pytest.raises(KeyboardInterrupt) as captured:
        _store(database, phase).initialize()
    assert captured.value is primary
    assert secondary in _cleanup_errors(primary)
    assert replacement_name is not None
    replacement = database.parent / replacement_name
    assert replacement.read_bytes() == foreign
    assert not database.exists()
    assert not Path(f"{database}.phase5-owner.json").exists()
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(f"{database}{suffix}").exists()
    cleanup_pattern = (
        f".{database.name}.phase4-cleanup-*"
        if phase == 4
        else ".*.phase5-cleanup-*"
    )
    assert list(database.parent.glob(cleanup_pattern)) == [replacement]

    # The foreign file is test-owned and is removed only after survival was
    # proved.  Production cleanup never receives this operation.
    replacement.unlink()
    _assert_no_residue(database, phase)


@pytest.mark.parametrize("phase", (4, 5))
def test_historical_connection_close_entry_interrupt_releases_all_anchors(
    tmp_path, monkeypatch, phase
):
    """Probe 2: an entry-line BaseException is recovered by cleanup."""

    database = (tmp_path / f"phase{phase}-connection-entry.sqlite").resolve()
    module = phase4 if phase == 4 else phase5
    primary = KeyboardInterrupt("historical post-transfer failure")
    secondary = SystemExit("historical connection close entry failure")
    before = _fd_count()

    def initialization_failure(stage: str) -> None:
        if stage == "after_connection_transfer":
            raise primary

    hook = (
        "_phase4_initialization_failure_point"
        if phase == 4
        else "_phase5_initialization_failure_point"
    )
    monkeypatch.setattr(module, hook, initialization_failure)
    fragment = "lease = self._anchor_lease" if phase == 4 else "def close_anchor"
    trace, state = _one_line_interrupt(module._AnchoredConnection.close, fragment, secondary)

    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt) as captured:
            _store(database, phase).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert captured.value is primary
    assert secondary in _cleanup_errors(primary)
    assert _fd_count() == before
    _assert_no_residue(database, phase)


def test_historical_owned_descriptor_close_entry_interrupt_is_retryable(
    tmp_path,
):
    """Probe 3: the cleanup action retries a first-line interrupted close."""

    path = tmp_path / "owned-close-entry.txt"
    path.write_bytes(b"owned")
    before = _fd_count()
    descriptor = os.open(path, os.O_RDONLY)
    lease = ownership.OwnedDescriptor(
        descriptor, owner="probe-owner", label="historical close probe"
    )
    primary = KeyboardInterrupt("historical unwind")
    secondary = SystemExit("historical OwnedDescriptor.close entry failure")
    trace, state = _one_line_interrupt(ownership.OwnedDescriptor.close, "try:", secondary)

    sys.settrace(trace)
    try:
        ownership.run_cleanup(
            [("close historical probe lease", lease.cleanup("probe-owner"))],
            primary=primary,
        )
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert secondary in _cleanup_errors(primary)
    assert lease.closed is True
    assert lease.close("probe-owner") is False
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert _fd_count() == before


@pytest.mark.parametrize("phase", (4, 5))
def test_historical_reconciliation_reader_publish_interrupt_closes_reader(
    tmp_path, monkeypatch, phase
):
    """Probe 4: a published reconciliation reader closes during unwind."""

    database = (tmp_path / f"phase{phase}-reconcile-reader.sqlite").resolve()
    module = phase4 if phase == 4 else phase5
    primary = KeyboardInterrupt("historical after-commit failure")
    secondary = SystemExit("historical reconciliation publish interruption")
    original_connect = module.sqlite3.connect
    readers: list[sqlite3.Connection] = []
    before = _fd_count()

    def initialization_failure(stage: str) -> None:
        if stage == "after_commit":
            raise primary

    def recording_connect(target, *args, **kwargs):
        connection = original_connect(target, *args, **kwargs)
        if "mode=ro&immutable=1" in str(target):
            readers.append(connection)
        return connection

    hook = (
        "_phase4_initialization_failure_point"
        if phase == 4
        else "_phase5_initialization_failure_point"
    )
    monkeypatch.setattr(module, hook, initialization_failure)
    monkeypatch.setattr(module.sqlite3, "connect", recording_connect)
    trace, state = _one_line_interrupt(
        module.Phase4ShadowStore._reconcile_initialization_outcome
        if phase == 4
        else module.Phase5SupervisorStore._reconcile_initialization_outcome,
        "readonly.row_factory = sqlite3.Row",
        secondary,
    )

    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt) as captured:
            _store(database, phase).initialize()
    finally:
        sys.settrace(None)
    assert state["raised"] is True
    assert captured.value is primary
    assert secondary in _cleanup_errors(primary)
    assert len(readers) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        readers[0].execute("SELECT 1")
    assert _fd_count() == before
    _assert_restartable(database, phase)


def test_historical_ctypes_argument_converter_poison_cannot_split_close_state(
    tmp_path, monkeypatch
):
    """Probe 5: the removed Python ``ctypes.c_int`` gap cannot be reached.

    The historical implementation published ``kernel_call_attempted`` and
    then called ``ctypes.c_int(descriptor)`` before entering libc.  The fixed
    implementation passes the integer directly to a pre-typed libc function.
    Poisoning the old conversion symbol therefore must not run or prevent the
    unique kernel close.
    """

    path = tmp_path / "ctypes-conversion-probe.txt"
    path.write_bytes(b"owned")
    before = _fd_count()
    descriptor = os.open(path, os.O_RDONLY)
    lease = ownership.OwnedDescriptor(
        descriptor, owner="probe-owner", label="ctypes conversion probe"
    )
    calls = 0

    def poisoned_converter(_value):
        nonlocal calls
        calls += 1
        raise MemoryError("historical ctypes.c_int conversion failure")

    monkeypatch.setattr(ownership.ctypes, "c_int", poisoned_converter)
    assert lease.close("probe-owner") is True
    assert calls == 0
    assert lease.closed is True
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert _fd_count() == before


@pytest.mark.parametrize("phase", (4, 5))
def test_normal_create_close_restart_and_concurrent_reopen(tmp_path, phase):
    """Normal-flow control for create, close, restart, and concurrency."""

    database = (tmp_path / f"phase{phase}-normal.sqlite").resolve()
    store = _store(database, phase)
    store.initialize()
    before = _fd_count()
    connection = store._connect()
    connection.execute("SELECT 1").fetchone()
    connection.close()
    assert _fd_count() == before
    _assert_restartable(database, phase)

    def reopen(_index: int) -> None:
        candidate = _store(database, phase)
        candidate.initialize()
        assert set(candidate.table_counts().values()) == {0}

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(reopen, range(12)))
    _assert_restartable(database, phase)
