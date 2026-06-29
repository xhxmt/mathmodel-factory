#!/usr/bin/env python3
"""Submit cumcm_2025_a_v2 solver to Cloud Run and validate results."""

import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] / "complete" / "cumcm_2025_a_v2"
MODEL_DIR = PROJECT / "models" / "m1_pso"
REGION = "europe-west4"
SERVICE = "solver-api"


def get_service_url() -> str:
    out = subprocess.check_output(
        [
            "gcloud", "run", "services", "describe", SERVICE,
            "--region", REGION,
            "--format=value(status.url)",
        ],
        text=True,
    )
    return out.strip()


def get_token() -> str:
    return subprocess.check_output(
        ["gcloud", "auth", "print-identity-token"], text=True
    ).strip()


def submit_job(problem: int, n_particles: int = 80, n_iterations: int = 150) -> dict:
    url = get_service_url()
    token = get_token()
    job_id = f"cumcm2025a-p{problem}-{int(time.time())}"

    wrapper = f"""#!/usr/bin/env python3
import subprocess
import sys
subprocess.run(["python3", "models/m1_pso/01_data.py"], check=True)
subprocess.run([
    "python3", "models/m1_pso/03_solve.py",
    "--problem", "{problem}",
    "--seed", "42",
    "--n-particles", "{n_particles}",
    "--n-iterations", "{n_iterations}",
], check=True)
"""

    working_files = {
        "models/m1_pso/01_data.py": (MODEL_DIR / "01_data.py").read_text(),
        "models/m1_pso/02_model.py": (MODEL_DIR / "02_model.py").read_text(),
        "models/m1_pso/03_solve.py": (MODEL_DIR / "03_solve.py").read_text(),
    }

    payload = {
        "job_id": job_id,
        "solver_type": "python",
        "script_content": wrapper,
        "script_name": "run_cloud_solve.py",
        "max_time": 900 if problem > 1 else 120,
        "working_files": working_files,
        "env_vars": {},
    }

    import urllib.request

    req = urllib.request.Request(
        f"{url}/solve/python",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())
    print(f"[submit] job_id={job_id} problem={problem}")
    return {"job_id": job_id, "url": url, "token": token}


def poll_job(job_id: str, url: str, token: str, timeout: int = 900) -> dict:
    import urllib.request

    start = time.time()
    while time.time() - start < timeout:
        req = urllib.request.Request(
            f"{url}/jobs/{job_id}/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = json.loads(resp.read().decode())
        st = status.get("status")
        print(f"[poll] {job_id} status={st}")
        if st in ("completed", "failed", "timeout"):
            return status
        time.sleep(5)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout}s")


def download_logs(status: dict, out_dir: Path, label: str):
    import urllib.request

    for stream, key in [("stdout", "stdout_url"), ("stderr", "stderr_url")]:
        gcs_url = status.get(key)
        if not gcs_url or gcs_url == "null":
            continue
        local = out_dir / f"{label}.{stream}.log"
        subprocess.run(["gsutil", "cp", gcs_url, str(local)], check=False)
        if local.exists():
            print(f"[log] {stream}:")
            print(local.read_text()[:3000])


def main():
    out_dir = Path(__file__).resolve().parent / "cloudrun_output"
    out_dir.mkdir(exist_ok=True)

    tests = [
        {"problem": 1, "expected_objective": 1.4, "tolerance": 0.01, "particles": 80, "iterations": 150},
        {"problem": 2, "expected_objective": 4.7, "tolerance": 0.5, "particles": 40, "iterations": 80},
    ]

    passed = 0
    for t in tests:
        p = t["problem"]
        print(f"\n=== Cloud Run test: problem {p} ===")
        meta = submit_job(p, t["particles"], t["iterations"])
        status = poll_job(meta["job_id"], meta["url"], meta["token"])
        download_logs(status, out_dir, f"p{p}")

        if status["status"] != "completed":
            print(f"FAIL problem {p}: status={status['status']} error={status.get('error_message')}")
            continue

        if status.get("exit_code", 1) != 0:
            print(f"FAIL problem {p}: exit_code={status.get('exit_code')}")
            continue

        # Parse objective from stdout log
        log_path = out_dir / f"p{p}.stdout.log"
        objective = None
        if log_path.exists():
            for line in log_path.read_text().splitlines():
                if "遮蔽时间:" in line:
                    objective = float(line.split(":")[-1].strip().split()[0])
                    break

        if objective is None:
            print(f"FAIL problem {p}: could not parse objective from stdout")
            continue

        diff = abs(objective - t["expected_objective"])
        if diff <= t["tolerance"]:
            print(f"PASS problem {p}: objective={objective:.3f}s (expected ~{t['expected_objective']})")
            passed += 1
        else:
            print(
                f"FAIL problem {p}: objective={objective:.3f}s "
                f"expected ~{t['expected_objective']}±{t['tolerance']}"
            )

    print(f"\n=== Summary: {passed}/{len(tests)} passed ===")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())