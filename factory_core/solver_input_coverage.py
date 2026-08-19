from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from scripts.solver_job_receipt import (
    COMPLETION_SCHEMA,
    ReceiptError,
    SUBMISSION_SCHEMA,
    file_sha256,
    read_receipt,
)

SOLVER_INPUT_EXCLUSION_SCHEMA = "factory-solver-input-exclusion-v1"
TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SCHEMA = (
    "factory-technical-solver-drift-authorization-v1"
)
TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_ENV = (
    "FACTORY_TECHNICAL_FLOW_AUTHORIZATION"
)
TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SHA_ENV = (
    "FACTORY_TECHNICAL_FLOW_AUTHORIZATION_SHA256"
)


@dataclass(frozen=True)
class SolverInputCoverage:
    included_paths: tuple[Path, ...]
    evidence_paths: tuple[Path, ...]
    excluded: tuple[dict[str, Any], ...]
    authorized_drifts: tuple[dict[str, Any], ...] = ()


class SolverInputDriftError(ValueError):
    """Describe one current solver input that no live receipt attests."""

    def __init__(
        self,
        *,
        current_path: Path | None,
        relative_path: str,
        kind: str,
        current_size: int | None,
        current_sha256: str | None,
        receipts: tuple[dict[str, Any], ...],
    ) -> None:
        self.current_path = current_path
        self.relative_path = relative_path
        self.kind = kind
        self.current_size = int(current_size) if current_size is not None else None
        self.current_sha256 = current_sha256
        self.receipts = receipts
        super().__init__(f"solver input {kind} drift: {relative_path}")

    def to_dict(self) -> dict[str, Any]:
        current: dict[str, Any]
        if self.current_path is None:
            current = {"exists": False}
        else:
            current = {
                "size": self.current_size,
                "sha256": self.current_sha256,
            }
        identity = {
            "schema_version": "factory-solver-input-drift-v1",
            "path": self.relative_path,
            "kind": self.kind,
            "current": current,
            "receipts": [dict(item) for item in self.receipts],
        }
        return {**identity, "fingerprint": _canonical_hash(identity)}


class _MissingProjectFileError(ValueError):
    """Distinguish an absent file from unsafe or non-regular paths."""


@dataclass(frozen=True)
class TechnicalSolverDriftAuthorization:
    path: Path
    sha256: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _CurrentCompletedOutput:
    finished_at: int
    submission_path: Path
    completion_path: Path


@dataclass(frozen=True)
class _CompletedJob:
    finished_at: int
    submission_path: Path
    completion_path: Path
    outputs: tuple[dict[str, Any], ...]
    script_path: str
    script_current: bool
    declared_outputs: frozenset[str]


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def technical_solver_drift_authorization(
    project_dir: str | Path,
) -> TechnicalSolverDriftAuthorization | None:
    """Load one exact, expiring authorization for downstream-only validation.

    The default remains fail-closed.  Both an in-project immutable receipt and
    its expected SHA-256 must be supplied through the worker environment.  The
    receipt never authorizes content freeze, quality acceptance, or delivery.
    """

    project = Path(project_dir).resolve()
    configured = os.getenv(TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_ENV, "").strip()
    expected_sha256 = os.getenv(
        TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SHA_ENV, ""
    ).strip()
    if not configured or len(expected_sha256) != 64:
        return None
    candidate = Path(configured)
    if not candidate.is_absolute() or candidate.is_symlink():
        return None
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(project / ".factory" / "technical_flow")
    except (OSError, ValueError):
        return None
    if not path.is_file() or file_sha256(path) != expected_sha256:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        expires_at = int(payload.get("expires_at") or 0)
    except (TypeError, ValueError):
        return None
    if (
        payload.get("schema_version")
        != TECHNICAL_SOLVER_DRIFT_AUTHORIZATION_SCHEMA
        or payload.get("project_id") != project.name
        or payload.get("project_path") != str(project)
        or payload.get("scope") != "run4_downstream_bug_validation"
        or payload.get("quality_pass_fabricated") is not False
        or payload.get("content_freeze_approved") is not False
        or payload.get("delivery_allowed") is not False
        or expires_at <= int(time.time())
        or not isinstance(payload.get("drifts"), list)
        or not payload["drifts"]
    ):
        return None
    return TechnicalSolverDriftAuthorization(path, expected_sha256, payload)


