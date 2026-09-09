from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from factory_core.audit.acceptance import build_final_acceptance_receipt
from factory_core.audit.domain import AuditSnapshot
from factory_core.delivery.release import ReleasePublisher, resolve_current_release
from factory_core.phase9_authority_lease import authority_state_commit_lease
from factory_core.phase9_delivery_fence import Phase9DeliveryFenceError
from factory_core.contest import ContestPolicy
from factory_core.storage import SQLiteStateStore
from scripts.package_submission import package_submission
from scripts.publish_release import publish_current_audit


def _write_approved_audit(project: Path, snapshot_id: str) -> None:
    outputs = project / "judge_outputs"
    outputs.mkdir(exist_ok=True)
    for name, value in (
        ("final_paper_checks.json", {"status": "PASS"}),
        ("visual_gate.json", {"status": "PASS"}),
        ("decision_route.json", {"effective_decision": "PASS"}),
        ("judgment_receipt.json", {"status": "VALID"}),
    ):
        (outputs / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
    snapshot = AuditSnapshot(
        snapshot_id=snapshot_id,
        base=project.name,
        profile="final",
        created_at="2026-08-09T00:00:00+00:00",
        identity={
            "source": "injected_fingerprinter",
            "fingerprint": snapshot_id,
        },
    )
    audit_dir = project / ".factory/audits" / snapshot_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "snapshot.json").write_text(
        json.dumps(snapshot.to_dict()) + "\n", encoding="utf-8"
    )
    (project / ".factory/audits/latest.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "base": project.name,
                "profile": "final",
                "status": "PASS",
                "decision": "PASS",
                "judge_completed": True,
                "delivery_allowed": True,
                "override": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    build_final_acceptance_receipt(project, snapshot, status="PASS")


def _project(root: Path, base: str, snapshot_id: str) -> Path:
    project = root / "ongoing" / base
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{base}_paper.tex").write_text(
        "\\begin{document}approved\\end{document}\n", encoding="utf-8"
    )
    (project / f"{base}_paper.pdf").write_bytes(b"%PDF audited\n")
    (project / "models").mkdir()
    (project / "models/solve.py").write_text("pass\n", encoding="utf-8")
    (project / "results").mkdir()
    (project / "results/result.json").write_text("{}\n", encoding="utf-8")
    _write_approved_audit(project, snapshot_id)
    return project


def _package(project: Path, base: str):
    def build(output: Path) -> bool:
        from factory_core.submission_bundle import submission_bundle_manifest

        manifest = submission_bundle_manifest(project, base)
        with zipfile.ZipFile(output, "w") as archive:
            for item in manifest["members"]:
                archive.write(
                    project / item["source_path"], item["archive_path"]
                )
        return True

    return build


