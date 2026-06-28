import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_file(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def fake_cloud_client(tmp_path: Path) -> Path:
    client = tmp_path / "fake_gcp_solver_client.sh"
    write_file(
        client,
        "#!/usr/bin/env bash\n"
        "echo CLOUD_CLIENT \"$@\"\n"
        "exit 0\n",
        executable=True,
    )
    return client


def test_solver_router_routes_enabled_long_python_job_to_cloud_client(tmp_path):
    script = tmp_path / "project" / "models" / "solve.py"
    write_file(script, "print('ok')\n")

    result = subprocess.run(
        [
            str(REPO_ROOT / "scripts" / "solver_router.sh"),
            "--type",
            "python",
            "--max-time",
            "400",
            str(script),
        ],
        env={
            **os.environ,
            "USE_CLOUD_SOLVER": "true",
            "CLOUD_SOLVER_TYPES": "python",
            "CLOUD_THRESHOLD_TIME": "300",
            "CLOUD_SOLVER_CLIENT": str(fake_cloud_client(tmp_path)),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "[solver_router] Routing to Cloud Run" in result.stderr
    assert "CLOUD_CLIENT --type python --max-time 400" in result.stdout


def test_solver_submit_sources_project_cloud_env_for_long_jobs(tmp_path):
    project = tmp_path / "demo_project"
    script = project / "models" / "solve.py"
    write_file(script, "print('ok')\n")
    write_file(
        project / ".env.cloud",
        "USE_CLOUD_SOLVER=true\n"
        "CLOUD_THRESHOLD_TIME=300\n"
        "CLOUD_SOLVER_TYPES=python\n",
    )

    result = subprocess.run(
        [
            str(REPO_ROOT / "solver_submit.sh"),
            "--type",
            "python",
            "--max-time",
            "400",
            str(script),
        ],
        env={
            **os.environ,
            "CLOUD_SOLVER_CLIENT": str(fake_cloud_client(tmp_path)),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "[solver_submit] Loaded cloud config:" in result.stderr
    assert "[solver_submit] Routing to Cloud Run" in result.stderr

    jobid = result.stdout.strip()
    assert jobid.startswith("cloud_python_")

    wait = subprocess.run(
        [str(REPO_ROOT / "solver_submit.sh"), "--wait", jobid],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert wait.returncode == 0, wait.stderr
    assert "COMPLETED" in wait.stdout
    assert "CLOUD_CLIENT --type python --max-time 400" in (script.with_suffix(".log")).read_text(encoding="utf-8")


def test_gcp_solver_client_describes_service_in_configured_project(tmp_path):
    bin_dir = tmp_path / "bin"
    script = tmp_path / "solve.py"
    gcloud_log = tmp_path / "gcloud.args"
    write_file(script, "print('ok')\n")
    write_file(
        bin_dir / "gcloud",
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {gcloud_log}\n"
        "if [[ \"$*\" == *'print-identity-token'* ]]; then echo token; else echo https://solver.example; fi\n",
        executable=True,
    )
    write_file(
        bin_dir / "curl",
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *'/solve/'* ]]; then\n"
        "  echo '{\"job_id\":\"job-test\"}'\n"
        "else\n"
        "  echo '{\"status\":\"completed\",\"exit_code\":0,\"stdout_url\":null,\"stderr_url\":null,\"result_files\":[]}'\n"
        "fi\n",
        executable=True,
    )
    write_file(
        bin_dir / "gsutil",
        "#!/usr/bin/env bash\nexit 0\n",
        executable=True,
    )

    result = subprocess.run(
        [
            str(REPO_ROOT / "scripts" / "gcp_solver_client.sh"),
            "--type",
            "python",
            "--max-time",
            "60",
            str(script),
        ],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GCP_PROJECT_ID": "configured-project",
            "GCP_REGION": "europe-west4",
            "GCP_SOLVER_SERVICE": "solver-api",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--project=configured-project" in gcloud_log.read_text(encoding="utf-8")
