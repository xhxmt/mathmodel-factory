from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import zipfile

import pytest

from scripts.evidence_payload_policy import PayloadPolicyFinding


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools/generate_freeze_inventory.py"
BUILDER = ROOT / "archive_tools/build_deterministic_candidate.py"
VERIFY = ROOT / "archive_tools/verify_candidate_zip.py"


def test_candidate_manifest_schema_is_phase_neutral_and_versioned() -> None:
    safety = importlib.import_module("archive_tools.archive_safety")

    assert safety.MANIFEST_SCHEMA == "paper-factory-full-shadow-candidate-manifest-v2"
    assert "phase4-6" not in safety.MANIFEST_SCHEMA


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _run(*arguments: object, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, *(str(argument) for argument in arguments)],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _initialize_collection_root(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("normal candidate input\n", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested/ok.txt").write_text("included\n", encoding="utf-8")


def _unsafe_archive_name(name: str) -> bool:
    folded = name.replace("\\", "/").casefold().rsplit("/", 1)[-1]
    database_suffixes = (".db", ".sqlite", ".sqlite3")
    sidecars = ("", "-wal", "-shm", "-journal")
    return folded.startswith(".put-") or any(
        folded.endswith(database_suffix + sidecar)
        for database_suffix in database_suffixes
        for sidecar in sidecars
    )


def _write_self_consistent_candidate(
    archive: Path,
    payload: dict[str, bytes],
    *,
    archive_root: str = "normal_flow_payload",
) -> None:
    """Write a builder-schema-valid archive without invoking path policy."""

    safety = importlib.import_module("archive_tools.archive_safety")
    source_paths = sorted(payload)
    inventory_raw = ("\n".join(source_paths) + "\n").encode("utf-8")
    manifest_name = f"{archive_root}/{safety.MANIFEST_BASENAME}"
    checksums_name = f"{archive_root}/{safety.CHECKSUMS_RELATIVE}"
    files = [
        {
            "source_path": relative,
            "archive_path": f"{archive_root}/{relative}",
            "size": len(payload[relative]),
            "sha256": safety.sha256_bytes(payload[relative]),
            "mode": 0o644,
        }
        for relative in source_paths
    ]
    manifest = {
        "schema": safety.MANIFEST_SCHEMA,
        "builder": safety.BUILDER_ID,
        "archive_root": archive_root,
        "deterministic_timestamp": "1980-01-01T00:00:00Z",
        "inventory_sha256": safety.sha256_bytes(inventory_raw),
        "metadata": {"round": "normal-flow-f3-verifier"},
        "closure": {
            "manifest": manifest_name,
            "checksums": checksums_name,
            "checksums_cover": "every payload member plus MANIFEST.json",
            "checksums_exclude": (
                "checksums/SHA256SUMS (self-reference is forbidden)"
            ),
        },
        "files": files,
    }
    manifest_raw = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    checksum_values = {
        entry["archive_path"]: entry["sha256"] for entry in files
    }
    checksum_values[manifest_name] = safety.sha256_bytes(manifest_raw)
    checksums_raw = "".join(
        f"{checksum_values[name]}  {name}\n" for name in sorted(checksum_values)
    ).encode("utf-8")

    def member_info(name: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=safety.FIXED_ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        info.extra = b""
        info.comment = b""
        return info

    with zipfile.ZipFile(
        archive,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as candidate:
        for relative in source_paths:
            candidate.writestr(
                member_info(f"{archive_root}/{relative}"), payload[relative]
            )
        candidate.writestr(member_info(manifest_name), manifest_raw)
        candidate.writestr(member_info(checksums_name), checksums_raw)


def _record_zip_member_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []
    original_open = zipfile.ZipFile.open

    def tracked_open(self, name, *args, **kwargs):
        opened.append(
            name.filename if isinstance(name, zipfile.ZipInfo) else str(name)
        )
        return original_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", tracked_open)
    return opened


def test_live_sqlite_wal_is_excluded_by_real_inventory_and_builder(
    tmp_path: Path,
) -> None:
    source = tmp_path / "collection"
    _initialize_collection_root(source)
    runtime = source / "runtime/acl"
    runtime.mkdir(parents=True)
    database = runtime / "controller.sqlite"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE grants(subject TEXT NOT NULL)")
        connection.execute("INSERT INTO grants VALUES ('normal-user')")
        connection.commit()
        # Keep a reader open so SQLite retains both ordinary live sidecars.
        connection.execute("SELECT * FROM grants").fetchall()
        assert database.is_file()
        assert Path(f"{database}-wal").is_file()
        assert Path(f"{database}-shm").is_file()

        denied_fixtures = {
            source / "nested/STATE.SQLite-JOURNAL": b"journal-state",
            source / "windows\\ACL.Db-WaL": b"windows-sidecar",
            source / "runtime/cas/objects/sha256/aa/.put-deadbeef-1234": b"cas-temp",
        }
        for path, value in denied_fixtures.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        _git(source, "add", "-f", "--", ".")

        inventory = tmp_path / "inventory"
        generated = _run(
            GENERATOR,
            "--source-root",
            source,
            "--output-dir",
            inventory,
        )
        assert generated.returncode == 0, (generated.stdout, generated.stderr)
        paths = (inventory / "CANDIDATE_SOURCE_PATHS.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        assert paths == ["README.md", "nested/ok.txt"]
        exclusions = (inventory / "CANDIDATE_EXCLUSIONS.tsv").read_text(
            encoding="utf-8"
        )
        for denied in (
            "runtime/acl/controller.sqlite",
            "runtime/acl/controller.sqlite-wal",
            "runtime/acl/controller.sqlite-shm",
            "nested/STATE.SQLite-JOURNAL",
            "windows\\ACL.Db-WaL",
        ):
            assert (
                f"{denied}\tpath-policy-before-file-io:database_state"
                in exclusions
            )
        assert (
            "runtime/cas/objects/sha256/aa/.put-deadbeef-1234"
            "\tpath-policy-before-file-io:cas_temporary_state"
            in exclusions
        )

        metadata = tmp_path / "metadata.json"
        metadata.write_text(
            json.dumps({"round": "normal-flow-f3-e2e"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        output = tmp_path / "candidate"
        built = _run(
            BUILDER,
            "--source-root",
            source,
            "--inventory",
            inventory / "CANDIDATE_SOURCE_PATHS.txt",
            "--metadata",
            metadata,
            "--output-dir",
            output,
            "--archive-name",
            "normal-flow-payload.zip",
            "--archive-root",
            "normal_flow_payload",
            "--require",
            "README.md",
            "--require",
            "nested/ok.txt",
        )
        assert built.returncode == 0, (built.stdout, built.stderr)
        archive = output / "normal-flow-payload.zip"
        verified = _run(
            VERIFY,
            archive,
            "--outer-sha256sums",
            output / "SHA256SUMS",
        )
        assert verified.returncode == 0, (verified.stdout, verified.stderr)
        with zipfile.ZipFile(archive) as candidate:
            names = candidate.namelist()
            assert not any(_unsafe_archive_name(name) for name in names)
            manifest = json.loads(
                candidate.read("normal_flow_payload/MANIFEST.json")
            )
        assert [entry["source_path"] for entry in manifest["files"]] == paths
    finally:
        connection.close()


def test_inventory_policy_denial_precedes_lstat_and_content_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = importlib.import_module("tools.generate_freeze_inventory")
    source = tmp_path / "source"
    _initialize_collection_root(source)
    blocked = source / "blocked.db"
    blocked.write_bytes(b"must-not-be-read")
    _git(source, "add", "-f", "--", ".")

    original_lstat = Path.lstat
    original_hash = generator._sha256_file

    def guarded_lstat(path: Path):
        if path == blocked:
            raise AssertionError("blocked database was lstat'ed")
        return original_lstat(path)

    def guarded_hash(path: Path) -> str:
        if path == blocked:
            raise AssertionError("blocked database was read")
        return original_hash(path)

    monkeypatch.setattr(Path, "lstat", guarded_lstat)
    monkeypatch.setattr(generator, "_sha256_file", guarded_hash)
    output = tmp_path / "inventory"
    generator.generate(source, output)
    assert "blocked.db" not in (
        output / "CANDIDATE_SOURCE_PATHS.txt"
    ).read_text(encoding="utf-8").splitlines()


def test_inventory_policy_error_fails_closed_before_candidate_file_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = importlib.import_module("tools.generate_freeze_inventory")
    source = tmp_path / "source"
    _initialize_collection_root(source)
    blocked = source / "blocked.db"
    blocked.write_bytes(b"must-not-be-read")
    _git(source, "add", "-f", "--", ".")

    original_lstat = Path.lstat

    def guarded_lstat(path: Path):
        if path == blocked:
            raise AssertionError("policy failure reached candidate lstat")
        return original_lstat(path)

    def broken_policy(path: str) -> PayloadPolicyFinding | None:
        if path == "blocked.db":
            raise RuntimeError("synthetic central policy failure")
        return None

    monkeypatch.setattr(Path, "lstat", guarded_lstat)
    monkeypatch.setattr(
        generator.payload_policy, "payload_path_finding", broken_policy
    )
    with pytest.raises(SystemExit, match="failed closed"):
        generator.generate(source, tmp_path / "inventory")


@pytest.mark.parametrize(
    "relative",
    [
        "runtime/NESTED/AUTH.DB-WAL",
        "runtime\\nested\\Auth.Db-ShM",
        "nested/cache.sqlite-journal",
        "nested/project.SQLITE3",
    ],
)
def test_builder_rejects_cross_platform_database_paths_before_source_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    builder = importlib.import_module("archive_tools.build_deterministic_candidate")
    source = tmp_path / "source"
    source.mkdir()
    inventory = tmp_path / "inventory.txt"
    inventory.write_text(relative + "\n", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    metadata.write_text('{"round":"f3-builder-policy"}\n', encoding="utf-8")
    output = tmp_path / "candidate"
    source_opened = False

    def forbidden_source_open(path: Path) -> int:
        nonlocal source_opened
        source_opened = True
        raise AssertionError("source root opened before payload path policy")

    monkeypatch.setattr(builder, "_open_source_root", forbidden_source_open)
    arguments = argparse.Namespace(
        source_root=str(source),
        inventory=str(inventory),
        metadata=str(metadata),
        output_dir=output,
        archive_name="candidate.zip",
        archive_root="normal_flow_payload",
        require=[],
        max_total_bytes=1024 * 1024,
    )
    with pytest.raises(builder.ArchivePolicyError, match="before file I/O"):
        builder.build(arguments)
    assert source_opened is False
    assert output.exists() is False
    assert (output / "candidate.zip").exists() is False


def test_builder_policy_exception_fails_closed_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = importlib.import_module("archive_tools.build_deterministic_candidate")
    source = tmp_path / "source"
    source.mkdir()
    inventory = tmp_path / "inventory.txt"
    inventory.write_text("normal.txt\n", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    metadata.write_text('{"round":"f3-policy-error"}\n', encoding="utf-8")
    output = tmp_path / "candidate"

    def broken_policy(_path: str) -> PayloadPolicyFinding | None:
        raise RuntimeError("synthetic policy failure")

    monkeypatch.setattr(builder.payload_policy, "payload_path_finding", broken_policy)
    monkeypatch.setattr(
        builder,
        "_open_source_root",
        lambda _path: pytest.fail("source root opened after policy failure"),
    )
    arguments = argparse.Namespace(
        source_root=str(source),
        inventory=str(inventory),
        metadata=str(metadata),
        output_dir=output,
        archive_name="candidate.zip",
        archive_root="normal_flow_payload",
        require=[],
        max_total_bytes=1024 * 1024,
    )
    with pytest.raises(builder.ArchivePolicyError, match="failed closed"):
        builder.build(arguments)
    assert output.exists() is False


def test_verifier_rejects_self_consistent_sqlite_wal_before_member_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safety = importlib.import_module("archive_tools.archive_safety")
    archive = tmp_path / "forged-sqlite-wal.zip"
    _write_self_consistent_candidate(
        archive,
        {
            "README.md": b"ordinary payload\n",
            "runtime/controller.sqlite-wal": b"live sqlite wal bytes\n",
        },
    )

    # Establish that the forged archive is otherwise a valid manifest and
    # checksum closure, so the central path policy is the rejecting boundary.
    original_require = safety.payload_policy.require_payload_path_allowed
    monkeypatch.setattr(
        safety.payload_policy, "require_payload_path_allowed", lambda _path: None
    )
    assert safety.verify_archive(archive)["manifest_checksum_closure"] == "PASS"
    monkeypatch.setattr(
        safety.payload_policy, "require_payload_path_allowed", original_require
    )

    opened = _record_zip_member_opens(monkeypatch)
    with pytest.raises(
        safety.ArchivePolicyError,
        match="payload path rejected before file I/O.*database_state",
    ):
        safety.verify_archive(archive)
    assert opened == []


def test_verifier_policy_exception_fails_closed_before_member_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safety = importlib.import_module("archive_tools.archive_safety")
    archive = tmp_path / "ordinary.zip"
    _write_self_consistent_candidate(archive, {"README.md": b"ordinary payload\n"})

    def broken_policy(_path: str) -> None:
        raise RuntimeError("synthetic verifier policy failure")

    monkeypatch.setattr(
        safety.payload_policy, "require_payload_path_allowed", broken_policy
    )
    opened = _record_zip_member_opens(monkeypatch)
    with pytest.raises(
        safety.ArchivePolicyError,
        match="payload path policy failed closed.*README.md",
    ):
        safety.verify_archive(archive)
    assert opened == []
