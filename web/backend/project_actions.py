from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from factory_core.service import FactoryService


@dataclass
class ActionResult:
    ok: bool
    stdout: str
    stderr: str

    @property
    def returncode(self) -> int:
        return 0 if self.ok else 1


def run_action(
    factory_root: Path,
    action: str,
    base_name: str,
    expected_revision: int | None = None,
    reason: str | None = None,
    step: int | None = None,
) -> ActionResult:
    if action not in {"pause", "resume", "kill", "grant_reopen", "retry_step"}:
        return ActionResult(False, "", f"unsupported project action: {action}")
    if action in {"grant_reopen", "retry_step"} and not str(reason or "").strip():
        return ActionResult(False, "", f"{action} requires a reason")
    if action == "retry_step" and step is None:
        return ActionResult(False, "", "retry_step requires a step")
    try:
        service = FactoryService(factory_root)
        worker = None
        if action == "retry_step":
            # A fresh attempt budget for the failed Step at the cursor.
            retried = service.retry_step(
                base_name, step_id=int(step), reason=str(reason),
                expected_revision=expected_revision,
            )
            worker = service.start(base_name, expected_revision=retried.revision)
            state = worker.state
        elif action == "grant_reopen":
            # Allow the reopen the budget pause stopped, then resume: recovery
            # rechecks the change set and performs the reopen.
            granted = service.grant_semantic_reopen(
                base_name, reason=str(reason), expected_revision=expected_revision
            )
            state, worker = service.resume_and_start(
                base_name, expected_revision=granted.revision
            )
        elif action == "resume":
            state, worker = service.resume_and_start(
                base_name, expected_revision=expected_revision
            )
        else:
            operation = getattr(service, action)
            state = operation(base_name, expected_revision=expected_revision)
        payload = {"status": state.status.value, "revision": state.revision}
        if worker is not None:
            payload.update(worker_pid=worker.pid, log=str(worker.log_path))
        return ActionResult(
            True,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            ),
            "",
        )
    except Exception as exc:
        return ActionResult(False, "", str(exc))