def _install_current_phase9(project: Path) -> None:
    database = project / ".factory" / "state.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    if not database.exists():
        SQLiteStateStore(project).initialize(
            project_id=project.name, project_type="modeling"
        )
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
            INSERT INTO authority_production_run_generations VALUES (
                'demo', 'workflow:demo', 'run-generation:current',
                'FORENSIC_REPLAY'
            );
            INSERT INTO authority_production_run_generation_current VALUES (
                'workflow:demo', 'run-generation:current'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _approve_content_freeze(project: Path) -> dict[str, object]:
    policy = ContestPolicy.default(started_at=1_000)
    store = SQLiteStateStore(project, clock=lambda: 2_000)
    store.initialize(
        project_id=project.name,
        project_type="modeling",
        contest_policy=policy.to_dict(),
    )
    return store.record_decision(
        "content_freeze",
        {
            "selected_option_id": "approve_content_freeze",
            "approved": True,
            "selected_at": 2_000,
        },
    )


def test_atomic_release_flips_one_verified_current_pointer(tmp_path: Path) -> None:
    snapshot_id = "a" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    publisher = ReleasePublisher(tmp_path / "papers")

    result = publisher.publish(
        project,
        snapshot_id,
        status="PASS",
        package_builder=_package(project, "demo"),
    )

    current = resolve_current_release(
        tmp_path / "papers", "demo", project=project
    )
    assert current is not None
    assert current.release_id == snapshot_id
    assert current.paper.read_bytes() == project.joinpath("demo_paper.pdf").read_bytes()
    assert result.pointer == tmp_path / "papers/demo/current.json"
    assert (tmp_path / "papers/demo_paper.pdf").read_bytes() == current.paper.read_bytes()


def test_release_contains_verified_human_approval_receipt(tmp_path: Path) -> None:
    snapshot_id = "9" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    decision = _approve_content_freeze(project)
    _write_approved_audit(project, snapshot_id)

    release = ReleasePublisher(tmp_path / "papers").publish(
        project,
        snapshot_id,
        status="PASS",
        package_builder=_package(project, "demo"),
    )

    key = f"approval_content_freeze_{decision['decision_id']}"
    copied = release.release_dir / f"{key}.json"
    assert copied.read_bytes() == project.joinpath(
        decision["artifact_refs"][0]["path"]
    ).read_bytes()
    manifest = json.loads(release.manifest.read_text(encoding="utf-8"))
    assert manifest["evidence_artifacts"][key] == f"{key}.json"
    assert resolve_current_release(
        tmp_path / "papers", "demo", project=project
    ) is not None


def test_content_freeze_receipt_tampered_during_packaging_blocks_release(
    tmp_path: Path,
) -> None:
    snapshot_id = "8" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    decision = _approve_content_freeze(project)
    _write_approved_audit(project, snapshot_id)
    receipt = project / decision["artifact_refs"][0]["path"]
    package = _package(project, "demo")

    def tampering_package(output: Path) -> bool:
        built = package(output)
        receipt.write_text('{"tampered":true}\n', encoding="utf-8")
        return built

    with pytest.raises(ValueError, match="acceptance receipt|approval receipt"):
        ReleasePublisher(tmp_path / "papers").publish(
            project,
            snapshot_id,
            status="PASS",
            package_builder=tampering_package,
        )

    assert not (tmp_path / "papers/demo/current.json").exists()


def test_failed_release_keeps_previous_current_release(tmp_path: Path) -> None:
    first_id = "b" * 64
    project = _project(tmp_path, "demo", first_id)
    publisher = ReleasePublisher(tmp_path / "papers")
    publisher.publish(
        project,
        first_id,
        status="PASS",
        package_builder=_package(project, "demo"),
    )

    second_id = "c" * 64
    _write_approved_audit(project, second_id)

    with pytest.raises(RuntimeError, match="packaging failed"):
        publisher.publish(
            project,
            second_id,
            status="PASS",
            package_builder=lambda _output: False,
        )

    current = resolve_current_release(
        tmp_path / "papers", "demo", project=project
    )
    assert current is not None
    assert current.release_id == first_id
    assert not (tmp_path / "papers/releases/demo" / second_id).exists()


def test_phase9_transition_during_staging_blocks_release_commit(tmp_path: Path) -> None:
    snapshot_id = "0" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    transitioned: dict[str, object] = {}

    def transition_after_package(output: Path) -> bool:
        package_submission(
            project,
            "demo",
            output,
            stage_only=True,
        )
        with authority_state_commit_lease(project):
            _install_current_phase9(project)
        transitioned["project_files"] = _file_bytes(project)
        return True

    with pytest.raises(
        Phase9DeliveryFenceError,
        match="Phase9 release requires explicit workflow_id and run_generation",
    ):
        ReleasePublisher(tmp_path / "papers").publish(
            project,
            snapshot_id,
            status="PASS",
            package_builder=transition_after_package,
        )

    assert transitioned
    assert _file_bytes(project) == transitioned["project_files"]
    assert not (
        project / ".factory/finalization/submission_bundle_manifest.json"
    ).exists()
    assert not (tmp_path / "papers").exists()


def test_stage_only_child_phase9_refusal_is_reclassified_by_release(
    tmp_path: Path,
) -> None:
    snapshot_id = "6" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    repository = Path(__file__).resolve().parents[1]
    transitioned: dict[str, dict[str, bytes]] = {}

    def transition_before_child(output: Path) -> bool:
        with authority_state_commit_lease(project):
            _install_current_phase9(project)
        transitioned["project_files"] = _file_bytes(project)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(repository / "scripts/package_submission.py"),
                str(project),
                "demo",
                str(output),
                "--stage-only",
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert result.returncode != 0
        assert "Phase9 submission requires explicit workflow_id" in result.stderr
        assert _file_bytes(project) == transitioned["project_files"]
        return False

    with pytest.raises(
        Phase9DeliveryFenceError,
        match="Phase9 release requires explicit workflow_id and run_generation",
    ):
        ReleasePublisher(tmp_path / "papers").publish(
            project,
            snapshot_id,
            status="PASS",
            package_builder=transition_before_child,
        )

    assert transitioned
    assert _file_bytes(project) == transitioned["project_files"]
    assert not (
        project / ".factory/finalization/submission_bundle_manifest.json"
    ).exists()
    assert not (tmp_path / "papers").exists()


def test_release_real_stage_only_subprocess_does_not_deadlock(tmp_path: Path) -> None:
    snapshot_id = "7" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    repository = Path(__file__).resolve().parents[1]

    def build_with_real_subprocess(output: Path) -> bool:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(repository / "scripts/package_submission.py"),
                str(project),
                "demo",
                str(output),
                "--stage-only",
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return True

    release = ReleasePublisher(tmp_path / "papers").publish(
        project,
        snapshot_id,
        status="PASS",
        package_builder=build_with_real_subprocess,
    )

    assert release.release_dir.is_dir()
    assert release.submission_zip.is_file()
    assert not (
        project / ".factory/finalization/submission_bundle_manifest.json"
    ).exists()


def test_no_judge_ablation_cannot_replace_current_release(tmp_path: Path) -> None:
    first_id = "3" * 64
    ablation_id = "4" * 64
    project = _project(tmp_path, "demo", first_id)
    publisher = ReleasePublisher(tmp_path / "papers")
    publisher.publish(
        project,
        first_id,
        status="PASS",
        package_builder=_package(project, "demo"),
    )
    _write_approved_audit(project, ablation_id)
    marker = project / "judge_outputs/final_submission.ablation.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": "final-submission-ablation-v1",
                "ablation": "ABLATE_NO_JUDGE",
                "judge_executed": False,
                "quality_pass_fabricated": False,
                "snapshot_id": ablation_id,
                "delivery_allowed": False,
                "terminal_reason": "PERMANENT_ABLATION_NO_DELIVERY",
                "returncode": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    snapshot = AuditSnapshot(
        **json.loads(
            (
                project / ".factory/audits" / ablation_id / "snapshot.json"
            ).read_text(encoding="utf-8")
        )
    )
    build_final_acceptance_receipt(
        project,
        snapshot,
        status="OVERRIDDEN",
        override_receipt=str(marker.relative_to(project)),
    )
    latest_path = project / ".factory/audits/latest.json"
    legacy_ablation = json.loads(latest_path.read_text(encoding="utf-8"))
    legacy_ablation.update(
        {
            "status": "OVERRIDDEN",
            "decision": "ABLATE_NO_JUDGE",
            "judge_completed": False,
            # Model the historical vulnerable record so release itself proves
            # it will not trust a caller-supplied delivery flag.
            "delivery_allowed": True,
            "override": False,
        }
    )
    latest_path.write_text(json.dumps(legacy_ablation) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no-judge ablation"):
        publisher.publish(
            project,
            ablation_id,
            status="OVERRIDDEN",
            package_builder=_package(project, "demo"),
        )

    current = resolve_current_release(
        tmp_path / "papers", "demo", project=project
    )
    assert current is not None
    assert current.release_id == first_id
    assert not (tmp_path / "papers/releases/demo" / ablation_id).exists()


def test_release_recovery_is_idempotent_and_repairs_legacy_aliases(
    tmp_path: Path,
) -> None:
    snapshot_id = "d" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    publisher = ReleasePublisher(tmp_path / "papers")
    first = publisher.publish(
        project,
        snapshot_id,
        status="PASS",
        package_builder=_package(project, "demo"),
    )
    (tmp_path / "papers/demo_paper.pdf").unlink()
    (tmp_path / "papers/demo_submission.zip").unlink()

    recovered = publisher.recover("demo", project=project)
    second = publisher.publish(
        project,
        snapshot_id,
        status="PASS",
        package_builder=lambda _output: (_ for _ in ()).throw(
            AssertionError("idempotent publish must not rebuild package")
        ),
    )

    assert recovered is not None
    assert second.reused is True
    assert second.release_dir == first.release_dir
    assert (tmp_path / "papers/demo_paper.pdf").is_file()
    assert (tmp_path / "papers/demo_submission.zip").is_file()


def test_publish_current_audit_uses_the_approved_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_id = "e" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "scripts",
        tmp_path / "scripts",
    )
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "factory_core",
        tmp_path / "factory_core",
    )

    def build_test_fixture_package(argv, **_kwargs):
        from factory_core.submission_bundle import submission_bundle_manifest

        assert "--stage-only" in argv
        project_arg = Path(argv[2])
        base_arg = str(argv[3])
        output_arg = Path(argv[4])
        manifest = submission_bundle_manifest(project_arg, base_arg)
        with zipfile.ZipFile(output_arg, "w") as archive:
            for item in manifest["members"]:
                archive.write(
                    project_arg / item["source_path"], item["archive_path"]
                )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(
        "scripts.publish_release.subprocess.run", build_test_fixture_package
    )

    release = publish_current_audit(project, tmp_path)

    assert release.release_id == snapshot_id
    assert resolve_current_release(
        tmp_path / "papers", "demo", project=project
    ) is not None


