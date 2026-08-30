from __future__ import annotations

import ctypes
import os
import json
from pathlib import Path
import select
import sqlite3
import stat
import struct
import subprocess
import sys
import time

import pytest

import factory_core.phase5_shadow_supervisor as supervisor_module
from factory_core.adapters.infrastructure.pause_policy import (
    PauseAction,
    PauseMode,
    ProcessScopeKind,
)
from factory_core.phase5_shadow_supervisor import (
    Phase5SupervisorError,
    Phase5SupervisorFenceError,
    Phase5SupervisorReplayConflict,
    Phase5SupervisorStore,
    Phase5SupervisorStoreError,
    SupervisorScopeBinding,
    SupervisorStatus,
    SyntheticEffectObservation,
    SyntheticObservationOutcome,
    run_phase5_full_shadow,
)


def _binding(*, workflow="workflow-1", scope="scope-1", kind=ProcessScopeKind.WORKER):
    return SupervisorScopeBinding(
        workflow_id=workflow,
        invocation_id="invocation-1",
        attempt_id="attempt-1",
        process_scope_id=scope,
        operation_identity_sha256="1" * 64,
        scope_kind=kind,
    )


def _store(tmp_path):
    store = Phase5SupervisorStore((tmp_path / "phase5-shadow.sqlite").resolve())
    store.initialize()
    return store


def _install_initialization_interrupt(
    monkeypatch,
    *,
    stage: str,
    interrupt_type: type[BaseException],
) -> None:
    real_connect = sqlite3.connect
    anchored_type = supervisor_module._AnchoredConnection

    class InterruptingConnection(anchored_type):
        def execute(self, sql, parameters=(), /):
            normalized = " ".join(str(sql).split())
            if stage == "first-ddl" and normalized.startswith("CREATE TABLE"):
                raise interrupt_type("synthetic initialization interrupt")
            if stage == "schema-state" and normalized.startswith(
                "INSERT INTO phase5_shadow_schema_state"
            ):
                raise interrupt_type("synthetic initialization interrupt")
            return super().execute(sql, parameters)

        def commit(self):
            if stage == "pre-commit":
                raise interrupt_type("synthetic initialization interrupt")
            if stage == "post-commit":
                super().commit()
                raise interrupt_type("synthetic post-commit interrupt")
            return super().commit()

    def interrupting_connect(target, *args, **kwargs):
        if "mode=rw" in str(target) and kwargs.get("factory") is anchored_type:
            if stage == "connect":
                raise interrupt_type("synthetic initialization interrupt")
            kwargs["factory"] = InterruptingConnection
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(supervisor_module.sqlite3, "connect", interrupting_connect)


