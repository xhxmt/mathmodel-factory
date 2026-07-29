from __future__ import annotations

import pytest

from scripts import cloud_solver_auth as auth


def test_auto_auth_prefers_google_auth_with_cloud_run_audience(monkeypatch):
    seen = []
    monkeypatch.delenv("CLOUD_SOLVER_AUTH_BACKEND", raising=False)
    monkeypatch.setattr(
        auth,
        "_google_auth_identity_token",
        lambda audience: seen.append(("google-auth", audience)) or "token-value",
    )
    monkeypatch.setattr(
        auth,
        "_gcloud_identity_token",
        lambda audience: seen.append(("gcloud", audience)) or "fallback-token",
    )

    token = auth.get_identity_token("https://solver.example/")

    assert token == "token-value"
    assert seen == [("google-auth", "https://solver.example")]


def test_auto_auth_falls_back_to_gcloud_without_leaking_provider_errors(monkeypatch):
    monkeypatch.delenv("CLOUD_SOLVER_AUTH_BACKEND", raising=False)

    def fail_google(_audience):
        raise auth.IdentityTokenError("credential detail that must not escape")

    monkeypatch.setattr(auth, "_google_auth_identity_token", fail_google)
    monkeypatch.setattr(auth, "_gcloud_identity_token", lambda audience: "fallback-token")

    assert auth.get_identity_token("https://solver.example") == "fallback-token"


@pytest.mark.parametrize("audience", ["", "http://solver.example", "solver.example"])
def test_auth_rejects_invalid_audience(audience):
    with pytest.raises(auth.IdentityTokenError, match="https URL"):
        auth.get_identity_token(audience)


def test_auth_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("CLOUD_SOLVER_AUTH_BACKEND", "password-file")

    with pytest.raises(auth.IdentityTokenError, match="auto, google-auth, or gcloud"):
        auth.get_identity_token("https://solver.example")


def test_token_validation_rejects_whitespace_and_empty_values():
    for token in ("", "   ", "header payload"):
        with pytest.raises(auth.IdentityTokenError):
            auth._validate_token(token)


def test_gcloud_auth_impersonates_dedicated_invoker_without_writing_a_key(monkeypatch):
    seen = []
    target = "solver-invoker@level-night-476302-k0.iam.gserviceaccount.com"
    monkeypatch.setenv("CLOUD_SOLVER_IMPERSONATE_SERVICE_ACCOUNT", target)
    monkeypatch.setattr(auth, "resolve_gcloud_binary", lambda: "/safe/gcloud")
    monkeypatch.setattr(
        auth,
        "_run_gcloud_token_command",
        lambda arguments: seen.append(arguments) or "token-value",
    )

    assert auth._gcloud_identity_token("https://solver.example") == "token-value"
    assert seen == [
        [
            "/safe/gcloud",
            "auth",
            "print-identity-token",
            f"--impersonate-service-account={target}",
            "--audiences=https://solver.example",
        ]
    ]


def test_auth_rejects_malformed_impersonation_target(monkeypatch):
    monkeypatch.setenv("CLOUD_SOLVER_IMPERSONATE_SERVICE_ACCOUNT", "../../service-account.json")

    with pytest.raises(auth.IdentityTokenError, match="is invalid"):
        auth.impersonated_service_account()
