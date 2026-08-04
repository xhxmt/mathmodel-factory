#!/usr/bin/env python3
"""Create and verify two-stage content-addressed solver job receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable


SUBMISSION_SCHEMA = "solver-job-submission-receipt-v1"
COMPLETION_SCHEMA = "solver-job-completion-receipt-v1"
EVIDENCE_SCHEMA = "solver-job-evidence-v2"
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "EXITED"}


class ReceiptError(ValueError):
    """Raised when solver evidence is unsafe, incomplete, or inconsistent."""


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _with_content_hash(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["content_sha256"] = canonical_hash(value)
    return result


def _validate_content_hash(value: dict[str, Any], schema: str) -> None:
    if value.get("schema") != schema:
        raise ReceiptError(f"receipt schema must be {schema}")
    expected = value.get("content_sha256")
    actual = canonical_hash({key: item for key, item in value.items() if key != "content_sha256"})
    if expected != actual:
        raise ReceiptError(f"{schema} content hash mismatch")


def _project_path(
    project_dir: Path, value: str | Path, *, must_exist: bool
) -> tuple[Path, str]:
    project = project_dir.resolve()
    raw = Path(value)
    candidate = raw if raw.is_absolute() else project / raw
    try:
        resolved = candidate.resolve(strict=must_exist)
        relative = resolved.relative_to(project).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReceiptError(f"path must stay inside project: {value}") from exc
    return resolved, relative


def _file_record(project_dir: Path, value: str | Path) -> dict[str, Any]:
    path, relative = _project_path(project_dir, value, must_exist=True)
    if not path.is_file():
        raise ReceiptError(f"receipt input must be a regular file: {value}")
    return {"path": relative, "sha256": file_sha256(path), "size": path.stat().st_size}


def _output_record(project_dir: Path, value: str | Path) -> dict[str, Any]:
    path, relative = _project_path(project_dir, value, must_exist=False)
    if not path.is_file() or path.is_symlink():
        return {"path": relative, "exists": False, "sha256": None, "size": None}
    return {
        "path": relative,
        "exists": True,
        "sha256": file_sha256(path),
        "size": path.stat().st_size,
    }


def _runtime_environment(runtime: str) -> dict[str, Any]:
    executable_name = {
        "python": sys.executable,
        "julia": "julia",
        "R": "Rscript",
        "r": "Rscript",
        "rscript": "Rscript",
        "Rscript": "Rscript",
        "matlab": "matlab",
        "gurobi": "gurobi_cl",
    }.get(runtime, runtime)
    located = shutil.which(executable_name) if not Path(executable_name).is_absolute() else executable_name
    resolved = Path(located).resolve() if located else None
    return {
        "runtime_executable": str(resolved) if resolved else None,
        "runtime_executable_sha256": (
            file_sha256(resolved) if resolved and resolved.is_file() else None
        ),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }


def build_submission_receipt(
    *,
    project_dir: str | Path,
    job_id: str,
    backend: str,
    runtime: str,
    script: str | Path,
    workdir: str | Path,
    argv: Iterable[str],
    max_time_seconds: int,
    requested_at: int,
    input_paths: Iterable[str | Path] = (),
    output_paths: Iterable[str | Path] = (),
    seeds: Iterable[str | int] = (),
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    if not job_id or not backend or not runtime:
        raise ReceiptError("job_id, backend, and runtime must be nonempty")
    script_record = _file_record(project, script)
    workdir_path, workdir_relative = _project_path(project, workdir, must_exist=True)
    if not workdir_path.is_dir():
        raise ReceiptError("solver workdir must be a directory")
    input_records = [_file_record(project, path) for path in input_paths]
    if len({item["path"] for item in input_records}) != len(input_records):
        raise ReceiptError("declared solver inputs must be unique")
    declared_outputs = [
        _project_path(project, path, must_exist=False)[1] for path in output_paths
    ]
    if len(set(declared_outputs)) != len(declared_outputs):
        raise ReceiptError("declared solver outputs must be unique")
    argv_values = [str(item) for item in argv]
    base = {
        "schema": SUBMISSION_SCHEMA,
        "stage": "submitted",
        "job_id": job_id,
        "backend": backend,
        "runtime": runtime,
        "project": project.name,
        "script": script_record,
        "workdir": workdir_relative,
        "argv_sha256": canonical_hash(argv_values),
        "max_time_seconds": int(max_time_seconds),
        "requested_at": int(requested_at),
        "inputs": input_records,
        "declared_outputs": declared_outputs,
        "seeds": [str(seed) for seed in seeds],
        "environment": _runtime_environment(runtime),
    }
    base["request_sha256"] = canonical_hash(base)
    return _with_content_hash(base)


def read_receipt(path: str | Path, schema: str) -> dict[str, Any]:
    receipt_path = Path(path)
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"invalid receipt {receipt_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReceiptError(f"receipt root must be an object: {receipt_path}")
    _validate_content_hash(value, schema)
    return value


def write_receipt(path: str | Path, value: dict[str, Any]) -> Path:
    target = Path(path)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if target.exists():
        if target.read_text(encoding="utf-8") != payload:
            raise ReceiptError(f"immutable receipt already exists with different content: {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)
    return target


def _records_unchanged(project: Path, records: list[dict[str, Any]]) -> bool:
    for record in records:
        try:
            current = _file_record(project, record["path"])
        except ReceiptError:
            return False
        if current != {key: record.get(key) for key in ("path", "sha256", "size")}:
            return False
    return True


def _result_artifacts(project: Path, refs: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for name, raw in sorted(refs.items()):
        if not isinstance(raw, str) or not raw:
            continue
        try:
            record = _output_record(project, raw)
        except ReceiptError:
            artifacts.append({"name": name, "reference_sha256": canonical_hash(raw), "local": False})
            continue
        artifacts.append({"name": name, "local": True, **record})
    return artifacts


def build_completion_receipt(
    *,
    project_dir: str | Path,
    submission_path: str | Path,
    status: str,
    finished_at: int,
    result_refs: dict[str, Any],
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    submitted_path = Path(submission_path).resolve()
    submitted = read_receipt(submitted_path, SUBMISSION_SCHEMA)
    normalized_status = status.upper()
    if normalized_status not in TERMINAL_STATUSES:
        raise ReceiptError("completion receipt requires a terminal status")
    script_unchanged = _records_unchanged(project, [submitted["script"]])
    inputs_unchanged = _records_unchanged(project, submitted["inputs"])
    outputs = [_output_record(project, path) for path in submitted["declared_outputs"]]
    outputs_complete = bool(outputs) and all(item["exists"] for item in outputs)
    successful_outputs = (
        normalized_status == "COMPLETED"
        and script_unchanged
        and inputs_unchanged
        and outputs_complete
    )
    base = {
        "schema": COMPLETION_SCHEMA,
        "stage": "completed",
        "job_id": submitted["job_id"],
        "submission_receipt_sha256": file_sha256(submitted_path),
        "submission_content_sha256": submitted["content_sha256"],
        "request_sha256": submitted["request_sha256"],
        "status": normalized_status,
        "finished_at": int(finished_at),
        "script_unchanged": script_unchanged,
        "inputs_unchanged": inputs_unchanged,
        "outputs_complete": outputs_complete,
        "successful_outputs": successful_outputs,
        "outputs": outputs,
        "result_artifacts": _result_artifacts(project, result_refs),
    }
    return _with_content_hash(base)


def build_evidence(
    project_dir: str | Path,
    submission_path: str | Path,
    completion_path: str | Path | None = None,
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    submitted_path = Path(submission_path).resolve()
    submitted = read_receipt(submitted_path, SUBMISSION_SCHEMA)
    completion = None
    completion_file_hash = None
    current_outputs_match = False
    if completion_path is not None and Path(completion_path).is_file():
        completed_path = Path(completion_path).resolve()
        completion = read_receipt(completed_path, COMPLETION_SCHEMA)
        completion_file_hash = file_sha256(completed_path)
        if completion.get("submission_receipt_sha256") != file_sha256(submitted_path):
            raise ReceiptError("completion receipt does not bind the submission receipt")
        if completion.get("job_id") != submitted.get("job_id"):
            raise ReceiptError("completion and submission job ids differ")
        current_outputs = [
            _output_record(project, item["path"]) for item in completion.get("outputs", [])
        ]
        current_outputs_match = current_outputs == completion.get("outputs")
    receipt_ready = bool(
        completion
        and completion.get("successful_outputs") is True
        and current_outputs_match
        and _records_unchanged(project, [submitted["script"], *submitted["inputs"]])
    )
    errors: list[str] = []
    if completion is None:
        errors.append("COMPLETION_RECEIPT_MISSING")
    else:
        if completion.get("successful_outputs") is not True:
            errors.append("COMPLETION_DID_NOT_PRODUCE_TRUSTED_OUTPUTS")
        if not current_outputs_match:
            errors.append("CURRENT_OUTPUTS_DIFFER_FROM_COMPLETION_RECEIPT")
        if not _records_unchanged(project, [submitted["script"], *submitted["inputs"]]):
            errors.append("SUBMITTED_CODE_OR_INPUTS_CHANGED")
    return {
        "schema": EVIDENCE_SCHEMA,
        "job_id": submitted["job_id"],
        "backend": submitted["backend"],
        "runtime": submitted["runtime"],
        "script": str((project / submitted["script"]["path"]).resolve()),
        "workdir": str((project / submitted["workdir"]).resolve()),
        "status": completion.get("status") if completion else "RUNNING",
        "max_time_seconds": submitted["max_time_seconds"],
        "requested_at": submitted["requested_at"],
        "submission_receipt_sha256": file_sha256(submitted_path),
        "completion_receipt_sha256": completion_file_hash,
        "submission": submitted,
        "completion": completion,
        "current_outputs_match_completion": current_outputs_match,
        "receipt_ready": receipt_ready,
        "errors": errors,
        "claim_limit": "EXECUTION_IDENTITY_AND_DECLARED_OUTPUTS_ONLY_NO_OPTIMALITY_PROOF",
    }


def build_legacy_evidence(
    *,
    job_id: str,
    backend: str,
    runtime: str,
    script: str,
    workdir: str,
    status: str,
    max_time_seconds: int,
    requested_at: int,
) -> dict[str, Any]:
    """Describe an old job without promoting mutable metadata to a receipt."""

    return {
        "schema": EVIDENCE_SCHEMA,
        "job_id": job_id,
        "backend": backend,
        "runtime": runtime,
        "script": script,
        "workdir": workdir,
        "status": status.upper(),
        "max_time_seconds": int(max_time_seconds),
        "requested_at": int(requested_at),
        "submission": None,
        "completion": None,
        "receipt_ready": False,
        "errors": ["MISSING_TWO_STAGE_RECEIPT"],
        "claim_limit": "LEGACY_JOB_METADATA_ONLY",
    }


def bind_event_stream(evidence: dict[str, Any], events: Iterable[Any]) -> dict[str, Any]:
    """Require native receipt hashes to appear in the append-only workflow events."""

    result = dict(evidence)
    errors = list(result.get("errors") or [])
    job_id = str(result.get("job_id") or "")
    indexed: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        if isinstance(event, dict):
            event_type = event.get("type")
            payload = event.get("payload")
        if isinstance(event_type, str) and isinstance(payload, dict):
            indexed.setdefault(event_type, []).append(payload)

    checks = [
        (
            "submitted",
            "SOLVER_JOB_RECEIPT_SUBMITTED",
            result.get("submission_receipt_sha256"),
            (result.get("submission") or {}).get("content_sha256"),
            (result.get("submission") or {}).get("request_sha256"),
        )
    ]
    if result.get("completion") is not None:
        checks.append(
            (
                "completed",
                "SOLVER_JOB_RECEIPT_COMPLETED",
                result.get("completion_receipt_sha256"),
                (result.get("completion") or {}).get("content_sha256"),
                (result.get("completion") or {}).get("request_sha256"),
            )
        )
    for stage, event_type, receipt_hash, content_hash, request_hash in checks:
        matches = [
            payload
            for payload in indexed.get(event_type, [])
            if str(payload.get("job_id") or "") == job_id
        ]
        if not matches:
            errors.append(f"{stage.upper()}_RECEIPT_EVENT_MISSING")
            continue
        if not any(
            payload.get("stage") == stage
            and payload.get("receipt_sha256") == receipt_hash
            and payload.get("content_sha256") == content_hash
            and payload.get("request_sha256") == request_hash
            for payload in matches
        ):
            errors.append(f"{stage.upper()}_RECEIPT_EVENT_HASH_MISMATCH")
    event_errors = [
        error
        for error in errors
        if error.endswith("_RECEIPT_EVENT_MISSING")
        or error.endswith("_RECEIPT_EVENT_HASH_MISMATCH")
    ]
    result["event_stream_bound"] = not event_errors
    result["errors"] = errors
    if event_errors:
        result["receipt_ready"] = False
    return result


def receipt_paths(receipt_dir: str | Path, job_id: str) -> tuple[Path, Path]:
    root = Path(receipt_dir)
    return root / f"{job_id}.submitted.json", root / f"{job_id}.completed.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--project-dir", required=True, type=Path)
    submit.add_argument("--receipt-dir", required=True, type=Path)
    submit.add_argument("--job-id", required=True)
    submit.add_argument("--backend", required=True)
    submit.add_argument("--runtime", required=True)
    submit.add_argument("--script", required=True, type=Path)
    submit.add_argument("--workdir", required=True, type=Path)
    submit.add_argument("--argv-json", default="[]")
    submit.add_argument("--argv", action="append", default=[])
    submit.add_argument("--max-time", required=True, type=int)
    submit.add_argument("--requested-at", required=True, type=int)
    submit.add_argument("--input", action="append", default=[])
    submit.add_argument("--output", action="append", default=[])
    submit.add_argument("--seed", action="append", default=[])
    complete = sub.add_parser("complete")
    complete.add_argument("--project-dir", required=True, type=Path)
    complete.add_argument("--receipt-dir", required=True, type=Path)
    complete.add_argument("--job-id", required=True)
    complete.add_argument("--status", required=True)
    complete.add_argument("--finished-at", required=True, type=int)
    complete.add_argument("--result-refs-json", default="{}")
    evidence = sub.add_parser("evidence")
    evidence.add_argument("--project-dir", required=True, type=Path)
    evidence.add_argument("--receipt-dir", required=True, type=Path)
    evidence.add_argument("--job-id", required=True)
    legacy = sub.add_parser("legacy-evidence")
    legacy.add_argument("--job-id", required=True)
    legacy.add_argument("--backend", default="local")
    legacy.add_argument("--runtime", required=True)
    legacy.add_argument("--script", required=True)
    legacy.add_argument("--workdir", required=True)
    legacy.add_argument("--status", required=True)
    legacy.add_argument("--max-time", type=int, default=0)
    legacy.add_argument("--requested-at", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "legacy-evidence":
            print(
                json.dumps(
                    build_legacy_evidence(
                        job_id=args.job_id,
                        backend=args.backend,
                        runtime=args.runtime,
                        script=args.script,
                        workdir=args.workdir,
                        status=args.status,
                        max_time_seconds=args.max_time,
                        requested_at=args.requested_at,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        submitted_path, completed_path = receipt_paths(args.receipt_dir, args.job_id)
        if args.command == "submit":
            argv_values = args.argv or json.loads(args.argv_json)
            if not isinstance(argv_values, list):
                raise ReceiptError("--argv-json must be an array")
            receipt = build_submission_receipt(
                project_dir=args.project_dir,
                job_id=args.job_id,
                backend=args.backend,
                runtime=args.runtime,
                script=args.script,
                workdir=args.workdir,
                argv=argv_values,
                max_time_seconds=args.max_time,
                requested_at=args.requested_at,
                input_paths=args.input,
                output_paths=args.output,
                seeds=args.seed,
            )
            print(write_receipt(submitted_path, receipt))
            return 0
        if args.command == "complete":
            refs = json.loads(args.result_refs_json)
            if not isinstance(refs, dict):
                raise ReceiptError("--result-refs-json must be an object")
            receipt = build_completion_receipt(
                project_dir=args.project_dir,
                submission_path=submitted_path,
                status=args.status,
                finished_at=args.finished_at,
                result_refs=refs,
            )
            print(write_receipt(completed_path, receipt))
            return 0
        print(
            json.dumps(
                build_evidence(
                    args.project_dir,
                    submitted_path,
                    completed_path if completed_path.is_file() else None,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, json.JSONDecodeError, ReceiptError) as exc:
        print(f"solver receipt rejected input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
