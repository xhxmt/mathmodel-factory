from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import factory_core.authority_production_writer as writer_module
from factory_core.authority_operations import CANARY, V1_ONLY, AuthorityOperations
from factory_core.authority_production_writer import (
    AuthorityProductionWriter,
    AuthorityProductionWriterDisabled,
    AuthorityProductionWriterFenceLost,
)
from factory_core.authority_repository import (
    AuthorityRepository,
    AuthorityIdempotencyConflict,
    AuthorityRevisionConflict,
    AuthorityWriteShadowDisabled,
)
from tests.support.authority_production import (
    bundle,
    configure_canary,
    install_foundation,
    persist_one,
    table_counts,
)


MUTATION_TABLES = (
    "authority_contract_pin_sets",
    "authority_commands",
    "authority_events",
    "authority_receipts",
    "authority_idempotency_records",
    "authority_outbox",
    "authority_production_command_commits",
    "authority_production_outbox_delivery_state",
)


def test_writer_is_default_disabled_until_persisted_cas_switch(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = AuthorityProductionWriter(
        fixture.database,
        writer_id="writer-a",
        writer_epoch=1,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    command, event, receipt, outbox = bundle(requested_revision=1)
    with pytest.raises(AuthorityProductionWriterDisabled):
        writer.persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key="disabled",
            command=command,
            event=event,
            receipt=receipt,
            outbox=outbox,
            occurred_at=1200,
        )
    assert all(value == 0 for value in table_counts(fixture.database, MUTATION_TABLES).values())


def test_legacy_shadow_repository_cannot_bypass_production_writer_fence(tmp_path):
    fixture = install_foundation(tmp_path)
    command, event, receipt, outbox = bundle(requested_revision=1)
    with pytest.raises(AuthorityWriteShadowDisabled, match="production foundation"):
        AuthorityRepository(
            fixture.database, write_shadow=True
        ).persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key="shadow-bypass",
            command=command,
            event=event,
            receipt=receipt,
            outbox=outbox,
        )
    assert all(value == 0 for value in table_counts(fixture.database, MUTATION_TABLES).values())


def test_complete_bundle_revision_and_delivery_intent_commit_atomically(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    result, _values = persist_one(writer)
    assert result.revision == 2
    assert result.committed_writer_id == "writer-a"
    assert result.committed_writer_epoch == 1
    assert result.committed_switch_epoch == 1
    assert result.committed_switch_mode == "CANARY"
    assert result.replayed is False
    counts = table_counts(fixture.database, MUTATION_TABLES)
    assert counts == {name: 1 for name in MUTATION_TABLES}
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT current_revision FROM authority_workflows"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT status FROM authority_production_outbox_delivery_state"
        ).fetchone()[0] == "PENDING"
    finally:
        connection.close()


def test_same_idempotency_and_exact_companion_bytes_replay_without_new_revision(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    first, values = persist_one(writer)
    command, event, receipt, outbox = values
    replay = writer.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-1",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
        occurred_at=1300,
    )
    assert replay.replayed is True
    assert replay.bundle_sha256 == first.bundle_sha256
    assert replay.revision == first.revision
    assert all(value == 1 for value in table_counts(fixture.database, MUTATION_TABLES).values())


def test_same_idempotency_key_with_different_command_bytes_fails_closed(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    persist_one(writer)
    command, event, receipt, outbox = bundle(requested_revision=2, suffix="different")
    with pytest.raises(AuthorityIdempotencyConflict):
        writer.persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key="idempotency-1",
            command=command,
            event=event,
            receipt=receipt,
            outbox=outbox,
            occurred_at=1300,
        )
    assert all(value == 1 for value in table_counts(fixture.database, MUTATION_TABLES).values())


def test_two_concurrent_connections_allocate_only_one_expected_revision(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    barrier = threading.Barrier(2)

    def commit(suffix: str):
        command, event, receipt, outbox = bundle(requested_revision=1, suffix=suffix)
        barrier.wait()
        try:
            value = writer.persist_command_bundle(
                workflow_id="legacy_current",
                idempotency_key=f"key-{suffix}",
                command=command,
                event=event,
                receipt=receipt,
                outbox=outbox,
                occurred_at=1300,
            )
            return "committed", value.revision
        except AuthorityRevisionConflict:
            return "conflict", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(commit, ("a", "b")))
    assert outcomes == [("committed", 2), ("conflict", None)]
    assert table_counts(fixture.database, ("authority_commands", "authority_outbox")) == {
        "authority_commands": 1,
        "authority_outbox": 1,
    }


