#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PAUSE_MARKER = ".paused"
KILL_MARKER = ".killed"
PID_FILE = ".runner.pid"
REGISTRY_FILE = "run_state/process_registry"
TOTAL_STEPS = 16
ROOT = Path(__file__).resolve().parents[2]
RUN_PAPER = ROOT / "run_paper.sh"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.engine import FactoryEngine
from factory_core.domain import RevisionConflict
from factory_core.projections import write_compatibility_projections
from factory_core.storage import SQLiteStateStore


def _load_state_store():
    return importlib.import_module("web.backend.state_store")


def _engine_for(project_dir: Path) -> FactoryEngine | None:
    store = SQLiteStateStore(project_dir)
    if not store.exists or store.load().control_mode != "engine":
        return None
    return FactoryEngine(project_dir, store=store, projector=write_compatibility_projections)


def _pending_action_is_resolved(project_dir: Path, pending: dict) -> bool:
    action_type = pending.get("type")
    gate = pending.get("gate")
    if action_type == "step3_selection":
        return (project_dir / "selection" / f"{gate or 'step3'}_decision.json").is_file()
    if action_type == "human_consultation":
        human_review = project_dir / "human_review.md"
        if not human_review.is_file():
            return False
        text = human_review.read_text(encoding="utf-8", errors="replace")
        if gate:
            pattern = rf"##\s+CONSULT\s+{re.escape(str(gate))}\b[\s\S]*?STATUS:\s*READY"
            return re.search(pattern, text, re.IGNORECASE) is not None
        return "STATUS: READY" in text
    return False


def _registry_path(factory_root: Path) -> Path:
    return factory_root / REGISTRY_FILE


def _project_name(project_dir: Path) -> str:
    return project_dir.name


def _pause_path(project_dir: Path) -> Path:
    return project_dir / PAUSE_MARKER


def _kill_path(project_dir: Path) -> Path:
    return project_dir / KILL_MARKER


def _pid_path(project_dir: Path) -> Path:
    return project_dir / PID_FILE


