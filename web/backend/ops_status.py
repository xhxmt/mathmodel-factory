from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from .config import Settings
from .schemas import LocalEnvFileStatus, OpsSecretsStatus, SecretBindingStatus


SECRET_BINDINGS: tuple[tuple[str, str], ...] = (
    ("MINERU_TOKEN", "mineru-token"),
    ("GEMINI_API_KEY", "gemini-api-key"),
    ("DEEPSEEK_API_KEY", "deepseek-api-key"),
    ("JWT_SECRET", "dashboard-jwt-secret"),
    ("ADMIN_PASSWORD", "dashboard-admin-password"),
)

SENSITIVE_ENV_KEYS = {
    "MINERU_TOKEN",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "JWT_SECRET",
    "JWT_SECRET_KEY",
    "ADMIN_PASSWORD",
}


def build_secret_ops_status(settings: Settings) -> OpsSecretsStatus:
    project_id = _resolve_project_id(settings)
    gcloud_path, gcloud_error = _resolve_gcloud()
    local_config = [_local_env_status(settings.factory_root, path) for path in _env_files(settings.factory_root)]

    secrets = []
    for env_name, secret_name in SECRET_BINDINGS:
        loaded = bool((os.getenv(env_name) or "").strip())
        accessible, error = _check_secret_access(gcloud_path, project_id, secret_name, gcloud_error)
        secrets.append(
            SecretBindingStatus(
                env=env_name,
                secret=secret_name,
                loaded=loaded,
                accessible=accessible,
                error=error,
            )
        )

    return OpsSecretsStatus(
        project_id=project_id,
        gcloud_path=gcloud_path,
        loader=str(settings.factory_root / "scripts" / "load_secrets.sh"),
        secrets=secrets,
        local_config=local_config,
    )


def _env_files(factory_root: Path) -> tuple[Path, Path]:
    return (factory_root / ".env", factory_root / "web" / ".env")


def _resolve_project_id(settings: Settings) -> str:
    if settings.gcp_project_id:
        return settings.gcp_project_id
    value = (os.getenv("GCP_PROJECT_ID") or "").strip()
    if value:
        return value
    for env_file in _env_files(settings.factory_root):
        value = _read_env_value(env_file, "GCP_PROJECT_ID")
        if value:
            return value
    return ""


def _resolve_gcloud() -> tuple[str, str | None]:
    configured = (os.getenv("GCLOUD_BIN") or "").strip()
    if configured:
        if os.access(configured, os.X_OK):
            return configured, None
        return "", f"GCLOUD_BIN is not executable: {configured}"

    found = shutil.which("gcloud")
    if found:
        return found, None

    home = Path.home() / "google-cloud-sdk" / "bin" / "gcloud"
    if home.exists() and os.access(home, os.X_OK):
        return str(home), None

    fallback = Path("/home/tfisher/google-cloud-sdk/bin/gcloud")
    if fallback.exists() and os.access(fallback, os.X_OK):
        return str(fallback), None

    return "", "gcloud CLI not found; install Google Cloud SDK or set GCLOUD_BIN"


def _check_secret_access(
    gcloud_path: str,
    project_id: str,
    secret_name: str,
    gcloud_error: str | None,
) -> tuple[bool, str | None]:
    if gcloud_error:
        return False, gcloud_error
    if not project_id:
        return False, "GCP_PROJECT_ID is not configured"

    try:
        result = subprocess.run(
            [
                gcloud_path,
                "secrets",
                "versions",
                "access",
                "latest",
                f"--secret={secret_name}",
                f"--project={project_id}",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, "Secret Manager access timed out"
    except OSError as exc:
        return False, _short_error(str(exc))

    if result.returncode != 0:
        return False, _short_error(result.stderr) or f"gcloud exited with code {result.returncode}"
    if not result.stdout.strip():
        return False, "Secret Manager returned an empty value"
    return True, None


def _local_env_status(factory_root: Path, env_file: Path) -> LocalEnvFileStatus:
    exists = env_file.exists()
    mode = None
    secure_mode = False
    sensitive_keys: list[str] = []
    if exists:
        file_stat = env_file.stat()
        mode = oct(stat.S_IMODE(file_stat.st_mode))[2:].zfill(3)
        secure_mode = (stat.S_IMODE(file_stat.st_mode) & 0o077) == 0
        sensitive_keys = _read_sensitive_keys(env_file)
    return LocalEnvFileStatus(
        path=_relative_path(factory_root, env_file),
        exists=exists,
        mode=mode,
        secure_mode=secure_mode,
        sensitive_keys=sensitive_keys,
    )


def _read_sensitive_keys(env_file: Path) -> list[str]:
    keys = set()
    for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", maxsplit=1)[0].strip()
        if key in SENSITIVE_ENV_KEYS:
            keys.add(key)
    return sorted(keys)


def _read_env_value(env_file: Path, key: str) -> str:
    if not env_file.exists():
        return ""
    prefix = f"{key}="
    for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped.split("=", maxsplit=1)[1].strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        return value
    return ""


def _relative_path(factory_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(factory_root))
    except ValueError:
        return str(path)


def _short_error(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return (lines[0] if lines else "")[:300]