def test_two_independent_processes_cannot_commit_the_same_expected_revision(tmp_path):
    fixture = install_foundation(tmp_path)
    configure_canary(fixture)
    barrier = tmp_path / "multiprocess-writer"
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.support.authority_production_process",
                str(fixture.database),
                fixture.preflight.source_fence_sha256,
                suffix,
                str(barrier),
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        for suffix in ("process-a", "process-b")
    ]
    deadline = time.monotonic() + 10
    while not all(
        Path(f"{barrier}.{suffix}.ready").exists()
        for suffix in ("process-a", "process-b")
    ):
        if time.monotonic() >= deadline:
            pytest.fail("writer subprocesses did not reach the barrier")
        time.sleep(0.01)
    Path(f"{barrier}.go").write_text("go\n", encoding="utf-8")
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        outputs.append(json.loads(stdout))
    assert sorted(item["status"] for item in outputs) == [
        "AuthorityRevisionConflict",
        "committed",
    ]
    assert table_counts(fixture.database, ("authority_commands", "authority_outbox")) == {
        "authority_commands": 1,
        "authority_outbox": 1,
    }


def test_writer_handoff_invalidates_stale_epoch_and_is_audited(tmp_path):
    fixture = install_foundation(tmp_path)
    stale = configure_canary(fixture)
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    stopped = operations.switch_mode(
        target_mode=V1_ONLY,
        expected_switch_epoch=1,
        operator_subject="operator-a",
        reason="writer handoff",
        occurred_at=1400,
    )
    assert stopped.switch_epoch == 2 and not stopped.writer_enabled
    configured = operations.configure_writer(
        new_writer_id="writer-b",
        enabled=True,
        expected_writer_epoch=1,
        expected_switch_epoch=2,
        operator_subject="operator-a",
        reason="new durable writer",
        occurred_at=1401,
    )
    operations.configure_consumer(
        new_consumer_id="consumer-b",
        enabled=True,
        expected_consumer_epoch=1,
        expected_switch_epoch=2,
        operator_subject="operator-a",
        reason="new durable consumer",
        occurred_at=1402,
    )
    operations.switch_mode(
        target_mode=CANARY,
        expected_switch_epoch=2,
        operator_subject="operator-a",
        reason="resume canary",
        occurred_at=1403,
    )
    assert configured.writer_epoch == 2
    command, event, receipt, outbox = bundle(requested_revision=1)
    with pytest.raises(AuthorityProductionWriterFenceLost):
        stale.persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key="stale",
            command=command,
            event=event,
            receipt=receipt,
            outbox=outbox,
            occurred_at=1404,
        )
    fresh = AuthorityProductionWriter(
        fixture.database,
        writer_id="writer-b",
        writer_epoch=2,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    assert persist_one(fresh)[0].committed_writer_id == "writer-b"


@pytest.mark.parametrize(
    "failure_stage",
    (
        "after_contract_pins",
        "after_command",
        "after_event",
        "after_receipt",
        "after_outbox_intent",
        "after_production_commit_evidence",
        "before_commit",
    ),
)
def test_failure_at_every_bundle_stage_rolls_back_allocator_and_all_rows(
    tmp_path, monkeypatch, failure_stage
):
    fixture = install_foundation(tmp_path, name=f"failure-{failure_stage}")
    writer = configure_canary(fixture)

    def fail(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(f"injected {stage}")

    monkeypatch.setattr(writer_module, "_writer_failure_point", fail)
    with pytest.raises(RuntimeError, match="injected"):
        persist_one(writer)
    assert all(value == 0 for value in table_counts(fixture.database, MUTATION_TABLES).values())
    connection = sqlite3.connect(fixture.database)
    try:
        assert connection.execute(
            "SELECT current_revision FROM authority_workflows"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_allocator_drift_fails_before_any_partial_bundle(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    connection = sqlite3.connect(fixture.database)
    connection.execute(
        "UPDATE authority_revision_allocator SET next_revision=99"
    )
    connection.commit()
    connection.close()
    with pytest.raises(AuthorityRevisionConflict, match="allocator drifted"):
        persist_one(writer)
    assert all(value == 0 for value in table_counts(fixture.database, MUTATION_TABLES).values())


def test_source_row_drift_disables_production_writer_without_touching_v1(tmp_path):
    fixture = install_foundation(tmp_path)
    writer = configure_canary(fixture)
    connection = sqlite3.connect(fixture.database)
    connection.execute("UPDATE project_state SET status='paused'")
    connection.commit()
    connection.close()
    with pytest.raises(Exception, match="source fence"):
        persist_one(writer)
    assert all(value == 0 for value in table_counts(fixture.database, MUTATION_TABLES).values())


def test_writer_facade_exposes_no_connection_transaction_or_allocator_surface():
    public_callables = {
        name
        for name, value in AuthorityProductionWriter.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    assert public_callables == {"persist_command_bundle"}
