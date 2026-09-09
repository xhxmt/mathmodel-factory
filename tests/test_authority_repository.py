from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

import factory_core.authority_repository as authority_repository
from factory_core.authority_envelopes import (
    EVENT_ENVELOPE_SCHEMA,
    OUTBOX_MESSAGE_SCHEMA,
    RECEIPT_ENVELOPE_SCHEMA,
    EnvelopeFieldV1,
    EventEnvelopeV1,
    OutboxMessageV1,
    ReceiptEnvelopeV1,
    event_envelope_sha256,
    outbox_message_sha256,
    receipt_envelope_sha256,
)
from factory_core.authority_repository import (
    AUTHORITY_SCHEMA_V2_WRITE_SHADOW,
    AuthorityEnvelopePersistenceError,
    AuthorityIdempotencyConflict,
    AuthorityRepository,
    AuthorityRepositoryNotReady,
    AuthorityRevisionConflict,
    AuthorityWriteShadowDisabled,
)
from factory_core.authority_schema import migrate_authority_schema_v2
from factory_core.command_envelope import (
    COMMAND_ENVELOPE_SCHEMA,
    ActorRefV1,
    ActorType,
    CommandEnvelopeV1,
    CommandType,
    NoEntityScopeV1,
    NoPayloadV1,
    NoSubjectScopeV1,
    PayloadBindingV1,
    ProjectGenerationBindingV1,
    RunGenerationBindingV1,
    compile_read_set,
)
from factory_core.contract_pins import CONTRACT_PIN_SET_SCHEMA, ContractPinSetV1
from factory_core.canonical import canonical_sha256
from factory_core.domain import WorkflowStatus
from factory_core.project_snapshot_v0 import (
    PROJECT_SNAPSHOT_V0_SCHEMA,
    SNAPSHOT_COORDINATE_SCHEMA,
    ProjectSnapshotV0,
    SnapshotAvailabilityV0,
    SnapshotCompletenessV0,
    SnapshotCoordinateV0,
    SnapshotErrorCodeV0,
    SnapshotSectionIdV0,
    SnapshotSectionV0,
)
from factory_core.storage import SQLiteStateStore


def _ready_database(tmp_path):
    path = tmp_path / "authority-ready.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_info(
                singleton INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL
            );
            INSERT INTO schema_info VALUES (1, 9);
            CREATE TABLE project_state(
                singleton INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                last_completed_step INTEGER NOT NULL,
                active_step INTEGER,
                runtime_generation TEXT NOT NULL,
                scheduler_generation TEXT NOT NULL,
                last_completed_stage INTEGER NOT NULL,
                active_stage INTEGER
            );
            INSERT INTO project_state VALUES(
                1, 'demo', 7, 2, NULL, 'native_v2', 'stage_v1', 1, 2
            );
            CREATE TABLE stage_checkpoints(
                stage_id INTEGER NOT NULL,
                subtask TEXT NOT NULL,
                completed_revision INTEGER,
                receipt_json TEXT,
                PRIMARY KEY(stage_id, subtask)
            );
            INSERT INTO stage_checkpoints VALUES(
                2, 'repository-ready', 7, '{"receipt":"recorded"}'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()
    report = migrate_authority_schema_v2(path, owner_token="repository-fixture")
    assert report.state == "READY"
    return path


def _pins() -> ContractPinSetV1:
    values = tuple(character * 64 for character in "12345678")
    return ContractPinSetV1(CONTRACT_PIN_SET_SCHEMA, *values)


def _bundle(*, command_id="command-1", event_id="event-1", receipt_id="receipt-1", message_id="message-1"):
    pins = _pins()
    command = CommandEnvelopeV1(
        schema_version=COMMAND_ENVELOPE_SCHEMA,
        command_id=command_id,
        command_type=CommandType.SHADOW_ADVANCE,
        project_binding=ProjectGenerationBindingV1("demo", "legacy_unknown", 7),
        run_binding=RunGenerationBindingV1("native_v2", "stage_v1", "legacy_unknown"),
        entity_scope=NoEntityScopeV1(),
        subject_scope=NoSubjectScopeV1(),
        actor=ActorRefV1(ActorType.TEST_FIXTURE, "phase2-test"),
        payload_binding=NoPayloadV1(),
        read_set=compile_read_set(()),
        contract_pins=pins,
    )
    pin_sha256 = canonical_sha256(pins)
    event = EventEnvelopeV1(
        EVENT_ENVELOPE_SCHEMA,
        event_id,
        "demo",
        "legacy_current",
        8,
        "SHADOW_RECORDED",
        command_id,
        "legacy_unknown",
        "legacy_unknown",
        "native_v2",
        "stage_v1",
        pin_sha256,
        (EnvelopeFieldV1("result", "recorded"),),
    )
    receipt = ReceiptEnvelopeV1(
        RECEIPT_ENVELOPE_SCHEMA,
        receipt_id,
        "demo",
        "legacy_current",
        8,
        command_id,
        event_id,
        "RECORDED",
        pin_sha256,
        (EnvelopeFieldV1("assurance", "shadow-only"),),
    )
    outbox = OutboxMessageV1(
        OUTBOX_MESSAGE_SCHEMA,
        message_id,
        "legacy_current",
        8,
        event_id,
        "authority.shadow.recorded",
        (EnvelopeFieldV1("receipt_id", receipt_id),),
    )
    return command, event, receipt, outbox


def _partial_snapshot(*, project_revision: int = 7) -> ProjectSnapshotV0:
    coordinate = SnapshotCoordinateV0(
        SNAPSHOT_COORDINATE_SCHEMA,
        "demo",
        9,
        project_revision,
        None,
        None,
        "native_v2",
        "stage_v1",
        None,
    )
    sections = tuple(
        SnapshotSectionV0(
            section_id,
            SnapshotAvailabilityV0.ERROR,
            coordinate,
            (),
            SnapshotErrorCodeV0.SECTION_READ_FAILED,
            None,
            None,
            None,
        )
        for section_id in SnapshotSectionIdV0
    )
    return ProjectSnapshotV0(
        PROJECT_SNAPSHOT_V0_SCHEMA,
        coordinate,
        SnapshotCompletenessV0.PARTIAL,
        sections,
        None,
        False,
        (),
        (),
    )


def test_write_shadow_is_default_off_and_requires_explicit_enable(tmp_path):
    path = _ready_database(tmp_path)
    assert AUTHORITY_SCHEMA_V2_WRITE_SHADOW is False
    repository = AuthorityRepository(path)
    command, event, receipt, outbox = _bundle()

    with pytest.raises(AuthorityWriteShadowDisabled, match="disabled"):
        repository.persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key="disabled-write",
            command=command,
            event=event,
            receipt=receipt,
            outbox=outbox,
        )

    assert repository.table_count("authority_commands") == 0