def _assert_phase5_process_restart(database: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from factory_core.phase5_shadow_supervisor import Phase5SupervisorStore; "
            "store=Phase5SupervisorStore(Path(sys.argv[1])); store.initialize(); "
            "assert set(store.table_counts().values()) == {0}",
            str(database),
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


class RecordingPort:
    def __init__(self, outcome=SyntheticObservationOutcome.CONFIRMED_APPLIED):
        self.outcome = outcome
        self.calls = []

    def record_would_apply(self, *, request_id, binding, decision):
        self.calls.append((request_id, binding, decision))
        return SyntheticEffectObservation(
            observation_id=f"observation-{len(self.calls)}",
            outcome=self.outcome,
            observed_at=12,
            evidence_sha256="2" * 64,
        )


class PoisonPort:
    def record_would_apply(self, **_kwargs):
        pytest.fail("disabled supervisor called its effect port")


class CrashingPort:
    def __init__(self):
        self.calls = 0

    def record_would_apply(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("synthetic port crash")


def test_default_disabled_returns_before_path_sql_or_port(monkeypatch, tmp_path):
    monkeypatch.setattr(
        supervisor_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("disabled supervisor opened SQLite"),
    )
    path = tmp_path / "must-not-exist.sqlite"

    result = run_phase5_full_shadow(database=path, effect_port=PoisonPort())

    assert result.enabled is False
    assert result.authoritative is False
    assert result.authority_transferred is False
    assert result.dispatch_performed is False
    assert result.process_signal_performed is False
    assert result.provider_call_performed is False
    assert result.effect_port_called is False
    assert len(result.run_sha256) == 64
    assert not path.exists()


def test_request_is_atomic_exactly_replayed_and_conflicts_on_other_bytes(tmp_path):
    store = _store(tmp_path)
    first = store.request_pause(
        _binding(),
        PauseMode.PAUSE,
        request_idempotency_key="pause-request-1",
        occurred_at=10,
    )
    replay = store.request_pause(
        _binding(),
        PauseMode.PAUSE,
        request_idempotency_key="pause-request-1",
        occurred_at=10,
    )

    assert first.replayed is False and replay.replayed is True
    assert replay.state == first.state
    assert replay.receipt == first.receipt
    assert first.state.decision.action is PauseAction.TERMINATE_SCOPE
    assert store.table_counts() == {
        "phase5_shadow_requests": 1,
        "phase5_shadow_current": 1,
        "phase5_shadow_receipts": 1,
        "phase5_shadow_idempotency": 1,
    }

    with pytest.raises(Phase5SupervisorReplayConflict, match="different canonical bytes"):
        store.request_pause(
            _binding(kind=ProcessScopeKind.DURABLE_SOLVER),
            PauseMode.PAUSE_AND_CANCEL_SOLVERS,
            request_idempotency_key="pause-request-1",
            occurred_at=10,
        )
    assert store.table_counts()["phase5_shadow_requests"] == 1


@pytest.mark.parametrize(
    "failure_stage",
    ("after_request", "after_current", "after_receipt", "after_idempotency"),
)
def test_request_failure_points_roll_back_whole_transaction(
    tmp_path, monkeypatch, failure_stage
):
    store = _store(tmp_path)

    def fail(stage):
        if stage == failure_stage:
            raise RuntimeError("simulated supervisor death")

    monkeypatch.setattr(supervisor_module, "_phase5_failure_point", fail)
    with pytest.raises(RuntimeError, match="supervisor death"):
        store.request_pause(
            _binding(),
            PauseMode.PAUSE,
            request_idempotency_key="pause-request-1",
            occurred_at=10,
        )

    assert set(store.table_counts().values()) == {0}


def test_enabled_cycle_uses_recording_port_after_checkpoint_and_never_dispatches(tmp_path):
    port = RecordingPort()
    result = run_phase5_full_shadow(
        enabled=True,
        database=(tmp_path / "cycle.sqlite").resolve(),
        binding=_binding(),
        mode=PauseMode.PAUSE,
        request_idempotency_key="cycle-1",
        occurred_at=10,
        effect_port=port,
    )

    assert len(port.calls) == 1
    assert result.enabled is True
    assert result.effect_port_called is True
    assert result.authoritative is False
    assert result.authority_transferred is False
    assert result.dispatch_performed is False
    assert result.process_signal_performed is False
    assert result.provider_call_performed is False
    store = Phase5SupervisorStore((tmp_path / "cycle.sqlite").resolve())
    store.initialize()
    state = store.load(result.request_id)
    assert state.status is SupervisorStatus.COMPLETED
    assert state.transition_index == 2
    assert state.last_observation.outcome is SyntheticObservationOutcome.CONFIRMED_APPLIED


def test_completed_exact_replay_does_not_call_effect_port_again(tmp_path):
    database = (tmp_path / "exact-replay.sqlite").resolve()
    first_port = RecordingPort()
    first = run_phase5_full_shadow(
        enabled=True,
        database=database,
        binding=_binding(),
        request_idempotency_key="exact-cycle",
        occurred_at=10,
        effect_port=first_port,
    )
    replay_port = RecordingPort()
    replay = run_phase5_full_shadow(
        enabled=True,
        database=database,
        binding=_binding(),
        request_idempotency_key="exact-cycle",
        occurred_at=10,
        effect_port=replay_port,
    )

    assert len(first_port.calls) == 1
    assert replay_port.calls == []
    assert replay.replayed is True
    assert replay.effect_port_called is False
    assert replay.request_id == first.request_id
    assert replay.final_state_sha256 == first.final_state_sha256


def test_crash_after_durable_checkpoint_restarts_into_reconciliation_without_recall(
    tmp_path,
):
    database = (tmp_path / "port-crash.sqlite").resolve()
    crashing = CrashingPort()
    with pytest.raises(RuntimeError, match="synthetic port crash"):
        run_phase5_full_shadow(
            enabled=True,
            database=database,
            binding=_binding(),
            request_idempotency_key="crash-cycle",
            occurred_at=10,
            effect_port=crashing,
        )
    assert crashing.calls == 1

    replay_port = RecordingPort()
    recovered = run_phase5_full_shadow(
        enabled=True,
        database=database,
        binding=_binding(),
        request_idempotency_key="crash-cycle",
        occurred_at=10,
        effect_port=replay_port,
    )
    assert replay_port.calls == []
    assert recovered.effect_port_called is False
    store = Phase5SupervisorStore(database)
    store.initialize()
    assert store.load(recovered.request_id).status is SupervisorStatus.RECONCILIATION_REQUIRED


def test_checkpoint_crash_restart_recovery_and_synthetic_reconcile(tmp_path):
    store = _store(tmp_path)
    requested = store.request_pause(
        _binding(kind=ProcessScopeKind.DURABLE_SOLVER),
        PauseMode.PAUSE_AND_CANCEL_SOLVERS,
        request_idempotency_key="request-1",
        occurred_at=10,
    )
    checkpoint = store.checkpoint_effect(
        requested.state.request_id,
        expected_binding_sha256=requested.state.binding.binding_sha256,
        request_idempotency_key="checkpoint-1",
        occurred_at=11,
    )
    assert checkpoint.state.status is SupervisorStatus.EFFECT_CHECKPOINTED

    restarted = Phase5SupervisorStore(store.path)
    restarted.initialize()
    recovery = restarted.recover_uncertain(
        requested.state.request_id,
        expected_binding_sha256=requested.state.binding.binding_sha256,
        request_idempotency_key="recovery-1",
        occurred_at=20,
    )
    recovery_replay = restarted.recover_uncertain(
        requested.state.request_id,
        expected_binding_sha256=requested.state.binding.binding_sha256,
        request_idempotency_key="recovery-1",
        occurred_at=20,
    )
    assert recovery.state.status is SupervisorStatus.RECONCILIATION_REQUIRED
    assert recovery_replay.replayed is True

    observation = SyntheticEffectObservation(
        "reconcile-observation-1",
        SyntheticObservationOutcome.CONFIRMED_APPLIED,
        21,
        "3" * 64,
    )
    completed = restarted.record_observation(
        requested.state.request_id,
        expected_binding_sha256=requested.state.binding.binding_sha256,
        request_idempotency_key="reconcile-1",
        occurred_at=21,
        observation=observation,
    )
    assert completed.state.status is SupervisorStatus.COMPLETED
    assert completed.state.decision.action is PauseAction.REQUEST_CANCEL
    assert completed.state.as_dict()["dispatch_performed"] is False

    second_restart = Phase5SupervisorStore(store.path)
    second_restart.initialize()
    assert second_restart.load(requested.state.request_id) == completed.state


def test_unknown_observation_stays_reconciliation_required(tmp_path):
    store = _store(tmp_path)
    requested = store.request_pause(
        _binding(), PauseMode.PAUSE, request_idempotency_key="request", occurred_at=10
    )
    store.checkpoint_effect(
        requested.state.request_id,
        expected_binding_sha256=requested.state.binding.binding_sha256,
        request_idempotency_key="checkpoint",
        occurred_at=11,
    )
    unknown = SyntheticEffectObservation(
        "observation-unknown",
        SyntheticObservationOutcome.UNKNOWN,
        12,
        "4" * 64,
    )
    result = store.record_observation(
        requested.state.request_id,
        expected_binding_sha256=requested.state.binding.binding_sha256,
        request_idempotency_key="observation",
        occurred_at=12,
        observation=unknown,
    )
    assert result.state.status is SupervisorStatus.RECONCILIATION_REQUIRED


def test_scope_binding_fence_rejects_cross_workflow_or_scope(tmp_path):
    store = _store(tmp_path)
    requested = store.request_pause(
        _binding(), PauseMode.PAUSE, request_idempotency_key="request", occurred_at=10
    )
    foreign = _binding(workflow="workflow-2", scope="scope-2")

    with pytest.raises(Phase5SupervisorFenceError, match="scope binding is stale"):
        store.checkpoint_effect(
            requested.state.request_id,
            expected_binding_sha256=foreign.binding_sha256,
            request_idempotency_key="foreign-checkpoint",
            occurred_at=11,
        )
    assert store.load(requested.state.request_id) == requested.state


@pytest.mark.parametrize("failure_stage", ("after_current", "after_receipt", "after_idempotency"))
def test_transition_failure_points_restore_current_and_append_ledgers(
    tmp_path, monkeypatch, failure_stage
):
    store = _store(tmp_path)
    requested = store.request_pause(
        _binding(), PauseMode.PAUSE, request_idempotency_key="request", occurred_at=10
    )
    before = store.table_counts()

    def fail(stage):
        if stage == failure_stage:
            raise RuntimeError("simulated checkpoint death")

    monkeypatch.setattr(supervisor_module, "_phase5_failure_point", fail)
    with pytest.raises(RuntimeError, match="checkpoint death"):
        store.checkpoint_effect(
            requested.state.request_id,
            expected_binding_sha256=requested.state.binding.binding_sha256,
            request_idempotency_key="checkpoint",
            occurred_at=11,
        )

    assert store.load(requested.state.request_id) == requested.state
    assert store.table_counts() == before


def test_store_rejects_foreign_schema_and_detects_current_state_tamper(tmp_path):
    foreign_path = (tmp_path / "foreign.sqlite").resolve()
    connection = sqlite3.connect(foreign_path)
    try:
        connection.execute("CREATE TABLE production_like_state(value TEXT)")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(Phase5SupervisorStoreError, match="non-shadow"):
        Phase5SupervisorStore(foreign_path).initialize()

    store = _store(tmp_path)
    requested = store.request_pause(
        _binding(), PauseMode.PAUSE, request_idempotency_key="request", occurred_at=10
    )
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "UPDATE phase5_shadow_current SET state_sha256=? WHERE request_id=?",
            ("0" * 64, requested.state.request_id),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(Phase5SupervisorStoreError, match="canonical bytes or hash differ"):
        store.load(requested.state.request_id)


_INOTIFY_MUTATION_MASK = (
    0x00000002  # IN_MODIFY
    | 0x00000004  # IN_ATTRIB
    | 0x00000008  # IN_CLOSE_WRITE
    | 0x00000040  # IN_MOVED_FROM
    | 0x00000080  # IN_MOVED_TO
    | 0x00000100  # IN_CREATE
    | 0x00000200  # IN_DELETE
)
_INOTIFY_EVENT = struct.Struct("iIII")


class _LinuxMutationWatcher:
    def __init__(self, directory: Path) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add = libc.inotify_add_watch
        add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add.restype = ctypes.c_int
        self.fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        if add(self.fd, os.fsencode(directory), _INOTIFY_MUTATION_MASK) < 0:
            error = ctypes.get_errno()
            os.close(self.fd)
            raise OSError(error, "inotify_add_watch failed")

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def read(self, timeout: float = 0.05) -> list[tuple[str, int]]:
        events: list[tuple[str, int]] = []
        while self.fd >= 0 and select.select([self.fd], [], [], timeout)[0]:
            timeout = 0
            data = os.read(self.fd, 65536)
            offset = 0
            while offset < len(data):
                _, mask, _, name_length = _INOTIFY_EVENT.unpack_from(data, offset)
                offset += _INOTIFY_EVENT.size
                raw_name = data[offset : offset + name_length].split(b"\0", 1)[0]
                offset += name_length
                events.append((os.fsdecode(raw_name), mask))
        return events


def _self_check_watcher(watcher: _LinuxMutationWatcher, directory: Path) -> None:
    probe = directory / "inotify-transient-self-check"
    moved = directory / "inotify-transient-self-check-moved"
    probe.write_bytes(b"watch")
    probe.replace(moved)
    moved.unlink()
    events = watcher.read(0.25)
    assert any(mask & 0x00000100 for _, mask in events)
    assert any(mask & 0x00000200 for _, mask in events)
    assert any(mask & 0x00000008 for _, mask in events)
    assert watcher.read(0) == []


def _directory_snapshot(directory: Path) -> dict[str, tuple[bytes, int, int, int, int]]:
    result = {}
    for entry in directory.iterdir():
        if entry.is_file():
            info = entry.stat()
            result[entry.name] = (
                entry.read_bytes(),
                info.st_mode,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            )
    return result


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux inotify required")
def test_closed_foreign_wal_rejection_catches_transient_sidecar_regression(
    monkeypatch, tmp_path
):
    database = (tmp_path / "closed-foreign-wal.sqlite").resolve()
    foreign = sqlite3.connect(database)
    try:
        assert foreign.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        foreign.execute("CREATE TABLE production_state(value TEXT NOT NULL)")
        foreign.execute("INSERT INTO production_state VALUES('must-not-change')")
        foreign.commit()
    finally:
        foreign.close()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()

    watcher = _LinuxMutationWatcher(tmp_path)
    try:
        _self_check_watcher(watcher, tmp_path)
        before = _directory_snapshot(tmp_path)
        real_connect = sqlite3.connect
        opens: list[tuple[str, dict[str, object]]] = []

        def audited_connect(target, *args, **kwargs):
            opens.append((str(target), dict(kwargs)))
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(supervisor_module.sqlite3, "connect", audited_connect)
        with pytest.raises(Phase5SupervisorStoreError, match="header"):
            Phase5SupervisorStore(database).initialize()
        target_names = {
            database.name,
            f"{database.name}-wal",
            f"{database.name}-shm",
            f"{database.name}-journal",
            f"{database.name}.phase5-owner.json",
        }
        mutations = [
            (name, mask)
            for name, mask in watcher.read(0.1)
            if name in target_names and mask & _INOTIFY_MUTATION_MASK
        ]
        assert opens == []
        assert mutations == []
        assert _directory_snapshot(tmp_path) == before
    finally:
        watcher.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux inotify required")
def test_foreign_wal_fails_before_directory_write_or_rw_sqlite(monkeypatch, tmp_path):
    database = (tmp_path / "foreign-wal.sqlite").resolve()
    foreign = sqlite3.connect(database, isolation_level=None)
    watcher = _LinuxMutationWatcher(tmp_path)
    try:
        assert foreign.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        foreign.execute("CREATE TABLE production_state(value TEXT NOT NULL)")
        foreign.execute("INSERT INTO production_state VALUES('must-not-change')")
        foreign.execute("BEGIN IMMEDIATE")
        foreign.execute("UPDATE production_state SET value='writer-lock-held'")
        _self_check_watcher(watcher, tmp_path)
        before = _directory_snapshot(tmp_path)
        target_names = {
            database.name,
            f"{database.name}-wal",
            f"{database.name}-shm",
            f"{database.name}-journal",
            f"{database.name}.phase5-owner.json",
        }
        real_connect = sqlite3.connect
        opens = []

        def audited_connect(target, *args, **kwargs):
            opens.append((str(target), dict(kwargs)))
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(supervisor_module.sqlite3, "connect", audited_connect)
        started = time.monotonic()
        with pytest.raises(Phase5SupervisorStoreError):
            Phase5SupervisorStore(database).initialize()
        elapsed = time.monotonic() - started
        mutations = [
            (name, mask)
            for name, mask in watcher.read(0.1)
            if name in target_names and mask & _INOTIFY_MUTATION_MASK
        ]

        assert elapsed < 1.0
        assert _directory_snapshot(tmp_path) == before
        assert not Path(f"{database}.phase5-owner.json").exists()
        assert opens == []
        assert mutations == []
        assert foreign.in_transaction is True
    finally:
        watcher.close()
        foreign.rollback()
        foreign.close()


def test_new_store_uses_exclusive_creation_and_exact_owned_profile(
    monkeypatch, tmp_path
):
    database = (tmp_path / "owned.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")
    real_open = os.open
    opens = []

    def audited_open(path, flags, *args, **kwargs):
        observed = Path(path)
        if kwargs.get("dir_fd") is not None:
            if observed == Path(database.name):
                observed = database
            elif observed == Path(marker.name):
                observed = marker
        if observed in {database, marker}:
            opens.append((observed, flags))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(supervisor_module.os, "open", audited_open)
    store = Phase5SupervisorStore(database)
    store.initialize()

    assert any(
        path == database and flags & os.O_CREAT and flags & os.O_EXCL
        for path, flags in opens
    )
    assert any(
        path == marker and flags & os.O_CREAT and flags & os.O_EXCL
        for path, flags in opens
    )
    marker_value = json.loads(marker.read_text(encoding="utf-8"))
    assert set(marker_value) == {
        "allowed_objects_sha256",
        "database_device",
        "database_inode",
        "database_path_sha256",
        "marker_sha256",
        "parent_device",
        "parent_inode",
        "schema_profile_sha256",
        "schema_version",
        "store_schema_version",
    }
    parent_stat = database.parent.stat()
    assert marker_value["parent_device"] == parent_stat.st_dev
    assert marker_value["parent_inode"] == parent_stat.st_ino
    assert marker_value["schema_version"] == (
        supervisor_module.PHASE5_OWNERSHIP_MARKER_SCHEMA
    )
    assert marker.read_bytes() == supervisor_module.canonical_bytes(marker_value)

    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE phase5_shadow_intruder(value TEXT)")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(Phase5SupervisorStoreError, match="schema|objects"):
        store.initialize()


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE INDEX phase5_extra_ordinary ON phase5_shadow_current(updated_at)",
        "CREATE UNIQUE INDEX phase5_extra_unique ON phase5_shadow_current(updated_at)",
        "CREATE INDEX phase5_extra_partial ON phase5_shadow_current(updated_at) "
        "WHERE updated_at >= 0",
        "CREATE INDEX phase5_extra_expression ON phase5_shadow_current(lower(request_id))",
        "CREATE VIEW phase5_extra_view AS "
        "SELECT request_id FROM phase5_shadow_current",
    ),
    ids=("ordinary-index", "unique-index", "partial-index", "expression-index", "view"),
)
def test_exact_inventory_rejects_every_extra_persistent_index_or_view(
    tmp_path, statement
):
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(statement)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(Phase5SupervisorStoreError, match="schema objects"):
        store.initialize()


