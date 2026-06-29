"""
Paper Factory Cloud Solver API
FastAPI service for executing solver jobs on Cloud Run
"""
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import subprocess
import os
import json
import time
import uuid
import shutil
import hmac
from pathlib import Path
from google.cloud import storage
import logging
import psutil

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Paper Factory Solver API", version="1.0.0")

# Configuration
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "level-night-476302-k0")
BUCKET_NAME = os.environ.get("SOLVER_BUCKET", f"{PROJECT_ID}-solver-jobs")
MAX_EXECUTION_TIME = int(os.environ.get("MAX_EXECUTION_TIME", "3600"))  # 1 hour default
JOBS_DIR = Path("/tmp/jobs")
RESULTS_DIR = Path("/tmp/results")

# Create directories
JOBS_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

# In-memory job registry is a hot cache only. GCS manifests are the durable
# contract across Cloud Run instance restarts.
job_registry: Dict[str, Dict[str, Any]] = {}

# Storage client is lazy so importing the module does not require ADC.
storage_client: Optional[Any] = None


class SolverRequest(BaseModel):
    """Request to execute a solver script"""
    job_id: Optional[str] = Field(default=None, description="Job ID (auto-generated if not provided)")
    solver_type: str = Field(..., description="Solver type: python, julia, matlab, R, gurobi")
    script_content: str = Field(..., description="Script content to execute")
    script_name: str = Field(default="solve.py", description="Script filename")
    max_time: int = Field(default=1800, description="Maximum execution time in seconds")
    working_files: Optional[Dict[str, str]] = Field(default=None, description="Additional files needed {filename: content}")
    env_vars: Optional[Dict[str, str]] = Field(default=None, description="Environment variables")


class JobStatus(BaseModel):
    """Job status response"""
    job_id: str
    status: str  # queued, running, completed, failed, timeout
    submitted_at: Optional[float] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration: Optional[float] = None
    exit_code: Optional[int] = None
    stdout_url: Optional[str] = None
    stderr_url: Optional[str] = None
    result_files: Optional[List[str]] = None
    error_message: Optional[str] = None
    gcs_prefix: Optional[str] = None
    manifest_url: Optional[str] = None


def verify_solver_auth(x_solver_token: Optional[str] = Header(default=None, alias="X-Solver-Token")) -> None:
    expected = os.environ.get("SOLVER_API_TOKEN", "").strip()
    if not expected:
        return None
    if not x_solver_token or not hmac.compare_digest(x_solver_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid solver API token",
        )
    return None


def get_storage_client():
    """Return a cached GCS client, creating it only when storage is used."""
    global storage_client
    if storage_client is None:
        storage_client = storage.Client(project=PROJECT_ID)
    return storage_client


def gcs_url(path: str) -> str:
    return f"gs://{BUCKET_NAME}/{path}"


