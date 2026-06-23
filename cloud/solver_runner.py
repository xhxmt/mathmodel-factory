"""
Solver runner - handles execution isolation and result collection.
Used by the solver_api to run scripts in a controlled environment.
"""
import subprocess
import os
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any, List


def collect_output_files(job_dir: Path, extensions: Optional[List[str]] = None) -> Dict[str, str]:
    """Collect output files from the job directory"""
    if extensions is None:
        extensions = [".json", ".csv", ".txt", ".md", ".xlsx", ".png", ".pdf"]

    results = {}
    for item in job_dir.rglob("*"):
        if item.is_file() and item.suffix in extensions:
            rel_path = str(item.relative_to(job_dir))
            try:
                if item.suffix in [".png", ".pdf", ".xlsx"]:
                    import base64
                    results[rel_path] = base64.b64encode(item.read_bytes()).decode()
                else:
                    results[rel_path] = item.read_text(errors="replace")
            except Exception:
                pass

    return results


def run_solver(
    solver_type: str,
    script_path: str,
    working_dir: str,
    max_time: int = 1800,
    env_vars: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run a solver script and return results"""

    commands = {
        "python": ["python3", script_path],
        "julia": ["julia", script_path],
        "matlab": ["matlab", "-batch", f"run('{script_path}')"],
        "R": ["Rscript", script_path],
    }

    if solver_type not in commands:
        return {"status": "failed", "error": f"Unsupported solver type: {solver_type}"}

    command = commands[solver_type]

    env = os.environ.copy()
    env["PYTHONPATH"] = working_dir
    if env_vars:
        env.update(env_vars)

    start_time = time.time()

    try:
        result = subprocess.run(
            command,
            cwd=working_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=max_time,
        )

        elapsed = time.time() - start_time

        return {
            "status": "completed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration": elapsed,
        }

    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "error": f"Exceeded {max_time}s timeout",
            "duration": time.time() - start_time,
        }

    except Exception as e:
        return {
            "status": "failed",
            "error": str(e),
            "duration": time.time() - start_time,
        }