def test_canonical_automatic_indexes_are_owned_and_restart_cleanly(tmp_path):
    store = _store(tmp_path)
    connection = sqlite3.connect(store.path)
    try:
        rows = connection.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master "
            "WHERE type='index' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [
        (name, table, None)
        for name, table in supervisor_module._AUTO_INDEX_DEFINITIONS
    ]
    restarted = Phase5SupervisorStore(store.path)
    restarted.initialize()
    assert set(restarted.table_counts().values()) == {0}


@pytest.mark.parametrize(
    "entrypoint", ("initialize", "restart", "public-mutation")
)
def test_parent_replacement_rejects_same_database_and_marker_inodes(
    tmp_path, entrypoint
):
    parent = (tmp_path / f"parent-{entrypoint}").resolve()
    parent.mkdir()
    database = parent / "owned.sqlite"
    marker = Path(f"{database}.phase5-owner.json")
    store = Phase5SupervisorStore(database)
    store.initialize()
    database_inode = database.stat().st_ino
    marker_inode = marker.stat().st_ino

    displaced = (tmp_path / f"displaced-{entrypoint}").resolve()
    parent.replace(displaced)
    parent.mkdir()
    (displaced / database.name).replace(database)
    (displaced / marker.name).replace(marker)
    assert database.stat().st_ino == database_inode
    assert marker.stat().st_ino == marker_inode

    candidate = store if entrypoint != "restart" else Phase5SupervisorStore(database)
    with pytest.raises(Phase5SupervisorStoreError, match="parent|ownership marker"):
        if entrypoint == "public-mutation":
            candidate.request_pause(
                _binding(),
                PauseMode.PAUSE,
                request_idempotency_key="parent-replaced",
                occurred_at=10,
            )
        else:
            candidate.initialize()


