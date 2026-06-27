from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends

from .auth import get_current_user
from .config import Settings
from .schemas import UserInfo


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
        del current_user
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
        del current_user
        return {
            "use_cloud": os.getenv("USE_CLOUD_SOLVER", "false"),
            "threshold_time": int(os.getenv("CLOUD_THRESHOLD_TIME", "300")),
            "solver_types": os.getenv("CLOUD_SOLVER_TYPES", "python,julia,matlab,R").split(","),
            "project_id": settings.gcp_project_id,
            "region": settings.gcp_region,
            "service_name": settings.gcp_solver_service,
        }

    @router.post("/api/projects/{base_name}/cloud/enable")
    async def enable_cloud_solver(
        base_name: str,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        del current_user
        project_path = settings.ongoing_dir / base_name
        env_file = project_path / ".env.cloud"
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
        return {"status": "enabled", "base_name": base_name}

    @router.post("/api/projects/{base_name}/cloud/disable")
    async def disable_cloud_solver(
        base_name: str,
        current_user: UserInfo = Depends(get_current_user(settings)),
    ):
        del current_user
        env_file = settings.ongoing_dir / base_name / ".env.cloud"
        env_file.unlink(missing_ok=True)
        return {"status": "disabled", "base_name": base_name}

    return router
