from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.solver_job_receipt import (
    COMPLETION_SCHEMA,
    ReceiptError,
    SUBMISSION_SCHEMA,
    file_sha256,
    read_receipt,
    initial_input_records,
)

SOLVER_INPUT_DRIFT_SCHEMA = "factory-solver-input-drift-v1"


@dataclass(frozen=True)
class SolverInputCoverage:
    """Solver-declared inputs of the final submission.

    ``drifts`` lists every declared input whose current bytes differ from what
    a live submission receipt recorded, or that no longer exists.  Drift is
    recorded, not blocking: the current file is included, its receipts become
    evidence, and the final Judge sees each drift in the execution packet.
    """

    included_paths: tuple[Path, ...]
    evidence_paths: tuple[Path, ...]
    drifts: tuple[dict[str, Any], ...] = ()


def solver_input_drift_record(
    *,
    relative_path: str,
    kind: str,
    current_size: int | None,
    current_sha256: str | None,
    receipts: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """One drifted solver input: what the receipts expected and what is there now."""

    current: dict[str, Any] = (
        {"exists": False}
        if current_sha256 is None
        else {"size": int(current_size), "sha256": current_sha256}
    )
    identity = {
        "schema_version": SOLVER_INPUT_DRIFT_SCHEMA,
        "path": relative_path,
        "kind": kind,
        "current": current,
        "receipts": [dict(item) for item in receipts],
    }
    return {**identity, "fingerprint": _canonical_hash(identity)}


class _MissingProjectFileError(ValueError):
    """Distinguish an absent file from unsafe or non-regular paths."""


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
        return SolverInputCoverage((), ())
    if root.is_symlink() or not root.is_dir():
        raise ValueError("solver receipt directory is unsafe")
    included: dict[str, Path] = {}
    evidence: dict[str, Path] = {}
    stale_drifts: dict[str, dict[str, Any]] = {}
    missing_inputs: dict[str, dict[str, Any]] = {}
    current_identities: dict[str, dict[str, Any]] = {}
    current_submission_times: dict[str, int] = {}
    stale_submission_times: dict[str, list[tuple[int, bool]]] = {}
    drifts: list[dict[str, Any]] = []
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
        inputs = initial_input_records(receipt)
        if not isinstance(inputs, list):
            raise ValueError(f"solver submission receipt inputs are invalid: {relative_receipt}")
        completed = completed_jobs.get(str(receipt.get("job_id") or ""))
        if receipt.get("dependency_enforcement") == "python-audit-open-v2" and completed is not None:
            completion = read_receipt(completed.completion_path, COMPLETION_SCHEMA)
            closures = [r for r in completion.get("result_artifacts", [])
                        if r.get("name") == "input_closure" and r.get("local")]
            if len(closures) != 1:
                raise ValueError("completed Python job has no unique input closure")
            record = closures[0]
            closure_path = _regular_project_file(project, record["path"], label="solver input closure")
            if closure_path.stat().st_size != record["size"] or file_sha256(closure_path) != record["sha256"]:
                raise ValueError("completed Python input closure changed")
            evidence[record["path"]] = closure_path
            evidence[relative_receipt] = safe_receipt
            evidence[completed.completion_path.relative_to(project).as_posix()] = completed.completion_path
        matched_records: list[tuple[dict[str, Any], Path]] = []
        for index, record in enumerate(inputs):
            if not isinstance(record, dict):
                raise ValueError(
                    f"solver input record {index} is invalid in {relative_receipt}"
                )
            relative = str(record.get("path") or "")
            expected_size = int(record.get("size", -1))
            expected_sha256 = str(record.get("sha256") or "")
            receipt_identity = {
                "path": relative_receipt,
                "expected_size": expected_size,
                "expected_sha256": expected_sha256,
            }
            try:
                path = _regular_project_file(
                    project,
                    relative,
                    label=f"solver input {index} from {relative_receipt}",
                )
            except _MissingProjectFileError:
                missing = missing_inputs.setdefault(relative, {"receipts": {}})
                missing["receipts"][_canonical_hash(receipt_identity)] = receipt_identity
                continue
            current = current_identities.setdefault(
                relative,
                {"size": path.stat().st_size, "sha256": file_sha256(path)},
            )
            if current["size"] != expected_size or current["sha256"] != expected_sha256:
                drift = stale_drifts.setdefault(
                    relative, {"path": path, "kinds": set(), "receipts": {}}
                )
                stale_submission_times.setdefault(relative, []).append(
                    (int(receipt["requested_at"]), completed is not None)
                )
                drift["kinds"].add("size" if current["size"] != expected_size else "content")
                drift["receipts"][_canonical_hash(receipt_identity)] = receipt_identity
            else:
                matched_records.append((record, path))
                current_submission_times[relative] = max(
                    current_submission_times.get(relative, -1), int(receipt["requested_at"])
                )

        if matched_records:
            evidence[relative_receipt] = safe_receipt
        for record, path in matched_records:
            relative = str(record.get("path") or "")
            prior = included.get(relative)
            if prior is not None and prior != path:
                raise ValueError(f"solver input identity conflict: {relative}")
            included[relative] = path

    def _record(relative: str, details: dict[str, Any], **current: Any) -> None:
        receipts = tuple(details["receipts"][key] for key in sorted(details["receipts"]))
        for receipt in receipts:
            evidence[str(receipt["path"])] = _regular_project_file(
                project, str(receipt["path"]), label="drifted solver input receipt"
            )
        drifts.append(
            solver_input_drift_record(relative_path=relative, receipts=receipts, **current)
        )

    for relative in sorted(missing_inputs):
        if relative in included:
            continue
        _record(relative, missing_inputs[relative], kind="missing",
                current_size=None, current_sha256=None)
    for relative in sorted(stale_drifts):
        # A match for one receipt does not attest a different historical
        # identity at the same path. Superseded receipts were filtered above.
        # Preserve the legacy submission-only rerun contract: a newer direct
        # attestation of current bytes replaces older pending submissions, and
        # a completed job requires the output-bound supersession proof above.
        latest_current = current_submission_times.get(relative)
        if latest_current is not None and all(
            not was_completed and requested_at < latest_current
            for requested_at, was_completed in stale_submission_times[relative]
        ):
            continue
        details = stale_drifts[relative]
        current = current_identities[relative]
        included[relative] = details["path"]
        _record(
            relative,
            details,
            kind="content" if "content" in details["kinds"] else "size",
            current_size=current["size"],
            current_sha256=current["sha256"],
        )
    return SolverInputCoverage(
        tuple(included[key] for key in sorted(included)),
        tuple(evidence[key] for key in sorted(evidence)),
        tuple(drifts),
    )
