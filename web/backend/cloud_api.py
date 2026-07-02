from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from .access_control import require_admin, require_project_access
from .auth import get_current_user
from .config import Settings
from .schemas import UserInfo


CLOUD_ENV_NAME = ".env.cloud"


def _project_cloud_env_file(settings: Settings, base_name: str) -> Path:
    for root in (settings.ongoing_dir, settings.complete_dir):
        candidate = root / base_name
        if candidate.is_dir():
            return candidate / CLOUD_ENV_NAME
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project not found: {base_name}")


def _env_flag_enabled(path: Path, key: str) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == f"{key}=true":
            return True
    return False


def project_cloud_config(settings: Settings, base_name: str) -> dict:
    env_file = _project_cloud_env_file(settings, base_name)
    return {
        "enabled": _env_flag_enabled(env_file, "USE_CLOUD_SOLVER"),
        "env_file": str(env_file),
        "threshold_time": 300,
        "solver_types": ["python", "julia", "matlab", "R"],
        "project_id": settings.gcp_project_id or "level-night-476302-k0",
        "region": settings.gcp_region,
        "service_name": settings.gcp_solver_service,
    }


def set_project_cloud_enabled(settings: Settings, base_name: str, enabled: bool) -> dict:
    env_file = _project_cloud_env_file(settings, base_name)
    if enabled:
        env_file.write_text(
            "\n".join(
                [
                    "# Cloud Run solver configuration",
                    "USE_CLOUD_SOLVER=true",
                    "CLOUD_THRESHOLD_TIME=300",
                    "CLOUD_SOLVER_TYPES=python,julia,matlab,R",
                    f"GCP_PROJECT_ID={settings.gcp_project_id or 'level-night-476302-k0'}",
                    f"GCP_REGION={settings.gcp_region}",
                    f"GCP_SOLVER_SERVICE={settings.gcp_solver_service}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    else:
        env_file.unlink(missing_ok=True)
    return project_cloud_config(settings, base_name)


def _resolve_gcloud_binary() -> str | None:
    env_override = (os.getenv("GCLOUD_BIN") or "").strip()
    if env_override:
        return env_override

    path_hit = shutil.which("gcloud")
    if path_hit:
        return path_hit

    home_sdk = Path.home() / "google-cloud-sdk" / "bin" / "gcloud"
    if home_sdk.is_file():
        return str(home_sdk)

    tfisher_sdk = Path("/home/tfisher/google-cloud-sdk/bin/gcloud")
    if tfisher_sdk.is_file():
        return str(tfisher_sdk)

    return None


def create_cloud_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/api/cloud/status")
    async def cloud_status(current_user: UserInfo = Depends(get_current_user(settings))):
        require_admin(current_user)
        try:
            gcloud_bin = _resolve_gcloud_binary()
            if not gcloud_bin:
                return {"available": False, "error": "gcloud CLI not installed"}

            result = subprocess.run(
                [gcloud_bin, "version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                return {"available": False, "error": "gcloud CLI not installed"}

            project_id = settings.gcp_project_id or "level-night-476302-k0"
            region = settings.gcp_region
            service_name = settings.gcp_solver_service
            result = subprocess.run(
                [
                    gcloud_bin,
                    "run",
                    "services",
                    "describe",
                    service_name,
                    f"--region={region}",
                    f"--project={project_id}",
                    "--format=json",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                return {
                    "available": False,
                    "region": region,
                    "project_id": project_id,
                    "error": f"Service {service_name} not found",
                }

            service_info = json.loads(result.stdout)
            annotations = (
                service_info.get("spec", {})
                .get("template", {})
                .get("metadata", {})
                .get("annotations", {})
            )
            max_instances = int(annotations.get("autoscaling.knative.dev/maxScale", "10"))
            return {
                "available": True,
                "region": region,
                "project_id": project_id,
                "service_name": service_name,
                "max_instances": max_instances,
                "solvers": ["python", "julia", "matlab", "R"],
            }
        except subprocess.TimeoutExpired:
            return {"available": False, "error": "gcloud command timeout"}
        except Exception as exc:
            return {"available": False, "error": str(exc)}

    @router.get("/api/cloud/config")
    async def cloud_config(current_user: UserInfo = Depends(get_current_user(settings))):
        require_admin(current_user)
        return {
            "use_cloud": os.getenv("USE_CLOUD_SOLVER", "false"),
            "threshold_time": int(os.getenv("CLOUD_THRESHOLD_TIME", "300")),
            "solver_types": os.getenv("CLOUD_SOLVER_TYPES", "python,julia,matlab,R").split(","),
            "project_id": settings.gcp_project_id,
            "region": settings.gcp_region,
            "service_name": settings.gcp_solver_service,
        }

    @router.get("/api/projects/{base_name}/cloud/config")
    async def get_project_cloud_config(
        base_name: str,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        return project_cloud_config(settings, base_name)

    @router.post("/api/projects/{base_name}/cloud/enable")
    async def enable_cloud_solver(
        base_name: str,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        config = set_project_cloud_enabled(settings, base_name, True)
        return {"status": "enabled", "base_name": base_name, "config": config}

    @router.post("/api/projects/{base_name}/cloud/disable")
    async def disable_cloud_solver(
        base_name: str,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        require_project_access(settings, current_user, base_name)
        config = set_project_cloud_enabled(settings, base_name, False)
        return {"status": "disabled", "base_name": base_name, "config": config}

    return router