def test_command_event_receipt_idempotency_and_outbox_commit_atomically(tmp_path):
    path = _ready_database(tmp_path)
    repository = AuthorityRepository(path, write_shadow=True)
    command, event, receipt, outbox = _bundle()

    committed = repository.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-1",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
    )

    assert committed.committed_revision == 8
    assert committed.replayed is False
    for table in (
        "authority_commands",
        "authority_events",
        "authority_receipts",
        "authority_idempotency_records",
        "authority_outbox",
    ):
        assert repository.table_count(table) == 1

    replayed = repository.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-1",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
    )
    assert replayed.replayed is True
    assert replayed == replace(committed, replayed=True)
    assert repository.table_count("authority_outbox") == 1

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        workflow = connection.execute(
            "SELECT * FROM authority_workflows WHERE workflow_id='legacy_current'"
        ).fetchone()
        assert workflow["current_revision"] == 8
        assert workflow["contract_pin_availability"] == "RECORDED"
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 9
        idempotency = connection.execute(
            "SELECT request_schema, request_sha256 FROM authority_idempotency_records"
        ).fetchone()
        assert tuple(idempotency) == (
            authority_repository.AUTHORITY_IDEMPOTENCY_REQUEST_SCHEMA,
            committed.request_sha256,
        )
        for table in ("authority_commands", "authority_events", "authority_receipts", "authority_outbox"):
            row = connection.execute(
                f"SELECT envelope_json, envelope_sha256 FROM {table}"
            ).fetchone()
            assert hashlib.sha256(row["envelope_json"].encode("utf-8")).hexdigest() == row[
                "envelope_sha256"
            ]
    finally:
        connection.close()


def test_authority_envelope_bytes_have_stable_golden_hashes():
    _command, event, receipt, outbox = _bundle()

    assert event_envelope_sha256(event) == (
        "bb329b4bbb58fa948c809e53cf1f95f85d25ebda8dc59d6dff00ab1979761376"
    )
    assert receipt_envelope_sha256(receipt) == (
        "a6053dda939014b8ca252b66c9594d13c9518835db162ba221f8059b78ae0a41"
    )
    assert outbox_message_sha256(outbox) == (
        "a3186b3f77d3a773b670d18bac36a7ea59798a24442093f6c6cf9b13c685e43e"
    )


