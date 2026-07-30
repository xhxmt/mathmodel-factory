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
) -> ActionResult:
    if action not in {"pause", "resume", "kill"}:
        return ActionResult(False, "", f"unsupported project action: {action}")
    try:
        service = FactoryService(factory_root)
        worker = None
        if action == "resume":
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
