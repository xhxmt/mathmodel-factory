from __future__ import annotations

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
_PHASE9_RELEASE_MANIFEST_SCHEMA = "paper-factory-release-v2"
_PHASE9_RELEASE_POINTER_SCHEMA = "paper-factory-release-pointer-v2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CORE_RELEASE_ARTIFACTS = {
    "paper": "paper.pdf",
    "submission_zip": "submission.zip",
    "final_audit_receipt": "final_audit_receipt.json",
    "audit_result": "audit_result.json",
    "audit_snapshot": "audit_snapshot.json",
}


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


def _expected_release_artifacts(manifest: dict[str, object]) -> dict[str, str]:
    expected = dict(_CORE_RELEASE_ARTIFACTS)
    evidence = manifest.get("evidence_artifacts")
    if evidence is None:
        evidence = manifest.get("authorization_artifacts") or {}
    if not isinstance(evidence, dict):
        raise ValueError("release evidence artifact map is invalid")
    for name, filename in evidence.items():
        if (
            not isinstance(name, str)
            or not isinstance(filename, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]+", name)
            or Path(filename).name != filename
            or not filename.endswith(".json")
        ):
            raise ValueError("release authorization artifact name is invalid")
        expected[name] = filename
    return expected


def _manifest_delivery_fence(
    manifest: dict[str, object], base: str
) -> dict[str, object] | None:
    fence = manifest.get("phase9_delivery_fence")
    expected = {
        "project_id",
        "workflow_id",
        "run_generation",
        "replay_id",
        "replay_mode",
        "terminal_receipt_sha256",
        "run_mode",
        "modeling_consultation_contract",
        "delivery_capability",
    }
    if not isinstance(fence, dict) or set(fence) != expected:
        return None
    text_fields = expected - {"terminal_receipt_sha256"}
    if (
        fence.get("project_id") != base
        or any(
            not isinstance(fence.get(name), str) or not str(fence[name]).strip()
            for name in text_fields
        )
        or SHA256_RE.fullmatch(str(fence.get("terminal_receipt_sha256") or ""))
        is None
    ):
        return None
    return dict(fence)


@dataclass(frozen=True)
class ReleaseResult:
    release_id: str
    release_dir: Path
    paper: Path
    submission_zip: Path
    manifest: Path
    pointer: Path
    reused: bool = False


