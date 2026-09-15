from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from factory_core.authority_operations import AuthorityOperations
from factory_core.authority_production_schema import legacy_database_content_sha256
from factory_core.authority_production_writer import (
    AuthorityProductionWriter, AuthorityProductionWriterDisabled,
    AuthorityProductionWriterBusy, AuthorityProductionWriterFenceLost,
)
from factory_core.authority_repository import AuthorityEnvelopePersistenceError
from factory_core.domain import InvalidTransition, RevisionConflict
from factory_core.service import FactoryService
from factory_core.solver_policy_routing import authority_solver_route, configure_authority_solver_policy
from factory_core.storage import SQLiteStateStore
from tests.support.authority_production import install_foundation


ACTOR = dict(operator_subject="test", reason="isolated route test", occurred_at=2000)


def setup_route(tmp_path, *, mode="CANARY", writer_id="factory-service"):
    fixture = install_foundation(tmp_path)
    operations = AuthorityOperations(fixture.database, expected_source_fence_sha256=fixture.preflight.source_fence_sha256)
    operations.configure_writer(new_writer_id=writer_id, enabled=True, expected_writer_epoch=0, expected_switch_epoch=0, **ACTOR)
    operations.configure_consumer(new_consumer_id="test-consumer", enabled=True, expected_consumer_epoch=0, expected_switch_epoch=0, **ACTOR)
    operations.switch_mode(target_mode="CANARY", expected_switch_epoch=0, **ACTOR)
    if mode == "AUTHORITY_PRIMARY":
        operations.switch_mode(target_mode=mode, expected_switch_epoch=1, **ACTOR)
    return fixture, operations, FactoryService(tmp_path)


def query(database, sql):
    with sqlite3.connect(database) as connection:
        return connection.execute(sql).fetchall()


def legacy_hash(database):
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return legacy_database_content_sha256(connection)


def configure(service, fixture, *, revision=1, threshold=301):
    return service.configure_solver_policy(fixture.project_dir, mode="local", threshold_seconds=threshold,
                                           allowed_runtimes=["python"], expected_revision=revision)


@pytest.mark.parametrize("mode", ["CANARY", "AUTHORITY_PRIMARY"])
def test_service_writes_only_authority_and_reads_its_revision(tmp_path, mode):
    fixture, _, service = setup_route(tmp_path, mode=mode)
    before = legacy_hash(fixture.database)
    first = configure(service, fixture)
    assert first["revision"] == 2 and first["authority"] == "authority"
    assert service.solver_policy(fixture.project_dir) == {**first, "enabled": False, "quarantined": service._cloud_quarantined()}
    assert configure(service, fixture) == first  # Exact request replay.
    with pytest.raises(RevisionConflict):
        configure(service, fixture, threshold=302)
    second = configure(service, fixture, revision=2, threshold=302)
    assert second["revision"] == 3
    assert service.solver_policy(fixture.project_dir) == {**second, "enabled": False, "quarantined": service._cloud_quarantined()}
    assert configure(service, fixture) == first
    assert service.solver_policy(fixture.project_dir)["threshold_seconds"] == 302
    assert legacy_hash(fixture.database) == before
    assert SQLiteStateStore(fixture.project_dir).load().revision == 1
    assert query(fixture.database, "SELECT current_revision FROM authority_workflows") == [(3,)]
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(2,)]
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_receipts") == [(2,)]
    assert query(fixture.database, "SELECT topic FROM authority_outbox") == [("authority.solver-policy.changed",)] * 2
    assert query(fixture.database, "SELECT status FROM authority_production_outbox_delivery_state") == [("PENDING",)] * 2


def test_no_authority_installation_keeps_native_configuration(tmp_path):
    project = tmp_path / "native"
    store = SQLiteStateStore(project)
    store.initialize(project_id="native", project_type="modeling")
    service = FactoryService(tmp_path)
    result = service.configure_solver_policy(project, mode="local", threshold_seconds=301, expected_revision=1)
    assert result["revision"] == 2 and "authority" not in result
    assert store.load().revision == 2 and service.solver_policy(project)["threshold_seconds"] == 301