@pytest.mark.parametrize(
    "stage",
    ("pre-open", "post-open", "pre-begin", "post-begin", "pre-commit", "post-commit"),
)
def test_initialization_parent_fence_covers_every_sqlite_boundary(
    monkeypatch, tmp_path, stage
):
    parent = (tmp_path / f"initial-parent-{stage}").resolve()
    parent.mkdir()
    displaced = (tmp_path / f"displaced-initial-parent-{stage}").resolve()
    database = parent / "owned.sqlite"
    marker = Path(f"{database}.phase5-owner.json")
    real_connect = sqlite3.connect
    real_fsync = os.fsync
    anchored_type = supervisor_module._AnchoredConnection
    statements: list[str] = []
    replaced = False
    commit_finished = False

    def replace_parent_and_move_created_inodes() -> None:
        nonlocal replaced
        if replaced:
            return
        parent.replace(displaced)
        parent.mkdir()
        (displaced / database.name).replace(database)
        (displaced / marker.name).replace(marker)
        replaced = True

    class BoundaryConnection(anchored_type):
        def execute(self, sql, parameters=(), /):
            normalized = " ".join(str(sql).split())
            statements.append(normalized)
            result = super().execute(sql, parameters)
            if stage == "pre-begin" and normalized == "PRAGMA foreign_keys=ON":
                replace_parent_and_move_created_inodes()
            elif stage == "post-begin" and normalized == "BEGIN IMMEDIATE":
                replace_parent_and_move_created_inodes()
            elif stage == "pre-commit" and normalized.startswith(
                "INSERT INTO phase5_shadow_schema_state"
            ):
                replace_parent_and_move_created_inodes()
            return result

        def commit(self):
            nonlocal commit_finished
            result = super().commit()
            commit_finished = True
            if stage == "post-commit":
                replace_parent_and_move_created_inodes()
            return result

    def boundary_connect(target, *args, **kwargs):
        if "mode=rw" in str(target) and kwargs.get("factory") is anchored_type:
            kwargs["factory"] = BoundaryConnection
            connection = real_connect(target, *args, **kwargs)
            if stage == "post-open":
                replace_parent_and_move_created_inodes()
            return connection
        return real_connect(target, *args, **kwargs)

    def boundary_fsync(descriptor):
        result = real_fsync(descriptor)
        if stage == "pre-open" and not replaced and marker.exists():
            if os.fstat(descriptor).st_ino == marker.stat().st_ino:
                replace_parent_and_move_created_inodes()
        return result

    with monkeypatch.context() as context:
        context.setattr(supervisor_module.sqlite3, "connect", boundary_connect)
        context.setattr(supervisor_module.os, "fsync", boundary_fsync)
        with pytest.raises(Phase5SupervisorStoreError, match="parent"):
            Phase5SupervisorStore(database).initialize()

    assert replaced is True
    if stage in {"pre-open", "post-open", "pre-begin"}:
        assert "BEGIN IMMEDIATE" not in statements
    elif stage == "post-begin":
        assert not any(item.startswith("CREATE TABLE") for item in statements)
    elif stage == "pre-commit":
        assert commit_finished is False
    else:
        assert commit_finished is True