def test_authority_envelope_identity_is_stable_across_hash_seeds(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = r'''
from tests.test_authority_repository import _bundle
from factory_core.authority_envelopes import (
    event_envelope_sha256,
    outbox_message_sha256,
    receipt_envelope_sha256,
)
from factory_core.command_envelope import command_envelope_sha256
from factory_core.authority_schema import MIGRATIONS

command, event, receipt, outbox = _bundle()
print("|".join((
    command_envelope_sha256(command),
    event_envelope_sha256(event),
    receipt_envelope_sha256(receipt),
    outbox_message_sha256(outbox),
    *(migration.checksum_sha256 for migration in MIGRATIONS),
)))
'''
    outputs = []
    for seed in ("0", "1", "17", "999", "random"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        outputs.append(result.stdout)
    assert len(set(outputs)) == 1


def test_idempotency_key_reuse_with_different_command_bytes_fails_closed(tmp_path):
    path = _ready_database(tmp_path)
    repository = AuthorityRepository(path, write_shadow=True)
    first = _bundle()
    repository.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-1",
        command=first[0],
        event=first[1],
        receipt=first[2],
        outbox=first[3],
    )
    different_payload_command = replace(
        first[0],
        payload_binding=PayloadBindingV1(
            "phase2-review-payload-v1",
            "9" * 64,
        ),
    )

    with pytest.raises(AuthorityIdempotencyConflict, match="different command bytes"):
        repository.persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key="idempotency-1",
            command=different_payload_command,
            event=first[1],
            receipt=first[2],
            outbox=first[3],
        )

    assert repository.table_count("authority_commands") == 1


def test_idempotency_replay_rejects_different_companion_envelope_identity(tmp_path):
    path = _ready_database(tmp_path)
    repository = AuthorityRepository(path, write_shadow=True)
    command, event, receipt, outbox = _bundle()
    repository.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-1",
        command=command,
        event=event,
        receipt=receipt,
        outbox=outbox,
    )
    other_event = replace(event, workflow_id="other-workflow")
    other_receipt = replace(receipt, workflow_id="other-workflow")
    other_outbox = replace(outbox, workflow_id="other-workflow")

    with pytest.raises(
        AuthorityEnvelopePersistenceError, match="companion envelope identity differs"
    ):
        repository.persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key="idempotency-1",
            command=command,
            event=other_event,
            receipt=other_receipt,
            outbox=other_outbox,
        )

    assert repository.table_count("authority_commands") == 1
    assert repository.table_count("authority_outbox") == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("UPDATE schema_info SET schema_version=10 WHERE singleton=1", "future legacy"),
        ("UPDATE project_state SET revision=10 WHERE singleton=1", "source identity drifted"),
    ),
)
def test_repository_revalidates_legacy_source_fence_before_access(
    tmp_path, mutation, message
):
    path = _ready_database(tmp_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(mutation)
        connection.commit()
    finally:
        connection.close()

    repository = AuthorityRepository(path, write_shadow=True)
    with pytest.raises(AuthorityRepositoryNotReady, match=message):
        repository.table_count("authority_commands")


@pytest.mark.parametrize(
    "failing_method",
    (
        "_persist_contract_pins",
        "_persist_command_envelope",
        "_persist_event_envelope",
        "_persist_receipt_envelope",
        "_enqueue_outbox",
        "_commit_idempotency",
        "_update_workflow_revision",
    ),
)
def test_every_bundle_step_failure_rolls_back_the_complete_transaction(
    tmp_path, monkeypatch, failing_method
):
    path = _ready_database(tmp_path)
    repository = AuthorityRepository(path, write_shadow=True)
    command, event, receipt, outbox = _bundle()

    def fail_step(*args, **kwargs):
        raise RuntimeError(f"injected {failing_method} failure")

    monkeypatch.setattr(authority_repository, failing_method, fail_step)
    with pytest.raises(RuntimeError, match=f"injected {failing_method} failure"):
        repository.persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key="idempotency-1",
            command=command,
            event=event,
            receipt=receipt,
            outbox=outbox,
        )

    for table in (
        "authority_commands",
        "authority_events",
        "authority_receipts",
        "authority_idempotency_records",
        "authority_outbox",
        "authority_contract_pin_sets",
    ):
        assert repository.table_count(table) == 0
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT current_revision FROM authority_workflows"
        ).fetchone()[0] == 7
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 8
        workflow = connection.execute(
            "SELECT contract_pin_set_sha256, contract_pin_availability "
            "FROM authority_workflows"
        ).fetchone()
        assert workflow == (None, "legacy_unknown")
    finally:
        connection.close()


