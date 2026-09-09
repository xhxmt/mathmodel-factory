from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from factory_core.authority_envelopes import (
    EVENT_ENVELOPE_SCHEMA,
    OUTBOX_MESSAGE_SCHEMA,
    RECEIPT_ENVELOPE_SCHEMA,
    EnvelopeFieldV1,
    EventEnvelopeV1,
    OutboxMessageV1,
    ReceiptEnvelopeV1,
)
from factory_core.authority_operations import (
    CANARY,
    AuthorityOperations,
    BackupEvidence,
    create_authority_backup,
    initial_database_identity_binding,
)
from factory_core.authority_production_schema import (
    ProductionPreflight,
    migrate_authority_production_foundation,
    production_preflight,
)
from factory_core.authority_production_writer import AuthorityProductionWriter
from factory_core.authority_schema import migrate_authority_schema_v2
from factory_core.canonical import canonical_sha256
from factory_core.command_envelope import (
    COMMAND_ENVELOPE_SCHEMA,
    ActorRefV1,
    ActorType,
    CommandEnvelopeV1,
    CommandType,
    NoEntityScopeV1,
    NoPayloadV1,
    NoSubjectScopeV1,
    ProjectGenerationBindingV1,
    RunGenerationBindingV1,
    compile_read_set,
)
from factory_core.contract_pins import CONTRACT_PIN_SET_SCHEMA, ContractPinSetV1
from factory_core.stages import STAGE_SCHEDULER_GENERATION
from factory_core.storage import SQLiteStateStore


@dataclass(frozen=True)
class InstalledFoundation:
    project_dir: Path
    database: Path
    preflight: ProductionPreflight
    backup: BackupEvidence | None
    backup_path: Path | None


def prepare_initial_backup(
    tmp_path: Path,
    database: Path,
    preflight: ProductionPreflight,
    *,
    database_id: str,
    name: str,
    occurred_at: int = 1000,
):
    path = tmp_path / f"{name}.pre-authority.backup.db"
    evidence = create_authority_backup(
        database,
        path,
        database_id=database_id,
        occurred_at=occurred_at,
        expected_source_fence_sha256=preflight.source_fence_sha256,
    )
    return evidence, path, initial_database_identity_binding(evidence)


def create_real_schema_v9(tmp_path: Path, *, name: str = "authority-project") -> tuple[Path, Path]:
    project = tmp_path / name
    store = SQLiteStateStore(project, clock=lambda: 100)
    store.initialize(
        project_id="demo",
        project_type="modeling",
        last_completed_step=1,
        scheduler_generation=STAGE_SCHEDULER_GENERATION,
    )
    assert store.stage_checkpoints()
    return project, store.path


def install_foundation(
    tmp_path: Path,
    *,
    name: str = "authority-project",
    with_backup: bool = False,
) -> InstalledFoundation:
    project, database = create_real_schema_v9(tmp_path, name=name)
    preflight = production_preflight(database, database_id=f"{name}-db")
    migration_backup_path = tmp_path / f"{name}.pre-authority.backup.db"
    migration_backup = create_authority_backup(
        database,
        migration_backup_path,
        database_id=f"{name}-db",
        occurred_at=1000,
        expected_source_fence_sha256=preflight.source_fence_sha256,
    )
    base = migrate_authority_schema_v2(database, owner_token=f"{name}-base-owner")
    assert base.state == "READY"
    production = migrate_authority_production_foundation(
        database,
        database_id=f"{name}-db",
        expected_source_fence_sha256=preflight.source_fence_sha256,
        owner_token=f"{name}-production-owner",
        identity_binding=initial_database_identity_binding(migration_backup),
        pre_authority_backup=migration_backup_path,
    )
    assert production.state == "READY"
    return InstalledFoundation(
        project, database, preflight, migration_backup, migration_backup_path
    )