def _authorization_allows_drift(
    authorization: TechnicalSolverDriftAuthorization | None,
    drift: SolverInputDriftError,
) -> bool:
    if authorization is None:
        return False
    fingerprint = drift.to_dict()["fingerprint"]
    return any(
        isinstance(item, Mapping)
        and item.get("path") == drift.relative_path
        and item.get("fingerprint") == fingerprint
        for item in authorization.payload.get("drifts", ())
    )


def _regular_project_file(project: Path, relative: str, *, label: str) -> Path:
    pure = PurePosixPath(relative.replace("\\", "/"))
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ValueError(f"{label} escapes project: {relative}")
    lexical = Path(os.path.abspath(project / Path(*pure.parts)))
    try:
        lexical.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"{label} escapes project: {relative}") from exc
    cursor = project
    for component in pure.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError(f"{label} is or traverses a symlink: {pure.as_posix()}")
    if not lexical.exists():
        raise _MissingProjectFileError(
            f"{label} is missing or not a regular file: {pure.as_posix()}"
        )
    if not lexical.is_file():
        raise ValueError(f"{label} is missing or not a regular file: {pure.as_posix()}")
    resolved = lexical.resolve(strict=True)
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside project: {pure.as_posix()}") from exc
    return resolved


def solver_input_exclusion_path(
    project_dir: str | Path, *, relative_path: str, input_sha256: str
) -> Path:
    project = Path(project_dir).resolve()
    key = _canonical_hash(
        {
            "path": relative_path,
            "input_sha256": input_sha256,
            "scope": "final_input_and_submission",
        }
    )
    path = (
        project
        / ".factory"
        / "finalization"
        / "input_exclusions"
        / f"{key}.json"
    )
    cursor = project
    for component in path.relative_to(project).parts[:-1]:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError(
                "solver input exclusion path traverses a symlink"
            )
    if path.is_symlink():
        raise ValueError("solver input exclusion receipt must not be a symlink")
    return path


def _validate_exclusion_value(
    value: Mapping[str, Any], input_record: Mapping[str, Any]
) -> dict[str, Any]:
    normalized = dict(value)
    identity = {
        key: item
        for key, item in normalized.items()
        if key != "content_sha256"
    }
    relative = str(input_record.get("path") or "")
    if normalized.get("schema_version") != SOLVER_INPUT_EXCLUSION_SCHEMA:
        raise ValueError("solver input exclusion receipt schema is invalid")
    if normalized.get("content_sha256") != _canonical_hash(identity):
        raise ValueError("solver input exclusion receipt content hash mismatch")
    if str(normalized.get("path") or "") != relative:
        raise ValueError("solver input exclusion receipt path mismatch")
    if str(normalized.get("input_sha256") or "") != str(
        input_record.get("sha256") or ""
    ):
        raise ValueError("solver input exclusion receipt hash mismatch")
    if str(normalized.get("scope") or "") != "final_input_and_submission":
        raise ValueError("solver input exclusion receipt scope is invalid")
    if not str(normalized.get("reason") or "").strip():
        raise ValueError("solver input exclusion receipt requires a reason")
    return normalized