def test_revision_allocator_drift_fails_closed_without_partial_writes(tmp_path):
    path = _ready_database(tmp_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE authority_revision_allocator SET next_revision=7 "
            "WHERE workflow_id='legacy_current'"
        )
        connection.commit()
    finally:
        connection.close()
    command, event, receipt, outbox = _bundle()
    event = replace(event, revision=7)
    receipt = replace(receipt, revision=7)
    outbox = replace(outbox, revision=7)

    with pytest.raises(AuthorityRevisionConflict, match="next monotonic revision"):
        AuthorityRepository(path, write_shadow=True).persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key="idempotency-drift",
            command=command,
            event=event,
            receipt=receipt,
            outbox=outbox,
        )

    repository = AuthorityRepository(path, write_shadow=True)
    for table in (
        "authority_commands",
        "authority_events",
        "authority_receipts",
        "authority_idempotency_records",
        "authority_outbox",
        "authority_contract_pin_sets",
    ):
        assert repository.table_count(table) == 0
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT current_revision FROM authority_workflows"
        ).fetchone()[0] == 7
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 7
    finally:
        connection.close()


def test_final_workflow_revision_failure_rolls_back_idempotency_and_outbox(tmp_path):
    path = _ready_database(tmp_path)
    command, event, receipt, outbox = _bundle()

    def fail_workflow_update(*args, **kwargs):
        raise RuntimeError("injected workflow revision failure")

    original = authority_repository._update_workflow_revision
    authority_repository._update_workflow_revision = fail_workflow_update
    try:
        with pytest.raises(RuntimeError, match="injected workflow revision failure"):
            AuthorityRepository(path, write_shadow=True).persist_command_bundle(
                workflow_id="legacy_current",
                idempotency_key="idempotency-final-step",
                command=command,
                event=event,
                receipt=receipt,
                outbox=outbox,
            )
    finally:
        authority_repository._update_workflow_revision = original

    repository = AuthorityRepository(path, write_shadow=True)
    for table in (
        "authority_contract_pin_sets",
        "authority_commands",
        "authority_events",
        "authority_receipts",
        "authority_idempotency_records",
        "authority_outbox",
    ):
        assert repository.table_count(table) == 0
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT current_revision FROM authority_workflows"
        ).fetchone()[0] == 7
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 8
    finally:
        connection.close()


def test_authority_module_import_has_no_external_or_write_side_effects(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = r'''
import sys

sys.path.insert(0, sys.argv[1])

blocked_events = {
    "os.link",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.symlink",
    "os.system",
    "shutil.copyfile",
    "socket.connect",
    "socket.getaddrinfo",
    "sqlite3.connect",
    "subprocess.Popen",
}

def audit(event, args):
    if event in blocked_events:
        raise RuntimeError(f"blocked import audit event: {event}")
    if event == "open" and len(args) > 1:
        mode = args[1]
        if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
            raise RuntimeError(f"blocked import write-open: {mode}")

sys.addaudithook(audit)
import factory_core.authority_envelopes
import factory_core.authority_schema
import factory_core.authority_repository
print("authority-import-clean")
'''
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "authority-import-clean\n"
    assert tuple(tmp_path.iterdir()) == ()


def test_scheduler_engine_and_web_do_not_import_or_call_phase2_authority_modules():
    root = Path(__file__).resolve().parents[1]
    targets = (
        root / "factory_core/engine.py",
        root / "factory_core/shadow_scheduler.py",
        *sorted((root / "web").rglob("*.py")),
    )
    forbidden = re.compile(
        r"\b(?:authority_schema|authority_repository|authority_envelopes|"
        r"AuthorityRepository|migrate_authority_schema_v2)\b"
    )

    violations = {
        str(path.relative_to(root)): sorted(set(forbidden.findall(path.read_text("utf-8"))))
        for path in targets
        if forbidden.search(path.read_text("utf-8"))
    }
    assert violations == {}


def test_concurrent_stale_commands_cannot_allocate_the_same_revision(tmp_path):
    path = _ready_database(tmp_path)
    first = _bundle()
    second = _bundle(
        command_id="command-2",
        event_id="event-2",
        receipt_id="receipt-2",
        message_id="message-2",
    )

    def commit(key, bundle):
        return AuthorityRepository(path, write_shadow=True).persist_command_bundle(
            workflow_id="legacy_current",
            idempotency_key=key,
            command=bundle[0],
            event=bundle[1],
            receipt=bundle[2],
            outbox=bundle[3],
        )

    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(commit, "idempotency-1", first),
            pool.submit(commit, "idempotency-2", second),
        ]
        for future in futures:
            try:
                outcomes.append(("committed", future.result(timeout=5).committed_revision))
            except AuthorityRevisionConflict:
                outcomes.append(("conflict", None))

    assert sorted(outcomes, key=lambda item: item[0]) == [
        ("committed", 8),
        ("conflict", None),
    ]
    repository = AuthorityRepository(path, write_shadow=True)
    assert repository.table_count("authority_events") == 1
    assert repository.table_count("authority_outbox") == 1
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 9
    finally:
        connection.close()


