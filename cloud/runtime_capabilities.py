"""Single source of truth for Cloud Solver runtime capabilities."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import shutil
from pathlib import Path
from typing import Any


CAPABILITIES_FILE = Path(__file__).with_name("runtime_capabilities.json")


def load_runtime_manifest(path: Path | None = None) -> dict[str, Any]:
    manifest_path = path or CAPABILITIES_FILE
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("runtimes"), dict):
        raise RuntimeError("invalid Cloud Solver runtime capability manifest")
    return payload


def enabled_solver_types(path: Path | None = None) -> tuple[str, ...]:
    manifest = load_runtime_manifest(path)
    return tuple(
        name
        for name, runtime in manifest["runtimes"].items()
        if runtime.get("enabled") is True
    )


def runtime_capability_payload(path: Path | None = None) -> dict[str, Any]:
    manifest = load_runtime_manifest(path)
    runtime_details: dict[str, dict[str, Any]] = {}
    for name, declared in manifest["runtimes"].items():
        details = dict(declared)
        executable = details.get("executable")
        if details.get("enabled") is True:
            details["installed"] = bool(executable and shutil.which(executable))
            if name == "python":
                details["version"] = platform.python_version()
        runtime_details[name] = details

    package_versions: dict[str, str | None] = {}
    for package in manifest.get("python_packages", []):
        try:
            package_versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            package_versions[package] = None

    return {
        "schema_version": manifest["schema_version"],
        "image_family": manifest["image_family"],
        "available_solvers": list(enabled_solver_types(path)),
        "runtimes": runtime_details,
        "python_packages": package_versions,
    }


def assert_enabled_runtimes_installed(path: Path | None = None) -> None:
    payload = runtime_capability_payload(path)
    missing = [
        name
        for name in payload["available_solvers"]
        if not payload["runtimes"][name].get("installed")
    ]
    missing_packages = [
        name for name, version in payload["python_packages"].items() if version is None
    ]
    if missing or missing_packages:
        parts = []
        if missing:
            parts.append(f"runtimes={','.join(missing)}")
        if missing_packages:
            parts.append(f"packages={','.join(missing_packages)}")
        raise RuntimeError("capability manifest does not match image: " + " ".join(parts))
