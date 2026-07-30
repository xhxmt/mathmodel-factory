from __future__ import annotations

import os
from typing import Protocol

from .types import SolverRequest, SolverSubmission


class CloudTransport(Protocol):
    def submit(self, request: SolverRequest) -> SolverSubmission: ...

    def status(self, external_id: str) -> str: ...

    def cancel(self, external_id: str) -> None: ...


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