def test_v1_only_installation_keeps_native_route(tmp_path):
    fixture = install_foundation(tmp_path)
    result = configure(FactoryService(tmp_path), fixture)
    assert result["revision"] == 2 and "authority" not in result
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(0,)]


@pytest.mark.parametrize("sql", [
    "UPDATE project_state SET revision=revision+1",
    "DELETE FROM events",
    "INSERT INTO project_config VALUES (1,'local',400,'[\"python\"]',1)",
])
def test_database_fence_blocks_direct_legacy_sql(tmp_path, sql):
    fixture, _, _ = setup_route(tmp_path)
    before = legacy_hash(fixture.database)
    with sqlite3.connect(fixture.database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="AUTHORITY_LEGACY_WRITE_DISABLED"):
            connection.execute(sql)
    assert legacy_hash(fixture.database) == before


def test_existing_store_and_service_controls_cannot_bypass_fence(tmp_path):
    fixture, _, service = setup_route(tmp_path)
    store = SQLiteStateStore(fixture.project_dir)
    with pytest.raises(InvalidTransition, match="legacy state writes"):
        store.configure_solver_policy(expected_revision=1, mode="local", threshold_seconds=400, allowed_runtimes=["python"])
    with pytest.raises(InvalidTransition, match="legacy state writes"):
        service.pause(fixture.project_dir)
    assert store.load().revision == 1


def test_solver_dispatch_remains_blocked_until_its_authority_handler_exists(tmp_path):
    fixture, _, service = setup_route(tmp_path)
    script = fixture.project_dir / "solver.py"
    script.write_text("print(1)\n")
    with pytest.raises(InvalidTransition, match="legacy state writes"):
        service.submit_solver(fixture.project_dir, script=script, runtime="python", max_time_seconds=10, expected_revision=1)
    assert query(fixture.database, "SELECT COUNT(*) FROM solver_jobs") == [(0,)]


def test_cached_old_connection_rechecks_database_mode(tmp_path):
    fixture = install_foundation(tmp_path)
    with sqlite3.connect(fixture.database) as old:
        old.execute("UPDATE project_state SET revision=revision")
        old.commit()
        operations = AuthorityOperations(fixture.database, expected_source_fence_sha256=fixture.preflight.source_fence_sha256)
        operations.configure_writer(new_writer_id="factory-service", enabled=True, expected_writer_epoch=0, expected_switch_epoch=0, **ACTOR)
        operations.configure_consumer(new_consumer_id="test-consumer", enabled=True, expected_consumer_epoch=0, expected_switch_epoch=0, **ACTOR)
        operations.switch_mode(target_mode="CANARY", expected_switch_epoch=0, **ACTOR)
        with pytest.raises(sqlite3.IntegrityError, match="AUTHORITY_LEGACY_WRITE_DISABLED"):
            old.execute("UPDATE project_state SET revision=revision")


def test_route_captured_before_fallback_cannot_commit(tmp_path):
    fixture, operations, _ = setup_route(tmp_path)
    route = authority_solver_route(fixture.project_dir)
    operations.switch_mode(target_mode="V1_ONLY", expected_switch_epoch=1, **ACTOR)
    with pytest.raises(AuthorityProductionWriterDisabled):
        configure_authority_solver_policy(route, mode="local", threshold_seconds=400, allowed_runtimes=["python"], expected_revision=1)
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(0,)]
    assert SQLiteStateStore(fixture.project_dir).load().revision == 1


def test_wrong_durable_writer_never_falls_back_to_native(tmp_path):
    fixture, _, service = setup_route(tmp_path, writer_id="other-owner")
    with pytest.raises(InvalidTransition, match="factory-service writer"):
        configure(service, fixture)
    assert SQLiteStateStore(fixture.project_dir).load().revision == 1


