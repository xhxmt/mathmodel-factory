import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_load_secrets_overrides_sensitive_env_from_secret_manager(tmp_path):
    log_file = tmp_path / "gcloud.log"
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$GCLOUD_LOG"
secret=""
for arg in "$@"; do
  case "$arg" in
    --secret=*) secret="${arg#--secret=}" ;;
  esac
done
case "$secret" in
  mineru-token) printf 'mineru-from-sm\n' ;;
  gemini-api-key) printf 'gemini-from-sm\n' ;;
  deepseek-api-key) printf 'deepseek-from-sm\n' ;;
  dashboard-jwt-secret) printf 'jwt-from-sm-0123456789abcdef0123456789abcdef\n' ;;
  dashboard-admin-password) printf 'admin-from-sm\n' ;;
  *) exit 7 ;;
esac
""",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            "-lc",
            (
                f'source "{REPO_ROOT}/scripts/load_secrets.sh" >/dev/null; '
                'python3 - <<\'PY\'\n'
                'import os\n'
                'print(os.environ["MINERU_TOKEN"])\n'
                'print(os.environ["GEMINI_API_KEY"])\n'
                'print(os.environ["DEEPSEEK_API_KEY"])\n'
                'print(os.environ["JWT_SECRET"])\n'
                'print(os.environ["ADMIN_PASSWORD"])\n'
                'PY'
            ),
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "GCLOUD_BIN": str(fake_gcloud),
            "GCLOUD_LOG": str(log_file),
            "GCP_PROJECT_ID": "configured-project",
            "JWT_SECRET": "old-jwt",
            "ADMIN_PASSWORD": "old-admin",
            "MINERU_TOKEN": "old-mineru",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines == [
        "mineru-from-sm",
        "gemini-from-sm",
        "deepseek-from-sm",
        "jwt-from-sm-0123456789abcdef0123456789abcdef",
        "admin-from-sm",
    ]
    assert "--project=configured-project" in log_file.read_text(encoding="utf-8")


def test_load_secrets_fails_when_gcloud_is_unavailable():
    result = subprocess.run(
        [
            "bash",
            "-lc",
            f'source "{REPO_ROOT}/scripts/load_secrets.sh"',
        ],
        env={**os.environ, "PATH": "/usr/bin:/bin", "GCP_PROJECT_ID": "configured-project"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "gcloud" in result.stderr.lower()


def test_run_paper_loads_secret_manager_for_direct_runs():
    head = "\n".join((REPO_ROOT / "run_paper.sh").read_text(encoding="utf-8").splitlines()[:70])

    assert "scripts/load_secrets.sh" in head
