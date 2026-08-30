from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import sqlite3
import threading

import pytest

from factory_core.authority_read_repository import (
    AuthorityReadError,
    AuthorityReadRepository,
)
from factory_core.canonical import canonical_sha256
from tests.support.authority_production import (
    configure_canary,
    install_foundation,
    persist_one,
)


def _file_identity(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns


def test_supported_read_repository_returns_typed_frozen_identity_checked_values(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    committed, _ = persist_one(writer)
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )

    coordinate = repository.workflow_coordinate("legacy_current")
    bundle = repository.command_bundle(
        workflow_id="legacy_current", idempotency_key="idempotency-1"
    )
    stream = repository.revision_snapshot("legacy_current")
    delivery = repository.outbox_delivery_state("message-1")

    assert coordinate.current_revision == committed.revision == 2
    assert coordinate.source_fence_sha256 == fixture.preflight.source_fence_sha256
    assert bundle.bundle_sha256 == committed.bundle_sha256
    assert hashlib.sha256(bundle.command_bytes).hexdigest() == bundle.command_sha256
    assert hashlib.sha256(bundle.event_bytes).hexdigest() == bundle.event_sha256
    assert stream.through_revision == coordinate.current_revision
    assert [event.revision for event in stream.events] == [2]
    assert stream.snapshot_sha256 == canonical_sha256(
        {
            "schema": "authority-revision-stream-v1",
            "coordinate": coordinate.as_dict(),
            "through_revision": 2,
            "events": [
                {
                    "event_id": "event-1",
                    "revision": 2,
                    "command_id": "command-1",
                    "event_type": "PRODUCTION_RECORDED",
                    "envelope_sha256": bundle.event_sha256,
                }
            ],
        }
    )
    assert delivery.status == "PENDING"
    with pytest.raises(FrozenInstanceError):
        coordinate.current_revision = 99  # type: ignore[misc]


def test_revision_snapshot_rejects_implicit_or_explicit_stale_cursor(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer)
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    current = repository.workflow_coordinate("legacy_current").current_revision

    with pytest.raises(AuthorityReadError, match="after_revision exceeds"):
        repository.revision_snapshot("legacy_current", after_revision=current + 1)
    with pytest.raises(AuthorityReadError, match="through_revision precedes"):
        repository.revision_snapshot(
            "legacy_current",
            after_revision=current,
            through_revision=current - 1,
        )


def test_mode_ro_query_only_reads_create_no_wal_or_shm_and_modify_no_bytes(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer)
    connection = sqlite3.connect(fixture.database)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.close()
    wal = Path(str(fixture.database) + "-wal")
    shm = Path(str(fixture.database) + "-shm")
    assert not wal.exists() and not shm.exists()
    before = _file_identity(fixture.database)

    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    repository.workflow_coordinate("legacy_current")
    repository.command_bundle(
        workflow_id="legacy_current", idempotency_key="idempotency-1"
    )
    repository.revision_snapshot("legacy_current")
    repository.outbox_delivery_state("message-1")

    assert _file_identity(fixture.database) == before
    assert not wal.exists() and not shm.exists()


@pytest.mark.parametrize(
    ("call", "match"),
    (
        (lambda repo: repo.workflow_coordinate("unknown"), "unknown workflow"),
        (
            lambda repo: repo.command_bundle(
                workflow_id="legacy_current", idempotency_key="unknown"
            ),
            "unavailable",
        ),
        (lambda repo: repo.outbox_delivery_state("unknown"), "unavailable"),
        (
            lambda repo: repo.revision_snapshot(
                "legacy_current", through_revision=999
            ),
            "exceeds",
        ),
    ),
)
def test_unknown_or_stale_supported_queries_fail_closed(tmp_path, call, match):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer)
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    with pytest.raises(AuthorityReadError, match=match):
        call(repository)


def test_malformed_mutable_delivery_row_fails_closed(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer)
    connection = sqlite3.connect(fixture.database)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "UPDATE authority_production_outbox_delivery_state SET status='BOGUS'"
    )
    connection.commit()
    connection.close()
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    with pytest.raises(AuthorityReadError, match="status is malformed"):
        repository.outbox_delivery_state("message-1")


def test_immutable_envelope_or_schema_tamper_is_rejected_before_return(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer)
    connection = sqlite3.connect(fixture.database)
    connection.execute("DROP TRIGGER authority_commands_append_only_update")
    connection.execute(
        "UPDATE authority_commands SET envelope_json='{}' WHERE command_id='command-1'"
    )
    connection.commit()
    connection.close()
    repository = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    with pytest.raises(Exception, match="sqlite_master"):
        repository.command_bundle(
            workflow_id="legacy_current", idempotency_key="idempotency-1"
        )


def test_one_read_transaction_never_mixes_a_concurrent_later_revision(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer)
    coordinate_read = threading.Event()
    writer_finished = threading.Event()

    class PausingRepository(AuthorityReadRepository):
        def _coordinate(self, connection, workflow_id):
            value = super()._coordinate(connection, workflow_id)
            coordinate_read.set()
            assert writer_finished.wait(timeout=5)
            return value

    repository = PausingRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    output = []

    def read_snapshot():
        output.append(repository.revision_snapshot("legacy_current"))

    thread = threading.Thread(target=read_snapshot)
    thread.start()
    assert coordinate_read.wait(timeout=5)
    persist_one(writer, requested_revision=2, suffix="2", occurred_at=1300)
    writer_finished.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    snapshot = output[0]
    assert snapshot.coordinate.current_revision == 2
    assert snapshot.through_revision == 2
    assert [event.revision for event in snapshot.events] == [2]
    fresh = AuthorityReadRepository(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    ).revision_snapshot("legacy_current")
    assert fresh.coordinate.current_revision == 3
    assert [event.revision for event in fresh.events] == [2, 3]


def test_read_repository_has_no_table_count_or_mutation_surface():
    public_callables = {
        name
        for name, value in AuthorityReadRepository.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    assert public_callables == {
        "workflow_coordinate",
        "command_bundle",
        "phase3_artifact_state",
        "revision_snapshot",
        "outbox_delivery_state",
    }