def _is_pid_live(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_runner(pid: int | None) -> None:
    if pid is None or pid == os.getpid() or not _is_pid_live(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    for _ in range(20):
        if not _is_pid_live(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _read_pid_file(project_dir: Path) -> int | None:
    path = _pid_path(project_dir)
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _get_registered_pid(factory_root: Path, project_name: str) -> int | None:
    registry = _registry_path(factory_root)
    if not registry.is_file():
        return None
    for line in registry.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == project_name:
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def _update_registry_entry(factory_root: Path, project_name: str, pid: int | None) -> None:
    registry = _registry_path(factory_root)
    registry.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if registry.is_file():
        for line in registry.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith(f"{project_name} "):
                lines.append(line)
    if pid is not None:
        lines.append(f"{project_name} {pid}")
    registry.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _remove_registry_entry(factory_root: Path, project_name: str) -> None:
    _update_registry_entry(factory_root, project_name, None)


def _cleanup_project_lock(project_dir: Path) -> None:
    (project_dir / ".runner.lock.info").unlink(missing_ok=True)
    (project_dir / ".runner.lock" / "info").unlink(missing_ok=True)
    try:
        (project_dir / ".runner.lock").rmdir()
    except OSError:
        pass


def _read_timestamp(project_dir: Path) -> str:
    checkpoint = project_dir / "checkpoint.md"
    if not checkpoint.is_file():
        return ""
    content = checkpoint.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"Timestamp\*{0,2}\s*[:：]\s*(.+)", content)
    return match.group(1).strip() if match else ""


def _infer_checkpoint_step(project_dir: Path) -> str:
    checkpoint = project_dir / "checkpoint.md"
    if not checkpoint.is_file():
        return "?"
    content = checkpoint.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"Last completed step\*{0,2}\s*[:：]\s*(-?\d+)", content)
    return match.group(1) if match else "?"


def _infer_step(project_dir: Path) -> str:
    result = subprocess.run(
        [str(RUN_PAPER), "--infer-step", str(project_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return "?"
    value = result.stdout.strip()
    return value or "?"


def _status_process_label(factory_root: Path, project_dir: Path) -> str:
    project_name = _project_name(project_dir)
    if _kill_path(project_dir).is_file():
        return "KILLED"
    if _pause_path(project_dir).is_file():
        return "PAUSED"
    if (project_dir / ".awaiting_consultation").is_file():
        content = (project_dir / ".awaiting_consultation").read_text(encoding="utf-8", errors="replace")
        match = re.search(r"STEP:(\d+)", content)
        return f"CONSULT({match.group(1) if match else '?'})"
    heartbeat = project_dir / ".heartbeat"
    if heartbeat.is_file():
        hb = heartbeat.read_text(encoding="utf-8", errors="replace")
        if hb.startswith("AWAITING_STEP8_5:"):
            return "AWAIT_8.5"
    pid = _read_pid_file(project_dir)
    if pid is None:
        pid = _get_registered_pid(factory_root, project_name)
    if _is_pid_live(pid):
        return f"RUNNING({pid})"
    if pid is not None:
        return f"EXITED({pid})"
    return "stopped"


def _project_row(factory_root: Path, project_dir: Path) -> str:
    return "  %-25s %-10s %-12s %-18s %s" % (
        _project_name(project_dir),
        _infer_step(project_dir),
        _infer_checkpoint_step(project_dir),
        _status_process_label(factory_root, project_dir),
        _read_timestamp(project_dir),
    )


def _iter_projects(search_dir: Path):
    if not search_dir.is_dir():
        return
    for child in sorted(search_dir.iterdir()):
        if child.is_dir() and (child / "checkpoint.md").is_file():
            yield child


def render_status(factory_root: str | Path) -> str:
    root = Path(factory_root)
    lines = ["", "=== Modeling Factory Status ==="]
    for section, folder in (("ONGOING", root / "ongoing"), ("COMPLETE", root / "complete")):
        projects = list(_iter_projects(folder))
        if not projects:
            continue
        lines.extend(
            [
                "",
                f"  {section}",
                "  %-25s %-10s %-12s %-18s %s" % ("PROJECT", "INFERRED", "CHECKPOINT", "PROCESS", "TIMESTAMP"),
                "  %-25s %-10s %-12s %-18s %s" % ("-------", "--------", "----------", "-------", "---------"),
            ]
        )
        lines.extend(_project_row(root, project) for project in projects)
    lines.append("")
    return "\n".join(lines)


def kill_project(
    project_dir: str | Path,
    *,
    factory_root: str | Path | None = None,
    expected_revision: int | None = None,
) -> dict:
    project = Path(project_dir)
    engine = _engine_for(project)
    if engine is not None:
        state = engine.get_state()
        if expected_revision is not None and expected_revision != state.revision:
            raise RevisionConflict(
                f"expected revision {expected_revision}, found {state.revision}"
            )
        updated = engine.kill(
            expected_revision=state.revision
            if expected_revision is None
            else expected_revision
        )
        _terminate_runner(state.runner_pid)
        _pid_path(project).unlink(missing_ok=True)
        _cleanup_project_lock(project)
        if factory_root is not None:
            _remove_registry_entry(Path(factory_root), _project_name(project))
        return {
            "project_dir": str(project),
            "killed": True,
            "revision": updated.revision,
            "control_mode": "engine",
        }
    (_kill_path(project)).touch()
    _pid_path(project).unlink(missing_ok=True)
    _cleanup_project_lock(project)
    (project / ".heartbeat").unlink(missing_ok=True)
    if factory_root is not None:
        _remove_registry_entry(Path(factory_root), _project_name(project))
    return {"project_dir": str(project), "killed": True}


def pause_project(
    project_dir: str | Path,
    base_name: str,
    *,
    factory_root: str | Path | None = None,
    expected_revision: int | None = None,
) -> dict:
    project = Path(project_dir)
    engine = _engine_for(project)
    if engine is not None:
        state = engine.get_state()
        if expected_revision is not None and expected_revision != state.revision:
            raise RevisionConflict(
                f"expected revision {expected_revision}, found {state.revision}"
            )
        updated = engine.pause(
            expected_revision=state.revision
            if expected_revision is None
            else expected_revision
        )
        _terminate_runner(state.runner_pid)
        if factory_root is not None:
            _remove_registry_entry(Path(factory_root), base_name)
        _cleanup_project_lock(project)
        return {
            "project_dir": str(project),
            "paused": True,
            "revision": updated.revision,
            "control_mode": "engine",
        }
    (_pause_path(project)).touch()
    pid = _read_pid_file(project)
    if pid is None and factory_root is not None:
        pid = _get_registered_pid(Path(factory_root), base_name)
    if _is_pid_live(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(10):
            if not _is_pid_live(pid):
                break
            time.sleep(1)
        if _is_pid_live(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    if factory_root is not None:
        _remove_registry_entry(Path(factory_root), base_name)
    _pid_path(project).unlink(missing_ok=True)
    _cleanup_project_lock(project)
    (project / ".heartbeat").unlink(missing_ok=True)
    return {"project_dir": str(project), "paused": True}


def _submit_project(project_dir: Path, base_name: str, factory_root: Path) -> dict:
    current_pid = _get_registered_pid(factory_root, base_name)
    if _is_pid_live(current_pid):
        return {"project_dir": str(project_dir), "started": False, "pid": current_pid, "already_running": True}

    (factory_root / "logs").mkdir(parents=True, exist_ok=True)
    (project_dir / "logs").mkdir(parents=True, exist_ok=True)
    out = factory_root / "logs" / f"{base_name}_{time.strftime('%Y%m%d_%H%M%S')}.out"
    fh = out.open("ab")
    proc = subprocess.Popen(
        [str(RUN_PAPER), str(project_dir)],
        cwd=str(factory_root),
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _pid_path(project_dir).write_text(f"{proc.pid}\n", encoding="utf-8")
    _update_registry_entry(factory_root, base_name, proc.pid)
    fh.close()
    return {"project_dir": str(project_dir), "started": True, "pid": proc.pid, "already_running": False}


def resume_project(
    project_dir: str | Path,
    base_name: str,
    *,
    factory_root: str | Path | None = None,
    start_runner: bool = True,
    expected_revision: int | None = None,
) -> dict:
    project = Path(project_dir)
    engine = _engine_for(project)
    if engine is not None:
        state = engine.get_state()
        if expected_revision is not None and expected_revision != state.revision:
            raise RevisionConflict(
                f"expected revision {expected_revision}, found {state.revision}"
            )
        if state.pending_action is not None:
            if not _pending_action_is_resolved(project, state.pending_action):
                raise RuntimeError(
                    f"{base_name} still has unresolved action {state.pending_action.get('type')}"
                )
            state = engine.resolve_action(
                {"source": "compatibility_artifact"}, expected_revision=state.revision
            )
        state = engine.resume(expected_revision=state.revision)
        result = {
            "project_dir": str(project),
            "resumed": True,
            "started": False,
            "revision": state.revision,
            "control_mode": "engine",
        }
        if start_runner:
            if factory_root is None:
                raise RuntimeError("factory_root is required when start_runner=True")
            result.update(_submit_project(project, base_name, Path(factory_root)))
        return result
    if _kill_path(project).is_file():
        raise RuntimeError(f"{base_name} was killed by the viability gate and will not be resumed")
    _pause_path(project).unlink(missing_ok=True)
    _cleanup_project_lock(project)
    (project / ".heartbeat").unlink(missing_ok=True)

    result = {"project_dir": str(project), "resumed": True, "started": False}
    if start_runner:
        if factory_root is None:
            raise RuntimeError("factory_root is required when start_runner=True")
        result.update(_submit_project(project, base_name, Path(factory_root)))
    return result


def project_summary(project_dir: str | Path, base_name: str) -> dict:
    state_store = _load_state_store()
    return state_store.read_runtime_status(project_dir, base_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    kill_cmd = sub.add_parser("kill")
    kill_cmd.add_argument("project_dir")
    kill_cmd.add_argument("--factory-root", default=None)
    kill_cmd.add_argument("--expected-revision", type=int, default=None)

    pause_cmd = sub.add_parser("pause")
    pause_cmd.add_argument("project_dir")
    pause_cmd.add_argument("base_name")
    pause_cmd.add_argument("--factory-root", default=None)
    pause_cmd.add_argument("--expected-revision", type=int, default=None)

    resume_cmd = sub.add_parser("resume")
    resume_cmd.add_argument("project_dir")
    resume_cmd.add_argument("base_name")
    resume_cmd.add_argument("--factory-root", default=None)
    resume_cmd.add_argument("--no-start", action="store_true")
    resume_cmd.add_argument("--expected-revision", type=int, default=None)

    summary_cmd = sub.add_parser("summary")
    summary_cmd.add_argument("project_dir")
    summary_cmd.add_argument("base_name")

    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("--factory-root", required=True)

    args = parser.parse_args()

    if args.cmd == "kill":
        print(
            json.dumps(
                kill_project(
                    args.project_dir,
                    factory_root=args.factory_root,
                    expected_revision=args.expected_revision,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    if args.cmd == "pause":
        print(
            json.dumps(
                pause_project(
                    args.project_dir,
                    args.base_name,
                    factory_root=args.factory_root,
                    expected_revision=args.expected_revision,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    if args.cmd == "resume":
        print(
            json.dumps(
                resume_project(
                    args.project_dir,
                    args.base_name,
                    factory_root=args.factory_root,
                    start_runner=not args.no_start,
                    expected_revision=args.expected_revision,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    if args.cmd == "summary":
        print(json.dumps(project_summary(args.project_dir, args.base_name), ensure_ascii=False))
        return 0

    print(render_status(args.factory_root))
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)