class JobStore:
    """Persist solver job status in GCS while keeping an in-memory cache."""

    def __init__(self, registry: Dict[str, Dict[str, Any]]):
        self.registry = registry

    def _manifest_path(self, job_id: str) -> str:
        return f"jobs/{job_id}/manifest.json"

    def _manifest_blob(self, job_id: str):
        return get_storage_client().bucket(BUCKET_NAME).blob(self._manifest_path(job_id))

    def _with_storage_contract(self, job: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(job)
        job_id = enriched["job_id"]
        enriched.setdefault("gcs_prefix", gcs_url(f"jobs/{job_id}/"))
        enriched.setdefault("manifest_url", gcs_url(self._manifest_path(job_id)))
        return enriched

    def save(self, job: Dict[str, Any]) -> Dict[str, Any]:
        enriched = self._with_storage_contract(job)
        self.registry[enriched["job_id"]] = enriched
        payload = {
            "schema_version": 1,
            "updated_at": time.time(),
            "job": enriched,
        }
        try:
            self._manifest_blob(enriched["job_id"]).upload_from_string(
                json.dumps(payload, ensure_ascii=False, indent=2),
                content_type="application/json",
            )
        except Exception as exc:
            logger.error(f"Failed to persist job manifest for {enriched['job_id']}: {exc}")
        return enriched

    def update(self, job_id: str, **fields: Any) -> Dict[str, Any]:
        job = self.load(job_id) or {"job_id": job_id, "status": "unknown"}
        job.update(fields)
        return self.save(job)

    def load(self, job_id: str) -> Optional[Dict[str, Any]]:
        if job_id in self.registry:
            return self.registry[job_id]
        try:
            blob = self._manifest_blob(job_id)
            if hasattr(blob, "exists") and not blob.exists():
                return None
            payload = json.loads(blob.download_as_text())
            job = self._with_storage_contract(payload["job"])
        except Exception as exc:
            logger.info(f"Job manifest not available for {job_id}: {exc}")
            return None
        self.registry[job_id] = job
        return job

    def delete(self, job_id: str) -> bool:
        found = self.load(job_id) is not None
        self.registry.pop(job_id, None)
        try:
            blob = self._manifest_blob(job_id)
            if not hasattr(blob, "exists") or blob.exists():
                blob.delete()
        except Exception as exc:
            logger.warning(f"Failed to delete manifest for {job_id}: {exc}")
        return found


job_store = JobStore(job_registry)


def get_solver_command(solver_type: str, script_path: str) -> List[str]:
    """Get the command to execute based on solver type"""
    commands = {
        "python": ["python3", script_path],
        "julia": ["julia", script_path],
        "matlab": ["matlab", "-batch", f"run('{script_path}')"],
        "R": ["Rscript", script_path],
        "gurobi": ["python3", script_path],  # Gurobi via gurobipy
    }

    if solver_type not in commands:
        raise ValueError(f"Unsupported solver type: {solver_type}")

    return commands[solver_type]


def upload_to_gcs(local_path: Path, gcs_path: str) -> Optional[str]:
    """Upload file to GCS and return public URL"""
    try:
        bucket = get_storage_client().bucket(BUCKET_NAME)
        blob = bucket.blob(gcs_path)
        blob.upload_from_filename(str(local_path))
        return gcs_url(gcs_path)
    except Exception as e:
        logger.error(f"Failed to upload to GCS: {e}")
        return None


def execute_solver_job(job_id: str, request: SolverRequest):
    """Background task to execute solver job"""
    job_dir = JOBS_DIR / job_id
    result_dir = RESULTS_DIR / job_id

    try:
        job_dir.mkdir(exist_ok=True)
        result_dir.mkdir(exist_ok=True)

        # Update status to running
        job_store.update(job_id, status="running", started_at=time.time())

        # Write script file
        script_path = job_dir / request.script_name
        script_path.write_text(request.script_content)
        script_path.chmod(0o755)

        # Write additional files
        if request.working_files:
            for filename, content in request.working_files.items():
                file_path = job_dir / filename
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content)

        # Prepare environment
        env = os.environ.copy()
        if request.env_vars:
            env.update(request.env_vars)

        # Get solver command
        command = get_solver_command(request.solver_type, str(script_path))

        # Execute with timeout
        stdout_path = result_dir / "stdout.log"
        stderr_path = result_dir / "stderr.log"

        with open(stdout_path, "w") as stdout_f, open(stderr_path, "w") as stderr_f:
            logger.info(f"Executing job {job_id}: {' '.join(command)}")

            result = subprocess.run(
                command,
                cwd=str(job_dir),
                env=env,
                stdout=stdout_f,
                stderr=stderr_f,
                timeout=request.max_time,
            )

        # Job completed
        completed_at = time.time()
        job = job_store.load(job_id) or {"job_id": job_id}
        job.update(
            {
                "status": "completed" if result.returncode == 0 else "failed",
                "completed_at": completed_at,
                "duration": completed_at - (job.get("started_at") or completed_at),
                "exit_code": result.returncode,
            }
        )

        # Upload results to GCS
        stdout_url = upload_to_gcs(stdout_path, f"jobs/{job_id}/stdout.log")
        stderr_url = upload_to_gcs(stderr_path, f"jobs/{job_id}/stderr.log")

        job["stdout_url"] = stdout_url
        job["stderr_url"] = stderr_url

        # Find and upload result files
        result_files = []
        result_suffixes = {".json", ".csv", ".txt", ".md", ".log", ".xlsx", ".png", ".pdf"}
        for item in job_dir.rglob("*"):
            if item.is_file() and item.suffix in result_suffixes:
                rel_path = item.relative_to(job_dir)
                gcs_path = f"jobs/{job_id}/{rel_path}"
                url = upload_to_gcs(item, gcs_path)
                if url:
                    result_files.append(url)

        job["result_files"] = result_files
        job_store.save(job)

        logger.info(f"Job {job_id} completed with exit code {result.returncode}")

    except subprocess.TimeoutExpired:
        job_store.update(
            job_id,
            status="timeout",
            completed_at=time.time(),
            error_message=f"Execution exceeded {request.max_time}s timeout",
        )
        logger.warning(f"Job {job_id} timed out")

    except Exception as e:
        job_store.update(
            job_id,
            status="failed",
            completed_at=time.time(),
            error_message=str(e),
        )
        logger.error(f"Job {job_id} failed: {e}")

    finally:
        # Cleanup local files (keep results for a bit)
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to cleanup job dir: {e}")


