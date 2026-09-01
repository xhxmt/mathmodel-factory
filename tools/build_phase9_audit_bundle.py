#!/usr/bin/env python3
"""Build and independently verify a deterministic Phase9 Pro audit ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import unicodedata
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.canonical import canonical_bytes, canonical_sha256


FIXED_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
NORMALIZED_MODE = 0o100644
DEFAULT_SOURCE_PATHS = (
    "AGENTS.md", "CHANGELOG.md", "CLAUDE.md", "DOCUMENTATION_INDEX.md",
    "docs/architecture/PHASE2_PRODUCTION_AUTHORITY_FOUNDATION.md",
    "docs/architecture/PHASE7_8_DURABLE_FULL_SHADOW.md",
    "docs/operations/PHASE9_ENTRY_GATE.md",
    "docs/operations/PHASE9_GAP_MATRIX.md",
    "docs/operations/PHASE9_IMPLEMENTATION_AND_ROLLBACK.md",
    "docs/operations/PHASE9_PREP_RUNBOOK.md",
    "docs/operations/PHASE9_REQUIREMENT_IMPLEMENTATION_TEST_EVIDENCE_MAP.tsv",
    "docs/operations/PHASE9_TEST_EVIDENCE_MATRIX.md",
    "factory_core/authority_operations.py",
    "factory_core/authority_production_schema.py",
    "factory_core/delivery/release.py",
    "factory_core/phase9_config.py", "factory_core/phase9_entry.py",
    "factory_core/phase9_forensic_replay.py",
    "factory_core/phase9_run_generation.py",
    "scripts/authority_operator.py", "scripts/phase9_entry_gate.py",
    "scripts/phase9_forensic_replay.py", "scripts/phase9_prep_manifest.py",
    "tests/test_atomic_release.py", "tests/test_authority_operations.py",
    "tests/test_authority_production_migration.py",
    "tests/test_m01_runtime_parity.py",
    "tests/test_phase1_8_durable_continuous_chain.py",
    "tests/test_phase78_enabled_e2e.py", "tests/test_phase9_entry_gate.py",
    "tests/test_phase9_forensic_replay.py",
    "tests/test_phase9_prep_manifest.py", "tests/test_phase9_run_generation.py",
    "tests/test_workflow_state.py", "tools/build_phase9_audit_bundle.py",
    "tools/build_phase9_test_summary.py", "tools/run_audit_command.py",
    "tools/run_full_repo_with_frontend_deps.py",
)


def _git(repository: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repository, input=input_bytes, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def _identity(repository: Path, commit: str) -> dict[str, str]:
    resolved = _git(repository, "rev-parse", f"{commit}^{{commit}}").decode().strip()
    tree = _git(repository, "rev-parse", f"{resolved}^{{tree}}").decode().strip()
    parents = _git(repository, "show", "-s", "--format=%P", resolved).decode().split()
    if len(parents) != 1:
        raise RuntimeError("candidate must have exactly one parent")
    return {"commit": resolved, "tree": tree, "parent": parents[0]}


def _safe(path: str) -> str:
    pure = PurePosixPath(path)
    if (
        not path or pure.is_absolute() or pure.as_posix() != path
        or ".." in pure.parts or "\\" in path or "\x00" in path
        or unicodedata.normalize("NFC", path) != path
    ):
        raise RuntimeError(f"unsafe package path: {path!r}")
    return path


def _candidate_inventory(repository: Path, commit: str) -> tuple[bytes, set[str]]:
    raw = _git(repository, "ls-tree", "-rz", "--full-tree", commit)
    entries = []
    paths: set[str] = set()
    oids = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        path = _safe(raw_path.decode("utf-8", errors="strict"))
        if path in paths:
            raise RuntimeError(f"duplicate Git path: {path}")
        paths.add(path)
        oids.append(oid)
        entries.append((path, mode, kind, oid))
    query = b"".join(f"{oid}\n".encode() for oid in oids)
    sizes_raw = _git(
        repository, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_bytes=query,
    )
    sizes = {}
    for line in sizes_raw.decode("ascii").splitlines():
        oid, kind, size = line.split()
        sizes[oid] = (kind, int(size))
    lines = ["path\tmode\ttype\tobject_id\tbytes"]
    for path, mode, kind, oid in sorted(entries):
        observed_kind, size = sizes[oid]
        if observed_kind != kind:
            raise RuntimeError(f"Git object kind differs: {path}")
        lines.append(f"{path}\t{mode}\t{kind}\t{oid}\t{size}")
    return ("\n".join(lines) + "\n").encode(), paths


def _blob(repository: Path, commit: str, path: str) -> bytes:
    return _git(repository, "cat-file", "blob", f"{commit}:{path}")


def _copy_audit_evidence(audit_root: Path, payload: dict[str, bytes]) -> None:
    allowed_roots = {"command_records", "evidence", "receipts", "review", "test_logs"}
    for path in sorted(item for item in audit_root.rglob("*") if item.is_file()):
        relative = path.relative_to(audit_root).as_posix()
        if relative.split("/", 1)[0] not in allowed_roots:
            continue
        if (
            "__pycache__" in relative or relative.endswith((".pyc", ".db", ".db-wal", ".db-shm"))
            or "/tmp/" in f"/{relative}/" or relative.endswith(".zip")
        ):
            raise RuntimeError(f"forbidden audit evidence path: {relative}")
        payload[_safe(relative)] = path.read_bytes()


def _secret_scan(payload: dict[str, bytes]) -> None:
    forbidden = (
        b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN OPENSSH PRIVATE KEY-----",
        b"AIzaSy", b"AKIA",
    )
    for path, raw in payload.items():
        if any(marker in raw for marker in forbidden):
            raise RuntimeError(f"possible credential material in {path}")


def _manifest(payload: dict[str, bytes]) -> bytes:
    body = {
        "schema": "paper-factory-phase9-audit-manifest-v1",
        "closure": "all payload members except PACKAGE_MANIFEST.json and checksums/SHA256SUMS",
        "files": [
            {
                "path": path,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mode": "0644",
            }
            for path, raw in sorted(payload.items())
        ],
    }
    body["files_sha256"] = canonical_sha256(body["files"])
    return canonical_bytes(body) + b"\n"


def _verify_zip(path: Path, root_name: str) -> dict[str, object]:
    raw_zip = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("ZIP CRC verification failed")
        infos = archive.infolist()
        if not infos or len({info.filename for info in infos}) != len(infos):
            raise RuntimeError("ZIP member inventory is empty or duplicated")
        folded: set[str] = set()
        for info in infos:
            name = _safe(info.filename)
            parts = PurePosixPath(name).parts
            if not parts or parts[0] != root_name or info.is_dir():
                raise RuntimeError("ZIP must contain ordinary files under one root")
            relative = PurePosixPath(*parts[1:]).as_posix()
            _safe(relative)
            folded_name = unicodedata.normalize("NFC", relative).casefold()
            if folded_name in folded:
                raise RuntimeError("ZIP has a casefold/NFC collision")
            folded.add(folded_name)
            if info.date_time != FIXED_ZIP_TIMESTAMP:
                raise RuntimeError("ZIP timestamp is not fixed")
            if (info.external_attr >> 16) != NORMALIZED_MODE:
                raise RuntimeError("ZIP member mode is not normalized 0644")
        names = {info.filename for info in infos}
        prefix = f"{root_name}/"
        manifest_path = prefix + "PACKAGE_MANIFEST.json"
        checksum_path = prefix + "checksums/SHA256SUMS"
        manifest = json.loads(archive.read(manifest_path))
        expected_manifest = {
            prefix + row["path"]: (row["bytes"], row["sha256"], row["mode"])
            for row in manifest["files"]
        }
        if set(expected_manifest) != names - {manifest_path, checksum_path}:
            raise RuntimeError("manifest member closure differs")
        for member, (size, digest, mode) in expected_manifest.items():
            content = archive.read(member)
            if (
                len(content) != size
                or hashlib.sha256(content).hexdigest() != digest
                or mode != "0644"
            ):
                raise RuntimeError(f"manifest identity differs: {member}")
        checksum_lines = archive.read(checksum_path).decode("ascii").splitlines()
        checksums = {}
        for line in checksum_lines:
            digest, relative = line.split("  ", 1)
            checksums[prefix + relative] = digest
        if set(checksums) != names - {checksum_path}:
            raise RuntimeError("checksum closure differs")
        for member, digest in checksums.items():
            if hashlib.sha256(archive.read(member)).hexdigest() != digest:
                raise RuntimeError(f"checksum differs: {member}")
    return {
        "path": str(path.resolve()), "bytes": len(raw_zip),
        "member_count": len(infos), "sha256": hashlib.sha256(raw_zip).hexdigest(),
        "single_root": root_name, "crc_verified": True,
        "fixed_timestamp": "2026-01-01T00:00:00Z", "normalized_mode": "0644",
    }


def build(
    repository: Path,
    audit_root: Path,
    output: Path,
    *,
    root_name: str,
    freeze_utc: str,
    commit: str = "HEAD",
    source_paths: tuple[str, ...] = DEFAULT_SOURCE_PATHS,
) -> dict[str, object]:
    repository = repository.resolve(strict=True)
    audit_root = audit_root.resolve(strict=True)
    identity = _identity(repository, commit)
    inventory, candidate_paths = _candidate_inventory(repository, identity["commit"])
    payload: dict[str, bytes] = {
        "identity/CANDIDATE_FILE_INVENTORY.tsv": inventory,
    }
    source_rows = []
    for path in sorted(source_paths):
        _safe(path)
        if path not in candidate_paths:
            raise RuntimeError(f"frozen source path is absent from candidate: {path}")
        raw = _blob(repository, identity["commit"], path)
        package_path = f"source/{path}"
        payload[package_path] = raw
        source_rows.append(
            f"{path}\t{len(raw)}\t{hashlib.sha256(raw).hexdigest()}\t{package_path}"
        )
    payload["identity/FROZEN_SOURCE_SUBSET.tsv"] = (
        "candidate_path\tbytes\tsha256\tpackage_path\n"
        + "\n".join(source_rows) + "\n"
    ).encode()
    identity_body = {
        "schema": "paper-factory-phase9-candidate-identity-v1",
        **identity,
        "baseline": {
            "commit": "7c7f0d3f388e54913b036f4566bd6604e9c3d850",
            "tree": "9927e11e5f771206ac2a0403d6541c0853ccc096",
            "parent": "43138c5a5ee957fb19162abb9c33edb99f2ff010",
        },
        "freeze_utc": freeze_utc,
        "candidate_inventory_bytes": len(inventory),
        "candidate_inventory_sha256": hashlib.sha256(inventory).hexdigest(),
        "candidate_path_count": len(candidate_paths),
        "frozen_source_path_count": len(source_paths),
    }
    identity_body["identity_sha256"] = canonical_sha256(identity_body)
    payload["identity/CANDIDATE_IDENTITY.json"] = canonical_bytes(identity_body) + b"\n"
    _copy_audit_evidence(audit_root, payload)
    payload["PACKAGE_README.md"] = (
        "# Paper Factory Phase9 Pro audit package\n\n"
        "This deterministic, single-root package is bound to the candidate in "
        "`identity/CANDIDATE_IDENTITY.json`. It contains a full tracked-file "
        "inventory, a frozen source subset, exact test commands/raw logs/statistics, "
        "requirements/gaps/receipts, and an honestly BLOCKED production status. "
        "It contains no production database, official input, credentials, caches, "
        "dependencies, runtime state, generated paper, or nested archive. A review "
        "PASS does not authorize migration, provider/network use, outbox, delivery, "
        "release, deployment, or cutover.\n"
    ).encode()
    _secret_scan(payload)
    manifest = _manifest(payload)
    payload["PACKAGE_MANIFEST.json"] = manifest
    checksum_lines = [
        f"{hashlib.sha256(raw).hexdigest()}  {path}"
        for path, raw in sorted(payload.items())
    ]
    payload["checksums/SHA256SUMS"] = ("\n".join(checksum_lines) + "\n").encode("ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, raw in sorted(payload.items()):
            info = zipfile.ZipInfo(f"{root_name}/{relative}", FIXED_ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = NORMALIZED_MODE << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return _verify_zip(output, root_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--audit-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root-name", default="PAPER_FACTORY_PHASE9_PRO_AUDIT")
    parser.add_argument("--freeze-utc", required=True)
    parser.add_argument("--commit", default="HEAD")
    args = parser.parse_args(argv)
    if re.fullmatch(r"[A-Z0-9_]+", args.root_name) is None:
        parser.error("root name must contain only A-Z, 0-9, underscore")
    result = build(
        args.repository, args.audit_root, args.output, root_name=args.root_name,
        freeze_utc=args.freeze_utc, commit=args.commit,
    )
    print(canonical_bytes(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
