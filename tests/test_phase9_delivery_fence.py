from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import factory_core.phase9_delivery_fence as delivery_fence_module
from factory_core.cli import main as factory_cli_main
from factory_core.audit.acceptance import build_final_acceptance_receipt
from factory_core.audit.domain import AuditSnapshot, AuditStatus
from factory_core.audit.service import FinalAuditService
from factory_core.delivery.release import ReleasePublisher, resolve_current_release
from factory_core.domain import StepContext, WorkflowStatus
from factory_core.phase9_authority_lease import (
    AuthorityStateLeaseError,
    authority_database_commit_lease,
    authority_state_commit_lease,
    isolated_authority_snapshot_ro,
)
from factory_core.phase9_delivery_fence import (
    Phase9DeliveryFence,
    Phase9DeliveryFenceError,
    collect_phase9_delivery_fence,
    delivery_side_effect_commit_lease,
    require_delivery_side_effect_authority,
    require_phase9_delivery_authority,
)
from factory_core.phase9_forensic_replay import Phase9ForensicReplayConflict
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore
from factory_core.steps.catalog import contract_for
from factory_core.steps.specialized import DeliveryStep
from scripts.package_submission import package_submission
from scripts.publish_release import publish_current_audit
from tests.support.authority_production import install_foundation


def _tree_fingerprint(root: Path) -> tuple[tuple[object, ...], ...]:
    """Return a content/type snapshot without relying on directory mtimes."""

    if not root.exists():
        return ((".", "missing"),)
    result: list[tuple[object, ...]] = []
    for path in sorted((root, *root.rglob("*")), key=lambda item: item.as_posix()):
        relative = "." if path == root else path.relative_to(root).as_posix()
        stat_result = path.lstat()
        if path.is_symlink():
            result.append((relative, "symlink", path.readlink().as_posix()))
        elif path.is_dir():
            result.append((relative, "directory", stat_result.st_mode & 0o7777))
        elif path.is_file():
            content = path.read_bytes()
            result.append(
                (
                    relative,
                    "file",
                    stat_result.st_mode & 0o7777,
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                )
            )
        else:
            result.append((relative, "other", stat_result.st_mode))
    return tuple(result)


def _database_fingerprint(
    database: Path,
) -> tuple[int, str, tuple[tuple[str, int], ...]]:
    """Bind both raw Authority bytes and its complete table cardinalities."""

    content = database.read_bytes()
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro", uri=True
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        counts = []
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            count = int(
                connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            )
            counts.append((name, count))
    finally:
        connection.close()
    return len(content), hashlib.sha256(content).hexdigest(), tuple(counts)


def _database_family_bytes(database: Path) -> dict[str, bytes]:
    """Capture main and sidecar bytes without asking SQLite to open them."""

    return {
        path.name: path.read_bytes()
        for path in sorted(database.parent.glob(f"{database.name}*"))
        if path.is_file()
    }