def configure_canary(fixture: InstalledFoundation) -> AuthorityProductionWriter:
    operations = AuthorityOperations(
        fixture.database,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )
    writer = operations.configure_writer(
        new_writer_id="writer-a",
        enabled=True,
        expected_writer_epoch=0,
        expected_switch_epoch=0,
        operator_subject="operator-a",
        reason="test canary writer",
        occurred_at=1100,
    )
    assert writer.writer_epoch == 1
    consumer = operations.configure_consumer(
        new_consumer_id="consumer-a",
        enabled=True,
        expected_consumer_epoch=0,
        expected_switch_epoch=0,
        operator_subject="operator-a",
        reason="test canary consumer",
        occurred_at=1101,
    )
    assert consumer.consumer_epoch == 1
    switched = operations.switch_mode(
        target_mode=CANARY,
        expected_switch_epoch=0,
        operator_subject="operator-a",
        reason="test canary activation",
        occurred_at=1102,
    )
    assert switched.switch_epoch == 1
    return AuthorityProductionWriter(
        fixture.database,
        writer_id="writer-a",
        writer_epoch=1,
        expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
    )


def bundle(
    *,
    requested_revision: int,
    suffix: str = "1",
) -> tuple[CommandEnvelopeV1, EventEnvelopeV1, ReceiptEnvelopeV1, OutboxMessageV1]:
    pins = ContractPinSetV1(
        CONTRACT_PIN_SET_SCHEMA,
        *(character * 64 for character in "12345678"),
    )
    command_id = f"command-{suffix}"
    event_id = f"event-{suffix}"
    receipt_id = f"receipt-{suffix}"
    message_id = f"message-{suffix}"
    command = CommandEnvelopeV1(
        COMMAND_ENVELOPE_SCHEMA,
        command_id,
        CommandType.SHADOW_ADVANCE,
        ProjectGenerationBindingV1("demo", "legacy_unknown", requested_revision),
        RunGenerationBindingV1("native_v2", "stage_v1", "legacy_unknown"),
        NoEntityScopeV1(),
        NoSubjectScopeV1(),
        ActorRefV1(ActorType.TEST_FIXTURE, "production-authority-test"),
        NoPayloadV1(),
        compile_read_set(()),
        pins,
    )
    pin_sha256 = canonical_sha256(pins)
    revision = requested_revision + 1
    event = EventEnvelopeV1(
        EVENT_ENVELOPE_SCHEMA,
        event_id,
        "demo",
        "legacy_current",
        revision,
        "PRODUCTION_RECORDED",
        command_id,
        "legacy_unknown",
        "legacy_unknown",
        "native_v2",
        "stage_v1",
        pin_sha256,
        (EnvelopeFieldV1("fixture", suffix),),
    )
    receipt = ReceiptEnvelopeV1(
        RECEIPT_ENVELOPE_SCHEMA,
        receipt_id,
        "demo",
        "legacy_current",
        revision,
        command_id,
        event_id,
        "RECORDED",
        pin_sha256,
        (EnvelopeFieldV1("assurance", "production-foundation-test"),),
    )
    outbox = OutboxMessageV1(
        OUTBOX_MESSAGE_SCHEMA,
        message_id,
        "legacy_current",
        revision,
        event_id,
        "authority.production.recorded",
        (EnvelopeFieldV1("receipt_id", receipt_id),),
    )
    return command, event, receipt, outbox


def persist_one(
    writer: AuthorityProductionWriter,
    *,
    requested_revision: int = 1,
    suffix: str = "1",
    occurred_at: int = 1200,
):
    command, event, receipt, outbox = bundle(
        requested_revision=requested_revision, suffix=suffix
    )
    result = writer.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key=f"idempotency-{suffix}",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
        occurred_at=occurred_at,
    )
    return result, (command, event, receipt, outbox)


def table_counts(database: Path, names: tuple[str, ...]) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return {
            name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in names
        }
    finally:
        connection.close()
