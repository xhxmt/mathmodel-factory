import json

import pytest

from factory_core.domain import MigrationConflict
from factory_core.migration import LegacyInspector, apply_migration
from factory_core.storage import SQLiteStateStore


def write_project(project, *, checkpoint=2, inferred=2):
    (project / "problem").mkdir(parents=True)
    (project / "problem" / "problem_brief.md").write_text("# problem\n", encoding="utf-8")
    (project / "checkpoint.md").write_text(
        f"- **Last completed step**: {checkpoint}\n", encoding="utf-8"
    )
    (project / ".legacy_inferred_step").write_text(str(inferred), encoding="utf-8")


def fake_infer(project):
    return int((project / ".legacy_inferred_step").read_text(encoding="utf-8"))


def test_migration_requires_matching_inspection_report(tmp_path):
    project = tmp_path / "ongoing" / "demo"
    write_project(project)
    inspector = LegacyInspector(infer_step=fake_infer)

    report = inspector.inspect(project)
    state = apply_migration(project, report, expected_digest=report.digest)

    assert state.last_completed_step == 2
    assert state.status.value == "ready"
    assert SQLiteStateStore(project).events()[0].type == "PROJECT_IMPORTED"
    assert not (project / ".runner.lock").exists()
    assert not (project / ".runner.lock.info").exists()


def test_migration_rejects_checkpoint_conflict(tmp_path):
    project = tmp_path / "ongoing" / "demo"
    write_project(project, checkpoint=1, inferred=2)

    report = LegacyInspector(infer_step=fake_infer).inspect(project)

    assert report.conflicts
    with pytest.raises(MigrationConflict):
        apply_migration(project, report, expected_digest=report.digest)


def test_migration_rejects_retired_social_science_project(tmp_path):
    project = tmp_path / "ongoing" / "social"
    project.mkdir(parents=True)
    (project / "project_brief.md").write_text("legacy\n", encoding="utf-8")
    (project / "checkpoint.md").write_text("- **Last completed step**: 2\n", encoding="utf-8")

    report = LegacyInspector(infer_step=lambda _: 2).inspect(project)

    assert "LEGACY_DOMAIN_RETIRED" in report.conflicts
    assert json.loads(report.to_json())["project_type"] == "social_science"


def test_migration_preserves_pending_selection_as_authoritative_state(tmp_path):
    project = tmp_path / "ongoing" / "demo"
    write_project(project, checkpoint=2, inferred=2)
    (project / "selection").mkdir()
    (project / "selection" / "step3_options.json").write_text(
        json.dumps({"deadline_epoch": 1234}), encoding="utf-8"
    )

    report = LegacyInspector(infer_step=fake_infer).inspect(project)
    state = apply_migration(project, report, expected_digest=report.digest)

    assert state.status.value == "awaiting_selection"
    assert state.active_step == 3
    assert state.pending_action["deadline_epoch"] == 1234


def test_historical_complete_project_import_is_read_only_without_fake_step16(tmp_path):
    project = tmp_path / "complete" / "demo"
    write_project(project, checkpoint=16, inferred=2)

    report = LegacyInspector(infer_step=fake_infer).inspect(project)
    state = apply_migration(project, report, expected_digest=report.digest)

    assert report.conflicts == ()
    assert report.warnings == ("CHECKPOINT_MISMATCH:16!=2",)
    assert state.status.value == "completed"
    assert state.last_completed_step == 2


def test_migration_rechecks_runner_lock_at_apply_time(tmp_path):
    project = tmp_path / "ongoing" / "demo"
    write_project(project)
    report = LegacyInspector(infer_step=fake_infer).inspect(project)
    (project / ".runner.lock").mkdir()

    with pytest.raises(MigrationConflict, match="became active"):
        apply_migration(project, report, expected_digest=report.digest)

    assert not SQLiteStateStore(project).exists


def test_migration_refuses_orphaned_runner_lock_info(tmp_path):
    project = tmp_path / "ongoing" / "demo"
    write_project(project)
    (project / ".runner.lock.info").write_text("pid=999999\n", encoding="utf-8")

    report = LegacyInspector(infer_step=fake_infer).inspect(project)

    assert "ACTIVE_RUNNER" in report.conflicts
    with pytest.raises(MigrationConflict, match="became active"):
        apply_migration(project, report, expected_digest=report.digest)
    assert (project / ".runner.lock.info").read_text(encoding="utf-8") == "pid=999999\n"


def test_migration_releases_runner_lock_when_state_initialization_fails(
    tmp_path, monkeypatch
):
    project = tmp_path / "ongoing" / "demo"
    write_project(project)
    report = LegacyInspector(infer_step=fake_infer).inspect(project)

    def fail_initialize(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(SQLiteStateStore, "initialize", fail_initialize)

    with pytest.raises(RuntimeError, match="database unavailable"):
        apply_migration(project, report, expected_digest=report.digest)

    assert not (project / ".runner.lock").exists()
    assert not (project / ".runner.lock.info").exists()