def _install_current_phase9_coordinate(
    project: Path,
    *,
    project_id: str | None = None,
    workflow_id: str = "workflow:demo",
    run_generation: str = "run-generation:current",
) -> Path:
    state = project / ".factory"
    state.mkdir(parents=True, exist_ok=True)
    database = state / "state.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE authority_production_run_generations (
                project_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                run_mode TEXT NOT NULL,
                PRIMARY KEY (workflow_id, run_generation)
            );
            CREATE TABLE authority_production_run_generation_current (
                workflow_id TEXT PRIMARY KEY,
                run_generation TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO authority_production_run_generations VALUES (?, ?, ?, ?)",
            (
                project_id or project.name,
                workflow_id,
                run_generation,
                "FORENSIC_REPLAY",
            ),
        )
        connection.execute(
            "INSERT INTO authority_production_run_generation_current VALUES (?, ?)",
            (workflow_id, run_generation),
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _remove_phase9_generation_controls(
    database: Path, *, recreate_empty: bool
) -> None:
    """Leave Phase9 residue while optionally retaining empty control schema."""

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO authority_production_run_generation_source_inventories(
                inventory_sha256, schema_version,
                source_commit, source_tree, source_parent,
                path_count, total_bytes, inventory_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a" * 64,
                "authority-phase9-git-tracked-source-inventory-v1",
                "b" * 40,
                "c" * 40,
                "d" * 40,
                1,
                1,
                "{}",
                1,
            ),
        )
        schema = connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE (type='table' AND name IN (
                       'authority_production_run_generations',
                       'authority_production_run_generation_current'
                   ))
               OR (type='trigger' AND tbl_name IN (
                       'authority_production_run_generations',
                       'authority_production_run_generation_current'
                   ))
            ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name
            """
        ).fetchall()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE authority_production_run_generation_current")
        connection.execute("DROP TABLE authority_production_run_generations")
        if recreate_empty:
            for _kind, _name, statement in schema:
                assert isinstance(statement, str)
                connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture(scope="module")
def phase9_authority_template(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    fixture = install_foundation(
        tmp_path_factory.mktemp("delivery-authority-template"), name="demo"
    )
    return fixture.database.read_bytes()


@pytest.fixture(scope="module")
def completed_phase9_authority_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[bytes, str, str]:
    """Build one real immutable terminal graph for delivery-fence tests."""

    from tests.test_phase9_forensic_replay import _fixture

    root = tmp_path_factory.mktemp("completed-delivery-authority-template")
    foundation, _evidence, request, service = _fixture(root)
    service.execute(request)

    # Normalize the committed main/WAL state into one standalone database so
    # every parameterized case starts from identical immutable bytes.  This is
    # a fixture copy only; the collector and graph verifier remain entirely
    # real in each test invocation.
    snapshot = root / "completed-template.db"
    source = sqlite3.connect(
        f"file:{foundation.database.as_posix()}?mode=ro", uri=True
    )
    destination = sqlite3.connect(snapshot)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    return snapshot.read_bytes(), request.workflow_id, request.run_generation


def _seed_delivery_decoys(
    project: Path, papers: Path, *, authority_bytes: bytes
) -> Path:
    """Install tempting project-local PASS/override/stale-release material."""

    state_dir = project / ".factory"
    state_dir.mkdir(parents=True)
    database = state_dir / "state.db"
    database.write_bytes(authority_bytes)

    audit_dir = state_dir / "audits"
    audit_dir.mkdir()
    (audit_dir / "latest.json").write_text(
        json.dumps(
            {
                "profile": "final",
                "status": "PASS",
                "delivery_allowed": True,
                "snapshot_id": "a" * 64,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    judge_outputs = project / "judge_outputs"
    judge_outputs.mkdir()
    (judge_outputs / "delivery_override_receipt.json").write_text(
        '{"status":"OVERRIDDEN","delivery_allowed":true}\n', encoding="utf-8"
    )
    (judge_outputs / "final_acceptance_receipt.json").write_text(
        '{"status":"PASS","delivery_allowed":true}\n', encoding="utf-8"
    )
    (judge_outputs / "final_submission.sha256").write_text(
        "a" * 64 + "\n", encoding="ascii"
    )

    stale_release = papers / "releases" / project.name / ("b" * 64)
    stale_release.mkdir(parents=True)
    (stale_release / "delivery_manifest.json").write_text(
        '{"status":"PASS","delivery_capability":"ENABLED"}\n', encoding="utf-8"
    )
    stale_pointer = papers / project.name / "current.json"
    stale_pointer.parent.mkdir(parents=True)
    stale_pointer.write_text(
        json.dumps(
            {
                "release_id": "b" * 64,
                "run_generation": "run-generation:old",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return database


def _snapshot(project: Path) -> AuditSnapshot:
    return AuditSnapshot(
        snapshot_id="a" * 64,
        base=project.name,
        profile="final",
        created_at="2026-09-02T00:00:00+00:00",
        identity={"source": "formal"},
    )


@pytest.mark.parametrize(
    "operation",
    ["acceptance", "release", "submission", "delivery", "completion", "archive"],
)
def test_current_phase9_side_effect_requires_explicit_coordinates_before_io(
    tmp_path: Path,
    operation: str,
    completed_phase9_authority_template: tuple[bytes, str, str],
) -> None:
    authority_bytes, _workflow_id, _run_generation = (
        completed_phase9_authority_template
    )
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    database.write_bytes(authority_bytes)
    before = _tree_fingerprint(project)
    before_database = _database_family_bytes(database)

    with pytest.raises(
        Phase9DeliveryFenceError,
        match=rf"Phase9 {operation} requires explicit workflow_id and run_generation",
    ):
        require_delivery_side_effect_authority(project, operation=operation)

    assert _tree_fingerprint(project) == before
    assert _database_family_bytes(database) == before_database


def test_phase9_side_effect_rejects_mismatched_coordinate_before_io(
    tmp_path: Path,
    completed_phase9_authority_template: tuple[bytes, str, str],
) -> None:
    authority_bytes, workflow_id, run_generation = (
        completed_phase9_authority_template
    )
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    database.write_bytes(authority_bytes)
    before = _tree_fingerprint(project)
    before_database = _database_family_bytes(database)

    with pytest.raises(Phase9DeliveryFenceError, match="coordinate does not match"):
        require_delivery_side_effect_authority(
            project,
            operation="release",
            workflow_id=f"{workflow_id}:other",
            run_generation=run_generation,
        )

    assert _tree_fingerprint(project) == before
    assert _database_family_bytes(database) == before_database


@pytest.mark.parametrize(
    "operation",
    ["acceptance", "release", "submission", "delivery", "completion", "archive"],
)
def test_exact_phase9_coordinate_is_still_permanently_disabled(
    tmp_path: Path,
    operation: str,
    completed_phase9_authority_template: tuple[bytes, str, str],
) -> None:
    authority_bytes, workflow_id, run_generation = (
        completed_phase9_authority_template
    )
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    database.write_bytes(authority_bytes)
    before = _tree_fingerprint(project)
    before_database = _database_family_bytes(database)

    with pytest.raises(
        Phase9DeliveryFenceError,
        match=rf"Phase9 {operation} is permanently disabled",
    ):
        require_delivery_side_effect_authority(
            project,
            operation=operation,
            workflow_id=workflow_id,
            run_generation=run_generation,
        )

    assert _tree_fingerprint(project) == before
    assert _database_family_bytes(database) == before_database


def test_phase9_project_binding_mismatch_is_not_reclassified_as_legacy(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project, project_id="other")
    before = _tree_fingerprint(project)
    before_database = _database_fingerprint(database)

    with pytest.raises(Phase9DeliveryFenceError, match="project binding"):
        require_delivery_side_effect_authority(project, operation="delivery")

    assert _tree_fingerprint(project) == before
    assert _database_fingerprint(database) == before_database


def test_dangling_current_generation_is_not_reclassified_as_legacy(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM authority_production_run_generations")
        connection.commit()
    finally:
        connection.close()
    before = _tree_fingerprint(project)
    before_database = _database_fingerprint(database)

    with pytest.raises(Phase9DeliveryFenceError, match="without its immutable"):
        require_delivery_side_effect_authority(project, operation="delivery")

    assert _tree_fingerprint(project) == before
    assert _database_fingerprint(database) == before_database


def test_invalid_current_generation_mode_is_not_reclassified_as_legacy(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE authority_production_run_generations SET run_mode='DELIVERY'"
        )
        connection.commit()
    finally:
        connection.close()
    before = _tree_fingerprint(project)
    before_database = _database_fingerprint(database)

    with pytest.raises(Phase9DeliveryFenceError, match="invalid current"):
        require_delivery_side_effect_authority(project, operation="delivery")

    assert _tree_fingerprint(project) == before
    assert _database_fingerprint(database) == before_database


def test_immutable_phase9_history_without_current_pointer_fails_closed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM authority_production_run_generation_current")
        connection.commit()
    finally:
        connection.close()
    before_database = database.read_bytes()

    with pytest.raises(
        Phase9DeliveryFenceError, match="history without a current generation pointer"
    ):
        require_delivery_side_effect_authority(project, operation="delivery")

    assert database.read_bytes() == before_database


def test_empty_phase9_control_tables_without_foundation_fail_closed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE authority_production_run_generations (
                project_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                run_generation TEXT NOT NULL,
                run_mode TEXT NOT NULL,
                PRIMARY KEY (workflow_id, run_generation)
            );
            CREATE TABLE authority_production_run_generation_current (
                workflow_id TEXT PRIMARY KEY,
                run_generation TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()
    before = _database_family_bytes(database)

    with pytest.raises(
        Phase9DeliveryFenceError,
        match="Phase9 delivery scope cannot be classified safely",
    ):
        require_delivery_side_effect_authority(project, operation="delivery")

    assert _database_family_bytes(database) == before
    assert not (project / "judge_outputs").exists()


@pytest.mark.parametrize(
    "operation",
    ["acceptance", "release", "submission", "delivery", "completion", "archive"],
)
@pytest.mark.parametrize("recreate_empty", [False, True])
def test_phase9_history_cannot_be_reclassified_as_legacy_when_controls_are_removed(
    tmp_path: Path,
    operation: str,
    recreate_empty: bool,
    phase9_authority_template: bytes,
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    database.write_bytes(phase9_authority_template)
    _remove_phase9_generation_controls(database, recreate_empty=recreate_empty)
    before = _tree_fingerprint(project)
    before_database = _database_family_bytes(database)

    with pytest.raises(Phase9DeliveryFenceError, match="Phase9 delivery classification"):
        require_delivery_side_effect_authority(project, operation=operation)

    assert _tree_fingerprint(project) == before
    assert _database_family_bytes(database) == before_database
    assert not (project / "judge_outputs").exists()


@pytest.mark.parametrize(
    ("version", "last_migration", "migration_row"),
    [
        (3, "A2_0015_PHASE9_RUN_GENERATION", None),
        (
            2,
            "A2_0014_DATABASE_IDENTITY_AND_BACKUP_LINEAGE",
            "A2_0015_PHASE9_RUN_GENERATION",
        ),
    ],
)
def test_phase9_migration_metadata_cannot_be_reclassified_as_legacy(
    tmp_path: Path,
    version: int,
    last_migration: str,
    migration_row: str | None,
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE authority_production_schema_state (
                singleton INTEGER PRIMARY KEY,
                production_schema_version INTEGER NOT NULL,
                last_completed_migration TEXT
            );
            CREATE TABLE authority_production_migrations (
                migration_id TEXT PRIMARY KEY
            );
            """
        )
        connection.execute(
            "INSERT INTO authority_production_schema_state VALUES (1, ?, ?)",
            (version, last_migration),
        )
        if migration_row is not None:
            connection.execute(
                "INSERT INTO authority_production_migrations VALUES (?)",
                (migration_row,),
            )
        connection.commit()
    finally:
        connection.close()
    before = _database_family_bytes(database)

    with pytest.raises(Phase9DeliveryFenceError, match="Phase9 markers"):
        require_delivery_side_effect_authority(project, operation="delivery")

    assert _database_family_bytes(database) == before


