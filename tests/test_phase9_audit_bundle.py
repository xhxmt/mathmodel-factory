from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import zipfile

import pytest

from tools.build_phase9_audit_bundle import build


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Phase9 Test")
    _git(repo, "config", "user.email", "phase9-test@example.invalid")
    (repo / "tracked.txt").write_text("parent\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "parent")
    (repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "candidate")
    return repo


def test_audit_bundle_is_deterministic_single_root_and_closed(tmp_path):
    repo = _repository(tmp_path)
    audit = tmp_path / "audit"
    (audit / "evidence").mkdir(parents=True)
    (audit / "test_logs").mkdir()
    (audit / "evidence/summary.json").write_text("{}\n", encoding="utf-8")
    (audit / "test_logs/focused.log").write_text("1 passed\n", encoding="utf-8")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first_result = build(
        repo, audit, first, root_name="PHASE9_TEST", freeze_utc="2026-09-01T12:00:00Z",
        source_paths=("tracked.txt",),
    )
    second_result = build(
        repo, audit, second, root_name="PHASE9_TEST", freeze_utc="2026-09-01T12:00:00Z",
        source_paths=("tracked.txt",),
    )
    assert first.read_bytes() == second.read_bytes()
    assert first_result["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert first_result["sha256"] == second_result["sha256"]
    assert first_result["crc_verified"] is True
    with zipfile.ZipFile(first) as archive:
        assert archive.testzip() is None
        assert {name.split("/", 1)[0] for name in archive.namelist()} == {"PHASE9_TEST"}
        assert "PHASE9_TEST/PACKAGE_MANIFEST.json" in archive.namelist()
        assert "PHASE9_TEST/checksums/SHA256SUMS" in archive.namelist()
        assert "PHASE9_TEST/identity/CANDIDATE_FILE_INVENTORY.tsv" in archive.namelist()
        assert "PHASE9_TEST/source/tracked.txt" in archive.namelist()


def test_audit_bundle_rejects_private_key_material(tmp_path):
    repo = _repository(tmp_path)
    audit = tmp_path / "audit"
    (audit / "evidence").mkdir(parents=True)
    private_key_header = b"-----BEGIN " + b"PRIVATE KEY-----\n"
    (audit / "evidence/bad.txt").write_bytes(private_key_header)
    with pytest.raises(RuntimeError, match="credential material"):
        build(
            repo, audit, tmp_path / "bad.zip", root_name="PHASE9_TEST",
            freeze_utc="2026-09-01T12:00:00Z", source_paths=("tracked.txt",),
        )
