import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_load_secrets_preserves_explicit_env_when_gcloud_unavailable():
    env = os.environ.copy()
    env["JWT_SECRET"] = "0123456789abcdef0123456789abcdef"
    env["ADMIN_PASSWORD"] = "strong-password"
    env["MINERU_TOKEN"] = "mineru-token"

    result = subprocess.run(
        [
            "bash",
            "-lc",
            (
                f'source "{REPO_ROOT}/scripts/load_secrets.sh" >/dev/null 2>/dev/null; '
                'python3 - <<\'PY\'\n'
                'import os\n'
                'print(os.environ["JWT_SECRET"])\n'
                'print(os.environ["ADMIN_PASSWORD"])\n'
                'print(os.environ["MINERU_TOKEN"])\n'
                'PY'
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines == [
        "0123456789abcdef0123456789abcdef",
        "strong-password",
        "mineru-token",
    ]
