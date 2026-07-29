#!/usr/bin/env python3
"""Dependency-free safety preflight for the Cloud Solver build contract."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def _section(text: str, start_marker: str, end_marker: str | None = None) -> str:
    start = text.index(start_marker)
    if end_marker is None:
        return text[start:]
    return text[start : text.index(end_marker, start)]


def _yaml_list_tokens(section: str) -> list[str]:
    return re.findall(r"^\s*-\s*'([^']*)'\s*$", section, flags=re.MULTILINE)


def _argument_after(tokens: list[str], flag: str) -> str | None:
    try:
        return tokens[tokens.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def validate_cloudbuild_text(text: str) -> list[str]:
    errors: list[str] = []
    expected_substitutions = {
        "_REGION": "europe-west4",
        "_REPOSITORY": "solver-images",
        "_SERVICE_NAME": "solver-api",
        "_RUNTIME_SERVICE_ACCOUNT": "solver-runner",
    }
    for name, expected in expected_substitutions.items():
        match = re.search(rf"^\s{{2}}{re.escape(name)}:\s*'([^']+)'\s*$", text, re.MULTILINE)
        if not match:
            errors.append(f"missing substitution: {name}")
        elif match.group(1) != expected:
            errors.append(f"unexpected substitution value: {name}")

    try:
        build_section = _section(text, "# Step 1: Build", "# Step 2: Push")
        deploy_section = _section(text, "# Step 3: Deploy", "# Step 4: Record")
        images_section = _section(text, "images:")
    except ValueError:
        return errors + ["required build/deploy/images sections are missing"]

    immutable_image = (
        "${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_REPOSITORY}/solver-api:${BUILD_ID}"
    )
    latest_image = "${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_REPOSITORY}/solver-api:latest"
    expected_images = {immutable_image, latest_image}
    build_tokens = _yaml_list_tokens(build_section)
    built_images = {
        build_tokens[index + 1]
        for index, token in enumerate(build_tokens[:-1])
        if token == "-t"
    }
    declared_images = set(_yaml_list_tokens(images_section))
    deploy_tokens = _yaml_list_tokens(deploy_section)
    deploy_image = _argument_after(deploy_tokens, "--image")
    runtime_account = _argument_after(deploy_tokens, "--service-account")
    if built_images != expected_images:
        errors.append("build step must create exactly the BUILD_ID and browse-only latest images")
    if declared_images != built_images:
        errors.append("images field does not exactly match the tags produced by the build step")
    if deploy_image != immutable_image:
        errors.append("Cloud Run deploy does not reference the BUILD_ID image")
    if deploy_image == latest_image:
        errors.append("Cloud Run production deploy must not reference latest")
    if runtime_account != "${_RUNTIME_SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com":
        errors.append("Cloud Run deployment does not use the declared runtime service account")
    if "--no-allow-unauthenticated" not in deploy_section:
        errors.append("Cloud Run deployment is not explicitly private")
    if "SOLVER_EXECUTION_ENABLED=false" not in deploy_section:
        errors.append("Cloud Run deployment does not preserve execution quarantine")
    if "requestedVerifyOption: VERIFIED" not in text:
        errors.append("verified build provenance is not requested")
    if "spec.containers[0].image" not in text or "${BUILD_ID}" not in text:
        errors.append("deployment metadata does not record revision and immutable image")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Cloud Solver Cloud Build configuration")
    parser.add_argument("config", nargs="?", default="cloud/cloudbuild.yaml")
    args = parser.parse_args()
    config = Path(args.config)
    errors = validate_cloudbuild_text(config.read_text(encoding="utf-8"))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Cloud Solver build configuration is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