def test_authority_configuration_requires_explicit_revision(tmp_path):
    fixture, _, service = setup_route(tmp_path)
    with pytest.raises(InvalidTransition, match="explicit expected_revision"):
        configure(service, fixture, revision=None)
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(0,)]
    assert SQLiteStateStore(fixture.project_dir).load().revision == 1


def test_cli_missing_revision_returns_an_actionable_error(tmp_path, monkeypatch, capsys):
    from factory_core import cli

    fixture, _, _ = setup_route(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["solver", "policy", str(fixture.project_dir), "--mode", "local"]) == 1
    error = capsys.readouterr().err
    assert error.startswith("ERROR:")
    assert "read the current solver policy" in error
    assert "Traceback" not in error
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(0,)]


def test_web_policy_query_supplies_the_authority_revision_for_updates(tmp_path):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from web.backend.cloud_api import project_cloud_config, set_project_cloud_enabled

    fixture, _, _ = setup_route(tmp_path / "ongoing")
    settings = SimpleNamespace(
        factory_root=tmp_path, ongoing_dir=tmp_path / "ongoing",
        complete_dir=tmp_path / "complete", gcp_project_id="test-project",
        gcp_region="test-region", gcp_solver_service="test-service",
    )
    name = fixture.project_dir.name
    before = project_cloud_config(settings, name)
    assert before["revision"] == 1
    after = set_project_cloud_enabled(settings, name, False, expected_revision=before["revision"])
    assert after["revision"] == 2
    with pytest.raises(HTTPException) as missing:
        set_project_cloud_enabled(settings, name, False)
    assert missing.value.status_code == 409
    assert "read the current solver policy" in missing.value.detail
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(1,)]
    assert SQLiteStateStore(fixture.project_dir).load().revision == 1


def test_public_writer_rejects_valid_policy_from_another_enabled_owner(tmp_path, monkeypatch):
    fixture, operations, service = setup_route(tmp_path)
    original = AuthorityProductionWriter.persist_command_bundle

    def bypass_route(self, **kwargs):
        stopped = operations.switch_mode(
            target_mode="V1_ONLY", expected_switch_epoch=1, **ACTOR,
        )
        rotated = operations.configure_writer(
            new_writer_id="other-owner", enabled=True,
            expected_writer_epoch=stopped.writer_epoch,
            expected_switch_epoch=stopped.switch_epoch, **ACTOR,
        )
        operations.configure_consumer(
            new_consumer_id="test-consumer", enabled=True,
            expected_consumer_epoch=stopped.consumer_epoch,
            expected_switch_epoch=stopped.switch_epoch, **ACTOR,
        )
        operations.switch_mode(
            target_mode="CANARY", expected_switch_epoch=stopped.switch_epoch, **ACTOR,
        )
        other = AuthorityProductionWriter(
            fixture.database, writer_id="other-owner", writer_epoch=rotated.writer_epoch,
            expected_source_fence_sha256=fixture.preflight.source_fence_sha256,
        )
        return original(other, **kwargs)

    monkeypatch.setattr(AuthorityProductionWriter, "persist_command_bundle", bypass_route)
    with pytest.raises(AuthorityEnvelopePersistenceError, match="factory-service writer"):
        configure(service, fixture)
    assert query(fixture.database, "SELECT writer_id,writer_enabled FROM authority_production_writer_state") == [("other-owner", 1)]
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(0,)]
    assert query(fixture.database, "SELECT current_revision FROM authority_workflows") == [(1,)]


def test_missing_fence_is_detected_before_authority_write(tmp_path):
    fixture, _, service = setup_route(tmp_path)
    with sqlite3.connect(fixture.database) as connection:
        connection.execute("DROP TRIGGER authority_production_native_fence_project_state_update")
    with pytest.raises(Exception, match="sqlite_master identity drifted"):
        configure(service, fixture)
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(0,)]


