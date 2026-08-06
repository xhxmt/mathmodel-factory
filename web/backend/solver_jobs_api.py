"""Solver jobs API - list and detail endpoints for per-project solver job visibility."""

from __future__ import annotations

import json
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from factory_core.cli import solver_evidence_payload
from factory_core.storage import SQLiteStateStore
from scripts.solver_job_receipt import build_legacy_evidence

from .config import Settings


@lru_cache(maxsize=512)
def _parse_meta_file(path: Path, mtime_ns: int, size: int) -> dict[str, str] | None:
    """Parse legacy solver job meta file. Cached by (path, mtime, size)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        meta = {}
        for line in lines:
            if "=" in line:
                key, _, value = line.partition("=")
                meta[key.strip()] = value.strip()
        return meta
    except OSError:
        return None


def _list_native_jobs(project: Path) -> list[dict[str, Any]]:
    """List solver jobs from native engine state.db."""
    store = SQLiteStateStore(project)
    if not store.exists:
        return []

    receipt_dir = project / ".factory" / "solver_receipts"
    jobs = []
    now = int(time.time())

    for job in store.solver_jobs():
        # Compute duration
        duration = None
        if job["finished_at"]:
            duration = job["finished_at"] - (job["started_at"] or job["requested_at"])
        elif job["started_at"]:
            duration = now - job["started_at"]

        # Check receipt files
        job_id = job["job_id"]
        has_submission = (receipt_dir / f"{job_id}.submitted.json").is_file()
        has_completion = (receipt_dir / f"{job_id}.completed.json").is_file()

        jobs.append({
            "job_id": job_id,
            "backend": job["backend"],
            "runtime": job["runtime"],
            "status": job["status"].upper(),
            "script": job["script"],
            "requested_at": job["requested_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "duration_seconds": duration,
            "has_submission_receipt": has_submission,
            "has_completion_receipt": has_completion,
            "legacy": False,
            "result_refs": job.get("result_refs"),
        })

    return jobs




def _list_legacy_jobs(settings: Settings, project: Path) -> list[dict[str, Any]]:
    """List solver jobs from legacy run_state/solver_jobs/*.meta files."""
    legacy_dir = settings.factory_root / "run_state" / "solver_jobs"
    if not legacy_dir.is_dir():
        return []

    project_str = str(project)
    jobs = []
    now = int(time.time())

    for meta_path in legacy_dir.glob("*.meta"):
        stat = meta_path.stat()
        meta = _parse_meta_file(meta_path, stat.st_mtime_ns, stat.st_size)
        if not meta:
            continue

        # Filter by project
        if "project_dir" in meta:
            if meta["project_dir"] != project_str:
                continue
        elif "workdir" in meta:
            if not meta["workdir"].startswith(project_str):
                continue
        else:
            continue

        job_id = meta_path.stem

        # Infer backend from job_id prefix
        backend = "cloud" if job_id.startswith("cloud_") else "local"

        # Get runtime from type field
        runtime = meta.get("type", "unknown")

        # Determine status
        exit_file = legacy_dir / f"{job_id}.exit"
        status = "RUNNING"
        finished_at = None

        if exit_file.is_file():
            try:
                exit_code = int(exit_file.read_text(encoding="utf-8", errors="replace").strip())
                finished_at = int(exit_file.stat().st_mtime)
                if exit_code == 0:
                    status = "COMPLETED"
                elif exit_code == 124:
                    status = "TIMEOUT"
                else:
                    status = "FAILED"
            except (ValueError, OSError):
                status = "EXITED"
        elif "pid" in meta:
            try:
                os.kill(int(meta["pid"]), 0)
                status = "RUNNING"
            except (ValueError, ProcessLookupError, OSError):
                status = "EXITED"

        # Parse timestamps
        started_at = int(meta.get("started", 0)) if meta.get("started") else None
        requested_at = started_at or int(stat.st_mtime)

        # Compute duration
        duration = None
        if finished_at and started_at:
            duration = finished_at - started_at
        elif started_at and status == "RUNNING":
            duration = now - started_at

        # Build result_refs from meta
        result_refs = {}
        if "stdout_log" in meta:
            result_refs["stdout"] = meta["stdout_log"]
        if "stderr_log" in meta:
            result_refs["stderr"] = meta["stderr_log"]

        jobs.append({
            "job_id": job_id,
            "backend": backend,
            "runtime": runtime,
            "status": status,
            "script": meta.get("script", ""),
            "requested_at": requested_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration,
            "has_submission_receipt": False,
            "has_completion_receipt": False,
            "legacy": True,
            "result_refs": result_refs if result_refs else None,
        })

    return jobs




def list_solver_jobs(settings: Settings, project: Path, base_name: str) -> dict[str, Any]:
    """List all solver jobs (native + legacy) for a project."""
    native = _list_native_jobs(project)
    legacy = _list_legacy_jobs(settings, project)

    # Merge and sort by requested_at descending
    all_jobs = native + legacy
    all_jobs.sort(key=lambda j: j["requested_at"] or 0, reverse=True)

    # Compute summary stats
    total = len(all_jobs)
    running = sum(1 for j in all_jobs if j["status"] == "RUNNING")
    failed = sum(1 for j in all_jobs if j["status"] in ("FAILED", "TIMEOUT", "EXITED"))

    return {
        "jobs": all_jobs,
        "total": total,
        "running": running,
        "failed": failed,
    }


def get_solver_job_evidence(
    settings: Settings, project: Path, base_name: str, job_id: str
) -> dict[str, Any]:
    """Get full evidence for a single solver job."""
    # Try native first
    store = SQLiteStateStore(project)
    if store.exists:
        try:
            job = store.solver_job(job_id)
            return solver_evidence_payload(project, job)
        except KeyError:
            pass

    # Try legacy
    legacy_dir = settings.factory_root / "run_state" / "solver_jobs"
    meta_path = legacy_dir / f"{job_id}.meta"

    if not meta_path.is_file():
        raise KeyError(f"Solver job not found: {job_id}")

    stat = meta_path.stat()
    meta = _parse_meta_file(meta_path, stat.st_mtime_ns, stat.st_size)
    if not meta:
        raise KeyError(f"Invalid meta file for job: {job_id}")

    # Verify it belongs to this project
    project_str = str(project)
    if "project_dir" in meta:
        if meta["project_dir"] != project_str:
            raise KeyError(f"Job {job_id} does not belong to project {base_name}")
    elif "workdir" in meta:
        if not meta["workdir"].startswith(project_str):
            raise KeyError(f"Job {job_id} does not belong to project {base_name}")
    else:
        raise KeyError(f"Cannot determine project for job {job_id}")

    # Build legacy evidence
    backend = "cloud" if job_id.startswith("cloud_") else "local"
    runtime = meta.get("type", "unknown")
    script = meta.get("script", "")
    workdir = meta.get("workdir", "")
    max_time = int(meta.get("max_time", 0))
    requested_at = int(meta.get("started", stat.st_mtime))

    # Determine status
    exit_file = legacy_dir / f"{job_id}.exit"
    status = "EXITED"
    if exit_file.is_file():
        try:
            exit_code = int(exit_file.read_text(encoding="utf-8", errors="replace").strip())
            if exit_code == 0:
                status = "COMPLETED"
            elif exit_code == 124:
                status = "TIMEOUT"
            else:
                status = "FAILED"
        except (ValueError, OSError):
            pass

    return build_legacy_evidence(
        job_id=job_id,
        backend=backend,
        runtime=runtime,
        script=script,
        workdir=workdir,
        status=status,
        max_time_seconds=max_time,
        requested_at=requested_at,
    )
