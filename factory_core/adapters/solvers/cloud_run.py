from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .types import SolverRequest, SolverSubmission


class CloudTransport(Protocol):
    def submit(self, request: SolverRequest) -> SolverSubmission: ...

    def status(self, external_id: str) -> str: ...

    def cancel(self, external_id: str) -> None: ...


class CloudRunHttpTransport:
    """IAM-authenticated Cloud Run transport shared by CLI workers and Web."""

    def __init__(
        self,
        service_url: str | None = None,
        *,
        token_provider: Callable[[str], str] | None = None,
        opener: Callable[..., object] = urlopen,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.service_url = (service_url or os.getenv("CLOUD_SOLVER_URL") or "").rstrip(
            "/"
        )
        self._token_provider = token_provider
        self._opener = opener
        self.timeout_seconds = timeout_seconds

    def submit(self, request: SolverRequest) -> SolverSubmission:
        if request.args:
            raise ValueError("Cloud Run solver transport does not support argv")
        payload = self._request_json(
            "POST",
            f"/solve/{request.runtime}",
            {
                "job_id": request.job_id,
                "solver_type": request.runtime,
                "script_content": request.script.read_text(encoding="utf-8"),
                "script_name": request.script.name,
                "max_time": request.max_time_seconds,
                "working_files": {},
                "env_vars": request.env,
            },
        )
        external_id = str(payload.get("job_id") or request.job_id)
        return SolverSubmission(
            external_id=external_id,
            status=self._normalize_status(str(payload.get("status") or "running")),
            result_refs=self._result_refs(payload),
        )

    def status(self, external_id: str) -> str:
        payload = self._request_json("GET", f"/jobs/{external_id}/status")
        return self._normalize_status(str(payload.get("status") or "failed"))

    def cancel(self, external_id: str) -> None:
        self._request_json("DELETE", f"/jobs/{external_id}")

    def _request_json(
        self, method: str, path: str, payload: dict | None = None
    ) -> dict:
        if not self.service_url.startswith("https://"):
            raise RuntimeError(
                "CLOUD_SOLVER_URL must be an https URL before cloud execution is enabled"
            )
        token_provider = self._token_provider
        if token_provider is None:
            from scripts.cloud_solver_auth import get_identity_token

            token_provider = get_identity_token
        token = token_provider(self.service_url)
        body = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            if payload is not None
            else None
        )
        request = Request(
            self.service_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            response = self._opener(request, timeout=self.timeout_seconds)
            with response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(
                f"Cloud Run solver request failed with HTTP {exc.code}"
            ) from exc
        except (URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Cloud Run solver request failed") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("Cloud Run solver returned an invalid response")
        return decoded

    @staticmethod
    def _normalize_status(status: str) -> str:
        return "running" if status in {"queued", "submitted"} else status

    @staticmethod
    def _result_refs(payload: dict) -> dict[str, str]:
        refs = {
            "stdout": payload.get("stdout_url"),
            "stderr": payload.get("stderr_url"),
            "manifest": payload.get("manifest_url"),
        }
        return {key: str(value) for key, value in refs.items() if value}


class CloudRunSolverBackend:
    name = "cloud_run"

    def __init__(self, transport: CloudTransport, *, quarantined: bool | None = None):
        self.transport = transport
        self.quarantined = (
            os.getenv("CLOUD_SOLVER_QUARANTINED", "true").lower() == "true"
            if quarantined is None
            else quarantined
        )

    def submit(self, request: SolverRequest) -> SolverSubmission:
        if self.quarantined:
            raise RuntimeError("cloud solver execution is quarantined")
        return self.transport.submit(request)

    def status(self, job: dict) -> str:
        external_id = str(job.get("external_id") or "")
        if not external_id:
            return "failed"
        return self.transport.status(external_id)

    def cancel(self, job: dict) -> None:
        external_id = str(job.get("external_id") or "")
        if external_id:
            self.transport.cancel(external_id)
