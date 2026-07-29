from __future__ import annotations

import os
import subprocess
from pathlib import Path

from scripts.validate_cloudbuild import validate_cloudbuild_text


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cloudbuild_preflight_accepts_repository_configuration():
    text = (REPO_ROOT / "cloud" / "cloudbuild.yaml").read_text(encoding="utf-8")

    assert validate_cloudbuild_text(text) == []


def test_cloudbuild_preflight_rejects_latest_production_deploy():
    text = (REPO_ROOT / "cloud" / "cloudbuild.yaml").read_text(encoding="utf-8")
    deploy_start = text.index("# Step 3: Deploy")
    deploy_end = text.index("# Step 4: Record")
    deploy = text[deploy_start:deploy_end].replace("solver-api:${BUILD_ID}", "solver-api:latest")
    mutated = text[:deploy_start] + deploy + text[deploy_end:]

    errors = validate_cloudbuild_text(mutated)

    assert "Cloud Run production deploy must not reference latest" in errors


def test_solver_image_declares_unprivileged_execution_identity_and_safe_wrapper():
    dockerfile = (REPO_ROOT / "cloud" / "Dockerfile").read_text(encoding="utf-8")
    runner = (REPO_ROOT / "cloud" / "solver_runner.py").read_text(encoding="utf-8")

    assert "useradd --uid 10001" in dockerfile
    assert "ENV SOLVER_RUN_UID=10001" in dockerfile
    assert "COPY solver_exec_wrapper.py" in dockerfile
    assert "build-essential" not in dockerfile
    assert "gfortran" not in dockerfile
    assert "    git \\" not in dockerfile
    assert "preexec_fn" not in runner


def test_digest_rollback_is_dry_run_by_default_and_preserves_quarantine(tmp_path):
    gcloud = tmp_path / "gcloud"
    gcloud.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    gcloud.chmod(0o755)
    digest = "sha256:" + "a" * 64

    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "rollback_cloud_solver.sh"), "--digest", digest],
        env={**os.environ, "GCLOUD_BIN": str(gcloud)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"solver-api@{digest}" in result.stdout
    assert "SOLVER_EXECUTION_ENABLED=false" in result.stdout
    assert "add --execute" in result.stderr


def test_digest_rollback_rejects_mutable_or_malformed_target(tmp_path):
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "rollback_cloud_solver.sh"), "--digest", "latest"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "exactly 64 lowercase hex" in result.stderr
