from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from ..audit.acceptance import verify_final_acceptance_receipt
from ..audit.domain import AuditSnapshot
from ..audit.persistence import atomic_write_json


RELEASE_MANIFEST_SCHEMA = "paper-factory-release-v1"
RELEASE_POINTER_SCHEMA = "paper-factory-release-pointer-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _artifact(path: Path, relative: str) -> dict[str, object]:
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ReleaseResult:
    release_id: str
    release_dir: Path
    paper: Path
    submission_zip: Path
    manifest: Path
    pointer: Path
    reused: bool = False


def resolve_current_release(papers_root: Path, base: str) -> ReleaseResult | None:
    papers_root = papers_root.resolve()
    pointer_path = papers_root / base / "current.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        if (
            pointer.get("schema_version") != RELEASE_POINTER_SCHEMA
            or pointer.get("base") != base
            or not SHA256_RE.fullmatch(str(pointer.get("release_id") or ""))
        ):
            return None
        relative = Path(str(pointer["release_path"]))
        if relative.is_absolute():
            return None
        release_dir = (papers_root / relative).resolve()
        release_dir.relative_to(papers_root)
        manifest_path = release_dir / "delivery_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        declared = manifest.get("content_sha256")
        unsigned = dict(manifest)
        unsigned.pop("content_sha256", None)
        if (
            manifest.get("schema_version") != RELEASE_MANIFEST_SCHEMA
            or manifest.get("base") != base
            or manifest.get("release_id") != pointer["release_id"]
            or declared != _canonical_hash(unsigned)
            or pointer.get("manifest_sha256") != _sha256(manifest_path)
        ):
            return None
        expected = {
            "paper": "paper.pdf",
            "submission_zip": "submission.zip",
            "final_audit_receipt": "final_audit_receipt.json",
            "audit_result": "audit_result.json",
            "audit_snapshot": "audit_snapshot.json",
        }
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != set(expected):
            return None
        for name, filename in expected.items():
            record = artifacts.get(name)
            path = release_dir / filename
            if (
                not isinstance(record, dict)
                or record.get("path") != filename
                or not path.is_file()
                or record.get("bytes") != path.stat().st_size
                or record.get("sha256") != _sha256(path)
            ):
                return None
        with zipfile.ZipFile(release_dir / "submission.zip") as archive:
            if archive.testzip() is not None:
                return None
            member = f"{base}_paper.pdf"
            if (
                member not in archive.namelist()
                or hashlib.sha256(archive.read(member)).hexdigest()
                != _sha256(release_dir / "paper.pdf")
            ):
                return None
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile):
        return None
    return ReleaseResult(
        release_id=str(pointer["release_id"]),
        release_dir=release_dir,
        paper=release_dir / "paper.pdf",
        submission_zip=release_dir / "submission.zip",
        manifest=manifest_path,
        pointer=pointer_path,
        reused=True,
    )


def current_release_artifacts(
    papers_root: Path, base: str
) -> tuple[Path, Path] | None:
    release = resolve_current_release(papers_root, base)
    return (release.paper, release.submission_zip) if release is not None else None