def _read_exclusion(
    project: Path, input_record: dict[str, Any]
) -> tuple[dict[str, Any], Path] | None:
    relative = str(input_record.get("path") or "")
    path = solver_input_exclusion_path(
        project,
        relative_path=relative,
        input_sha256=str(input_record.get("sha256") or ""),
    )
    if not path.exists():
        return None
    exclusion_path = _regular_project_file(
        project,
        path.relative_to(project).as_posix(),
        label="solver input exclusion receipt",
    )
    try:
        value = json.loads(exclusion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid solver input exclusion receipt: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("solver input exclusion receipt root must be an object")
    return _validate_exclusion_value(value, input_record), exclusion_path


def _completion_index(
    project: Path, root: Path
) -> tuple[
    dict[str, _CompletedJob],
    dict[str, _CurrentCompletedOutput],
    dict[str, _CompletedJob],
    frozenset[str],
]:
    """Return current files attested by a later successful solver completion.

    A solver input path may be reused as the output of a later rerun.  In that
    case, old submission receipts describe a historical input version rather
    than drift of the current active file.  Only a valid, submission-bound
    completion receipt whose recorded output still matches the file may
    supersede that historical input identity.
    """

    jobs: dict[str, _CompletedJob] = {}
    current: dict[str, _CurrentCompletedOutput] = {}
    latest_current_job_by_script: dict[str, _CompletedJob] = {}
    unsuccessful: set[str] = set()
    for completion_path in sorted(root.glob("*.completed.json")):
        relative_completion = completion_path.relative_to(project).as_posix()
        safe_completion = _regular_project_file(
            project, relative_completion, label="solver completion receipt"
        )
        try:
            completion = read_receipt(safe_completion, COMPLETION_SCHEMA)
        except (OSError, ReceiptError) as exc:
            raise ValueError(
                f"invalid solver completion receipt {relative_completion}: {exc}"
            ) from exc
        job_id = str(completion.get("job_id") or "")
        submission_path = root / f"{job_id}.submitted.json"
        relative_submission = submission_path.relative_to(project).as_posix()
        safe_submission = _regular_project_file(
            project, relative_submission, label="solver submission receipt"
        )
        try:
            submission = read_receipt(safe_submission, SUBMISSION_SCHEMA)
        except (OSError, ReceiptError) as exc:
            raise ValueError(
                f"invalid solver submission receipt {relative_submission}: {exc}"
            ) from exc
        if (
            completion.get("submission_receipt_sha256")
            != file_sha256(safe_submission)
            or completion.get("submission_content_sha256")
            != submission.get("content_sha256")
        ):
            raise ValueError(
                f"solver completion receipt does not bind submission: {relative_completion}"
            )
        if (
            completion.get("status") != "COMPLETED"
            or completion.get("successful_outputs") is not True
        ):
            unsuccessful.add(job_id)
            continue
        finished_at = int(completion.get("finished_at") or -1)
        script_record = submission.get("script")
        if not isinstance(script_record, dict):
            raise ValueError(
                f"solver submission script is invalid: {relative_submission}"
            )
        script_relative = str(script_record.get("path") or "")
        try:
            current_script = _regular_project_file(
                project,
                script_relative,
                label=f"solver script from {relative_submission}",
            )
            script_current = (
                current_script.stat().st_size == int(script_record.get("size", -1))
                and file_sha256(current_script)
                == str(script_record.get("sha256") or "")
            )
        except ValueError:
            script_current = False
        outputs = tuple(
            record
            for record in (completion.get("outputs") or [])
            if isinstance(record, dict)
        )
        job = _CompletedJob(
            finished_at=finished_at,
            submission_path=safe_submission,
            completion_path=safe_completion,
            outputs=outputs,
            script_path=script_relative,
            script_current=script_current,
            declared_outputs=frozenset(
                str(item) for item in (submission.get("declared_outputs") or [])
            ),
        )
        jobs[job_id] = job
        prior_script_job = latest_current_job_by_script.get(script_relative)
        if script_current and (
            prior_script_job is None or finished_at > prior_script_job.finished_at
        ):
            latest_current_job_by_script[script_relative] = job
        for index, record in enumerate(outputs):
            if not isinstance(record, dict) or record.get("exists") is not True:
                continue
            relative = str(record.get("path") or "")
            try:
                path = _regular_project_file(
                    project,
                    relative,
                    label=f"solver output {index} from {relative_completion}",
                )
            except ValueError:
                continue
            if (
                path.stat().st_size != int(record.get("size", -1))
                or file_sha256(path) != str(record.get("sha256") or "")
            ):
                continue
            prior = current.get(relative)
            if prior is None or finished_at > prior.finished_at:
                current[relative] = _CurrentCompletedOutput(
                    finished_at=finished_at,
                    submission_path=safe_submission,
                    completion_path=safe_completion,
                )
    return jobs, current, latest_current_job_by_script, frozenset(unsuccessful)


def _submission_is_fully_superseded(
    project: Path,
    completed: _CompletedJob | None,
    current_outputs: dict[str, _CurrentCompletedOutput],
    latest_current_job_by_script: dict[str, _CompletedJob],
) -> bool:
    """Return true only when every output was re-attested by a later valid job.

    A deterministic rerun may reproduce byte-identical output.  Supersession
    is therefore established by a later successful, submission-bound
    completion receipt that attests the current file, not by requiring the
    file bytes to differ from the historical output.
    """

    if completed is None or not completed.outputs:
        return False
    same_script_replacement = latest_current_job_by_script.get(completed.script_path)
    for record in completed.outputs:
        if record.get("exists") is not True:
            return False
        relative = str(record.get("path") or "")
        replacement = current_outputs.get(relative)
        if replacement is not None and replacement.finished_at > completed.finished_at:
            continue
        try:
            _regular_project_file(
                project,
                relative,
                label="historical solver output",
            )
        except ValueError:
            if (
                same_script_replacement is not None
                and same_script_replacement.finished_at > completed.finished_at
                and relative not in same_script_replacement.declared_outputs
            ):
                continue
        return False
    return True


def solver_declared_input_coverage(project_dir: str | Path) -> SolverInputCoverage:
    project = Path(project_dir).resolve()
    root = project / ".factory" / "solver_receipts"
    if not root.exists():
        return SolverInputCoverage((), (), ())
    if root.is_symlink() or not root.is_dir():
        raise ValueError("solver receipt directory is unsafe")
    included: dict[str, Path] = {}
    evidence: dict[str, Path] = {}
    excluded: dict[str, dict[str, Any]] = {}
    stale_drifts: dict[str, dict[str, Any]] = {}
    missing_inputs: dict[str, dict[str, Any]] = {}
    current_identities: dict[str, dict[str, Any]] = {}
    authorization = technical_solver_drift_authorization(project)
    authorized_drifts: list[dict[str, Any]] = []
    (
        completed_jobs,
        current_outputs,
        latest_current_job_by_script,
        unsuccessful_jobs,
    ) = _completion_index(project, root)
    for receipt_path in sorted(root.glob("*.submitted.json")):
        relative_receipt = receipt_path.relative_to(project).as_posix()
        safe_receipt = _regular_project_file(
            project, relative_receipt, label="solver submission receipt"
        )
        try:
            receipt = read_receipt(safe_receipt, SUBMISSION_SCHEMA)
        except (OSError, ReceiptError) as exc:
            raise ValueError(f"invalid solver submission receipt {relative_receipt}: {exc}") from exc
        if str(receipt.get("job_id") or "") in unsuccessful_jobs:
            continue
        if _submission_is_fully_superseded(
            project,
            completed_jobs.get(str(receipt.get("job_id") or "")),
            current_outputs,
            latest_current_job_by_script,
        ):
            continue
        inputs = receipt.get("inputs")
        if not isinstance(inputs, list):
            raise ValueError(f"solver submission receipt inputs are invalid: {relative_receipt}")
        matched_records: list[tuple[dict[str, Any], Path]] = []
        for index, record in enumerate(inputs):
            if not isinstance(record, dict):
                raise ValueError(
                    f"solver input record {index} is invalid in {relative_receipt}"
                )
            relative = str(record.get("path") or "")
            expected_size = int(record.get("size", -1))
            expected_sha256 = str(record.get("sha256") or "")
            try:
                path = _regular_project_file(
                    project,
                    relative,
                    label=f"solver input {index} from {relative_receipt}",
                )
            except _MissingProjectFileError:
                missing = missing_inputs.setdefault(relative, {"receipts": {}})
                receipt_identity = {
                    "path": relative_receipt,
                    "expected_size": expected_size,
                    "expected_sha256": expected_sha256,
                }
                missing["receipts"][_canonical_hash(receipt_identity)] = (
                    receipt_identity
                )
                continue
            current = current_identities.setdefault(
                relative,
                {"size": path.stat().st_size, "sha256": file_sha256(path)},
            )
            if current["size"] != expected_size or current["sha256"] != expected_sha256:
                drift = stale_drifts.setdefault(
                    relative, {"path": path, "kinds": set(), "receipts": {}}
                )
                kind = "size" if current["size"] != expected_size else "content"
                drift["kinds"].add(kind)
                receipt_identity = {
                    "path": relative_receipt,
                    "expected_size": expected_size,
                    "expected_sha256": expected_sha256,
                }
                drift["receipts"][_canonical_hash(receipt_identity)] = receipt_identity
            else:
                matched_records.append((record, path))

        if matched_records:
            evidence[relative_receipt] = safe_receipt
        for record, path in matched_records:
            relative = str(record.get("path") or "")
            prior = included.get(relative)
            if prior is not None and prior != path:
                raise ValueError(f"solver input identity conflict: {relative}")
            exclusion = _read_exclusion(project, record)
            if exclusion is not None:
                value, exclusion_path = exclusion
                excluded[relative] = value
                evidence[exclusion_path.relative_to(project).as_posix()] = exclusion_path
                included.pop(relative, None)
                continue
            if relative in excluded:
                raise ValueError(f"solver input has conflicting inclusion and exclusion: {relative}")
            included[relative] = path
    for relative in sorted(missing_inputs):
        if relative in included or relative in excluded:
            continue
        details = missing_inputs[relative]
        drift = SolverInputDriftError(
            current_path=None,
            relative_path=relative,
            kind="missing",
            current_size=None,
            current_sha256=None,
            receipts=tuple(
                details["receipts"][key] for key in sorted(details["receipts"])
            ),
        )
        if not _authorization_allows_drift(authorization, drift):
            raise drift
        for receipt in drift.receipts:
            receipt_path = _regular_project_file(
                project,
                str(receipt["path"]),
                label="authorized missing-input solver receipt",
            )
            evidence[str(receipt["path"])] = receipt_path
        assert authorization is not None
        evidence[authorization.path.relative_to(project).as_posix()] = authorization.path
        authorized_drifts.append(drift.to_dict())
    for relative in sorted(stale_drifts):
        if relative in included or relative in excluded:
            continue
        details = stale_drifts[relative]
        current = current_identities[relative]
        kind = "content" if "content" in details["kinds"] else "size"
        drift = SolverInputDriftError(
            current_path=details["path"],
            relative_path=relative,
            kind=kind,
            current_size=current["size"],
            current_sha256=current["sha256"],
            receipts=tuple(
                details["receipts"][key] for key in sorted(details["receipts"])
            ),
        )
        if not _authorization_allows_drift(authorization, drift):
            raise drift
        assert drift.current_path is not None
        included[relative] = drift.current_path
        for receipt in drift.receipts:
            receipt_path = _regular_project_file(
                project,
                str(receipt["path"]),
                label="authorized stale solver receipt",
            )
            evidence[str(receipt["path"])] = receipt_path
        assert authorization is not None
        evidence[authorization.path.relative_to(project).as_posix()] = authorization.path
        authorized_drifts.append(drift.to_dict())
    return SolverInputCoverage(
        tuple(included[key] for key in sorted(included)),
        tuple(evidence[key] for key in sorted(evidence)),
        tuple(excluded[key] for key in sorted(excluded)),
        tuple(authorized_drifts),
    )


def build_solver_input_exclusion_receipt(
    *,
    relative_path: str,
    input_sha256: str,
    reason: str,
    scope: str = "final_input_and_submission",
) -> dict[str, Any]:
    identity = {
        "schema_version": SOLVER_INPUT_EXCLUSION_SCHEMA,
        "path": relative_path,
        "input_sha256": input_sha256,
        "reason": reason.strip(),
        "scope": scope,
    }
    if not identity["reason"]:
        raise ValueError("solver input exclusion receipt requires a reason")
    if scope != "final_input_and_submission":
        raise ValueError("solver input exclusion receipt scope is invalid")
    return {**identity, "content_sha256": _canonical_hash(identity)}

def write_solver_input_exclusion_receipt(
    project_dir: str | Path, receipt: Mapping[str, Any]
) -> Path:
    """Persist one versioned exclusion using atomic no-overwrite semantics."""

    project = Path(project_dir).resolve()
    relative = str(receipt.get("path") or "")
    input_sha256 = str(receipt.get("input_sha256") or "")
    value = _validate_exclusion_value(
        receipt, {"path": relative, "sha256": input_sha256}
    )
    path = solver_input_exclusion_path(
        project, relative_path=relative, input_sha256=input_sha256
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    for cursor in (path.parent,):
        if cursor.is_symlink() or not cursor.is_dir():
            raise ValueError("solver input exclusion directory is unsafe")
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError("solver input exclusion receipt path is unsafe")
        if path.read_bytes() != encoded:
            raise ValueError(
                "immutable solver input exclusion receipt already differs"
            )
        return path
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path