@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "active_jobs": sum(1 for j in job_registry.values() if j["status"] in ["queued", "running"]),
        "total_jobs": len(job_registry),
        "system": {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage("/tmp").percent,
        }
    }


@app.post("/solve/{solver_type}", response_model=JobStatus)
def submit_solver_job(
    solver_type: str,
    request: SolverRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(verify_solver_auth),
):
    """Submit a solver job for execution"""

    # Generate job ID if not provided
    job_id = request.job_id or str(uuid.uuid4())

    # Validate solver type
    if solver_type not in ["python", "julia", "matlab", "R", "gurobi"]:
        raise HTTPException(status_code=400, detail=f"Unsupported solver type: {solver_type}")

    # Check if job already exists
    if job_store.load(job_id) is not None:
        raise HTTPException(status_code=409, detail=f"Job {job_id} already exists")

    # Override solver type from path
    request.solver_type = solver_type

    # Register job
    job = job_store.save({
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
        "error_message": None,
    })

    # Submit background task
    background_tasks.add_task(execute_solver_job, job_id, request)

    logger.info(f"Submitted job {job_id} ({solver_type})")

    return JobStatus(**job)


@app.get("/jobs/{job_id}/status", response_model=JobStatus)
def get_job_status(job_id: str, _auth: None = Depends(verify_solver_auth)):
    """Get status of a solver job"""

    job = job_store.load(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return JobStatus(**job)


@app.get("/jobs/{job_id}/output")
def get_job_output(
    job_id: str,
    stream: str = "stdout",
    _auth: None = Depends(verify_solver_auth),
):
    """Get stdout or stderr output of a job"""

    job = job_store.load(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if stream == "stdout":
        url = job.get("stdout_url")
    elif stream == "stderr":
        url = job.get("stderr_url")
    else:
        raise HTTPException(status_code=400, detail="stream must be 'stdout' or 'stderr'")

    if not url:
        raise HTTPException(status_code=404, detail=f"{stream} not available yet")

    return {"job_id": job_id, "stream": stream, "url": url}


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, _auth: None = Depends(verify_solver_auth)):
    """Delete a job from registry"""

    if not job_store.delete(job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Cleanup local files
    job_dir = JOBS_DIR / job_id
    result_dir = RESULTS_DIR / job_id
    shutil.rmtree(job_dir, ignore_errors=True)
    shutil.rmtree(result_dir, ignore_errors=True)

    logger.info(f"Deleted job {job_id}")

    return {"status": "deleted", "job_id": job_id}


@app.get("/jobs")
def list_jobs(status: Optional[str] = None, _auth: None = Depends(verify_solver_auth)):
    """List all jobs, optionally filtered by status"""

    jobs = list(job_registry.values())

    if status:
        jobs = [j for j in jobs if j["status"] == status]

    return {"total": len(jobs), "jobs": jobs}


@app.get("/")
def root():
    """Root endpoint"""
    return {
        "service": "Paper Factory Cloud Solver API",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "submit_job": "POST /solve/{solver_type}",
            "job_status": "GET /jobs/{job_id}/status",
            "job_output": "GET /jobs/{job_id}/output?stream=stdout|stderr",
            "list_jobs": "GET /jobs?status={status}",
            "delete_job": "DELETE /jobs/{job_id}",
        }
    }