class ReleasePublisher:
    """Publish an immutable release and flip one atomic current pointer."""

    def __init__(self, papers_root: str | Path):
        self.papers_root = Path(papers_root).resolve()

    def publish(
        self,
        project: Path,
        snapshot_id: str,
        *,
        status: str,
        package_builder: Callable[[Path], bool],
        deadline_check: Callable[[], None] | None = None,
    ) -> ReleaseResult:
        check = deadline_check or (lambda: None)
        check()
        project = project.resolve()
        base = project.name
        if not SHA256_RE.fullmatch(snapshot_id):
            raise ValueError("release id must be the final-audit SHA-256")
        project_pdf = project / f"{base}_paper.pdf"
        if not project_pdf.is_file() or project_pdf.stat().st_size == 0:
            raise ValueError("final project PDF is missing")
        sources = self._validate_sources(project, snapshot_id, status)

        control_dir = self.papers_root / base
        releases_dir = self.papers_root / "releases" / base
        control_dir.mkdir(parents=True, exist_ok=True)
        releases_dir.mkdir(parents=True, exist_ok=True)
        lock_path = control_dir / ".publish.lock"
        with lock_path.open("a+", encoding="ascii") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            check()
            existing = self._existing_release(base, snapshot_id)
            if existing is not None:
                existing_status = json.loads(
                    existing.manifest.read_text(encoding="utf-8")
                ).get("status")
                if existing_status != status:
                    raise ValueError(
                        "immutable release exists with a different audit status"
                    )
                check()
                self._sync_legacy_aliases(base, existing)
                check()
                self._write_pointer(base, existing)
                return existing

            staging = Path(
                tempfile.mkdtemp(prefix=f".{snapshot_id}.staging-", dir=releases_dir)
            )
            try:
                shutil.copyfile(project_pdf, staging / "paper.pdf")
                if not package_builder(staging / "submission.zip"):
                    raise RuntimeError("submission packaging failed")
                check()
                self._validate_zip(staging / "submission.zip", project_pdf, base)
                sources = self._validate_sources(project, snapshot_id, status)
                if _sha256(staging / "paper.pdf") != _sha256(project_pdf):
                    raise ValueError("audited PDF changed during release construction")
                for name, source in sources.items():
                    shutil.copyfile(source, staging / f"{name}.json")
                artifacts = {
                    "paper": _artifact(staging / "paper.pdf", "paper.pdf"),
                    "submission_zip": _artifact(
                        staging / "submission.zip", "submission.zip"
                    ),
                    **{
                        name: _artifact(staging / f"{name}.json", f"{name}.json")
                        for name in sources
                    },
                }
                manifest: dict[str, object] = {
                    "schema_version": RELEASE_MANIFEST_SCHEMA,
                    "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                    "base": base,
                    "release_id": snapshot_id,
                    "snapshot_id": snapshot_id,
                    "status": status,
                    "artifacts": artifacts,
                }
                manifest["content_sha256"] = _canonical_hash(manifest)
                atomic_write_json(staging / "delivery_manifest.json", manifest)
                for child in staging.iterdir():
                    if child.is_file():
                        _fsync_file(child)
                _fsync_dir(staging)

                release_dir = releases_dir / snapshot_id
                os.replace(staging, release_dir)
                _fsync_dir(releases_dir)
                result = self._result(base, snapshot_id, reused=False)
                if result is None:
                    raise RuntimeError("committed release failed integrity verification")
                check()
                self._sync_legacy_aliases(base, result)
                check()
                self._write_pointer(base, result)
                return result
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def recover(self, base: str) -> ReleaseResult | None:
        current = resolve_current_release(self.papers_root, base)
        if current is not None:
            self._sync_legacy_aliases(base, current)
        return current

    def _existing_release(self, base: str, release_id: str) -> ReleaseResult | None:
        return self._result(base, release_id, reused=True)

    def _result(
        self, base: str, release_id: str, *, reused: bool
    ) -> ReleaseResult | None:
        release_dir = self.papers_root / "releases" / base / release_id
        manifest = release_dir / "delivery_manifest.json"
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            unsigned = dict(value)
            declared = unsigned.pop("content_sha256", None)
            if (
                value.get("schema_version") != RELEASE_MANIFEST_SCHEMA
                or value.get("base") != base
                or value.get("release_id") != release_id
                or declared != _canonical_hash(unsigned)
            ):
                return None
            expected = {
                "paper": "paper.pdf",
                "submission_zip": "submission.zip",
                "final_audit_receipt": "final_audit_receipt.json",
                "audit_result": "audit_result.json",
                "audit_snapshot": "audit_snapshot.json",
            }
            artifacts = value.get("artifacts")
            if not isinstance(artifacts, dict) or set(artifacts) != set(expected):
                return None
            for name, filename in expected.items():
                path = release_dir / filename
                record = artifacts.get(name)
                if (
                    not path.is_file()
                    or not isinstance(record, dict)
                    or record.get("path") != filename
                    or record.get("bytes") != path.stat().st_size
                    or record.get("sha256") != _sha256(path)
                ):
                    return None
            with zipfile.ZipFile(release_dir / "submission.zip") as archive:
                if archive.testzip() is not None:
                    return None
                member = f"{base}_paper.pdf"
                if (
                    member not in archive.namelist()
                    or hashlib.sha256(archive.read(member)).hexdigest()
                    != _sha256(release_dir / "paper.pdf")
                ):
                    return None
        except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile):
            return None
        pointer = self.papers_root / base / "current.json"
        return ReleaseResult(
            release_id=release_id,
            release_dir=release_dir,
            paper=release_dir / "paper.pdf",
            submission_zip=release_dir / "submission.zip",
            manifest=manifest,
            pointer=pointer,
            reused=reused,
        )

    def _write_pointer(self, base: str, release: ReleaseResult) -> None:
        pointer = {
            "schema_version": RELEASE_POINTER_SCHEMA,
            "base": base,
            "release_id": release.release_id,
            "release_path": str(release.release_dir.relative_to(self.papers_root)),
            "manifest_sha256": _sha256(release.manifest),
            "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        atomic_write_json(release.pointer, pointer)
        _fsync_dir(release.pointer.parent)

    def _sync_legacy_aliases(self, base: str, release: ReleaseResult) -> None:
        for source, target_name in (
            (release.paper, f"{base}_paper.pdf"),
            (release.submission_zip, f"{base}_submission.zip"),
        ):
            target = self.papers_root / target_name
            temporary = self.papers_root / f".{target_name}.{release.release_id}.tmp"
            shutil.copyfile(source, temporary)
            _fsync_file(temporary)
            os.replace(temporary, target)
        _fsync_dir(self.papers_root)

    @staticmethod
    def _validate_sources(
        project: Path, snapshot_id: str, status: str
    ) -> dict[str, Path]:
        sources = {
            "final_audit_receipt": project
            / "judge_outputs/final_acceptance_receipt.json",
            "audit_result": project / ".factory/audits/latest.json",
            "audit_snapshot": project
            / ".factory/audits"
            / snapshot_id
            / "snapshot.json",
        }
        try:
            audit_value = json.loads(
                sources["audit_result"].read_text(encoding="utf-8")
            )
            snapshot_value = json.loads(
                sources["audit_snapshot"].read_text(encoding="utf-8")
            )
            snapshot = AuditSnapshot(**snapshot_value)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"release source is missing or invalid: {exc}") from exc
        if (
            audit_value.get("snapshot_id") != snapshot_id
            or audit_value.get("base") != project.name
            or audit_value.get("profile") != "final"
            or audit_value.get("status") != status
            or audit_value.get("delivery_allowed") is not True
            or snapshot.snapshot_id != snapshot_id
            or snapshot.base != project.name
            or snapshot.profile != "final"
        ):
            raise ValueError("release sources do not bind the same approved snapshot")
        if status == "PASS" and (
            audit_value.get("decision") != "PASS"
            or audit_value.get("judge_completed") is not True
        ):
            raise ValueError("PASS release does not have a completed PASS judgment")
        if status == "OVERRIDDEN" and not (
            audit_value.get("override") is True
            or audit_value.get("decision") == "ABLATE_NO_JUDGE"
        ):
            raise ValueError("OVERRIDDEN release has no recognized authorization")
        valid, errors = verify_final_acceptance_receipt(
            project,
            snapshot,
            expected_snapshot_id=snapshot_id,
            expected_status=status,
        )
        if not valid:
            raise ValueError(
                "final acceptance receipt is stale or invalid: " + "; ".join(errors)
            )
        identity = snapshot.identity
        if not (
            isinstance(identity, dict)
            and identity.get("source") == "injected_fingerprinter"
        ):
            from scripts.submission_fingerprint import submission_fingerprint

            current = submission_fingerprint(
                project, project.name, policy_mode="enforce"
            )
            if current != snapshot_id:
                raise ValueError("project content changed after Final Audit")
        return sources

    @staticmethod
    def _validate_zip(path: Path, project_pdf: Path, base: str) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("submission zip is missing")
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("submission zip integrity check failed")
            member = f"{base}_paper.pdf"
            if member not in archive.namelist():
                raise ValueError("submission zip does not contain the final PDF")
            if hashlib.sha256(archive.read(member)).hexdigest() != _sha256(project_pdf):
                raise ValueError("submission zip PDF differs from the audited PDF")
        from ..submission_bundle import (
            submission_bundle_manifest,
            verify_zip_against_manifest,
        )

        project = project_pdf.parent.resolve()
        verify_zip_against_manifest(
            path, submission_bundle_manifest(project, base)
        )