def test_pre_phase9_production_metadata_remains_legacy_compatible(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE authority_production_schema_state (
                singleton INTEGER PRIMARY KEY,
                production_schema_version INTEGER NOT NULL,
                last_completed_migration TEXT
            );
            CREATE TABLE authority_production_migrations (
                migration_id TEXT PRIMARY KEY
            );
            INSERT INTO authority_production_schema_state VALUES (
                1, 2, 'A2_0014_DATABASE_IDENTITY_AND_BACKUP_LINEAGE'
            );
            INSERT INTO authority_production_migrations VALUES (
                'A2_0014_DATABASE_IDENTITY_AND_BACKUP_LINEAGE'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()
    before = _database_family_bytes(database)

    assert (
        require_delivery_side_effect_authority(project, operation="delivery") is None
    )

    assert _database_family_bytes(database) == before


def test_ready_foundation_without_generation_remains_legacy_compatible(
    tmp_path: Path,
    phase9_authority_template: bytes,
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    database.write_bytes(phase9_authority_template)
    before = _database_family_bytes(database)

    assert (
        require_delivery_side_effect_authority(project, operation="delivery") is None
    )

    assert _database_family_bytes(database) == before
    assert not (project / "judge_outputs").exists()


def test_phase9_project_cannot_be_completed_after_schedule_exhaustion(
    tmp_path: Path,
) -> None:
    project = tmp_path / "ongoing" / "demo"
    project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    original = store.initialize(
        project_id="demo", project_type="modeling", last_completed_step=16
    )
    database = _install_current_phase9_coordinate(project)
    before_database = database.read_bytes()

    with pytest.raises(
        Phase9DeliveryFenceError,
        match="Phase9 completion requires explicit workflow_id and run_generation",
    ):
        FactoryService(tmp_path).run(project, archive=False)

    current = store.load()
    assert current.revision == original.revision
    assert current.status is WorkflowStatus.READY
    assert database.read_bytes() == before_database


def test_phase9_completed_project_cannot_be_archived_or_moved(tmp_path: Path) -> None:
    project = tmp_path / "ongoing" / "demo"
    project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    state = store.initialize(project_id="demo", project_type="modeling")
    completed = store.transition(
        expected_revision=state.revision,
        event_type="PROJECT_COMPLETED",
        changes={"status": WorkflowStatus.COMPLETED, "last_completed_step": 16},
    )
    database = _install_current_phase9_coordinate(project)
    before_database = database.read_bytes()

    with pytest.raises(
        Phase9DeliveryFenceError,
        match="Phase9 archive requires explicit workflow_id and run_generation",
    ):
        FactoryService(tmp_path).archive(project)

    current = store.load()
    assert current.revision == completed.revision
    assert current.status is WorkflowStatus.COMPLETED
    assert project.is_dir()
    assert not (tmp_path / "complete" / "demo").exists()
    assert database.read_bytes() == before_database


def test_delivery_manifest_writer_is_a_real_fenced_side_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    before_database = database.read_bytes()

    def forbidden_evaluate(*_args, **_kwargs):
        raise AssertionError("delivery evaluation must not run before the fence")

    monkeypatch.setattr(
        "scripts.delivery_contract.evaluate_modeling_project.evaluate",
        forbidden_evaluate,
    )
    from scripts.delivery_contract import write_delivery_manifest

    with pytest.raises(
        Phase9DeliveryFenceError,
        match="Phase9 delivery requires explicit workflow_id and run_generation",
    ):
        write_delivery_manifest(project, tmp_path)

    assert not (project / "delivery_manifest.json").exists()
    assert database.read_bytes() == before_database


def test_authority_commit_lease_is_same_thread_reentrant(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    _install_current_phase9_coordinate(project)

    with authority_state_commit_lease(project):
        with authority_state_commit_lease(project):
            pass


def test_authority_commit_lease_serializes_threads(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    _install_current_phase9_coordinate(project)
    attempted = threading.Event()
    acquired = threading.Event()
    errors: list[BaseException] = []

    def contender() -> None:
        attempted.set()
        try:
            with authority_state_commit_lease(project):
                acquired.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with authority_state_commit_lease(project):
        thread = threading.Thread(target=contender)
        thread.start()
        assert attempted.wait(timeout=2)
        assert not acquired.wait(timeout=0.1)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert acquired.is_set()
    assert errors == []


def test_authority_commit_lease_does_not_inherit_reentrancy_across_fork(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    _install_current_phase9_coordinate(project)
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)

    def contender() -> None:
        sender.send("attempted")
        with authority_state_commit_lease(project):
            sender.send("acquired")

    process = context.Process(target=contender)
    try:
        with authority_state_commit_lease(project):
            process.start()
            sender.close()
            assert receiver.poll(2)
            assert receiver.recv() == "attempted"
            assert not receiver.poll(0.2)
        assert receiver.poll(3)
        assert receiver.recv() == "acquired"
        process.join(timeout=3)
        assert not process.is_alive()
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        receiver.close()


def test_absent_database_creation_is_serialized_by_project_inode(
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    attempted = threading.Event()
    created = threading.Event()

    def install_phase9() -> None:
        attempted.set()
        with authority_state_commit_lease(project):
            _install_current_phase9_coordinate(project)
            created.set()

    marker = project / "legacy-delivery-marker"
    with delivery_side_effect_commit_lease(project, operation="delivery"):
        thread = threading.Thread(target=install_phase9)
        thread.start()
        assert attempted.wait(timeout=2)
        assert not created.wait(timeout=0.1)
        marker.write_text("committed before Phase9\n", encoding="utf-8")
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert created.is_set()
    assert marker.is_file()
    with pytest.raises(Phase9DeliveryFenceError, match="requires explicit"):
        require_delivery_side_effect_authority(project, operation="delivery")


def test_sanctioned_run_generation_writer_obeys_delivery_commit_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.test_phase9_run_generation import _request, _service
    import factory_core.phase9_run_generation as run_generation_module

    fixture = install_foundation(tmp_path, name="demo")
    source_repository = tmp_path / "clean-source"
    source_repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source_repository, check=True)
    source_file = source_repository / "source.txt"
    source_file.write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.txt"], cwd=source_repository, check=True)
    commit_options = [
        "-c",
        "user.name=Phase9 Test",
        "-c",
        "user.email=phase9-test@example.invalid",
        "commit",
        "-q",
    ]
    subprocess.run(
        ["git", *commit_options, "-m", "first"],
        cwd=source_repository,
        check=True,
    )
    source_file.write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.txt"], cwd=source_repository, check=True)
    subprocess.run(
        ["git", *commit_options, "-m", "second"],
        cwd=source_repository,
        check=True,
    )
    monkeypatch.setenv("PHASE9_TEST_SOURCE_REPOSITORY", str(source_repository))
    verified_source = run_generation_module.read_current_git_source_snapshot(
        source_repository
    )
    request = _request()
    service = _service(fixture, request=request)
    monkeypatch.setattr(
        run_generation_module,
        "read_verified_execution_source_snapshot",
        lambda *_args, **_kwargs: verified_source,
    )
    original_lease = run_generation_module.authority_state_commit_lease
    attempted = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def signaled_lease(project):
        attempted.set()
        return original_lease(project)

    monkeypatch.setattr(
        run_generation_module, "authority_state_commit_lease", signaled_lease
    )

    def create_generation() -> None:
        try:
            service.create_or_rotate(request)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    marker = fixture.project_dir / "legacy-delivery-marker"
    with delivery_side_effect_commit_lease(
        fixture.project_dir, operation="delivery"
    ):
        thread = threading.Thread(target=create_generation)
        thread.start()
        assert attempted.wait(timeout=20)
        assert not finished.wait(timeout=0.1)
        marker.write_text("linearized before Phase9\n", encoding="utf-8")
    thread.join(timeout=30)

    assert not thread.is_alive()
    assert errors == []
    assert marker.is_file()
    with pytest.raises(Phase9DeliveryFenceError, match="requires explicit"):
        require_delivery_side_effect_authority(
            fixture.project_dir, operation="delivery"
        )


def test_authority_commit_lease_rejects_hardlinked_database(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    os.link(database, tmp_path / "state-alias.db")

    with pytest.raises(AuthorityStateLeaseError, match="link count"):
        with authority_state_commit_lease(project):
            raise AssertionError("unsafe hardlinked database was leased")


def test_authority_commit_lease_rejects_symlinked_database(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    target = tmp_path / "outside.db"
    sqlite3.connect(target).close()
    (state / "state.db").symlink_to(target)

    with pytest.raises(AuthorityStateLeaseError, match="unsafe type"):
        with authority_state_commit_lease(project):
            raise AssertionError("unsafe symlinked database was leased")


def test_authority_commit_lease_rejects_database_path_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    import factory_core.phase9_authority_lease as lease_module

    original_flock = lease_module.fcntl.flock
    replaced = False

    def replace_after_lock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        original_flock(descriptor, operation)
        if (
            not replaced
            and operation == lease_module.fcntl.LOCK_EX
            and os.fstat(descriptor).st_ino == database.stat().st_ino
        ):
            replaced = True
            database.replace(tmp_path / "original-state.db")
            sqlite3.connect(database).close()

    monkeypatch.setattr(lease_module.fcntl, "flock", replace_after_lock)

    with pytest.raises(AuthorityStateLeaseError, match="changed while acquiring"):
        with authority_state_commit_lease(project):
            raise AssertionError("replaced database path was leased")


@pytest.mark.parametrize("replacement", ["database", "factory-directory"])
def test_project_local_database_lease_rechecks_path_inside_project_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replacement: str,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    factory = database.parent
    import factory_core.phase9_authority_lease as lease_module

    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    replacement_database = replacement_root / "state.db"
    sqlite3.connect(replacement_database).close()

    @contextmanager
    def replace_before_inner_recheck(_project: Path):
        if replacement == "database":
            database.replace(tmp_path / "original-state.db")
            replacement_database.replace(database)
        else:
            replacement_factory = replacement_root / ".factory"
            replacement_factory.mkdir()
            replacement_database.replace(replacement_factory / "state.db")
            factory.replace(tmp_path / "original-factory")
            replacement_factory.replace(factory)
        yield

    monkeypatch.setattr(
        lease_module, "authority_state_commit_lease", replace_before_inner_recheck
    )

    entered = False
    with pytest.raises(AuthorityStateLeaseError, match="changed before"):
        with authority_database_commit_lease(database):
            entered = True
    assert entered is False


def test_explicit_non_project_database_preserves_unleased_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "authority.db"
    sqlite3.connect(database).close()
    before = database.read_bytes()
    import factory_core.phase9_authority_lease as lease_module

    @contextmanager
    def forbidden_project_lease(_project: Path):
        raise AssertionError("explicit Authority databases are not project state")
        yield  # pragma: no cover

    monkeypatch.setattr(
        lease_module, "authority_state_commit_lease", forbidden_project_lease
    )
    with authority_database_commit_lease(database):
        pass
    assert database.read_bytes() == before


def test_project_local_production_migration_waits_for_delivery_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from factory_core.authority_production_schema import (
        AuthorityProductionMigrationRunner,
    )

    project = tmp_path / "demo"
    project.mkdir()
    SQLiteStateStore(project).initialize(
        project_id=project.name, project_type="modeling"
    )
    database = project / ".factory" / "state.db"
    before = _database_family_bytes(database)
    runner = object.__new__(AuthorityProductionMigrationRunner)
    runner.path = database
    attempted = threading.Event()
    body_entered = threading.Event()
    result: list[object] = []
    sentinel = object()

    def fake_run(owner_token: str) -> object:
        assert owner_token == "test-owner"
        body_entered.set()
        return sentinel

    monkeypatch.setattr(runner, "_run_under_commit_lease", fake_run)

    def migrate() -> None:
        attempted.set()
        result.append(runner.run("test-owner"))

    with delivery_side_effect_commit_lease(project, operation="delivery"):
        thread = threading.Thread(target=migrate)
        thread.start()
        assert attempted.wait(timeout=2)
        assert not body_entered.wait(timeout=0.1)
        assert _database_family_bytes(database) == before
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert body_entered.is_set()
    assert result == [sentinel]
    assert _database_family_bytes(database) == before


def test_project_local_restore_waits_for_delivery_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import factory_core.authority_operations as operations_module

    project = tmp_path / "demo"
    project.mkdir()
    SQLiteStateStore(project).initialize(
        project_id=project.name, project_type="modeling"
    )
    database = project / ".factory" / "state.db"
    backup = tmp_path / "backup.db"
    backup.write_bytes(database.read_bytes())
    before = _database_family_bytes(database)
    attempted = threading.Event()
    body_entered = threading.Event()
    result: list[object] = []
    sentinel = object()

    def fake_restore(*_args, **_kwargs) -> object:
        body_entered.set()
        return sentinel

    monkeypatch.setattr(
        operations_module,
        "_restore_authority_backup_under_commit_lease",
        fake_restore,
    )

    def restore() -> None:
        attempted.set()
        result.append(
            operations_module.restore_authority_backup(
                database,
                backup,
                database_id="authority:test",
                occurred_at=1,
                expected_current_source_fence_sha256="a" * 64,
                expected_backup_sha256="b" * 64,
                expected_switch_epoch=0,
            )
        )

    with delivery_side_effect_commit_lease(project, operation="delivery"):
        thread = threading.Thread(target=restore)
        thread.start()
        assert attempted.wait(timeout=2)
        assert not body_entered.wait(timeout=0.1)
        assert _database_family_bytes(database) == before
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert body_entered.is_set()
    assert result == [sentinel]
    assert _database_family_bytes(database) == before


def test_complete_migrate_operation_waits_before_backup_or_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import factory_core.authority_operator_workflow as workflow_module

    project = tmp_path / "demo"
    project.mkdir()
    SQLiteStateStore(project).initialize(
        project_id=project.name, project_type="modeling"
    )
    database = project / ".factory" / "state.db"
    backup = tmp_path / "migration.backup.db"
    evidence = tmp_path / "migration.evidence.json"
    before = _database_family_bytes(database)
    attempted = threading.Event()
    body_entered = threading.Event()
    errors: list[BaseException] = []

    def fake_operation(_database: Path, **_kwargs) -> dict[str, object]:
        body_entered.set()
        backup.write_bytes(b"backup-after-lease\n")
        evidence.write_text("{}\n", encoding="utf-8")
        return {"status": "finished"}

    monkeypatch.setattr(
        workflow_module,
        "_run_authority_migrate_operation_under_commit_lease",
        fake_operation,
    )

    def migrate() -> None:
        attempted.set()
        try:
            workflow_module.run_authority_migrate_operation(
                database,
                database_id="authority:test",
                expected_source_fence_sha256="a" * 64,
                backup=backup,
                evidence_output=evidence,
                owner_token="test-owner",
                occurred_at=1,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with delivery_side_effect_commit_lease(project, operation="delivery"):
        thread = threading.Thread(target=migrate)
        thread.start()
        assert attempted.wait(timeout=2)
        assert not body_entered.wait(timeout=0.1)
        assert not backup.exists()
        assert not evidence.exists()
        assert _database_family_bytes(database) == before
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert body_entered.is_set()
    assert backup.read_bytes() == b"backup-after-lease\n"
    assert evidence.read_text(encoding="utf-8") == "{}\n"
    assert _database_family_bytes(database) == before


def test_complete_restore_operation_waits_before_operation_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import factory_core.authority_operator_workflow as workflow_module

    project = tmp_path / "demo"
    project.mkdir()
    SQLiteStateStore(project).initialize(
        project_id=project.name, project_type="modeling"
    )
    database = project / ".factory" / "state.db"
    backup = tmp_path / "restore.backup.db"
    backup.write_bytes(database.read_bytes())
    evidence = tmp_path / "restore.evidence.json"
    before = _database_family_bytes(database)
    attempted = threading.Event()
    body_entered = threading.Event()
    errors: list[BaseException] = []

    def fake_operation(
        _database: Path, _backup: Path, **_kwargs
    ) -> dict[str, object]:
        body_entered.set()
        evidence.write_text("{}\n", encoding="utf-8")
        return {"status": "finished"}

    monkeypatch.setattr(
        workflow_module,
        "_run_authority_restore_operation_under_commit_lease",
        fake_operation,
    )

    def restore() -> None:
        attempted.set()
        try:
            workflow_module.run_authority_restore_operation(
                database,
                backup,
                database_id="authority:test",
                occurred_at=1,
                expected_current_source_fence_sha256="a" * 64,
                expected_backup_sha256="b" * 64,
                expected_switch_epoch=0,
                evidence_output=evidence,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with delivery_side_effect_commit_lease(project, operation="delivery"):
        thread = threading.Thread(target=restore)
        thread.start()
        assert attempted.wait(timeout=2)
        assert not body_entered.wait(timeout=0.1)
        assert not evidence.exists()
        assert _database_family_bytes(database) == before
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert body_entered.is_set()
    assert evidence.read_text(encoding="utf-8") == "{}\n"
    assert _database_family_bytes(database) == before


def test_snapshot_reads_latest_uncheckpointed_wal_without_source_mutation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE committed(value TEXT NOT NULL)")
        writer.commit()
        writer.execute("INSERT INTO committed VALUES ('latest-in-wal')")
        writer.commit()
        before = _database_family_bytes(database)

        with authority_state_commit_lease(project):
            with isolated_authority_snapshot_ro(database) as connection:
                assert connection.execute(
                    "SELECT value FROM committed"
                ).fetchall()[0][0] == "latest-in-wal"

        assert _database_family_bytes(database) == before
    finally:
        writer.close()


def test_snapshot_private_permissions_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE stable(value INTEGER)")
        connection.commit()
    finally:
        connection.close()
    import factory_core.phase9_authority_lease as lease_module

    original_connect = lease_module.sqlite3.connect
    observed_root: Path | None = None

    def inspect_private_snapshot(database_uri, *args, **kwargs):
        nonlocal observed_root
        private_database = Path(
            str(database_uri).split("?", 1)[0].removeprefix("file://")
        )
        observed_root = private_database.parent
        assert private_database.stat().st_mode & 0o777 == 0o600
        assert observed_root.stat().st_mode & 0o777 == 0o700
        return original_connect(database_uri, *args, **kwargs)

    monkeypatch.setattr(lease_module.sqlite3, "connect", inspect_private_snapshot)
    with authority_state_commit_lease(project):
        with isolated_authority_snapshot_ro(database) as snapshot:
            assert snapshot.execute("SELECT COUNT(*) FROM stable").fetchone()[0] == 0
    assert observed_root is not None
    assert not observed_root.exists()


@pytest.mark.parametrize(
    ("component", "unsafe_kind"),
    (
        ("wal", "symlink"),
        ("wal", "hardlink"),
        ("wal", "fifo"),
        ("shm", "symlink"),
        ("shm", "hardlink"),
        ("shm", "fifo"),
    ),
)
def test_snapshot_rejects_unsafe_wal_and_shm_components(
    tmp_path: Path, component: str, unsafe_kind: str
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    sqlite3.connect(database).close()
    sidecar = Path(f"{database}-{component}")
    outside = tmp_path / f"outside-{component}"
    outside.write_bytes(b"unsafe")
    if unsafe_kind == "symlink":
        sidecar.symlink_to(outside)
    elif unsafe_kind == "hardlink":
        os.link(outside, sidecar)
    else:
        os.mkfifo(sidecar)

    with authority_state_commit_lease(project):
        with pytest.raises(AuthorityStateLeaseError, match="unsafe type or link count"):
            with isolated_authority_snapshot_ro(database):
                raise AssertionError("unsafe sidecar reached a query")


def test_snapshot_rejects_rollback_journal_without_touching_it(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    sqlite3.connect(database).close()
    journal = Path(f"{database}-journal")
    journal.write_bytes(b"hot-or-ambiguous")
    before = _database_family_bytes(database)

    with authority_state_commit_lease(project):
        with pytest.raises(AuthorityStateLeaseError, match="rollback journal"):
            with isolated_authority_snapshot_ro(database):
                raise AssertionError("rollback journal reached a query")
    assert _database_family_bytes(database) == before


def test_snapshot_rejects_sidecar_path_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    sqlite3.connect(database).close()
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"first")
    replacement = tmp_path / "replacement-wal"
    replacement.write_bytes(b"second")
    import factory_core.phase9_authority_lease as lease_module

    original_open = lease_module.os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and Path(path) == wal:
            replaced = True
            wal.replace(tmp_path / "original-wal")
            replacement.replace(wal)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(lease_module.os, "open", replace_before_open)
    with authority_state_commit_lease(project):
        with pytest.raises(AuthorityStateLeaseError, match="changed while being opened"):
            with isolated_authority_snapshot_ro(database):
                raise AssertionError("replaced WAL reached a query")


def test_clean_wal_phase9_rejections_preserve_database_file_family(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        connection.close()
    before = _database_family_bytes(database)
    assert set(before) == {"state.db"}
    papers = tmp_path / "papers"

    with pytest.raises(Phase9DeliveryFenceError, match="requires explicit"):
        build_final_acceptance_receipt(project, _snapshot(project), status="PASS")
    assert _database_family_bytes(database) == before
    assert not (project / "judge_outputs").exists()

    with pytest.raises(Phase9DeliveryFenceError, match="requires explicit"):
        ReleasePublisher(papers).publish(
            project,
            "a" * 64,
            status="PASS",
            package_builder=lambda _output: True,
        )
    assert _database_family_bytes(database) == before
    assert not papers.exists()

    with pytest.raises(Phase9DeliveryFenceError, match="requires explicit"):
        require_delivery_side_effect_authority(project, operation="delivery")
    assert _database_family_bytes(database) == before


def test_non_phase9_side_effect_guard_has_real_allow_branch(tmp_path: Path) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    before = _tree_fingerprint(project)

    assert (
        require_delivery_side_effect_authority(project, operation="delivery") is None
    )
    assert _tree_fingerprint(project) == before


def test_unknown_phase9_coordinate_blocks_all_delivery_producers_without_side_effects(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    papers = tmp_path / "papers"
    package_calls = 0

    def package_builder(_output: Path) -> bool:
        nonlocal package_calls
        package_calls += 1
        return True

    with pytest.raises(ValueError, match="coordinate does not match"):
        build_final_acceptance_receipt(
            project,
            _snapshot(project),
            status="PASS",
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )
    with pytest.raises(ValueError, match="coordinate does not match"):
        ReleasePublisher(papers).publish(
            project,
            "a" * 64,
            status="PASS",
            package_builder=package_builder,
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )
    with pytest.raises(ValueError, match="coordinate does not match"):
        publish_current_audit(
            project,
            tmp_path,
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )

    assert package_calls == 0
    assert not papers.exists()
    assert not (project / ".factory" / "state.db").exists()
    assert not (project / "judge_outputs" / "final_acceptance_receipt.json").exists()
    assert not (project / "judge_outputs" / "final_submission.sha256").exists()


def test_delivery_producers_require_explicit_current_coordinate_before_io(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    papers = tmp_path / "papers"
    package_calls = 0
    before = _tree_fingerprint(project)
    before_database = _database_fingerprint(database)

    def package_builder(_output: Path) -> bool:
        nonlocal package_calls
        package_calls += 1
        return True

    with pytest.raises(ValueError, match="explicit workflow_id and run_generation"):
        build_final_acceptance_receipt(project, _snapshot(project), status="PASS")
    with pytest.raises(ValueError, match="explicit workflow_id and run_generation"):
        ReleasePublisher(papers).publish(
            project,
            "a" * 64,
            status="PASS",
            package_builder=package_builder,
        )
    with pytest.raises(ValueError, match="explicit workflow_id and run_generation"):
        publish_current_audit(project, tmp_path)

    assert package_calls == 0
    assert not papers.exists()
    assert _tree_fingerprint(project) == before
    assert _database_fingerprint(database) == before_database
    assert not (project / "judge_outputs").exists()


def test_foundation_without_a_phase9_generation_fails_closed_without_db_mutation(
    tmp_path: Path,
    phase9_authority_template: bytes,
) -> None:
    project = tmp_path / "demo"
    state = project / ".factory"
    state.mkdir(parents=True)
    database = state / "state.db"
    database.write_bytes(phase9_authority_template)
    before = _database_fingerprint(database)

    with pytest.raises(
        Phase9DeliveryFenceError,
        match="one explicitly bound current Phase9 terminal",
    ):
        require_phase9_delivery_authority(
            project,
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )

    assert _database_fingerprint(database) == before
    assert not (project / "judge_outputs").exists()


@pytest.mark.parametrize(
    ("run_mode", "contract", "capability", "replay_mode"),
    [
        ("FORENSIC_REPLAY", "LEGACY_NOT_APPLICABLE", "DISABLED", "TECHNICAL"),
        (
            "FORENSIC_REPLAY",
            "LEGACY_NOT_APPLICABLE",
            "DISABLED",
            "ABLATE_NO_JUDGE",
        ),
        ("NORMAL_DELIVERY_RUN", "ACTIVE", "ENABLED", "TECHNICAL"),
        ("FORENSIC_REPLAY", "LEGACY_NOT_APPLICABLE", "ENABLED", "TECHNICAL"),
    ],
)
def test_phase9_modes_capability_and_override_can_never_authorize_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_mode: str,
    contract: str,
    capability: str,
    replay_mode: str,
) -> None:
    fence = Phase9DeliveryFence(
        project_id="demo",
        workflow_id="workflow:demo",
        run_generation="run-generation:current",
        replay_id="phase9-replay:current",
        replay_mode=replay_mode,
        terminal_receipt_sha256="b" * 64,
        run_mode=run_mode,
        modeling_consultation_contract=contract,
        delivery_capability=capability,
    )
    monkeypatch.setattr(
        "factory_core.phase9_delivery_fence.collect_phase9_delivery_fence",
        lambda *_args, **_kwargs: fence,
    )
    with pytest.raises(Phase9DeliveryFenceError):
        require_phase9_delivery_authority(
            tmp_path / "demo",
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "other-project"),
        ("workflow_id", "workflow:other"),
        ("run_generation", "run-generation:old"),
    ],
)
def test_delivery_authority_rejects_a_collector_coordinate_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    values = {
        "project_id": "demo",
        "workflow_id": "workflow:demo",
        "run_generation": "run-generation:current",
        "replay_id": "phase9-replay:current",
        "replay_mode": "TECHNICAL",
        "terminal_receipt_sha256": "b" * 64,
        "run_mode": "FORENSIC_REPLAY",
        "modeling_consultation_contract": "LEGACY_NOT_APPLICABLE",
        "delivery_capability": "DISABLED",
    }
    values[field] = value
    monkeypatch.setattr(
        "factory_core.phase9_delivery_fence.collect_phase9_delivery_fence",
        lambda *_args, **_kwargs: Phase9DeliveryFence(**values),
    )

    with pytest.raises(Phase9DeliveryFenceError, match="coordinate does not match"):
        require_phase9_delivery_authority(
            project,
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )


def test_final_audit_rejects_before_lock_cache_compiler_or_judge(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()

    class NeverCalled:
        def __getattr__(self, name):
            raise AssertionError(f"delivery side effect was reached: {name}")

    service = FinalAuditService(
        tmp_path,
        NeverCalled(),
        NeverCalled(),
        NeverCalled(),
        fingerprinter=lambda *_args: "a" * 64,
        override_provider=NeverCalled(),
    )
    context = StepContext(project, project.name, 16, 1, 3_600, 0)
    with pytest.raises(Phase9DeliveryFenceError, match="coordinate does not match"):
        service.run(
            context,
            analysis_only=False,
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )

    assert list(project.iterdir()) == []


def test_delivery_fence_reconstructs_terminal_semantics_before_returning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A relationally joined, hash-shaped terminal is not delivery authority."""

    row = {
        "project_id": "demo",
        "workflow_id": "workflow:demo",
        "run_generation": "run-generation:current",
        "run_mode": "FORENSIC_REPLAY",
        "modeling_consultation_contract": "LEGACY_NOT_APPLICABLE",
        "delivery_capability": "DISABLED",
        "replay_id": "phase9-replay:current",
        "replay_mode": "TECHNICAL",
        "replay_delivery_capability": "DISABLED",
        "terminal_receipt_sha256": "a" * 64,
    }

    class FakeCursor:
        def fetchall(self):
            return [row]

    class FakeConnection:
        def __init__(self) -> None:
            self.committed = False
            self.rolled_back = False
            self.closed = False

        def execute(self, statement, _parameters=()):
            if str(statement).strip() == "BEGIN":
                return self
            return FakeCursor()

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    @contextmanager
    def fake_lease(_project):
        yield

    @contextmanager
    def fake_snapshot(_database):
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr(
        delivery_fence_module,
        "authority_state_commit_lease",
        fake_lease,
    )
    monkeypatch.setattr(
        delivery_fence_module, "isolated_authority_snapshot_ro", fake_snapshot
    )
    import factory_core.authority_production_schema as production_schema_module

    monkeypatch.setattr(
        production_schema_module,
        "verify_production_installation",
        lambda *_args, **_kwargs: None,
    )

    validation_calls = 0

    def reject_semantic_graph(*_args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        assert kwargs == {
            "workflow_id": "workflow:demo",
            "expected_run_generation": "run-generation:current",
            "expected_terminal_receipt_sha256": "a" * 64,
        }
        raise Phase9ForensicReplayConflict("semantic terminal graph differs")

    monkeypatch.setattr(
        "factory_core.phase9_forensic_replay."
        "validate_current_phase9_completed_replay_in_transaction",
        reject_semantic_graph,
    )

    with pytest.raises(
        Phase9DeliveryFenceError, match="terminal graph is invalid"
    ):
        collect_phase9_delivery_fence(
            tmp_path / "demo",
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )

    assert validation_calls == 1
    assert connection.committed is False
    assert connection.rolled_back is True
    assert connection.closed is True


def test_real_damaged_terminal_graph_reports_fence_error_without_wal_mutation(
    tmp_path: Path,
) -> None:
    from tests.test_phase9_forensic_replay import _fixture

    foundation, _evidence, request, service = _fixture(tmp_path)
    service.execute(request)
    project = tmp_path / request.project_id
    foundation.project_dir.rename(project)
    database = project / ".factory" / "state.db"
    connection = sqlite3.connect(database)
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='authority_production_phase9_replay_events'"
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "UPDATE authority_production_phase9_replay_events "
            "SET event_json='{}' WHERE replay_id=? AND sequence=1",
            (request.replay_id,),
        )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        connection.close()
    before = _database_family_bytes(database)
    assert set(before) == {"state.db"}

    with pytest.raises(
        Phase9DeliveryFenceError, match="terminal graph is invalid"
    ):
        collect_phase9_delivery_fence(
            project,
            workflow_id=request.workflow_id,
            run_generation=request.run_generation,
        )

    assert _database_family_bytes(database) == before


def test_stale_release_is_not_resolved_without_matching_live_authority(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    _install_current_phase9_coordinate(project)
    papers = tmp_path / "papers"
    release_id = "a" * 64
    release_dir = papers / "releases" / "demo" / release_id
    release_dir.mkdir(parents=True)
    fence = {
        "project_id": "demo",
        "workflow_id": "workflow:old",
        "run_generation": "run-generation:old",
        "replay_id": "phase9-replay:old",
        "replay_mode": "TEST_FIXTURE_DELIVERY",
        "terminal_receipt_sha256": "b" * 64,
        "run_mode": "TEST_FIXTURE_DELIVERY",
        "modeling_consultation_contract": "TEST_FIXTURE",
        "delivery_capability": "TEST_FIXTURE_ENABLED",
    }
    manifest = {
        "schema_version": "paper-factory-release-v2",
        "base": "demo",
        "release_id": release_id,
        "phase9_delivery_fence": fence,
    }
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    manifest["content_sha256"] = hashlib.sha256(encoded).hexdigest()
    manifest_path = release_dir / "delivery_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    pointer = {
        "schema_version": "paper-factory-release-pointer-v2",
        "base": "demo",
        "release_id": release_id,
        "release_path": f"releases/demo/{release_id}",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    pointer_path = papers / "demo" / "current.json"
    pointer_path.parent.mkdir(parents=True)
    pointer_path.write_text(json.dumps(pointer) + "\n", encoding="utf-8")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    assert resolve_current_release(
        papers, "demo", project=project
    ) is None
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_release_reader_rejects_old_generation_even_when_files_self_validate(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    _install_current_phase9_coordinate(project)
    papers = tmp_path / "papers"
    release_id = "c" * 64
    release_dir = papers / "releases" / "demo" / release_id
    release_dir.mkdir(parents=True)
    recorded = Phase9DeliveryFence(
        project_id="demo",
        workflow_id="workflow:demo",
        run_generation="run-generation:old",
        replay_id="phase9-replay:old",
        replay_mode="TEST_FIXTURE_DELIVERY",
        terminal_receipt_sha256="d" * 64,
        run_mode="TEST_FIXTURE_DELIVERY",
        modeling_consultation_contract="TEST_FIXTURE",
        delivery_capability="TEST_FIXTURE_ENABLED",
    )
    manifest = {
        "schema_version": "paper-factory-release-v2",
        "base": "demo",
        "release_id": release_id,
        "phase9_delivery_fence": recorded.__dict__,
    }
    unsigned = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest["content_sha256"] = hashlib.sha256(unsigned).hexdigest()
    manifest_path = release_dir / "delivery_manifest.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    pointer_path = papers / "demo/current.json"
    pointer_path.parent.mkdir(parents=True)
    pointer_path.write_text(
        json.dumps(
            {
                "schema_version": "paper-factory-release-pointer-v2",
                "base": "demo",
                "release_id": release_id,
                "release_path": f"releases/demo/{release_id}",
                "manifest_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert resolve_current_release(papers, "demo", project=project) is None


@pytest.mark.parametrize(
    "producer",
    [
        "final_acceptance",
        "final_submission",
        "submission_package",
        "release",
        "release_recovery",
        "publish_release_cli",
    ],
)
@pytest.mark.parametrize(
    "scenario",
    [
        "technical",
        "ablation",
        "technical_and_ablation",
        "override_attempt",
        "old_generation",
        "no_generation",
        "old_terminal",
    ],
)
def test_every_delivery_producer_fails_before_any_side_effect_for_phase9_fences(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase9_authority_template: bytes,
    producer: str,
    scenario: str,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    papers = tmp_path / "papers"
    database = _seed_delivery_decoys(
        project, papers, authority_bytes=phase9_authority_template
    )
    if scenario == "override_attempt":
        (project / ".factory/audits/latest.json").write_text(
            json.dumps(
                {
                    "profile": "final",
                    "status": "OVERRIDDEN",
                    "delivery_allowed": True,
                    "snapshot_id": "a" * 64,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    if scenario == "technical_and_ablation":
        monkeypatch.setenv("ABLATE_NO_JUDGE", "1")

    collector_calls = 0

    def collect_fence(*_args, **kwargs):
        nonlocal collector_calls
        collector_calls += 1
        assert kwargs == {
            "workflow_id": "workflow:demo",
            "run_generation": "run-generation:current",
        }
        if scenario == "old_generation":
            raise Phase9DeliveryFenceError(
                "requested run generation is not the Authority current generation"
            )
        if scenario == "no_generation":
            raise Phase9DeliveryFenceError(
                "Authority has no current Phase9 generation"
            )
        if scenario == "old_terminal":
            raise Phase9DeliveryFenceError(
                "current Phase9 terminal belongs to an old generation"
            )
        return Phase9DeliveryFence(
            project_id=project.name,
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
            replay_id="phase9-replay:current",
            replay_mode=(
                "ABLATE_NO_JUDGE"
                if scenario in {"ablation", "technical_and_ablation"}
                else "TECHNICAL"
            ),
            terminal_receipt_sha256="d" * 64,
            run_mode="FORENSIC_REPLAY",
            modeling_consultation_contract="LEGACY_NOT_APPLICABLE",
            delivery_capability="DISABLED",
        )

    monkeypatch.setattr(
        "factory_core.phase9_delivery_fence.collect_phase9_delivery_fence",
        collect_fence,
    )
    monkeypatch.setattr(
        delivery_fence_module,
        "_current_phase9_generation",
        lambda _project: delivery_fence_module._CurrentPhase9Generation(
            project_id=project.name,
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        ),
    )

    package_calls = 0
    subprocess_calls = 0
    dependency_calls = 0

    def package_builder(_output: Path) -> bool:
        nonlocal package_calls
        package_calls += 1
        return True

    def forbidden_subprocess(*_args, **_kwargs):
        nonlocal subprocess_calls
        subprocess_calls += 1
        raise AssertionError("delivery package subprocess was reached")

    class NeverCalled:
        def __getattr__(self, name):
            def forbidden(*_args, **_kwargs):
                nonlocal dependency_calls
                dependency_calls += 1
                raise AssertionError(f"delivery dependency was reached: {name}")

            return forbidden

    monkeypatch.setattr(
        "scripts.publish_release.subprocess.run", forbidden_subprocess
    )

    before_database = _database_fingerprint(database)
    # Opening a read-only WAL-mode SQLite database may materialize its shared
    # memory sidecar.  Establish the filesystem baseline after that observer-
    # only preparation so only producer effects are compared below.
    before_project = _tree_fingerprint(project)
    before_papers = _tree_fingerprint(papers)

    with pytest.raises(Phase9DeliveryFenceError):
        if producer == "final_acceptance":
            build_final_acceptance_receipt(
                project,
                _snapshot(project),
                status=(
                    "OVERRIDDEN" if scenario == "override_attempt" else "PASS"
                ),
                override_receipt=(
                    "judge_outputs/delivery_override_receipt.json"
                    if scenario == "override_attempt"
                    else None
                ),
                workflow_id="workflow:demo",
                run_generation="run-generation:current",
            )
        elif producer == "final_submission":
            service = FinalAuditService(
                tmp_path,
                NeverCalled(),
                NeverCalled(),
                NeverCalled(),
                fingerprinter=lambda *_args: "a" * 64,
                override_provider=NeverCalled(),
                technical_flow_validation=scenario == "technical_and_ablation",
            )
            service.run(
                StepContext(project, project.name, 16, 1, 3_600, 0),
                analysis_only=False,
                workflow_id="workflow:demo",
                run_generation="run-generation:current",
            )
        elif producer == "release":
            ReleasePublisher(papers).publish(
                project,
                "a" * 64,
                status=(
                    "OVERRIDDEN" if scenario == "override_attempt" else "PASS"
                ),
                package_builder=package_builder,
                workflow_id="workflow:demo",
                run_generation="run-generation:current",
            )
        elif producer == "release_recovery":
            ReleasePublisher(papers).recover(
                project.name,
                project=project,
                workflow_id="workflow:demo",
                run_generation="run-generation:current",
            )
        elif producer == "submission_package":
            package_submission(
                project,
                project.name,
                papers / "submission.zip",
                workflow_id="workflow:demo",
                run_generation="run-generation:current",
            )
        else:
            assert producer == "publish_release_cli"
            publish_current_audit(
                project,
                tmp_path,
                workflow_id="workflow:demo",
                run_generation="run-generation:current",
            )

    assert collector_calls == 1
    assert package_calls == 0
    assert subprocess_calls == 0
    assert dependency_calls == 0
    after_database = _database_fingerprint(database)
    assert _tree_fingerprint(project) == before_project
    assert _tree_fingerprint(papers) == before_papers
    assert after_database == before_database


def test_final_audit_cli_enters_analysis_without_phase9_coordinates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    before = _tree_fingerprint(project)

    returncode = factory_cli_main(["audit", str(project), "--no-compile"])

    captured = capsys.readouterr()
    assert returncode == 1
    assert captured.err == ""
    assert "Traceback" not in captured.err
    record = json.loads(captured.out)
    assert record["profile"] == "final"
    assert record["error_class"] == "MISSING_COMPILED_PDF"
    assert record["delivery_allowed"] is False
    assert _tree_fingerprint(project) != before
    assert (project / ".factory/audits/latest.json").is_file()
    assert not (project / "judge_outputs/final_submission.sha256").exists()
    assert not (project / "judge_outputs/final_acceptance_receipt.json").exists()


def test_final_audit_cli_subprocess_runs_real_analysis_only_path(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    project = tmp_path / "empty-project"
    project.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "factory_core.cli",
            "audit",
            str(project),
            "--no-compile",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr == ""
    assert "Traceback" not in result.stdout
    record = json.loads(result.stdout)
    assert record["profile"] == "final"
    assert record["error_class"] == "MISSING_COMPILED_PDF"
    assert record["delivery_allowed"] is False
    assert (project / ".factory/audits/latest.json").is_file()
    assert not (project / "judge_outputs/final_submission.sha256").exists()
    assert not (project / "judge_outputs/delivery_override_receipt.json").exists()
    assert not (project / "judge_outputs/final_acceptance_receipt.json").exists()
    assert not (project / ".factory/finalization").exists()
    assert not (project / "delivery_manifest.json").exists()


def test_final_audit_cli_accept_delivery_treats_legacy_override_as_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "legacy-override"
    project.mkdir()
    calls: list[bool] = []

    class OverrideRecord:
        status = AuditStatus.OVERRIDDEN
        delivery_allowed = True

        @staticmethod
        def to_dict():
            return {
                "profile": "final",
                "status": "OVERRIDDEN",
                "delivery_allowed": True,
            }

    class OverrideAuditService:
        @staticmethod
        def run_project(_project, **kwargs):
            calls.append(bool(kwargs["analysis_only"]))
            return type("Outcome", (), {"record": OverrideRecord()})()

    monkeypatch.setattr(
        "factory_core.audit.build_final_audit_service",
        lambda _root: OverrideAuditService(),
    )

    returncode = factory_cli_main(["audit", str(project), "--accept-delivery"])

    assert returncode == 0
    assert calls == [False]
    assert json.loads(capsys.readouterr().out)["status"] == "OVERRIDDEN"


def test_native_phase9_delivery_step_fails_before_cleanup_audit_or_packaging(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    database = _install_current_phase9_coordinate(project)
    before = _tree_fingerprint(project)
    before_database = _database_fingerprint(database)

    class NeverCalled:
        def __getattr__(self, name):
            raise AssertionError(f"native delivery dependency was reached: {name}")

    step = DeliveryStep(
        contract_for(16),
        tmp_path,
        NeverCalled(),
        NeverCalled(),
        NeverCalled(),
        audit_service=NeverCalled(),
        release_publisher=NeverCalled(),
    )
    result = step.execute(StepContext(project, project.name, 16, 1, 600, 0))

    assert result.returncode == 2
    assert result.error_class == "PERMANENT_PHASE9_DELIVERY_DISABLED"
    assert result.metadata["delivery_allowed"] is False
    assert _tree_fingerprint(project) == before
    assert _database_fingerprint(database) == before_database


def test_native_delivery_step_reports_real_phase9_transition_at_commit_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    original = delivery_fence_module.require_delivery_side_effect_authority
    transitioned: dict[str, object] = {}

    def allow_then_install_phase9(*args, **kwargs):
        result = original(*args, **kwargs)
        database = _install_current_phase9_coordinate(project)
        transitioned["database"] = database
        transitioned["tree"] = _tree_fingerprint(project)
        transitioned["database_bytes"] = _database_fingerprint(database)
        return result

    monkeypatch.setattr(
        delivery_fence_module,
        "require_delivery_side_effect_authority",
        allow_then_install_phase9,
    )

    class NeverCalled:
        def __getattr__(self, name):
            raise AssertionError(f"native delivery dependency was reached: {name}")

    step = DeliveryStep(
        contract_for(16),
        tmp_path,
        NeverCalled(),
        NeverCalled(),
        NeverCalled(),
        audit_service=NeverCalled(),
        release_publisher=NeverCalled(),
    )
    result = step.execute(StepContext(project, project.name, 16, 1, 600, 0))

    assert result.returncode == 2
    assert result.error_class == "PERMANENT_PHASE9_DELIVERY_DISABLED"
    assert "requires explicit workflow_id and run_generation" in result.metadata[
        "delivery_error"
    ]
    database = transitioned["database"]
    assert isinstance(database, Path)
    assert _tree_fingerprint(project) == transitioned["tree"]
    assert _database_fingerprint(database) == transitioned["database_bytes"]
    assert not (project / "judge_outputs").exists()


def test_native_delivery_step_classifies_phase9_transition_before_audit(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    (project / "demo_paper.tex").write_text(
        "\\begin{document}fixture\\end{document}\n", encoding="utf-8"
    )
    transitioned: dict[str, object] = {}

    class NeverCalled:
        def __getattr__(self, name):
            raise AssertionError(f"native delivery dependency was reached: {name}")

    real_audit = FinalAuditService(
        tmp_path,
        NeverCalled(),
        NeverCalled(),
        NeverCalled(),
        fingerprinter=lambda *_args: "a" * 64,
        override_provider=NeverCalled(),
    )

    class TransitioningAudit:
        @staticmethod
        def run(context, **kwargs):
            with authority_state_commit_lease(project):
                database = _install_current_phase9_coordinate(project)
            transitioned["database"] = database
            transitioned["tree"] = _tree_fingerprint(project)
            transitioned["database_bytes"] = _database_fingerprint(database)
            return real_audit.run(context, **kwargs)

    step = DeliveryStep(
        contract_for(16),
        tmp_path,
        NeverCalled(),
        NeverCalled(),
        NeverCalled(),
        audit_service=TransitioningAudit(),
        release_publisher=NeverCalled(),
    )
    result = step.execute(StepContext(project, project.name, 16, 1, 600, 0))

    assert result.returncode == 2
    assert result.error_class == "PERMANENT_PHASE9_DELIVERY_DISABLED"
    assert result.metadata["delivery_allowed"] is False
    assert "requires explicit workflow_id and run_generation" in result.metadata[
        "delivery_error"
    ]
    database = transitioned["database"]
    assert isinstance(database, Path)
    assert _tree_fingerprint(project) == transitioned["tree"]
    assert _database_fingerprint(database) == transitioned["database_bytes"]
    assert not (project / ".factory/audits").exists()
    assert not (project / "judge_outputs").exists()
    assert not (tmp_path / "papers").exists()


def test_legacy_step16_checks_fence_before_cleanup_or_audit() -> None:
    repository = Path(__file__).resolve().parents[1]
    runner = (repository / "factory_core/adapters/legacy_runner.sh").read_text(
        encoding="utf-8"
    )
    step16 = runner.split("run_step_16() {", 1)[1].split("\n}", 1)[0]
    fence = step16.index("check_phase9_delivery_fence.py")
    cleanup = step16.index("cleanup_project_artifacts.py")
    audit = step16.index("factory_core.cli audit")
    publish = step16.index("publish_release.py")
    assert fence < cleanup < audit < publish
    assert '--accept-delivery' in step16
    checker = (repository / "scripts/check_phase9_delivery_fence.py").read_text(
        encoding="utf-8"
    )
    assert "require_delivery_side_effect_authority" in checker
    assert "require_phase9_delivery_authority" not in checker


def test_legacy_delivery_fence_check_is_read_only_and_allows_non_phase9(
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    before = _tree_fingerprint(project)

    result = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "scripts/check_phase9_delivery_fence.py"
            ),
            str(project),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert _tree_fingerprint(project) == before