def test_initialization_parent_symlink_replacement_fails_before_write_lock(
    monkeypatch, tmp_path
):
    parent = (tmp_path / "initial-symlink-parent").resolve()
    parent.mkdir()
    displaced = (tmp_path / "displaced-symlink-parent").resolve()
    database = parent / "owned.sqlite"
    marker = Path(f"{database}.phase5-owner.json")
    real_connect = sqlite3.connect
    anchored_type = supervisor_module._AnchoredConnection
    statements: list[str] = []

    class TracingConnection(anchored_type):
        def execute(self, sql, parameters=(), /):
            statements.append(" ".join(str(sql).split()))
            return super().execute(sql, parameters)

    def replacing_connect(target, *args, **kwargs):
        if "mode=rw" in str(target) and kwargs.get("factory") is anchored_type:
            kwargs["factory"] = TracingConnection
            connection = real_connect(target, *args, **kwargs)
            parent.replace(displaced)
            parent.symlink_to(displaced, target_is_directory=True)
            return connection
        return real_connect(target, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(supervisor_module.sqlite3, "connect", replacing_connect)
        with pytest.raises(Phase5SupervisorStoreError, match="parent"):
            Phase5SupervisorStore(database).initialize()

    assert not database.exists()
    assert not marker.exists()
    assert not (displaced / database.name).exists()
    assert not (displaced / marker.name).exists()
    assert "BEGIN IMMEDIATE" not in statements


def test_markerless_legacy_restart_after_parent_replacement_is_read_only(
    monkeypatch, tmp_path
):
    parent = (tmp_path / "legacy-parent").resolve()
    parent.mkdir()
    displaced = (tmp_path / "legacy-parent-displaced").resolve()
    database = parent / "legacy.sqlite"
    connection = sqlite3.connect(database)
    try:
        for _, statement in supervisor_module._LEGACY_TABLE_DEFINITIONS:
            connection.execute(statement)
        for _, _, statement in supervisor_module._TRIGGER_DEFINITIONS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO phase5_shadow_schema_state VALUES(?,?)",
            tuple(supervisor_module._LEGACY_SCHEMA_STATE.values()),
        )
        connection.commit()
    finally:
        connection.close()
    database_inode = database.stat().st_ino
    parent.replace(displaced)
    parent.mkdir()
    (displaced / database.name).replace(database)
    assert database.stat().st_ino == database_inode
    assert not Path(f"{database}.phase5-owner.json").exists()

    real_connect = sqlite3.connect
    opens: list[str] = []

    def audited_connect(target, *args, **kwargs):
        opens.append(str(target))
        return real_connect(target, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(supervisor_module.sqlite3, "connect", audited_connect)
        restarted = Phase5SupervisorStore(database)
        restarted.initialize()
        assert set(restarted.table_counts().values()) == {0}
        with pytest.raises(Phase5SupervisorStoreError, match="legacy.*read-only"):
            restarted.request_pause(
                _binding(),
                PauseMode.PAUSE,
                request_idempotency_key="legacy-parent-replaced",
                occurred_at=10,
            )

    assert not any("mode=rw" in target for target in opens)


def test_marker_is_exact_and_rejects_extra_fields(tmp_path):
    store = _store(tmp_path)
    marker = Path(f"{store.path}.phase5-owner.json")
    value = json.loads(marker.read_text(encoding="utf-8"))
    value["unexpected"] = False
    marker.write_bytes(supervisor_module.canonical_bytes(value))

    with pytest.raises(Phase5SupervisorStoreError, match="ownership marker"):
        store.initialize()


def test_commit_fence_rejects_path_replacement_and_rolls_back_anchored_db(
    monkeypatch, tmp_path
):
    store = _store(tmp_path)
    moved = (tmp_path / "moved-owned.sqlite").resolve()
    replacement = (tmp_path / "foreign-replacement.sqlite").resolve()
    connection = sqlite3.connect(replacement)
    try:
        connection.execute("CREATE TABLE foreign_state(value TEXT NOT NULL)")
        connection.execute("INSERT INTO foreign_state VALUES('must-not-change')")
        connection.commit()
    finally:
        connection.close()
    replacement_bytes = replacement.read_bytes()
    replaced = False

    def replace_after_first_write(stage):
        nonlocal replaced
        if stage == "after_request" and not replaced:
            store.path.replace(moved)
            replacement.replace(store.path)
            replaced = True

    monkeypatch.setattr(supervisor_module, "_phase5_failure_point", replace_after_first_write)
    with pytest.raises(Phase5SupervisorStoreError, match="anchored inode"):
        store.request_pause(
            _binding(),
            PauseMode.PAUSE,
            request_idempotency_key="path-race",
            occurred_at=10,
        )

    assert replaced is True
    assert store.path.read_bytes() == replacement_bytes
    moved_connection = sqlite3.connect(moved)
    try:
        assert moved_connection.execute(
            "SELECT COUNT(*) FROM phase5_shadow_requests"
        ).fetchone()[0] == 0
        assert moved_connection.execute(
            "SELECT COUNT(*) FROM phase5_shadow_current"
        ).fetchone()[0] == 0
    finally:
        moved_connection.close()


def test_exclusive_initialization_error_releases_database_and_marker_descriptors(
    monkeypatch, tmp_path
):
    database = (tmp_path / "exclusive-error.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")
    real_connect = sqlite3.connect

    def fail_anchored_connect(target, *args, **kwargs):
        if "mode=rw" in str(target):
            raise sqlite3.OperationalError("synthetic anchored open failure")
        return real_connect(target, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(supervisor_module.sqlite3, "connect", fail_anchored_connect)
        with pytest.raises(Phase5SupervisorStoreError, match="initialization failed"):
            Phase5SupervisorStore(database).initialize()

    assert not database.exists()
    assert not marker.exists()
    restarted = Phase5SupervisorStore(database)
    restarted.initialize()
    Phase5SupervisorStore(database).initialize()

    leaked_targets = []
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if str(database) in target or str(marker) in target:
            leaked_targets.append(target)
    assert leaked_targets == []


def test_schema_creation_failure_is_cleaned_and_process_restart_succeeds(
    monkeypatch, tmp_path
):
    database = (tmp_path / "schema-failure.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")
    invalid_definitions = supervisor_module._OWNED_TABLE_DEFINITIONS + (
        ("phase5_shadow_broken", "CREATE TABLE phase5_shadow_broken("),
    )
    with monkeypatch.context() as context:
        context.setattr(
            supervisor_module, "_OWNED_TABLE_DEFINITIONS", invalid_definitions
        )
        with pytest.raises(Phase5SupervisorStoreError, match="initialization failed"):
            Phase5SupervisorStore(database).initialize()

    assert not database.exists()
    assert not marker.exists()
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from factory_core.phase5_shadow_supervisor import Phase5SupervisorStore; "
            f"Phase5SupervisorStore({str(database)!r}).initialize()",
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    Phase5SupervisorStore(database).initialize()


def test_commit_failure_is_cleaned_fsynced_and_retryable(monkeypatch, tmp_path):
    database = (tmp_path / "commit-failure.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")
    directory_fsyncs = []
    real_fsync = os.fsync

    def audited_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs.append(descriptor)
        return real_fsync(descriptor)

    def fail_commit(_self, _connection):
        raise sqlite3.OperationalError("synthetic commit failure")

    with monkeypatch.context() as context:
        context.setattr(supervisor_module.os, "fsync", audited_fsync)
        context.setattr(Phase5SupervisorStore, "_commit_anchored", fail_commit)
        with pytest.raises(Phase5SupervisorStoreError, match="initialization failed"):
            Phase5SupervisorStore(database).initialize()

    assert directory_fsyncs
    assert not database.exists()
    assert not marker.exists()
    Phase5SupervisorStore(database).initialize()
    Phase5SupervisorStore(database).initialize()


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize(
    "stage", ("connect", "first-ddl", "schema-state", "pre-commit")
)
def test_baseexception_during_exclusive_initialization_cleans_and_retries(
    monkeypatch, tmp_path, stage, interrupt_type
):
    database = (tmp_path / f"baseexception-{stage}.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")

    with monkeypatch.context() as context:
        _install_initialization_interrupt(
            context,
            stage=stage,
            interrupt_type=interrupt_type,
        )
        with pytest.raises(interrupt_type, match="initialization interrupt"):
            Phase5SupervisorStore(database).initialize()

    assert not database.exists()
    assert not marker.exists()
    assert all(
        not Path(f"{database}{suffix}").exists()
        for suffix in ("-wal", "-shm", "-journal")
    )
    Phase5SupervisorStore(database).initialize()
    _assert_phase5_process_restart(database)


@pytest.mark.parametrize(
    ("interrupt_type", "expected_error"),
    (
        (sqlite3.OperationalError, Phase5SupervisorStoreError),
        (KeyboardInterrupt, KeyboardInterrupt),
    ),
)
def test_real_commit_then_immediate_error_preserves_database_marker_and_restart(
    monkeypatch, tmp_path, interrupt_type, expected_error
):
    database = (tmp_path / f"commit-reconcile-{interrupt_type.__name__}.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")

    with monkeypatch.context() as context:
        _install_initialization_interrupt(
            context,
            stage="post-commit",
            interrupt_type=interrupt_type,
        )
        with pytest.raises(expected_error):
            Phase5SupervisorStore(database).initialize()

    assert database.exists()
    assert database.stat().st_size > 0
    assert marker.exists()
    assert marker.stat().st_size > 0
    restarted = Phase5SupervisorStore(database)
    restarted.initialize()
    assert set(restarted.table_counts().values()) == {0}
    _assert_phase5_process_restart(database)


def test_cleanup_path_replacement_never_deletes_replacements(monkeypatch, tmp_path):
    database = (tmp_path / "cleanup-race.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")
    moved_database = tmp_path / "moved-created.sqlite"
    moved_marker = tmp_path / "moved-created.marker"
    replacement_database = b"foreign database replacement"
    replacement_marker = b"foreign marker replacement"
    invalid_definitions = supervisor_module._OWNED_TABLE_DEFINITIONS + (
        ("phase5_shadow_broken", "CREATE TABLE phase5_shadow_broken("),
    )
    real_cleanup = Phase5SupervisorStore._cleanup_exclusive_entry
    replaced = False

    def replace_then_cleanup(**kwargs):
        nonlocal replaced
        if not replaced:
            database.replace(moved_database)
            marker.replace(moved_marker)
            database.write_bytes(replacement_database)
            marker.write_bytes(replacement_marker)
            replaced = True
        return real_cleanup(**kwargs)

    with monkeypatch.context() as context:
        context.setattr(
            supervisor_module, "_OWNED_TABLE_DEFINITIONS", invalid_definitions
        )
        context.setattr(
            Phase5SupervisorStore,
            "_cleanup_exclusive_entry",
            staticmethod(replace_then_cleanup),
        )
        with pytest.raises(Phase5SupervisorStoreError, match="initialization failed"):
            Phase5SupervisorStore(database).initialize()

    assert replaced is True
    assert database.read_bytes() == replacement_database
    assert marker.read_bytes() == replacement_marker
    assert moved_database.exists()
    assert moved_marker.exists()


def test_cleanup_identity_check_to_delete_race_restores_foreign_database(
    monkeypatch, tmp_path
):
    database = (tmp_path / "cleanup-stat-race.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")
    moved_database = tmp_path / "moved-created-after-stat.sqlite"
    replacement = tmp_path / "foreign-replacement.sqlite"
    replacement.write_bytes(b"foreign database replacement after stat")
    replacement_bytes = replacement.read_bytes()
    real_stat = supervisor_module.os.stat
    swapped = False

    def replace_after_named_stat(path, *args, **kwargs):
        nonlocal swapped
        value = real_stat(path, *args, **kwargs)
        if (
            not swapped
            and path == database.name
            and kwargs.get("dir_fd") is not None
        ):
            os.rename(
                database.name,
                moved_database.name,
                src_dir_fd=kwargs["dir_fd"],
                dst_dir_fd=kwargs["dir_fd"],
            )
            os.rename(
                replacement.name,
                database.name,
                src_dir_fd=kwargs["dir_fd"],
                dst_dir_fd=kwargs["dir_fd"],
            )
            swapped = True
        return value

    invalid_definitions = supervisor_module._OWNED_TABLE_DEFINITIONS + (
        ("phase5_shadow_broken", "CREATE TABLE phase5_shadow_broken("),
    )
    with monkeypatch.context() as context:
        context.setattr(
            supervisor_module, "_OWNED_TABLE_DEFINITIONS", invalid_definitions
        )
        context.setattr(supervisor_module.os, "stat", replace_after_named_stat)
        with pytest.raises(Phase5SupervisorStoreError, match="initialization failed"):
            Phase5SupervisorStore(database).initialize()

    assert swapped is True
    assert database.read_bytes() == replacement_bytes
    assert moved_database.exists()
    assert not marker.exists()


def test_failure_after_successful_schema_commit_preserves_restartable_store(
    monkeypatch, tmp_path
):
    database = (tmp_path / "post-commit-failure.sqlite").resolve()
    marker = Path(f"{database}.phase5-owner.json")
    real_commit = Phase5SupervisorStore._commit_anchored

    def commit_then_fail(self, connection):
        real_commit(self, connection)
        raise Phase5SupervisorStoreError("synthetic post-commit fence failure")

    with monkeypatch.context() as context:
        context.setattr(Phase5SupervisorStore, "_commit_anchored", commit_then_fail)
        with pytest.raises(Phase5SupervisorStoreError, match="post-commit"):
            Phase5SupervisorStore(database).initialize()

    assert database.exists()
    assert marker.exists()
    restarted = Phase5SupervisorStore(database)
    restarted.initialize()
    assert set(restarted.table_counts().values()) == {0}


@pytest.mark.parametrize("key_length", (495, 496, 500, 511, 512))
def test_bounded_internal_stage_keys_cover_identifier_boundaries(
    tmp_path, key_length
):
    database = (tmp_path / f"bounded-{key_length}.sqlite").resolve()
    key = "k" * key_length
    first_port = RecordingPort()
    first = run_phase5_full_shadow(
        enabled=True,
        database=database,
        binding=_binding(),
        request_idempotency_key=key,
        occurred_at=10,
        effect_port=first_port,
    )
    replay_port = RecordingPort()
    replay = run_phase5_full_shadow(
        enabled=True,
        database=database,
        binding=_binding(),
        request_idempotency_key=key,
        occurred_at=10,
        effect_port=replay_port,
    )

    assert len(first_port.calls) == 1
    assert replay_port.calls == []
    assert replay.replayed is True
    assert replay.request_id == first.request_id
    connection = sqlite3.connect(database)
    try:
        keys = [
            row[0]
            for row in connection.execute(
                "SELECT request_idempotency_key FROM phase5_shadow_idempotency"
            )
        ]
    finally:
        connection.close()
    internal = [item for item in keys if item != key]
    assert len(internal) == 2
    assert all(item.startswith("phase5-internal@") for item in internal)
    assert all(len(item) <= 512 for item in internal)
    assert len(set(internal)) == 2

    conflict_port = RecordingPort()
    with pytest.raises(Phase5SupervisorReplayConflict, match="different canonical bytes"):
        run_phase5_full_shadow(
            enabled=True,
            database=database,
            binding=_binding(),
            request_idempotency_key=key,
            occurred_at=11,
            effect_port=conflict_port,
        )
    assert conflict_port.calls == []


@pytest.mark.parametrize("key_length", (495, 496, 500, 511, 512))
def test_bounded_restart_key_reconciles_port_crash_without_duplicate_port(
    tmp_path, key_length
):
    database = (tmp_path / f"restart-{key_length}.sqlite").resolve()
    key = "r" * key_length
    crashing = CrashingPort()
    with pytest.raises(RuntimeError, match="synthetic port crash"):
        run_phase5_full_shadow(
            enabled=True,
            database=database,
            binding=_binding(),
            request_idempotency_key=key,
            occurred_at=10,
            effect_port=crashing,
        )
    assert crashing.calls == 1

    replay_port = RecordingPort()
    recovered = run_phase5_full_shadow(
        enabled=True,
        database=database,
        binding=_binding(),
        request_idempotency_key=key,
        occurred_at=10,
        effect_port=replay_port,
    )
    assert replay_port.calls == []
    assert recovered.effect_port_called is False
    assert Phase5SupervisorStore(database).load(recovered.request_id).status is (
        SupervisorStatus.RECONCILIATION_REQUIRED
    )
    third_port = RecordingPort()
    exact = run_phase5_full_shadow(
        enabled=True,
        database=database,
        binding=_binding(),
        request_idempotency_key=key,
        occurred_at=10,
        effect_port=third_port,
    )
    assert third_port.calls == []
    assert exact.replayed is True
    assert exact.request_id == recovered.request_id
    assert exact.final_state_sha256 == recovered.final_state_sha256


def test_caller_key_cannot_enter_internal_stage_namespace(tmp_path):
    with pytest.raises(Phase5SupervisorError, match="reserved"):
        run_phase5_full_shadow(
            enabled=True,
            database=(tmp_path / "reserved.sqlite").resolve(),
            binding=_binding(),
            request_idempotency_key="phase5-internal@" + "a" * 64,
            occurred_at=10,
            effect_port=RecordingPort(),
        )


def test_production_cli_import_does_not_load_phase5_supervisor():
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import factory_core.cli

assert "factory_core.phase5_shadow_supervisor" not in sys.modules
print("phase5-durable-supervisor-not-in-production-imports")
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "phase5-durable-supervisor-not-in-production-imports\n"


def test_module_has_no_process_provider_network_or_production_authority_imports():
    source = Path(supervisor_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import subprocess",
        "import socket",
        "import requests",
        "authority_production_writer",
        "authority_outbox_delivery",
        "ProcessSupervisor",
        "killpg",
        "Popen",
    ):
        assert forbidden not in source