def test_failure_rolls_back_whole_authority_command(tmp_path, monkeypatch):
    fixture, _, service = setup_route(tmp_path)
    def fail(point):
        if point == "after_receipt":
            raise RuntimeError("injected stop")
    monkeypatch.setattr("factory_core.authority_production_writer._writer_failure_point", fail)
    with pytest.raises(RuntimeError, match="injected stop"):
        configure(service, fixture)
    for table in ("authority_commands", "authority_events", "authority_receipts", "authority_outbox"):
        assert query(fixture.database, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    assert query(fixture.database, "SELECT current_revision FROM authority_workflows") == [(1,)]


def test_writer_rejects_mismatched_actual_configuration(tmp_path, monkeypatch):
    fixture, _, service = setup_route(tmp_path)
    original = AuthorityProductionWriter.persist_command_bundle
    def corrupt(self, **kwargs):
        kwargs["outbox"] = replace(kwargs["outbox"], topic="unrelated.dispatch")
        return original(self, **kwargs)
    monkeypatch.setattr(AuthorityProductionWriter, "persist_command_bundle", corrupt)
    with pytest.raises(AuthorityEnvelopePersistenceError, match="companion type differs"):
        configure(service, fixture)
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(0,)]


@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_configuration_has_one_revision_owner(tmp_path, same_request):
    fixture, _, service = setup_route(tmp_path)
    def run(threshold):
        try:
            return configure(service, fixture, threshold=threshold)
        except RevisionConflict:
            return "conflict"
        except (AuthorityProductionWriterBusy, AuthorityProductionWriterFenceLost):
            return "retry"
    thresholds = [301, 301 if same_request else 302]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, thresholds))
    # Contention may exhaust the admission timeout on a loaded host. Once both
    # threads have exited, retry the exact request and still require one commit.
    results = [run(threshold) if result == "retry" else result
               for threshold, result in zip(thresholds, results)]
    assert "retry" not in results
    if same_request:
        assert results[0] == results[1]
    else:
        assert results.count("conflict") == 1
    assert query(fixture.database, "SELECT current_revision FROM authority_workflows") == [(2,)]
    assert query(fixture.database, "SELECT COUNT(*) FROM authority_commands") == [(1,)]
    assert SQLiteStateStore(fixture.project_dir).load().revision == 1


def test_fence_coverage_and_reserved_name_cannot_hide_altered_trigger(tmp_path):
    from factory_core.authority_schema import legacy_source_identity_sha256
    from factory_core.native_write_fence import NATIVE_FENCE_TABLES
    fixture, _, _ = setup_route(tmp_path)
    with sqlite3.connect(fixture.database) as connection:
        connection.row_factory = sqlite3.Row
        native_tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'authority_%'")}
        assert native_tables == set(NATIVE_FENCE_TABLES)
        original = legacy_source_identity_sha256(connection)
        assert original == fixture.preflight.source_fence_sha256
        connection.execute("DROP TRIGGER authority_production_native_fence_project_state_update")
        connection.execute("CREATE TRIGGER authority_production_native_fence_project_state_update BEFORE UPDATE ON project_state BEGIN SELECT 1; END")
        assert legacy_source_identity_sha256(connection) != original


def test_legacy_connection_selected_before_cutover_cannot_finish_write(tmp_path):
    fixture = install_foundation(tmp_path)
    assert authority_solver_route(fixture.project_dir) is None
    operations = AuthorityOperations(fixture.database, expected_source_fence_sha256=fixture.preflight.source_fence_sha256)
    operations.configure_writer(new_writer_id="factory-service", enabled=True, expected_writer_epoch=0, expected_switch_epoch=0, **ACTOR)
    operations.configure_consumer(new_consumer_id="test-consumer", enabled=True, expected_consumer_epoch=0, expected_switch_epoch=0, **ACTOR)
    operations.switch_mode(target_mode="CANARY", expected_switch_epoch=0, **ACTOR)
    with pytest.raises(InvalidTransition, match="legacy state writes"):
        SQLiteStateStore(fixture.project_dir).configure_solver_policy(expected_revision=1, mode="local", threshold_seconds=400, allowed_runtimes=["python"])
    assert SQLiteStateStore(fixture.project_dir).load().revision == 1