def test_publish_current_audit_rejects_nonfinal_or_unapproved_record(
    tmp_path: Path,
) -> None:
    snapshot_id = "f" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    latest = json.loads(
        (project / ".factory/audits/latest.json").read_text(encoding="utf-8")
    )
    latest["profile"] = "paper"
    (project / ".factory/audits/latest.json").write_text(
        json.dumps(latest) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="does not authorize"):
        publish_current_audit(project, tmp_path)


def test_alias_sync_failure_does_not_switch_the_current_pointer(
    tmp_path: Path,
) -> None:
    first_id = "1" * 64
    second_id = "2" * 64
    project = _project(tmp_path, "demo", first_id)
    ReleasePublisher(tmp_path / "papers").publish(
        project,
        first_id,
        status="PASS",
        package_builder=_package(project, "demo"),
    )
    _write_approved_audit(project, second_id)

    class AliasFailingPublisher(ReleasePublisher):
        def _sync_legacy_aliases(self, base, release):
            del base, release
            raise OSError("injected alias failure")

    with pytest.raises(OSError, match="injected alias failure"):
        AliasFailingPublisher(tmp_path / "papers").publish(
            project,
            second_id,
            status="PASS",
            package_builder=_package(project, "demo"),
        )

    current = resolve_current_release(
        tmp_path / "papers", "demo", project=project
    )
    assert current is not None
    assert current.release_id == first_id


def test_deadline_expiry_before_pointer_switch_leaves_current_release_unchanged(
    tmp_path: Path,
) -> None:
    first_id = "1" * 64
    second_id = "2" * 64
    project = _project(tmp_path, "demo", first_id)
    publisher = ReleasePublisher(tmp_path / "papers")
    publisher.publish(
        project,
        first_id,
        status="PASS",
        package_builder=_package(project, "demo"),
    )
    _write_approved_audit(project, second_id)
    checks = 0

    def deadline_check() -> None:
        nonlocal checks
        checks += 1
        if checks >= 4:
            raise RuntimeError("contest deadline reached")

    with pytest.raises(RuntimeError, match="contest deadline"):
        publisher.publish(
            project,
            second_id,
            status="PASS",
            package_builder=_package(project, "demo"),
            deadline_check=deadline_check,
        )

    current = resolve_current_release(
        tmp_path / "papers", "demo", project=project
    )
    assert current is not None
    assert current.release_id == first_id
