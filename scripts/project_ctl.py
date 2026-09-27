#!/usr/bin/env python3
"""Compatibility CLI for project lifecycle control.

Native projects call FactoryService directly. Historical projects remain
read-only and are never routed to an alternate execution engine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.domain import FactoryCoreError
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore


def _engine_owned(project: Path) -> bool:
    store = SQLiteStateStore(project)
    return store.exists and store.load().control_mode == "engine"


def _service(project: Path, factory_root: str | Path | None = None) -> FactoryService:
    root = Path(factory_root).resolve() if factory_root else ROOT
    return FactoryService(root)


def _is_pid_live(pid: int | None) -> bool:
    return FactoryService._pid_is_live(pid) if pid is not None else False


def _terminate_runner(pid: int | None, expected_identity: str | None = None) -> None:
    FactoryService._terminate_runner(pid, expected_identity)


def kill_project(
    project_dir: str | Path,
    *,
    factory_root: str | Path | None = None,
    expected_revision: int | None = None,
) -> dict:
    project = Path(project_dir).resolve()
    if not _engine_owned(project):
        raise FactoryCoreError("NATIVE_WORKFLOW_REQUIRED: historical project control is in paper_new")
    service = _service(project, factory_root)
    state = service.kill(
        project, expected_revision=expected_revision
    )
    return {
        "project_dir": str(project),
        "killed": True,
        "control_mode": "engine",
        "revision": state.revision,
    }


def pause_project(
    project_dir: str | Path,
    base_name: str,
    *,
    factory_root: str | Path | None = None,
    expected_revision: int | None = None,
) -> dict:
    project = Path(project_dir).resolve()
    if not _engine_owned(project):
        raise FactoryCoreError("NATIVE_WORKFLOW_REQUIRED: historical project control is in paper_new")
    service = _service(project, factory_root)
    state = service.pause(
        project,
        expected_revision=expected_revision,
        subcode="OPERATOR",
        actor="operator",
    )
    return {
        "project_dir": str(project),
        "paused": True,
        "control_mode": "engine",
        "revision": state.revision,
    }


def resume_project(
    project_dir: str | Path,
    base_name: str,
    *,
    factory_root: str | Path | None = None,
    start_runner: bool = True,
    expected_revision: int | None = None,
) -> dict:
    project = Path(project_dir).resolve()
    if not _engine_owned(project):
        raise FactoryCoreError("NATIVE_WORKFLOW_REQUIRED: historical project control is in paper_new")
    service = _service(project, factory_root)
    try:
        if start_runner:
            state, handle = service.resume_and_start(
                project, expected_revision=expected_revision
            )
        else:
            state = service.resume(
                project,
                expected_revision=expected_revision,
                subcode="OPERATOR",
                actor="operator",
            )
    except FactoryCoreError as exc:
        if "pending action" in str(exc):
            raise RuntimeError("unresolved action") from exc
        raise
    result = {
        "project_dir": str(project),
        "resumed": True,
        "started": False,
        "control_mode": "engine",
        "revision": state.revision,
    }
    if start_runner:
        result.update(started=True, pid=handle.pid, log=str(handle.log_path))
    return result


def project_summary(project_dir: str | Path, base_name: str) -> dict:
    from web.backend.state_store import read_runtime_status

    return read_runtime_status(project_dir, base_name)


def render_status(factory_root: str | Path) -> str:
    root = Path(factory_root).resolve()
    lines = ["PROJECT                         SCOPE       STATUS                    STAGE/SUBTASK"]
    for scope in ("ongoing", "complete"):
        for project in sorted((root / scope).glob("*")):
            if not project.is_dir():
                continue
            store = SQLiteStateStore(project)
            if not store.exists:
                lines.append(f"{project.name:<31} {scope:<11} HISTORICAL_READ_ONLY")
                continue
            try:
                state = store.load()
                status = state.status.value
                if state.control_mode != "engine" or state.runtime_generation != "native_v2":
                    status = "HISTORICAL_READ_ONLY"
                elif state.scheduler_generation != "stage_v1":
                    status = "STAGE_ACTIVATION_REQUIRED"
                cursor = f"{state.active_stage or '-'} / {state.active_subtask or '-'}"
                lines.append(f"{project.name:<31} {scope:<11} {status:<25} {cursor}")
            except (FactoryCoreError, OSError, ValueError) as exc:
                lines.append(f"{project.name:<31} {scope:<11} UNAVAILABLE: {exc}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    kill = sub.add_parser("kill")
    kill.add_argument("project_dir")
    kill.add_argument("--factory-root")
    kill.add_argument("--expected-revision", type=int)
    pause = sub.add_parser("pause")
    pause.add_argument("project_dir")
    pause.add_argument("base_name")
    pause.add_argument("--factory-root")
    pause.add_argument("--expected-revision", type=int)
    resume = sub.add_parser("resume")
    resume.add_argument("project_dir")
    resume.add_argument("base_name")
    resume.add_argument("--factory-root")
    resume.add_argument("--no-start", action="store_true")
    resume.add_argument("--expected-revision", type=int)
    summary = sub.add_parser("summary")
    summary.add_argument("project_dir")
    summary.add_argument("base_name")
    status = sub.add_parser("status")
    status.add_argument("--factory-root", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "kill":
        value = kill_project(
            args.project_dir,
            factory_root=args.factory_root,
            expected_revision=args.expected_revision,
        )
    elif args.cmd == "pause":
        value = pause_project(
            args.project_dir,
            args.base_name,
            factory_root=args.factory_root,
            expected_revision=args.expected_revision,
        )
    elif args.cmd == "resume":
        value = resume_project(
            args.project_dir,
            args.base_name,
            factory_root=args.factory_root,
            start_runner=not args.no_start,
            expected_revision=args.expected_revision,
        )
    elif args.cmd == "summary":
        value = project_summary(args.project_dir, args.base_name)
    else:
        print(render_status(args.factory_root))
        return 0
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FactoryCoreError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
