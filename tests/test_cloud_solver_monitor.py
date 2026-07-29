from __future__ import annotations

from dataclasses import dataclass

import pytest

from scripts import cloud_solver_monitor as monitor


@dataclass
class FakeResponse:
    status_code: int
    payload: dict

    def json(self):
        return self.payload


def test_health_check_uses_identity_token_without_recording_it(monkeypatch):
    seen = {}
    monkeypatch.setattr(monitor, "get_identity_token", lambda audience: "sensitive-id-token")

    def fake_get(url, *, headers, timeout):
        seen.update(url=url, headers=headers, timeout=timeout)
        return FakeResponse(200, {"status": "healthy"})

    monkeypatch.setattr(monitor.requests, "get", fake_get)

    result = monitor.check_health("https://solver.example")

    assert result["healthy"] is True
    assert seen["headers"] == {"Authorization": "Bearer sensitive-id-token"}
    assert seen["url"] == "https://solver.example/health"
    assert "sensitive-id-token" not in str(result)


@pytest.mark.parametrize("status_code", [401, 403])
def test_health_check_classifies_cloud_run_auth_failure(monkeypatch, status_code):
    monkeypatch.setattr(monitor, "get_identity_token", lambda audience: "id-token")
    monkeypatch.setattr(
        monitor.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(status_code, {}),
    )

    result = monitor.check_health("https://solver.example")

    assert result["healthy"] is False
    assert result["error_category"] == "authentication"
    assert result["error"] == f"Cloud Run authentication failed (HTTP {status_code})"


@pytest.mark.parametrize(
    ("status_code", "category"),
    [(429, "rate_limit"), (503, "service")],
)
def test_health_check_classifies_rate_limit_and_service_failure(
    monkeypatch, status_code, category
):
    monkeypatch.setattr(monitor, "get_identity_token", lambda audience: "id-token")
    monkeypatch.setattr(
        monitor.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(status_code, {}),
    )

    result = monitor.check_health("https://solver.example")

    assert result["healthy"] is False
    assert result["error_category"] == category
    assert f"HTTP {status_code}" in result["error"]


@pytest.mark.parametrize(
    ("exception", "category"),
    [(monitor.requests.Timeout(), "timeout"), (monitor.requests.ConnectionError(), "network")],
)
def test_health_check_classifies_timeout_and_network_failure(monkeypatch, exception, category):
    monkeypatch.setattr(monitor, "get_identity_token", lambda audience: "id-token")

    def fail_request(*args, **kwargs):
        raise exception

    monkeypatch.setattr(monitor.requests, "get", fail_request)

    result = monitor.check_health("https://solver.example")

    assert result["healthy"] is False
    assert result["error_category"] == category


def test_identity_token_failure_is_configuration_error(monkeypatch):
    def fail_token(audience):
        raise monitor.IdentityTokenError("identity token command failed")

    monkeypatch.setattr(monitor, "get_identity_token", fail_token)

    result = monitor.check_health("https://solver.example")

    assert result["healthy"] is False
    assert result["error_category"] == "authentication_config"
    assert "identity token command failed" in result["error"]


def test_authentication_failure_does_not_trigger_ordinary_fallback_count():
    history = monitor.empty_health_history()
    result = {
        "healthy": False,
        "timestamp": "2026-07-29T00:00:00",
        "error_category": "authentication",
        "error": "Cloud Run authentication failed (HTTP 403)",
    }

    monitor.apply_health_result(history, result)

    assert history["consecutive_failures"] == 0
    assert history["last_auth_error"] == result["timestamp"]


def test_service_failure_increments_fallback_count():
    history = monitor.empty_health_history()
    result = {
        "healthy": False,
        "timestamp": "2026-07-29T00:00:00",
        "error_category": "service",
        "error": "Cloud Run service error (HTTP 503)",
    }

    monitor.apply_health_result(history, result)

    assert history["consecutive_failures"] == 1


def test_intentional_quarantine_is_healthy_and_does_not_trigger_fallback(monkeypatch):
    monkeypatch.setattr(monitor, "get_identity_token", lambda audience: "id-token")
    monkeypatch.setattr(
        monitor.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            200,
            {"status": "healthy", "execution_enabled": False},
        ),
    )

    result = monitor.check_health("https://solver.example")
    history = monitor.empty_health_history()
    monitor.apply_health_result(history, result)

    assert result["healthy"] is True
    assert result["execution_available"] is False
    assert result["error_category"] == "quarantine"
    assert history["consecutive_failures"] == 0
    assert history["last_quarantine"] == result["timestamp"]
