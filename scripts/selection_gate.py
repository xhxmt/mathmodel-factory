#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.backend import selection_service


PENDING_EXIT = 10


def _decision_exists(project: Path, gate: str) -> bool:
    from factory_core.storage import SQLiteStateStore

    store = SQLiteStateStore(project)
    if store.exists:
        decision = store.decision(gate)
        if decision is not None or store.contest_policy() is not None:
            return decision is not None
    return (project / "selection" / f"{gate}_decision.json").is_file()


def _require_pending_gate(project: Path, gate: str) -> None:
    from factory_core.storage import SQLiteStateStore

    store = SQLiteStateStore(project)
    if not store.exists or store.contest_policy() is None:
        return
    state = store.load()
    pending_gate = str((state.pending_action or {}).get("gate") or "")
    if pending_gate != gate:
        raise selection_service.SelectionError(
            f"project is not awaiting {gate}; current pending gate is "
            f"{pending_gate or 'none'}"
        )


def _resume(project: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [str(root / "launch_agents.sh"), "resume", project.name],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )


def prepare_step3(
    project: Path, now_epoch: int | None, *, required: bool = False
) -> int:
    from factory_core.joint_modeling import policy, selection_evidence

    if policy(project)["enabled"]:
        required = True
        selection_evidence(project)
    if not required and not selection_service.selection_enabled(project, "step3"):
        return 0
    if _decision_exists(project, "step3"):
        return 0
    selection_service.build_step3_options(project, now_epoch=now_epoch)
    return PENDING_EXIT


def default_step3(project: Path, now_epoch: int | None, no_resume: bool) -> int:
    from factory_core.joint_modeling import policy

    if policy(project)["enabled"]:
        raise selection_service.SelectionError("联合建模必须由人工选模，不能超时自动选择")
    if _decision_exists(project, "step3"):
        return 0
    payload = selection_service.read_selection_request(project, "step3")
    if not payload.get("options"):
        payload = selection_service.build_step3_options(project, now_epoch=now_epoch)
    option_id = str(payload.get("default_option_id") or "")
    aux_id = str(payload.get("default_aux_id") or "NONE")
    if not option_id:
        return 2
    selection_service.write_selection_decision(
        project,
        gate="step3",
        selected_option_id=option_id,
        selected_aux_id=aux_id,
        source="auto-timeout",
        reason="No human selection before the deadline; selected top-ranked option.",
        now_epoch=now_epoch,
    )
    if not no_resume:
        _resume(project)
    return 0


def select_step3(
    project: Path,
    *,
    primary: str,
    aux: str,
    reason: str,
    now_epoch: int | None,
    no_resume: bool,
) -> int:
    if not (project / "selection" / "step3_options.json").is_file():
        selection_service.build_step3_options(project, now_epoch=now_epoch)
    selection_service.write_selection_decision(
        project,
        gate="step3",
        selected_option_id=primary,
        selected_aux_id=aux or "NONE",
        source="manual-cli",
        reason=reason or "Selected from selection_gate.py CLI.",
        now_epoch=now_epoch,
    )
    if not no_resume:
        _resume(project)
    return 0


def wait_default_step3(project: Path, no_resume: bool) -> int:
    payload = selection_service.read_selection_request(project, "step3")
    deadline = int(payload.get("deadline_epoch") or 0)
    delay = max(0, deadline - int(time.time()))
    if delay:
        time.sleep(delay)
    return default_step3(project, now_epoch=None, no_resume=no_resume)


def approve_content_freeze(
    project: Path, *, reason: str, now_epoch: int | None, no_resume: bool
) -> int:
    _require_pending_gate(project, "content_freeze")
    if _decision_exists(project, "content_freeze"):
        return 0
    if not (project / "selection" / "content_freeze_options.json").is_file():
        selection_service.build_content_freeze_options(
            project, now_epoch=now_epoch
        )
    selection_service.write_selection_decision(
        project,
        gate="content_freeze",
        selected_option_id="approve_content_freeze",
        selected_aux_id="NONE",
        source="manual-cli",
        reason=reason or "Content freeze approved from selection_gate.py CLI.",
        now_epoch=now_epoch,
    )
    if not no_resume:
        _resume(project)
    return 0


def approve_delivery_freeze_override(
    project: Path, *, reason: str, now_epoch: int | None, no_resume: bool
) -> int:
    gate = "delivery_freeze_override"
    _require_pending_gate(project, gate)
    if _decision_exists(project, gate):
        return 0
    payload = selection_service.read_selection_request(project, gate)
    resume_after = int(payload.get("resume_after_step", 15))
    if not payload.get("options"):
        selection_service.build_delivery_freeze_override_options(
            project,
            resume_after_step=resume_after,
            now_epoch=now_epoch,
        )
    selection_service.write_selection_decision(
        project,
        gate=gate,
        selected_option_id="approve_delivery_reopen",
        selected_aux_id="NONE",
        source="manual-cli",
        reason=reason or "Post-freeze reopen approved from selection_gate.py CLI.",
        now_epoch=now_epoch,
    )
    if not no_resume:
        _resume(project)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare-step3", "default-step3", "wait-default-step3"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("project")
        command_parser.add_argument("--now-epoch", type=int, default=None)
        command_parser.add_argument("--no-resume", action="store_true")
    select_parser = sub.add_parser("select-step3")
    select_parser.add_argument("project")
    select_parser.add_argument("--primary", required=True)
    select_parser.add_argument("--aux", default="NONE")
    select_parser.add_argument("--reason", default="")
    select_parser.add_argument("--now-epoch", type=int, default=None)
    select_parser.add_argument("--no-resume", action="store_true")
    freeze_parser = sub.add_parser("approve-content-freeze")
    freeze_parser.add_argument("project")
    freeze_parser.add_argument("--reason", default="")
    freeze_parser.add_argument("--now-epoch", type=int, default=None)
    freeze_parser.add_argument("--no-resume", action="store_true")
    override_parser = sub.add_parser("approve-delivery-freeze-override")
    override_parser.add_argument("project")
    override_parser.add_argument("--reason", required=True)
    override_parser.add_argument("--now-epoch", type=int, default=None)
    override_parser.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project = Path(args.project).resolve()
    if args.command == "prepare-step3":
        return prepare_step3(project, args.now_epoch)
    if args.command == "default-step3":
        return default_step3(project, args.now_epoch, args.no_resume)
    if args.command == "wait-default-step3":
        return wait_default_step3(project, args.no_resume)
    if args.command == "select-step3":
        return select_step3(
            project,
            primary=args.primary.strip(),
            aux=args.aux.strip(),
            reason=args.reason.strip(),
            now_epoch=args.now_epoch,
            no_resume=args.no_resume,
        )
    if args.command == "approve-content-freeze":
        return approve_content_freeze(
            project,
            reason=args.reason.strip(),
            now_epoch=args.now_epoch,
            no_resume=args.no_resume,
        )
    if args.command == "approve-delivery-freeze-override":
        return approve_delivery_freeze_override(
            project,
            reason=args.reason.strip(),
            now_epoch=args.now_epoch,
            no_resume=args.no_resume,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
