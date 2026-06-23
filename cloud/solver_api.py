"""
Paper Factory Cloud Solver API
FastAPI service for executing solver jobs on Cloud Run
"""
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import subprocess
import tempfile
import os
import json
import time
import uuid
import shutil
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

# In-memory job registry (Cloud Run is stateless, so this is per-instance)
# For production, use Cloud Firestore or Redis
job_registry: Dict[str, Dict[str, Any]] = {}

# Storage client
storage_client = storage.Client()


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
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration: Optional[float] = None
    exit_code: Optional[int] = None
    stdout_url: Optional[str] = None
    stderr_url: Optional[str] = None
    result_files: Optional[List[str]] = None
    error_message: Optional[str] = None


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


def upload_to_gcs(local_path: Path, gcs_path: str) -> str:
    """Upload file to GCS and return public URL"""
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(gcs_path)
        blob.upload_from_filename(str(local_path))
        return f"gs://{BUCKET_NAME}/{gcs_path}"
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
        job_registry[job_id]["status"] = "running"
        job_registry[job_id]["started_at"] = time.time()

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
        job_registry[job_id]["status"] = "completed" if result.returncode == 0 else "failed"
        job_registry[job_id]["completed_at"] = completed_at
        job_registry[job_id]["duration"] = completed_at - job_registry[job_id]["started_at"]
        job_registry[job_id]["exit_code"] = result.returncode

        # Upload results to GCS
        stdout_url = upload_to_gcs(stdout_path, f"jobs/{job_id}/stdout.log")
        stderr_url = upload_to_gcs(stderr_path, f"jobs/{job_id}/stderr.log")

        job_registry[job_id]["stdout_url"] = stdout_url
        job_registry[job_id]["stderr_url"] = stderr_url

        # Find and upload result files
        result_files = []
        for item in job_dir.rglob("*"):
            if item.is_file() and item.suffix in [".json", ".csv", ".txt", ".md", ".log"]:
                rel_path = item.relative_to(job_dir)
                gcs_path = f"jobs/{job_id}/{rel_path}"
                url = upload_to_gcs(item, gcs_path)
                if url:
                    result_files.append(url)

        job_registry[job_id]["result_files"] = result_files

        logger.info(f"Job {job_id} completed with exit code {result.returncode}")

    except subprocess.TimeoutExpired:
        job_registry[job_id]["status"] = "timeout"
        job_registry[job_id]["completed_at"] = time.time()
        job_registry[job_id]["error_message"] = f"Execution exceeded {request.max_time}s timeout"
        logger.warning(f"Job {job_id} timed out")

    except Exception as e:
        job_registry[job_id]["status"] = "failed"
        job_registry[job_id]["completed_at"] = time.time()
        job_registry[job_id]["error_message"] = str(e)
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
    background_tasks: BackgroundTasks
):
    """Submit a solver job for execution"""

    # Generate job ID if not provided
    job_id = request.job_id or str(uuid.uuid4())

    # Validate solver type
    if solver_type not in ["python", "julia", "matlab", "R", "gurobi"]:
        raise HTTPException(status_code=400, detail=f"Unsupported solver type: {solver_type}")

    # Check if job already exists
    if job_id in job_registry:
        raise HTTPException(status_code=409, detail=f"Job {job_id} already exists")

    # Override solver type from path
    request.solver_type = solver_type

    # Register job
    job_registry[job_id] = {
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
    }

    # Submit background task
    background_tasks.add_task(execute_solver_job, job_id, request)

    logger.info(f"Submitted job {job_id} ({solver_type})")

    return JobStatus(**job_registry[job_id])


@app.get("/jobs/{job_id}/status", response_model=JobStatus)
def get_job_status(job_id: str):
    """Get status of a solver job"""

    if job_id not in job_registry:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return JobStatus(**job_registry[job_id])


@app.get("/jobs/{job_id}/output")
def get_job_output(job_id: str, stream: str = "stdout"):
    """Get stdout or stderr output of a job"""

    if job_id not in job_registry:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job = job_registry[job_id]

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
def delete_job(job_id: str):
    """Delete a job from registry"""

    if job_id not in job_registry:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Remove from registry
    del job_registry[job_id]

    # Cleanup local files
    job_dir = JOBS_DIR / job_id
    result_dir = RESULTS_DIR / job_id
    shutil.rmtree(job_dir, ignore_errors=True)
    shutil.rmtree(result_dir, ignore_errors=True)

    logger.info(f"Deleted job {job_id}")

    return {"status": "deleted", "job_id": job_id}


@app.get("/jobs")
def list_jobs(status: Optional[str] = None):
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