def test_supported_bundle_boundary_allocates_exactly_consecutive_revisions(tmp_path):
    path = _ready_database(tmp_path)
    repository = AuthorityRepository(path, write_shadow=True)
    first = _bundle()
    first_result = repository.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-1",
        command=first[0],
        event=first[1],
        receipt=first[2],
        outbox=first[3],
    )
    second = _bundle(
        command_id="command-2",
        event_id="event-2",
        receipt_id="receipt-2",
        message_id="message-2",
    )
    second_command = replace(
        second[0],
        project_binding=replace(second[0].project_binding, project_revision=8),
    )
    second_event = replace(second[1], revision=9)
    second_receipt = replace(second[2], revision=9)
    second_outbox = replace(second[3], revision=9)
    second_result = repository.persist_command_bundle(
        workflow_id="legacy_current",
        idempotency_key="idempotency-2",
        command=second_command,
        event=second_event,
        receipt=second_receipt,
        outbox=second_outbox,
    )

    assert (first_result.committed_revision, second_result.committed_revision) == (8, 9)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT persisted_revision FROM authority_commands ORDER BY persisted_revision"
        ).fetchall() == [(8,), (9,)]
        assert connection.execute(
            "SELECT current_revision FROM authority_workflows"
        ).fetchone()[0] == 9
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 10
    finally:
        connection.close()


def test_snapshot_write_is_ready_gated_and_bound_to_current_workflow_coordinate(tmp_path):
    path = _ready_database(tmp_path)
    repository = AuthorityRepository(path, write_shadow=True)

    digest = repository.persist_project_snapshot(
        snapshot_id="snapshot-current",
        workflow_id="legacy_current",
        snapshot=_partial_snapshot(project_revision=7),
    )
    assert len(digest) == 64
    assert repository.table_count("authority_project_snapshots") == 1

    with pytest.raises(AuthorityRevisionConflict, match="differs from the current"):
        repository.persist_project_snapshot(
            snapshot_id="snapshot-forged-revision",
            workflow_id="legacy_current",
            snapshot=_partial_snapshot(project_revision=99),
        )
    assert repository.table_count("authority_project_snapshots") == 1


def test_repository_exposes_no_partial_mutation_or_allocator_transaction_surface(tmp_path):
    path = _ready_database(tmp_path)
    repository = AuthorityRepository(path, write_shadow=True)

    assert not hasattr(authority_repository, "AuthorityUnitOfWork")
    assert not hasattr(repository, "transaction")
    assert {
        name
        for name in dir(repository)
        if not name.startswith("_")
    } == {"path", "persist_command_bundle", "persist_project_snapshot", "table_count", "write_shadow"}

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT current_revision FROM authority_workflows"
        ).fetchone()[0] == 7
        assert connection.execute(
            "SELECT next_revision FROM authority_revision_allocator"
        ).fetchone()[0] == 8
    finally:
        connection.close()


def test_existing_v1_writer_behavior_is_unchanged_after_additive_install(tmp_path):
    project = tmp_path / "project"
    store = SQLiteStateStore(project)
    before = store.initialize(project_id="legacy-writer", project_type="modeling")
    before_events = store.events()

    report = migrate_authority_schema_v2(
        store.path, owner_token="legacy-writer-additive-install"
    )

    assert report.state == "MIGRATION_BLOCKED_OWNER_AMBIGUOUS"
    assert store.load() == before
    assert store.events() == before_events
    updated = store.transition(
        expected_revision=before.revision,
        event_type="PAUSED",
        changes={"status": WorkflowStatus.PAUSED},
    )
    assert updated.status is WorkflowStatus.PAUSED
    assert updated.revision == before.revision + 1
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT schema_version FROM schema_info WHERE singleton=1"
        ).fetchone()[0] == 9
    finally:
        connection.close()
