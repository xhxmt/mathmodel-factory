#!/usr/bin/env python3
"""Shared Cloud Run IAM identity-token acquisition for solver clients.

The token is returned only to the caller (or stdout for the small CLI wrapper)
and is never persisted or included in diagnostic messages.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


class IdentityTokenError(RuntimeError):
    """Raised when a Cloud Run identity token cannot be acquired safely."""


SERVICE_ACCOUNT_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
)


def impersonated_service_account() -> str | None:
    target = (os.getenv("CLOUD_SOLVER_IMPERSONATE_SERVICE_ACCOUNT") or "").strip()
    if not target:
        return None
    if not SERVICE_ACCOUNT_PATTERN.fullmatch(target):
        raise IdentityTokenError("CLOUD_SOLVER_IMPERSONATE_SERVICE_ACCOUNT is invalid")
    return target


def resolve_gcloud_binary() -> str:
    configured = (os.getenv("GCLOUD_BIN") or "").strip()
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        raise IdentityTokenError("GCLOUD_BIN is not executable")

    found = shutil.which("gcloud")
    if found:
        return found

    candidates = (
        Path.home() / "google-cloud-sdk" / "bin" / "gcloud",
        Path("/home/tfisher/google-cloud-sdk/bin/gcloud"),
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise IdentityTokenError("gcloud CLI is not available")


def _validate_token(token: str) -> str:
    token = token.strip()
    if not token or any(character.isspace() for character in token):
        raise IdentityTokenError("identity-token provider returned an invalid credential")
    return token


def _google_auth_identity_token(audience: str) -> str:
    try:
        import google.auth
        from google.auth import impersonated_credentials
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import fetch_id_token
    except ImportError as exc:
        raise IdentityTokenError("google-auth identity-token support is unavailable") from exc

    try:
        target = impersonated_service_account()
        if target:
            source_credentials, _project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            access_credentials = impersonated_credentials.Credentials(
                source_credentials=source_credentials,
                target_principal=target,
                target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
                lifetime=600,
            )
            id_credentials = impersonated_credentials.IDTokenCredentials(
                target_credentials=access_credentials,
                target_audience=audience,
                include_email=True,
            )
            id_credentials.refresh(Request())
            return _validate_token(id_credentials.token or "")
        return _validate_token(fetch_id_token(Request(), audience))
    except Exception as exc:
        raise IdentityTokenError("google-auth could not acquire an identity token") from exc


def _run_gcloud_token_command(arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise IdentityTokenError("identity-token command timed out") from exc
    except OSError as exc:
        raise IdentityTokenError("identity-token command could not start") from exc

    if completed.returncode != 0:
        raise IdentityTokenError(
            f"identity-token command failed with exit code {completed.returncode}"
        )
    return _validate_token(completed.stdout)


def _gcloud_identity_token(audience: str) -> str:
    gcloud = resolve_gcloud_binary()
    target = impersonated_service_account()
    impersonation_flag = [f"--impersonate-service-account={target}"] if target else []
    audience_command = [
        gcloud,
        "auth",
        "print-identity-token",
        *impersonation_flag,
        f"--audiences={audience}",
    ]
    try:
        return _run_gcloud_token_command(audience_command)
    except IdentityTokenError:
        # Active user credentials do not always support --audiences. Cloud Run's
        # documented local-development path is the command without that flag.
        return _run_gcloud_token_command(
            [gcloud, "auth", "print-identity-token", *impersonation_flag]
        )


def get_identity_token(audience: str) -> str:
    """Return an ID token for *audience* using the configured shared strategy."""
    normalized_audience = audience.strip().rstrip("/")
    if not normalized_audience.startswith("https://"):
        raise IdentityTokenError("Cloud Run audience must be an https URL")

    backend = (os.getenv("CLOUD_SOLVER_AUTH_BACKEND") or "auto").strip().lower()
    if backend not in {"auto", "google-auth", "gcloud"}:
        raise IdentityTokenError("CLOUD_SOLVER_AUTH_BACKEND must be auto, google-auth, or gcloud")

    errors: list[IdentityTokenError] = []
    providers = {
        "google-auth": _google_auth_identity_token,
        "gcloud": _gcloud_identity_token,
    }
    order = ("google-auth", "gcloud") if backend == "auto" else (backend,)
    for provider_name in order:
        try:
            return providers[provider_name](normalized_audience)
        except IdentityTokenError as exc:
            errors.append(exc)

    if backend == "auto":
        raise IdentityTokenError("no Cloud Run identity-token provider succeeded") from errors[-1]
    raise errors[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acquire a Cloud Run IAM identity token")
    subparsers = parser.add_subparsers(dest="command", required=True)
    token_parser = subparsers.add_parser("token", help="print a token for command substitution")
    token_parser.add_argument("--audience", required=True)
    args = parser.parse_args(argv)

    try:
        token = get_identity_token(args.audience)
    except IdentityTokenError as exc:
        print(f"Cloud Run authentication error: {exc}", file=sys.stderr)
        return 1

    # stdout is intentionally reserved for the credential so shell clients can
    # consume it without writing the value to a file.
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
