"""Private Cloud Run control API for bounded Python solver jobs."""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, model_validator
from starlette.responses import JSONResponse
from google.cloud import storage

try:  # Package import in tests; flat import in the container image.
    from .runtime_capabilities import enabled_solver_types, runtime_capability_payload
    from .solver_runner import (
        MAX_EXECUTION_TIME,
        MAX_INPUT_BYTES,
        MAX_LOG_BYTES,
        MAX_OUTPUT_BYTES,
        MAX_OUTPUT_DIRECTORIES,
        MAX_OUTPUT_FILES,
        MAX_REQUEST_BYTES,
        MAX_SCRIPT_BYTES,
        MAX_SINGLE_OUTPUT_BYTES,
        MAX_WORKING_FILE_BYTES,
        MAX_WORKING_FILES,
        InputValidationError,
        OutputLimitError,
        collect_output_files,
        prepare_workspace,
        run_solver,
        validate_env_vars,
        validate_job_id,
        validate_submission_files,
    )
except ImportError:  # pragma: no cover - exercised by the Docker entrypoint.
    from runtime_capabilities import enabled_solver_types, runtime_capability_payload
    from solver_runner import (
        MAX_EXECUTION_TIME,
        MAX_INPUT_BYTES,
        MAX_LOG_BYTES,
        MAX_OUTPUT_BYTES,
        MAX_OUTPUT_DIRECTORIES,
        MAX_OUTPUT_FILES,
        MAX_REQUEST_BYTES,
        MAX_SCRIPT_BYTES,
        MAX_SINGLE_OUTPUT_BYTES,
        MAX_WORKING_FILE_BYTES,
        MAX_WORKING_FILES,
        InputValidationError,
        OutputLimitError,
        collect_output_files,
        prepare_workspace,
        run_solver,
        validate_env_vars,
        validate_job_id,
        validate_submission_files,
    )


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RequestBodyLimitMiddleware:
    """Reject oversized request bodies before FastAPI parses their JSON."""

    def __init__(self, app: Any, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(
                    {"error_code": "INVALID_CONTENT_LENGTH", "detail": "Invalid Content-Length"},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
                await response(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: dict[str, Any], receive: Any, send: Any) -> None:
        response = JSONResponse(
            {"error_code": "REQUEST_TOO_LARGE", "detail": "Request body exceeds the limit"},
            status_code=413,
        )
        await response(scope, receive, send)


app = FastAPI(title="Paper Factory Solver API", version="2.0.0")
app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=MAX_REQUEST_BYTES)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(_request: Any, exc: RequestValidationError) -> JSONResponse:
    # Do not echo submitted script/file content from Pydantic's `input` field.
    errors = [
        {
            "location": list(error.get("loc", ())),
            "message": error.get("msg", "Invalid request"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        {"error_code": "INVALID_REQUEST", "detail": errors},
        status_code=422,
    )


PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "level-night-476302-k0")
BUCKET_NAME = os.environ.get("SOLVER_BUCKET", f"{PROJECT_ID}-solver-jobs")
CONTROL_DIR = Path("/tmp/solver-control")
JOBS_DIR = CONTROL_DIR / "jobs"
RESULTS_DIR = CONTROL_DIR / "results"
CONTROL_DIR.mkdir(mode=0o711, parents=False, exist_ok=True)
JOBS_DIR.mkdir(mode=0o711, parents=True, exist_ok=True)
RESULTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
CONTROL_DIR.chmod(0o711)
JOBS_DIR.chmod(0o711)
RESULTS_DIR.chmod(0o700)


def harden_shared_temp_directories() -> None:
    """Prevent the unprivileged solver UID from bypassing its output directory."""
    if os.geteuid() != 0:
        return
    for shared_temp in (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm")):
        try:
            path_stat = shared_temp.lstat()
        except FileNotFoundError:
            continue
        if shared_temp.is_symlink() or not stat.S_ISDIR(path_stat.st_mode) or path_stat.st_uid != 0:
            raise RuntimeError(f"unsafe shared temporary directory: {shared_temp}")
        shared_temp.chmod(0o755)


harden_shared_temp_directories()

job_registry: Dict[str, Dict[str, Any]] = {}
storage_client: Optional[Any] = None
submission_lock = threading.Lock()


class SolverRequest(BaseModel):
    """Validated request to execute one Python script."""

    job_id: Optional[str] = Field(default=None, max_length=64)
    solver_type: str = Field(..., min_length=1, max_length=16)
    script_content: str = Field(..., min_length=1)
    script_name: str = Field(default="solve.py", min_length=1, max_length=240)
    max_time: int = Field(default=1800, ge=1, le=MAX_EXECUTION_TIME)
    working_files: Optional[Dict[str, str]] = None
    env_vars: Optional[Dict[str, str]] = None

    @model_validator(mode="after")
    def validate_security_contract(self) -> "SolverRequest":
        if self.job_id is not None:
            validate_job_id(self.job_id)
        validate_submission_files(self.script_name, self.script_content, self.working_files)
        validate_env_vars(self.env_vars)
        return self


class JobStatus(BaseModel):
    job_id: str
    status: str
    submitted_at: Optional[float] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration: Optional[float] = None
    exit_code: Optional[int] = None
    stdout_url: Optional[str] = None
    stderr_url: Optional[str] = None
    result_files: Optional[List[str]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    gcs_prefix: Optional[str] = None
    manifest_url: Optional[str] = None


def solver_execution_enabled() -> bool:
    return os.environ.get("SOLVER_EXECUTION_ENABLED", "false").strip().lower() == "true"


def require_solver_execution_enabled() -> None:
    if not solver_execution_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "EXECUTION_QUARANTINED", "message": "Cloud solver execution is quarantined"},
        )


def get_storage_client():
    global storage_client
    if storage_client is None:
        storage_client = storage.Client(project=PROJECT_ID)
    return storage_client


def gcs_url(path: str) -> str:
    return f"gs://{BUCKET_NAME}/{path}"


def require_valid_job_id(job_id: str) -> str:
    try:
        return validate_job_id(job_id)
    except InputValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_JOB_ID", "message": str(exc)},
        ) from exc


class JobStore:
    """Persist solver job status in GCS while keeping an in-memory hot cache."""

    def __init__(self, registry: Dict[str, Dict[str, Any]]):
        self.registry = registry

    def _manifest_path(self, job_id: str) -> str:
        validate_job_id(job_id)
        return f"jobs/{job_id}/manifest.json"

    def _manifest_blob(self, job_id: str):
        return get_storage_client().bucket(BUCKET_NAME).blob(self._manifest_path(job_id))

    def _with_storage_contract(self, job: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(job)
        job_id = validate_job_id(enriched["job_id"])
        enriched.setdefault("gcs_prefix", gcs_url(f"jobs/{job_id}/"))
        enriched.setdefault("manifest_url", gcs_url(self._manifest_path(job_id)))
        return enriched

    def save(self, job: Dict[str, Any]) -> Dict[str, Any]:
        enriched = self._with_storage_contract(job)
        self.registry[enriched["job_id"]] = enriched
        payload = {"schema_version": 1, "updated_at": time.time(), "job": enriched}
        try:
            self._manifest_blob(enriched["job_id"]).upload_from_string(
                json.dumps(payload, ensure_ascii=False, indent=2),
                content_type="application/json",
                timeout=5,
                retry=None,
            )
        except Exception:
            logger.exception("Failed to persist solver job manifest for %s", enriched["job_id"])
        return enriched

    def update(self, job_id: str, **fields: Any) -> Dict[str, Any]:
        job = self.load(job_id) or {"job_id": job_id, "status": "unknown"}
        job.update(fields)
        return self.save(job)

    def load(self, job_id: str) -> Optional[Dict[str, Any]]:
        validate_job_id(job_id)
        if job_id in self.registry:
            return self.registry[job_id]
        try:
            blob = self._manifest_blob(job_id)
            if hasattr(blob, "exists") and not blob.exists(timeout=5, retry=None):
                return None
            payload = json.loads(blob.download_as_text(timeout=5, retry=None))
            job = self._with_storage_contract(payload["job"])
        except Exception:
            logger.info("Solver job manifest is not available for %s", job_id)
            return None
        self.registry[job_id] = job
        return job

    def delete(self, job_id: str) -> bool:
        validate_job_id(job_id)
        found = self.load(job_id) is not None
        self.registry.pop(job_id, None)
        try:
            blob = self._manifest_blob(job_id)
            if not hasattr(blob, "exists") or blob.exists(timeout=5, retry=None):
                blob.delete(timeout=5, retry=None)
        except Exception:
            logger.exception("Failed to delete solver manifest for %s", job_id)
        return found


job_store = JobStore(job_registry)


def upload_to_gcs(local_path: Path, gcs_path: str) -> Optional[str]:
    """Upload one already-validated regular file without following symlinks."""
    try:
        descriptor = os.open(local_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise OutputLimitError("only regular files can be uploaded")
            blob = get_storage_client().bucket(BUCKET_NAME).blob(gcs_path)
            blob.upload_from_file(handle, rewind=True, timeout=10, retry=None)
        return gcs_url(gcs_path)
    except Exception:
        logger.exception("Failed to upload bounded solver artifact to %s", gcs_path)
        return None


def _mark_internal_failure(job_id: str) -> None:
    job_store.update(
        job_id,
        status="failed",
        completed_at=time.time(),
        error_code="INTERNAL_EXECUTION_ERROR",
        error_message="Solver job failed inside the isolated execution service",
    )


def execute_solver_job(job_id: str, request: SolverRequest) -> None:
    job_root = JOBS_DIR / job_id
    result_dir = RESULTS_DIR / job_id
    try:
        result_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        input_dir, output_dir, script_path, identity = prepare_workspace(
            job_root,
            request.script_name,
            request.script_content,
            request.working_files,
        )
        started_at = time.time()
        job_store.update(job_id, status="running", started_at=started_at)

        stdout_path = result_dir / "stdout.log"
        stderr_path = result_dir / "stderr.log"
        outcome = run_solver(
            request.solver_type,
            script_path,
            input_dir,
            output_dir,
            stdout_path,
            stderr_path,
            max_time=request.max_time,
            env_vars=request.env_vars,
            identity=identity,
        )
        completed_at = time.time()
        job = job_store.load(job_id) or {"job_id": job_id}
        job.update(
            status=outcome["status"],
            completed_at=completed_at,
            duration=outcome["duration"],
            exit_code=outcome["exit_code"],
            error_code=outcome["error_code"],
            error_message=outcome["error_message"],
        )

        job["stdout_url"] = upload_to_gcs(stdout_path, f"jobs/{job_id}/stdout.log")
        job["stderr_url"] = upload_to_gcs(stderr_path, f"jobs/{job_id}/stderr.log")

        result_files: list[str] = []
        if outcome["error_code"] != "OUTPUT_LIMIT_EXCEEDED":
            for local_path, relative_path in collect_output_files(output_dir):
                artifact_url = upload_to_gcs(
                    local_path,
                    f"jobs/{job_id}/outputs/{relative_path.as_posix()}",
                )
                if artifact_url:
                    result_files.append(artifact_url)
        job["result_files"] = result_files
        job_store.save(job)
        logger.info("Solver job %s finished with status %s", job_id, job["status"])
    except OutputLimitError as exc:
        job_store.update(
            job_id,
            status="failed",
            completed_at=time.time(),
            error_code="OUTPUT_LIMIT_EXCEEDED",
            error_message=str(exc),
        )
        logger.warning("Solver job %s exceeded its output contract", job_id)
    except Exception:
        _mark_internal_failure(job_id)
        logger.exception("Solver job %s failed in the execution controller", job_id)
    finally:
        shutil.rmtree(job_root, ignore_errors=True)
        shutil.rmtree(result_dir, ignore_errors=True)


@app.get("/health")
def health_check() -> dict[str, Any]:
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "execution_enabled": solver_execution_enabled(),
        "authentication_strategy": "cloud_run_iam",
        "execution_profile": "p0-bounded-python-v1",
        "active_jobs": sum(
            1 for job in job_registry.values() if job["status"] in {"queued", "running"}
        ),
        "total_jobs": len(job_registry),
        "system": {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage("/tmp").percent,
        },
    }


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    payload = runtime_capability_payload()
    payload.update(
        {
            "execution_enabled": solver_execution_enabled(),
            "limits": {
                "request_bytes": MAX_REQUEST_BYTES,
                "script_bytes": MAX_SCRIPT_BYTES,
                "working_file_bytes": MAX_WORKING_FILE_BYTES,
                "working_files": MAX_WORKING_FILES,
                "input_bytes": MAX_INPUT_BYTES,
                "output_bytes": MAX_OUTPUT_BYTES,
                "single_output_bytes": MAX_SINGLE_OUTPUT_BYTES,
                "output_files": MAX_OUTPUT_FILES,
                "output_directories": MAX_OUTPUT_DIRECTORIES,
                "log_bytes": MAX_LOG_BYTES,
                "max_execution_seconds": MAX_EXECUTION_TIME,
            },
        }
    )
    return payload


@app.post("/solve/{solver_type}", response_model=JobStatus)
def submit_solver_job(
    solver_type: str,
    request: SolverRequest,
    background_tasks: BackgroundTasks,
    _execution_enabled: None = Depends(require_solver_execution_enabled),
) -> JobStatus:
    available = enabled_solver_types()
    if solver_type not in available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "RUNTIME_UNAVAILABLE",
                "message": f"Runtime is not available in this image: {solver_type}",
                "available_solvers": list(available),
            },
        )
    if request.solver_type != solver_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "RUNTIME_MISMATCH", "message": "Path and request solver types differ"},
        )

    job_id = validate_job_id(request.job_id or str(uuid.uuid4()))
    with submission_lock:
        if any(job["status"] in {"queued", "running"} for job in job_registry.values()):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "INSTANCE_BUSY",
                    "message": "This instance already has an active solver job",
                },
                headers={"Retry-After": "5"},
            )
        if job_store.load(job_id) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job already exists")

        job = job_store.save(
            {
                "job_id": job_id,
                "status": "queued",
                "submitted_at": time.time(),
                "started_at": None,
                "completed_at": None,
                "duration": None,
                "exit_code": None,
                "stdout_url": None,
                "stderr_url": None,
                "result_files": None,
                "error_code": None,
                "error_message": None,
            }
        )
    background_tasks.add_task(execute_solver_job, job_id, request)
    logger.info("Submitted bounded solver job %s (%s)", job_id, solver_type)
    return JobStatus(**job)


