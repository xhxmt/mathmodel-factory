from __future__ import annotations

import ctypes
import os
from pathlib import Path
import select
import sqlite3
import struct
import subprocess
import sys
import time

import pytest

import factory_core.phase4_shadow_runtime as runtime_module
from factory_core.durable_operation import (
    OperationEvent,
    OperationStatus,
    build_worker_launch_identity,
)
from factory_core.phase4_shadow_runtime import (
    Phase4ShadowFenceError,
    Phase4ShadowIdempotencyConflict,
    Phase4ShadowStore,
    Phase4ShadowStoreError,
    run_phase4_full_shadow,
)


def _identity(*, command="command-1", scope="scope-1", payload="1" * 64):
    return build_worker_launch_identity(
        outbox_command_id=command,
        invocation_id="invocation-1",
        attempt_id="attempt-1",
        process_scope_id=scope,
        payload_sha256=payload,
    )


def _store(tmp_path):
    store = Phase4ShadowStore(tmp_path / "phase4-shadow.sqlite")
    store.initialize()
    return store


def _install_initialization_interrupt(
    monkeypatch,
    *,
    stage: str,
    interrupt_type: type[BaseException],
) -> None:
    real_connect = sqlite3.connect
    anchored_type = runtime_module._AnchoredConnection

    class InterruptingConnection(anchored_type):
        def execute(self, sql, parameters=(), /):
            normalized = " ".join(str(sql).split())
            if stage == "first-ddl" and normalized.startswith("CREATE TABLE"):
                raise interrupt_type("synthetic initialization interrupt")
            if stage == "schema-state" and normalized.startswith(
                "INSERT INTO phase4_shadow_schema_state"
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

    monkeypatch.setattr(runtime_module.sqlite3, "connect", interrupting_connect)


def _assert_phase4_process_restart(database: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from factory_core.phase4_shadow_runtime import Phase4ShadowStore; "
            "store=Phase4ShadowStore(Path(sys.argv[1])); store.initialize(); "
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


def test_default_disabled_returns_before_path_or_sql(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("disabled run connected to SQLite"),
    )

    result = run_phase4_full_shadow(
        database=tmp_path / "must-not-exist.sqlite",
        identity=_identity(),
    )

    assert result.enabled is False
    assert result.authoritative is False
    assert result.dispatch_performed is False
    assert len(result.run_sha256) == 64
    assert not (tmp_path / "must-not-exist.sqlite").exists()


def test_atomic_intent_reservation_exact_replay_and_payload_conflict(tmp_path):
    store = _store(tmp_path)
    first = store.reserve_operation(_identity(), occurred_at=10)
    replay = store.reserve_operation(_identity(command="command-duplicate"), occurred_at=99)

    assert first.replayed is False and replay.replayed is True
    assert replay.state == first.state
    assert replay.receipt == first.receipt
    assert replay.state.operation.identity.outbox_command_id == "command-1"
    assert store.table_counts() == {
        "phase4_shadow_outbox_intents": 1,
        "phase4_shadow_current": 1,
        "phase4_shadow_receipts": 1,
        "phase4_shadow_idempotency": 1,
    }

    with pytest.raises(Phase4ShadowIdempotencyConflict, match="different bytes"):
        store.reserve_operation(_identity(command="command-2", scope="scope-2"), occurred_at=10)
    assert store.table_counts()["phase4_shadow_outbox_intents"] == 1


@pytest.mark.parametrize(
    "failure_stage",
    ("after_intent", "after_current", "after_receipt", "after_idempotency"),
)
def test_reservation_failure_points_roll_back_every_row(tmp_path, monkeypatch, failure_stage):
    store = _store(tmp_path)

    def fail(stage):
        if stage == failure_stage:
            raise RuntimeError("simulated transaction death")

    monkeypatch.setattr(runtime_module, "_phase4_failure_point", fail)
    with pytest.raises(RuntimeError, match="transaction death"):
        store.reserve_operation(_identity(), occurred_at=10)

    assert set(store.table_counts().values()) == {0}


def test_lease_retry_restart_reconcile_and_exact_transition_replay(tmp_path):
    store = _store(tmp_path)
    reserved = store.reserve_operation(_identity(), occurred_at=10)
    operation_sha = reserved.state.operation.identity.identity_sha256
    first_claim = store.claim_operation(
        operation_sha,
        request_idempotency_key="request-claim-1",
        claim_owner_id="consumer-a",
        claim_owner_epoch=1,
        expected_claim_generation=0,
        occurred_at=11,
        lease_seconds=5,
    )
    claim_replay = store.claim_operation(
        operation_sha,
        request_idempotency_key="request-claim-1",
        claim_owner_id="consumer-a",
        claim_owner_epoch=1,
        expected_claim_generation=0,
        occurred_at=11,
        lease_seconds=5,
    )
    assert claim_replay.replayed is True
    assert claim_replay.receipt == first_claim.receipt

    with pytest.raises(Phase4ShadowIdempotencyConflict, match="different bytes"):
        store.claim_operation(
            operation_sha,
            request_idempotency_key="request-claim-1",
            claim_owner_id="consumer-b",
            claim_owner_epoch=2,
            expected_claim_generation=1,
            occurred_at=16,
            lease_seconds=5,
        )

    restarted = Phase4ShadowStore(store.path)
    restarted.initialize()
    with pytest.raises(Phase4ShadowFenceError, match="has not expired"):
        restarted.claim_operation(
            operation_sha,
            request_idempotency_key="request-claim-too-early",
            claim_owner_id="consumer-b",
            claim_owner_epoch=2,
            expected_claim_generation=1,
            occurred_at=15,
            lease_seconds=5,
        )
    reclaimed = restarted.claim_operation(
        operation_sha,
        request_idempotency_key="request-reclaim-2",
        claim_owner_id="consumer-b",
        claim_owner_epoch=2,
        expected_claim_generation=1,
        occurred_at=16,
        lease_seconds=5,
    )
    assert reclaimed.state.operation.claim_generation == 2
    assert reclaimed.state.retry_count == 1

    with pytest.raises(Phase4ShadowFenceError, match="owner fence"):
        restarted.transition(
            operation_sha,
            OperationEvent.CHECKPOINT_DISPATCH,
            request_idempotency_key="request-stale-owner",
            expected_claim_generation=2,
            claim_owner_id="consumer-a",
            claim_owner_epoch=1,
            dispatch_nonce="dispatch-1",
            reason_code="DISPATCH_INTENT_DURABLE",
            occurred_at=17,
        )

    checkpoint = restarted.transition(
        operation_sha,
        OperationEvent.CHECKPOINT_DISPATCH,
        request_idempotency_key="request-checkpoint",
        expected_claim_generation=2,
        claim_owner_id="consumer-b",
        claim_owner_epoch=2,
        dispatch_nonce="dispatch-1",
        reason_code="DISPATCH_INTENT_DURABLE",
        occurred_at=17,
    )
    uncertain = restarted.transition(
        operation_sha,
        OperationEvent.MARK_DISPATCH_UNCERTAIN,
        request_idempotency_key="request-uncertain",
        expected_claim_generation=2,
        claim_owner_id="consumer-b",
        claim_owner_epoch=2,
        dispatch_nonce="dispatch-1",
        reason_code="SYNTHETIC_ACK_LOST",
        occurred_at=18,
    )
    required = restarted.transition(
        operation_sha,
        OperationEvent.REQUIRE_RECONCILIATION,
        request_idempotency_key="request-reconcile-required",
        expected_claim_generation=2,
        claim_owner_id="consumer-b",
        claim_owner_epoch=2,
        dispatch_nonce="dispatch-1",
        reason_code="SYNTHETIC_LOOKUP_REQUIRED",
        occurred_at=19,
    )
    assert uncertain.state.operation.status is OperationStatus.DISPATCH_UNCERTAIN
    assert required.state.operation.status is OperationStatus.RECONCILIATION_REQUIRED

    second_restart = Phase4ShadowStore(store.path)
    second_restart.initialize()
    active = second_restart.transition(
        operation_sha,
        OperationEvent.RECONCILE_ACTIVE,
        request_idempotency_key="request-reconcile-active",
        expected_claim_generation=2,
        claim_owner_id="consumer-b",
        claim_owner_epoch=2,
        dispatch_nonce="dispatch-1",
        reason_code="SYNTHETIC_SCOPE_FOUND",
        occurred_at=20,
    )
    succeeded = second_restart.transition(
        operation_sha,
        OperationEvent.CONFIRM_SUCCEEDED,
        request_idempotency_key="request-success",
        expected_claim_generation=2,
        claim_owner_id="consumer-b",
        claim_owner_epoch=2,
        dispatch_nonce="dispatch-1",
        reason_code="SYNTHETIC_EXIT_ZERO",
        occurred_at=21,
    )
    late_replay = second_restart.transition(
        operation_sha,
        OperationEvent.CHECKPOINT_DISPATCH,
        request_idempotency_key="request-checkpoint",
        expected_claim_generation=2,
        claim_owner_id="consumer-b",
        claim_owner_epoch=2,
        dispatch_nonce="dispatch-1",
        reason_code="DISPATCH_INTENT_DURABLE",
        occurred_at=17,
    )

    assert active.state.operation.status is OperationStatus.ACTIVE
    assert succeeded.state.operation.status is OperationStatus.SUCCEEDED
    assert succeeded.state.as_dict()["dispatch_performed"] is False
    assert late_replay.replayed is True
    assert late_replay.state == checkpoint.state
    assert second_restart.load(operation_sha) == succeeded.state
    assert second_restart.table_counts()["phase4_shadow_receipts"] == 8


@pytest.mark.parametrize("failure_stage", ("after_current", "after_receipt", "after_idempotency"))
def test_transition_failure_points_restore_current_and_append_ledgers(
    tmp_path, monkeypatch, failure_stage
):
    store = _store(tmp_path)
    reserved = store.reserve_operation(_identity(), occurred_at=10)
    operation_sha = reserved.state.operation.identity.identity_sha256
    claimed = store.claim_operation(
        operation_sha,
        request_idempotency_key="request-claim",
        claim_owner_id="consumer-a",
        claim_owner_epoch=1,
        expected_claim_generation=0,
        occurred_at=11,
        lease_seconds=10,
    )
    before = store.table_counts()

    def fail(stage):
        if stage == failure_stage:
            raise RuntimeError("simulated transition death")

    monkeypatch.setattr(runtime_module, "_phase4_failure_point", fail)
    with pytest.raises(RuntimeError, match="transition death"):
        store.transition(
            operation_sha,
            OperationEvent.CHECKPOINT_DISPATCH,
            request_idempotency_key="request-checkpoint",
            expected_claim_generation=1,
            claim_owner_id="consumer-a",
            claim_owner_epoch=1,
            dispatch_nonce="dispatch-1",
            reason_code="DISPATCH_INTENT_DURABLE",
            occurred_at=12,
        )

    assert store.load(operation_sha) == claimed.state
    assert store.table_counts() == before


def test_store_refuses_non_shadow_schema(tmp_path):
    database = tmp_path / "not-a-shadow.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE production_like_state(value TEXT)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(Phase4ShadowStoreError, match="non-shadow"):
        Phase4ShadowStore(database).initialize()


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

        monkeypatch.setattr(runtime_module.sqlite3, "connect", audited_connect)
        with pytest.raises(Phase4ShadowStoreError, match="header"):
            Phase4ShadowStore(database).initialize()
        target_names = {
            database.name,
            f"{database.name}-wal",
            f"{database.name}-shm",
            f"{database.name}-journal",
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
def test_foreign_wal_writer_is_rejected_without_any_directory_mutation_or_rw_open(
    monkeypatch, tmp_path
):
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
        }
        real_connect = sqlite3.connect
        opens: list[tuple[str, dict[str, object]]] = []

        def audited_connect(target, *args, **kwargs):
            opens.append((str(target), dict(kwargs)))
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(runtime_module.sqlite3, "connect", audited_connect)
        started = time.monotonic()
        with pytest.raises(Phase4ShadowStoreError, match="sidecar|ownership"):
            Phase4ShadowStore(database).initialize()
        elapsed = time.monotonic() - started
        mutations = [
            (name, mask)
            for name, mask in watcher.read(0.1)
            if name in target_names and mask & _INOTIFY_MUTATION_MASK
        ]

        assert elapsed < 1.0
        assert opens == []
        assert mutations == []
        assert _directory_snapshot(tmp_path) == before
        assert foreign.in_transaction is True
    finally:
        watcher.close()
        foreign.rollback()
        foreign.close()


def test_new_store_is_exclusive_and_rejects_unknown_schema_objects(
    monkeypatch, tmp_path
):
    database = (tmp_path / "owned.sqlite").resolve()
    real_open = os.open
    opens: list[int] = []

    def audited_open(path, flags, *args, **kwargs):
        if Path(path) == database or (
            Path(path) == Path(database.name) and kwargs.get("dir_fd") is not None
        ):
            opens.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(runtime_module.os, "open", audited_open)
    store = Phase4ShadowStore(database)
    store.initialize()
    assert any(flags & os.O_CREAT and flags & os.O_EXCL for flags in opens)

    connection = sqlite3.connect(database)
    try:
        marker = connection.execute(
            "SELECT * FROM phase4_shadow_schema_state WHERE singleton=1"
        ).fetchone()
        assert marker is not None and len(marker) == 5
        connection.execute("CREATE TABLE phase4_shadow_intruder(value TEXT)")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(Phase4ShadowStoreError, match="schema objects|digest"):
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
        if stage == "after_intent" and not replaced:
            store.path.replace(moved)
            replacement.replace(store.path)
            replaced = True

    monkeypatch.setattr(runtime_module, "_phase4_failure_point", replace_after_first_write)
    with pytest.raises(Phase4ShadowStoreError, match="anchored inode"):
        store.reserve_operation(_identity(), occurred_at=10)

    assert replaced is True
    assert store.path.read_bytes() == replacement_bytes
    moved_connection = sqlite3.connect(moved)
    try:
        assert moved_connection.execute(
            "SELECT COUNT(*) FROM phase4_shadow_outbox_intents"
        ).fetchone()[0] == 0
        assert moved_connection.execute(
            "SELECT COUNT(*) FROM phase4_shadow_current"
        ).fetchone()[0] == 0
    finally:
        moved_connection.close()


def test_exclusive_initialization_error_releases_database_descriptor(
    monkeypatch, tmp_path
):
    database = (tmp_path / "exclusive-error.sqlite").resolve()
    real_connect = sqlite3.connect
    real_fsync = os.fsync
    fsynced_directories = []

    def fail_anchored_connect(target, *args, **kwargs):
        if "mode=rw" in str(target):
            raise sqlite3.OperationalError("synthetic anchored open failure")
        return real_connect(target, *args, **kwargs)

    def audited_fsync(fd):
        if os.path.isdir(f"/proc/self/fd/{fd}"):
            fsynced_directories.append(os.fstat(fd).st_ino)
        return real_fsync(fd)

    monkeypatch.setattr(runtime_module.sqlite3, "connect", fail_anchored_connect)
    monkeypatch.setattr(runtime_module.os, "fsync", audited_fsync)
    with pytest.raises(Phase4ShadowStoreError, match="initialization fence"):
        Phase4ShadowStore(database).initialize()

    assert not database.exists()
    assert not Path(f"{database}-journal").exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert fsynced_directories == [tmp_path.stat().st_ino]

    leaked_targets = []
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if str(database) in target:
            leaked_targets.append(target)
    assert leaked_targets == []

    monkeypatch.setattr(runtime_module.sqlite3, "connect", real_connect)
    monkeypatch.setattr(runtime_module.os, "fsync", real_fsync)
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from factory_core.phase4_shadow_runtime import Phase4ShadowStore; "
            "store=Phase4ShadowStore(Path(sys.argv[1])); store.initialize(); "
            "print(store.table_counts())",
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
    assert completed.stderr == ""
    assert "phase4_shadow_schema_state" not in completed.stdout
    Phase4ShadowStore(database).initialize()


def test_schema_creation_failure_cleans_exclusive_file_and_allows_exact_retry(
    monkeypatch, tmp_path
):
    database = (tmp_path / "schema-failure.sqlite").resolve()
    original_statements = runtime_module._SCHEMA_STATEMENTS
    monkeypatch.setattr(
        runtime_module,
        "_SCHEMA_STATEMENTS",
        (original_statements[0], "CREATE TABLE phase4_shadow_broken("),
    )

    with pytest.raises(Phase4ShadowStoreError, match="initialization fence"):
        Phase4ShadowStore(database).initialize()

    assert not database.exists()
    assert not Path(f"{database}-journal").exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()

    monkeypatch.setattr(runtime_module, "_SCHEMA_STATEMENTS", original_statements)
    Phase4ShadowStore(database).initialize()
    Phase4ShadowStore(database).initialize()


def test_precommit_failure_cleans_exclusive_file_and_allows_restart(
    monkeypatch, tmp_path
):
    database = (tmp_path / "precommit-failure.sqlite").resolve()
    original_commit = Phase4ShadowStore._commit_anchored

    def fail_before_commit(_self, _connection):
        raise sqlite3.OperationalError("synthetic commit failure")

    monkeypatch.setattr(Phase4ShadowStore, "_commit_anchored", fail_before_commit)
    with pytest.raises(Phase4ShadowStoreError, match="initialization fence"):
        Phase4ShadowStore(database).initialize()

    assert not database.exists()
    assert not Path(f"{database}-journal").exists()
    monkeypatch.setattr(Phase4ShadowStore, "_commit_anchored", original_commit)
    Phase4ShadowStore(database).initialize()
    Phase4ShadowStore(database).initialize()


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize(
    "stage", ("connect", "first-ddl", "schema-state", "pre-commit")
)
def test_baseexception_during_exclusive_initialization_cleans_and_retries(
    monkeypatch, tmp_path, stage, interrupt_type
):
    database = (tmp_path / f"baseexception-{stage}.sqlite").resolve()

    with monkeypatch.context() as context:
        _install_initialization_interrupt(
            context,
            stage=stage,
            interrupt_type=interrupt_type,
        )
        with pytest.raises(interrupt_type, match="initialization interrupt"):
            Phase4ShadowStore(database).initialize()

    assert not database.exists()
    assert all(
        not Path(f"{database}{suffix}").exists()
        for suffix in ("-wal", "-shm", "-journal")
    )
    Phase4ShadowStore(database).initialize()
    _assert_phase4_process_restart(database)


@pytest.mark.parametrize(
    ("interrupt_type", "expected_error"),
    (
        (sqlite3.OperationalError, Phase4ShadowStoreError),
        (KeyboardInterrupt, KeyboardInterrupt),
    ),
)
def test_real_commit_then_immediate_error_preserves_restartable_store(
    monkeypatch, tmp_path, interrupt_type, expected_error
):
    database = (tmp_path / f"commit-reconcile-{interrupt_type.__name__}.sqlite").resolve()

    with monkeypatch.context() as context:
        _install_initialization_interrupt(
            context,
            stage="post-commit",
            interrupt_type=interrupt_type,
        )
        with pytest.raises(expected_error):
            Phase4ShadowStore(database).initialize()

    assert database.exists()
    assert database.stat().st_size > 0
    restarted = Phase4ShadowStore(database)
    restarted.initialize()
    assert set(restarted.table_counts().values()) == {0}
    _assert_phase4_process_restart(database)


def test_cleanup_path_replacement_never_unlinks_replacement_or_other_files(
    monkeypatch, tmp_path
):
    database = (tmp_path / "cleanup-race.sqlite").resolve()
    moved_original = (tmp_path / "moved-exclusive.sqlite").resolve()
    replacement = (tmp_path / "replacement.sqlite").resolve()
    sentinel = (tmp_path / "other-file.txt").resolve()
    replacement_connection = sqlite3.connect(replacement)
    try:
        replacement_connection.execute("CREATE TABLE foreign_state(value TEXT NOT NULL)")
        replacement_connection.execute("INSERT INTO foreign_state VALUES('preserve')")
        replacement_connection.commit()
    finally:
        replacement_connection.close()
    replacement_bytes = replacement.read_bytes()
    sentinel.write_bytes(b"must-not-change")

    original_entry_identity = Phase4ShadowStore._entry_identity
    entry_calls = 0
    swapped = False

    def replace_after_cleanup_identity_check(parent_fd, name):
        nonlocal entry_calls, swapped
        identity = original_entry_identity(parent_fd, name)
        if name == database.name:
            entry_calls += 1
            if entry_calls == 2:
                os.rename(
                    database.name,
                    moved_original.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                os.rename(
                    replacement.name,
                    database.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                swapped = True
        return identity

    monkeypatch.setattr(
        Phase4ShadowStore,
        "_entry_identity",
        staticmethod(replace_after_cleanup_identity_check),
    )
    original_statements = runtime_module._SCHEMA_STATEMENTS
    monkeypatch.setattr(
        runtime_module,
        "_SCHEMA_STATEMENTS",
        (original_statements[0], "CREATE TABLE phase4_shadow_broken("),
    )

    with pytest.raises(Phase4ShadowStoreError, match="initialization fence"):
        Phase4ShadowStore(database).initialize()

    assert swapped is True
    assert database.read_bytes() == replacement_bytes
    assert moved_original.exists()
    assert sentinel.read_bytes() == b"must-not-change"


def test_postcommit_failure_preserves_complete_database_for_restart(
    monkeypatch, tmp_path
):
    database = (tmp_path / "postcommit-failure.sqlite").resolve()
    original_commit = Phase4ShadowStore._commit_anchored

    def commit_then_fail(self, connection):
        original_commit(self, connection)
        raise sqlite3.OperationalError("synthetic post-commit failure")

    monkeypatch.setattr(Phase4ShadowStore, "_commit_anchored", commit_then_fail)
    with pytest.raises(Phase4ShadowStoreError, match="initialization fence"):
        Phase4ShadowStore(database).initialize()

    assert database.exists()
    assert database.stat().st_size > 0
    monkeypatch.setattr(Phase4ShadowStore, "_commit_anchored", original_commit)
    restarted = Phase4ShadowStore(database)
    restarted.initialize()
    assert set(restarted.table_counts().values()) == {0}


def test_store_requires_an_absolute_standalone_path():
    with pytest.raises(Phase4ShadowStoreError, match="must be absolute"):
        Phase4ShadowStore(Path("relative-phase4.sqlite"))


def test_restart_detects_current_state_hash_tamper(tmp_path):
    store = _store(tmp_path)
    reserved = store.reserve_operation(_identity(), occurred_at=10)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "UPDATE phase4_shadow_current SET state_sha256=? "
            "WHERE operation_identity_sha256=?",
            ("0" * 64, reserved.state.operation.identity.identity_sha256),
        )
        connection.commit()
    finally:
        connection.close()

    restarted = Phase4ShadowStore(store.path)
    restarted.initialize()
    with pytest.raises(Phase4ShadowStoreError, match="canonical bytes or hash differ"):
        restarted.load(reserved.state.operation.identity.identity_sha256)


def test_production_cli_import_does_not_load_phase4_runtime():
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import factory_core.cli

assert "factory_core.phase4_shadow_runtime" not in sys.modules
print("phase4-durable-runtime-not-in-production-imports")
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
    assert completed.stdout == "phase4-durable-runtime-not-in-production-imports\n"
