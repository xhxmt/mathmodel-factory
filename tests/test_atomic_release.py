from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from factory_core.audit.acceptance import build_final_acceptance_receipt
from factory_core.audit.domain import AuditSnapshot
from factory_core.delivery.release import ReleasePublisher, resolve_current_release
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
    (project / f"{base}_paper.pdf").write_bytes(b"%PDF audited\n")
    _write_approved_audit(project, snapshot_id)
    return project


def _package(project: Path, base: str):
    def build(output: Path) -> bool:
        with zipfile.ZipFile(output, "w") as archive:
            archive.write(project / f"{base}_paper.pdf", f"{base}_paper.pdf")
            archive.writestr("models/solve.py", "pass\n")
        return True

    return build


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

    current = resolve_current_release(tmp_path / "papers", "demo")
    assert current is not None
    assert current.release_id == snapshot_id
    assert current.paper.read_bytes() == project.joinpath("demo_paper.pdf").read_bytes()
    assert result.pointer == tmp_path / "papers/demo/current.json"
    assert (tmp_path / "papers/demo_paper.pdf").read_bytes() == current.paper.read_bytes()


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

    current = resolve_current_release(tmp_path / "papers", "demo")
    assert current is not None
    assert current.release_id == first_id
    assert not (tmp_path / "papers/releases/demo" / second_id).exists()


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

    recovered = publisher.recover("demo")
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


def test_publish_current_audit_uses_the_approved_snapshot(tmp_path: Path) -> None:
    snapshot_id = "e" * 64
    project = _project(tmp_path, "demo", snapshot_id)
    (project / "models").mkdir()
    (project / "models/solve.py").write_text("pass\n", encoding="utf-8")
    (project / "results").mkdir()
    (project / "results/result.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts/package_submission.py",
        tmp_path / "scripts/package_submission.py",
    )

    release = publish_current_audit(project, tmp_path)

    assert release.release_id == snapshot_id
    assert resolve_current_release(tmp_path / "papers", "demo") is not None


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

    current = resolve_current_release(tmp_path / "papers", "demo")
    assert current is not None
    assert current.release_id == first_id