@app.get("/jobs/{job_id}/status", response_model=JobStatus)
def get_job_status(job_id: str) -> JobStatus:
    job_id = require_valid_job_id(job_id)
    job = job_store.load(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobStatus(**job)


@app.get("/jobs/{job_id}/output")
def get_job_output(job_id: str, stream: str = "stdout") -> dict[str, str]:
    job_id = require_valid_job_id(job_id)
    job = job_store.load(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if stream not in {"stdout", "stderr"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid stream")
    output_url = job.get(f"{stream}_url")
    if not output_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output not available")
    return {"job_id": job_id, "stream": stream, "url": output_url}


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    job_id = require_valid_job_id(job_id)
    if not job_store.delete(job_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    shutil.rmtree(RESULTS_DIR / job_id, ignore_errors=True)
    logger.info("Deleted solver job %s", job_id)
    return {"status": "deleted", "job_id": job_id}


@app.get("/jobs")
def list_jobs(status_filter: Optional[str] = Query(default=None, alias="status")) -> dict[str, Any]:
    jobs = list(job_registry.values())
    if status_filter:
        jobs = [job for job in jobs if job["status"] == status_filter]
    return {"total": len(jobs), "jobs": jobs}


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "Paper Factory Cloud Solver API",
        "version": "2.0.0",
        "authentication_strategy": "cloud_run_iam",
        "available_solvers": list(enabled_solver_types()),
        "endpoints": {
            "health": "/health",
            "capabilities": "/capabilities",
            "submit": "/solve/{solver_type}",
            "status": "/jobs/{job_id}/status",
            "output": "/jobs/{job_id}/output",
            "list": "/jobs",
        },
    }
