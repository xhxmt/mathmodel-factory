from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import factory_core.phase9_delivery_fence as delivery_fence_module
from factory_core.cli import main as factory_cli_main
from factory_core.audit.acceptance import build_final_acceptance_receipt
from factory_core.audit.domain import AuditSnapshot
from factory_core.audit.service import FinalAuditService
from factory_core.delivery.release import ReleasePublisher, resolve_current_release
from factory_core.domain import StepContext
from factory_core.phase9_delivery_fence import (
    Phase9DeliveryFence,
    Phase9DeliveryFenceError,
    collect_phase9_delivery_fence,
    require_phase9_delivery_authority,
)
from factory_core.phase9_forensic_replay import Phase9ForensicReplayConflict
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


@pytest.fixture(scope="module")
def phase9_authority_template(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    fixture = install_foundation(
        tmp_path_factory.mktemp("delivery-authority-template"), name="demo"
    )
    return fixture.database.read_bytes()


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


def test_missing_authority_blocks_all_delivery_producers_without_side_effects(
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

    with pytest.raises(ValueError, match="database is missing"):
        build_final_acceptance_receipt(
            project,
            _snapshot(project),
            status="PASS",
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )
    with pytest.raises(ValueError, match="database is missing"):
        ReleasePublisher(papers).publish(
            project,
            "a" * 64,
            status="PASS",
            package_builder=package_builder,
            workflow_id="workflow:demo",
            run_generation="run-generation:current",
        )
    with pytest.raises(ValueError, match="database is missing"):
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
    papers = tmp_path / "papers"
    package_calls = 0

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
    assert not (project / ".factory" / "state.db").exists()
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

    with pytest.raises(
        Phase9DeliveryFenceError, match="requested delivery coordinate"
    ):
        require_phase9_delivery_authority(
            tmp_path / "demo",
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
    with pytest.raises(Phase9DeliveryFenceError, match="database is missing"):
        service.run(
            context,
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
    monkeypatch.setattr(
        delivery_fence_module,
        "authority_database_path",
        lambda _path: tmp_path / "state.db",
    )
    monkeypatch.setattr(
        delivery_fence_module, "connect_authority_ro", lambda _path: connection
    )
    monkeypatch.setattr(
        delivery_fence_module,
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


def test_stale_release_is_not_resolved_without_matching_live_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
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

    monkeypatch.setattr(
        "factory_core.phase9_delivery_fence.require_phase9_delivery_authority",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            Phase9DeliveryFenceError("current generation is delivery DISABLED")
        ),
    )
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
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
    current = Phase9DeliveryFence(
        project_id="demo",
        workflow_id="workflow:demo",
        run_generation="run-generation:current",
        replay_id="phase9-replay:current",
        replay_mode="TEST_FIXTURE_DELIVERY",
        terminal_receipt_sha256="e" * 64,
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
    monkeypatch.setattr(
        "factory_core.phase9_delivery_fence.require_phase9_delivery_authority",
        lambda *_args, **_kwargs: current,
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


def test_final_audit_cli_reports_missing_phase9_coordinate_without_side_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    before = _tree_fingerprint(project)

    returncode = factory_cli_main(["audit", str(project), "--no-compile"])

    captured = capsys.readouterr()
    assert returncode == 1
    assert captured.out == ""
    assert "ERROR: delivery requires explicit workflow_id and run_generation" in (
        captured.err
    )
    assert "Traceback" not in captured.err
    assert _tree_fingerprint(project) == before


def test_native_delivery_step_fails_before_cleanup_audit_or_packaging(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()

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
    assert list(project.iterdir()) == []


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