def resolve_current_release(
    papers_root: Path, base: str, *, project: Path
) -> ReleaseResult | None:
    """Resolve a release only while its exact Authority coordinate is current.

    The immutable manifest is necessary evidence, but it is never authority by
    itself.  A stale PASS on disk therefore becomes invisible as soon as the
    current generation, terminal, mode, or delivery capability changes.
    """

    papers_root = papers_root.resolve()
    project = project.resolve()
    if project.name != base:
        return None
    pointer_path = papers_root / base / "current.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer_schema = pointer.get("schema_version")
        if (
            pointer_schema
            not in {RELEASE_POINTER_SCHEMA, _PHASE9_RELEASE_POINTER_SCHEMA}
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
        manifest_schema = manifest.get("schema_version")
        expected_pointer_schema = (
            _PHASE9_RELEASE_POINTER_SCHEMA
            if manifest_schema == _PHASE9_RELEASE_MANIFEST_SCHEMA
            else RELEASE_POINTER_SCHEMA
        )
        if (
            manifest_schema
            not in {RELEASE_MANIFEST_SCHEMA, _PHASE9_RELEASE_MANIFEST_SCHEMA}
            or pointer_schema != expected_pointer_schema
            or manifest.get("base") != base
            or manifest.get("release_id") != pointer["release_id"]
            or declared != _canonical_hash(unsigned)
            or pointer.get("manifest_sha256") != _sha256(manifest_path)
        ):
            return None
        if manifest_schema == RELEASE_MANIFEST_SCHEMA:
            from ..phase9_delivery_fence import legacy_delivery_projection_allowed

            if not legacy_delivery_projection_allowed(project):
                return None
        else:
            recorded_fence = _manifest_delivery_fence(manifest, base)
            if recorded_fence is None:
                return None
            from ..phase9_delivery_fence import require_phase9_delivery_authority

            live_fence = require_phase9_delivery_authority(
                project,
                workflow_id=str(recorded_fence["workflow_id"]),
                run_generation=str(recorded_fence["run_generation"]),
                operation="release",
            )
            if live_fence.__dict__ != recorded_fence:
                return None
        expected = _expected_release_artifacts(manifest)
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
    papers_root: Path, base: str, *, project: Path
) -> tuple[Path, Path] | None:
    release = resolve_current_release(papers_root, base, project=project)
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
        workflow_id: str | None = None,
        run_generation: str | None = None,
    ) -> ReleaseResult:
        from ..phase9_delivery_fence import (
            delivery_side_effect_commit_lease,
            require_delivery_side_effect_authority,
        )

        project = project.resolve()

        def verify_fence() -> None:
            require_delivery_side_effect_authority(
                project,
                operation="release",
                workflow_id=workflow_id,
                run_generation=run_generation,
            )

        check = deadline_check or (lambda: None)
        verify_fence()
        check()
        base = project.name
        if not SHA256_RE.fullmatch(snapshot_id):
            raise ValueError("release id must be the final-audit SHA-256")
        project_pdf = project / f"{base}_paper.pdf"
        if not project_pdf.is_file() or project_pdf.stat().st_size == 0:
            raise ValueError("final project PDF is missing")
        sources = self._validate_sources(project, snapshot_id, status)

        verify_fence()
        check()

        def verify_existing(existing: ReleaseResult) -> None:
            existing_status = json.loads(
                existing.manifest.read_text(encoding="utf-8")
            ).get("status")
            if existing_status != status:
                raise ValueError(
                    "immutable release exists with a different audit status"
                )
            self._assert_sources_match_release(
                project, snapshot_id, status, existing
            )

        # A reusable release can be detected without creating the papers tree.
        # Re-resolve it under the Authority commit lease before repairing
        # aliases or the pointer so a concurrent Phase9 transition cannot leave
        # even a lock/control directory behind on rejection.
        existing = self._existing_release(base, snapshot_id)
        if existing is not None:
            verify_existing(existing)
            with delivery_side_effect_commit_lease(
                project,
                operation="release",
                workflow_id=workflow_id,
                run_generation=run_generation,
            ):
                check()
                current = self._existing_release(base, snapshot_id)
                if current is None:
                    raise RuntimeError("immutable release changed during recovery")
                verify_existing(current)
                current.pointer.parent.mkdir(parents=True, exist_ok=True)
                self._sync_legacy_aliases(base, current)
                check()
                self._assert_sources_match_release(
                    project, snapshot_id, status, current
                )
                self._write_pointer(base, current)
                return current

        # Package construction may invoke a child process which takes its own
        # submission lease.  Keep it outside the parent release lease to avoid
        # cross-process flock deadlock, and stage only in a private external
        # temporary directory.  Production callers use package_submission's
        # stage-only mode, so this phase writes neither papers/ nor project
        # finalization state.  Any late Phase9 refusal removes the whole temp.
        with tempfile.TemporaryDirectory(
            prefix=f"paper-factory-release-{snapshot_id}."
        ) as temporary:
            staging = Path(temporary)
            shutil.copyfile(project_pdf, staging / "paper.pdf")
            if not package_builder(staging / "submission.zip"):
                # A production builder reports a child submission-fence
                # refusal as a nonzero/False result.  Reclassify after the
                # child returns so a Phase9 transition is not mislabeled as a
                # generic packaging failure by this parent release boundary.
                verify_fence()
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
                "evidence_artifacts": {
                    name: f"{name}.json"
                    for name in sources
                    if name
                    not in {
                        "final_audit_receipt",
                        "audit_result",
                        "audit_snapshot",
                    }
                },
            }
            manifest["content_sha256"] = _canonical_hash(manifest)
            atomic_write_json(staging / "delivery_manifest.json", manifest)
            for child in staging.iterdir():
                if child.is_file():
                    _fsync_file(child)
            _fsync_dir(staging)

            with delivery_side_effect_commit_lease(
                project,
                operation="release",
                workflow_id=workflow_id,
                run_generation=run_generation,
            ):
                check()
                # A peer may have committed this release while this caller was
                # building its private staging tree.  Treat that as idempotent
                # recovery instead of overwriting an immutable directory.
                current = self._existing_release(base, snapshot_id)
                if current is not None:
                    verify_existing(current)
                    current.pointer.parent.mkdir(parents=True, exist_ok=True)
                    self._sync_legacy_aliases(base, current)
                    check()
                    self._assert_sources_match_release(
                        project, snapshot_id, status, current
                    )
                    self._write_pointer(base, current)
                    return current

                current_sources = self._validate_sources(
                    project, snapshot_id, status
                )
                if set(current_sources) != set(sources):
                    raise ValueError("release evidence set changed during construction")
                if _sha256(staging / "paper.pdf") != _sha256(project_pdf):
                    raise ValueError("audited PDF changed before release commit")
                for name, source in current_sources.items():
                    if _sha256(staging / f"{name}.json") != _sha256(source):
                        raise ValueError(
                            f"release evidence changed before commit: {name}"
                        )
                self._validate_zip(staging / "submission.zip", project_pdf, base)

                control_dir = self.papers_root / base
                releases_dir = self.papers_root / "releases" / base
                control_dir.mkdir(parents=True, exist_ok=True)
                releases_dir.mkdir(parents=True, exist_ok=True)
                release_dir = releases_dir / snapshot_id
                if release_dir.exists() or release_dir.is_symlink():
                    raise RuntimeError("immutable release path already exists")
                committed_staging = Path(
                    tempfile.mkdtemp(
                        prefix=f".{snapshot_id}.staging-", dir=releases_dir
                    )
                )
                try:
                    for child in staging.iterdir():
                        if not child.is_file() or child.is_symlink():
                            raise RuntimeError("private release staging is unsafe")
                        shutil.copyfile(child, committed_staging / child.name)
                        _fsync_file(committed_staging / child.name)
                    _fsync_dir(committed_staging)
                    os.replace(committed_staging, release_dir)
                    _fsync_dir(releases_dir)
                finally:
                    if committed_staging.exists():
                        shutil.rmtree(committed_staging)
                result = self._result(base, snapshot_id, reused=False)
                if result is None:
                    raise RuntimeError(
                        "committed release failed integrity verification"
                    )
                check()
                self._assert_sources_match_release(
                    project, snapshot_id, status, result
                )
                self._sync_legacy_aliases(base, result)
                check()
                self._assert_sources_match_release(
                    project, snapshot_id, status, result
                )
                self._write_pointer(base, result)
                return result

    def recover(
        self,
        base: str,
        *,
        project: Path,
        workflow_id: str | None = None,
        run_generation: str | None = None,
    ) -> ReleaseResult | None:
        from ..phase9_delivery_fence import (
            delivery_side_effect_commit_lease,
            require_delivery_side_effect_authority,
        )

        project = Path(project).resolve()
        if project.name != base:
            raise ValueError("release recovery project binding differs")
        require_delivery_side_effect_authority(
            project,
            operation="release",
            workflow_id=workflow_id,
            run_generation=run_generation,
        )
        current = resolve_current_release(
            self.papers_root, base, project=project
        )
        if current is not None:
            with delivery_side_effect_commit_lease(
                project,
                operation="release",
                workflow_id=workflow_id,
                run_generation=run_generation,
            ):
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
                value.get("schema_version")
                not in {RELEASE_MANIFEST_SCHEMA, _PHASE9_RELEASE_MANIFEST_SCHEMA}
                or value.get("base") != base
                or value.get("release_id") != release_id
                or declared != _canonical_hash(unsigned)
            ):
                return None
            if (
                value.get("schema_version") == _PHASE9_RELEASE_MANIFEST_SCHEMA
                and _manifest_delivery_fence(value, base) is None
            ):
                return None
            expected = _expected_release_artifacts(value)
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
        project: Path,
        snapshot_id: str,
        status: str,
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
        if audit_value.get("decision") == "ABLATE_NO_JUDGE":
            raise ValueError("no-judge ablation never authorizes a release")
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
        if status == "OVERRIDDEN" and audit_value.get("override") is not True:
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
        try:
            acceptance_value = json.loads(
                sources["final_audit_receipt"].read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"final acceptance receipt cannot be read: {exc}") from exc
        approval_records = acceptance_value.get("approval_receipts") or []
        if not isinstance(approval_records, list):
            raise ValueError("final acceptance approval receipt list is invalid")
        for record in approval_records:
            if not isinstance(record, dict):
                raise ValueError("final acceptance approval receipt record is invalid")
            gate = re.sub(r"[^A-Za-z0-9._-]", "_", str(record.get("gate") or ""))
            decision_id = re.sub(
                r"[^A-Za-z0-9._-]", "_", str(record.get("decision_id") or "")
            )
            relative = record.get("path")
            if not gate or not decision_id or not isinstance(relative, str):
                raise ValueError("final acceptance approval receipt identity is invalid")
            key = f"approval_{gate}_{decision_id}"
            source = project / relative
            if (
                Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not source.is_file()
                or source.is_symlink()
                or source.stat().st_size != record.get("size")
                or _sha256(source) != record.get("sha256")
            ):
                raise ValueError(f"approval receipt changed before release: {gate}")
            sources[key] = source
        bibliography_record = (acceptance_value.get("artifacts") or {}).get(
            "bibliography_build_receipt"
        )
        if bibliography_record is not None:
            if not isinstance(bibliography_record, dict) or not isinstance(
                bibliography_record.get("path"), str
            ):
                raise ValueError("bibliography build receipt record is invalid")
            bibliography_source = project / str(bibliography_record["path"])
            if (
                not bibliography_source.is_file()
                or bibliography_source.is_symlink()
                or bibliography_source.stat().st_size
                != bibliography_record.get("bytes")
                or _sha256(bibliography_source)
                != bibliography_record.get("sha256")
            ):
                raise ValueError("bibliography build receipt changed before release")
            sources["bibliography_build_evidence"] = bibliography_source
        if status == "OVERRIDDEN":
            override_record = (acceptance_value.get("artifacts") or {}).get(
                "override_receipt"
            )
            if not isinstance(override_record, dict) or not isinstance(
                override_record.get("path"), str
            ):
                raise ValueError("override release has no bound authorization receipt")
            override_source = project / str(override_record["path"])
            if (
                not override_source.is_file()
                or override_source.is_symlink()
                or override_source.stat().st_size != override_record.get("bytes")
                or _sha256(override_source) != override_record.get("sha256")
            ):
                raise ValueError("delivery override receipt changed before release")
            sources["delivery_override_authorization"] = override_source
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

    @classmethod
    def _assert_sources_match_release(
        cls,
        project: Path,
        snapshot_id: str,
        status: str,
        release: ReleaseResult,
    ) -> None:
        """Recheck approvals immediately before the atomic pointer switch."""

        current = cls._validate_sources(
            project,
            snapshot_id,
            status,
        )
        for name, source in current.items():
            copied = release.release_dir / f"{name}.json"
            if not copied.is_file() or _sha256(copied) != _sha256(source):
                raise ValueError(
                    f"release evidence changed before pointer switch: {name}"
                )

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
