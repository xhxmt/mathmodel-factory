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
            "CLOUD_SOLVER_QUARANTINED": "false",
            "CLOUD_SOLVER_TYPES": "python",
            "CLOUD_THRESHOLD_TIME": "300",
            "CLOUD_SOLVER_FALLBACK_MARKER": str(tmp_path / "no-fallback.marker"),
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
            "CLOUD_SOLVER_QUARANTINED": "false",
            "CLOUD_SOLVER_CLIENT": str(fake_cloud_client(tmp_path)),
            "CLOUD_SOLVER_FALLBACK_MARKER": str(tmp_path / "no-fallback.marker"),
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


def test_solver_router_quarantines_cloud_by_default(tmp_path):
    script = tmp_path / "project" / "models" / "solve.py"
    write_file(script, "print('local-safe-path')\n")

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
    assert "Cloud Solver quarantined" in result.stderr
    assert "Routing to local solver" in result.stderr
    assert result.stdout.strip().startswith("local_python_")


def test_project_cloud_env_cannot_disable_global_quarantine(tmp_path):
    project = tmp_path / "quarantined_project"
    script = project / "models" / "solve.py"
    write_file(script, "print('local-safe-path')\n")
    write_file(
        project / ".env.cloud",
        "USE_CLOUD_SOLVER=true\n"
        "CLOUD_SOLVER_QUARANTINED=false\n"
        "CLOUD_THRESHOLD_TIME=0\n"
        "CLOUD_SOLVER_TYPES=python\n",
    )

    result = subprocess.run(
        [
            str(REPO_ROOT / "solver_submit.sh"),
            "--type",
            "python",
            "--max-time",
            "60",
            str(script),
        ],
        env={
            key: value
            for key, value in os.environ.items()
            if key != "CLOUD_SOLVER_QUARANTINED"
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("local_python_")
    assert "Routing to Cloud Run" not in result.stderr


def test_project_cloud_env_is_data_not_executable_shell(tmp_path):
    project = tmp_path / "untrusted_project"
    script = project / "models" / "solve.py"
    marker = tmp_path / "must-not-exist"
    write_file(script, "print('local-safe-path')\n")
    write_file(
        project / ".env.cloud",
        "USE_CLOUD_SOLVER=true\n"
        f"MALICIOUS=$(touch {marker})\n"
        "CLOUD_THRESHOLD_TIME=1\n"
        "CLOUD_SOLVER_TYPES=python\n",
    )

    result = subprocess.run(
        [
            str(REPO_ROOT / "solver_submit.sh"),
            "--type",
            "python",
            "--max-time",
            "60",
            str(script),
        ],
        env={**os.environ, "CLOUD_SOLVER_QUARANTINED": "true"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert "Ignoring unsupported cloud config key" in result.stderr
    assert result.stdout.strip().startswith("local_python_")


def test_gcp_solver_client_describes_service_in_configured_project(tmp_path):
    bin_dir = tmp_path / "bin"
    script = tmp_path / "solve.py"
    gcloud_log = tmp_path / "gcloud.args"
    curl_log = tmp_path / "curl.args"
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
        f"printf '%s\\n' \"$*\" >> {curl_log}\n"
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
            "CLOUD_SOLVER_AUTH_BACKEND": "gcloud",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--project=configured-project" in gcloud_log.read_text(encoding="utf-8")
    assert "Bearer token" not in curl_log.read_text(encoding="utf-8")
    assert "print('ok')" not in curl_log.read_text(encoding="utf-8")


def test_direct_cloud_client_rejects_runtime_not_in_capability_manifest(tmp_path):
    script = tmp_path / "solve.jl"
    write_file(script, "println(1)\n")

    result = subprocess.run(
        [
            str(REPO_ROOT / "scripts" / "gcp_solver_client.sh"),
            "--type",
            "julia",
            str(script),
        ],
        env={**os.environ},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "runtime is not available" in result.stderr
