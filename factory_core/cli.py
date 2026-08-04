from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

from .adapters.legacy import LegacyArtifactValidator
from .domain import FactoryCoreError, MigrationConflict, WorkflowStatus
from .engine import FactoryEngine
from .migration import MigrationReport
from .projections import runtime_payload, write_compatibility_projections
from .storage import SQLiteStateStore
from .service import FactoryService, wait_for_worker_ready
from scripts.solver_job_receipt import (
    ReceiptError,
    bind_event_stream,
    build_evidence,
    receipt_paths,
)


CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("FACTORY", CODE_ROOT)).resolve()
LEGACY_RUNNER = CODE_ROOT / "factory_core" / "adapters" / "legacy_runner.sh"


def _engine(project: Path) -> FactoryEngine:
    return FactoryService(ROOT).engine(project)


def solver_evidence_payload(project: Path, job: dict) -> dict:
    """Return the public two-stage solver receipt, failing closed for old jobs."""

    receipt_dir = project / ".factory" / "solver_receipts"
    submitted, completed = receipt_paths(receipt_dir, str(job["job_id"]))
    try:
        return bind_event_stream(
            build_evidence(
                project,
                submitted,
                completed if completed.is_file() else None,
            ),
            SQLiteStateStore(project).events(),
        )
    except (OSError, ReceiptError) as exc:
        script = Path(str(job["script"]))
        if not script.is_absolute():
            script = project / script
        workdir = Path(str(job["workdir"]))
        if not workdir.is_absolute():
            workdir = project / workdir
        return {
            "schema": "solver-job-evidence-v2",
            "job_id": str(job["job_id"]),
            "backend": str(job["backend"]),
            "runtime": str(job["runtime"]),
            "script": str(script.resolve()),
            "workdir": str(workdir.resolve()),
            "status": str(job["status"]).upper(),
            "max_time_seconds": int(job["max_time_seconds"]),
            "requested_at": int(job["requested_at"]),
            "submission": None,
            "completion": None,
            "receipt_ready": False,
            "errors": [f"MISSING_OR_INVALID_TWO_STAGE_RECEIPT: {exc}"],
            "claim_limit": "LEGACY_JOB_METADATA_ONLY",
        }


def _legacy_infer(project: Path) -> int:
    return LegacyArtifactValidator(ROOT, LEGACY_RUNNER).infer_step(project)


def _engine_authoritative(project: Path) -> bool:
    store = SQLiteStateStore(project)
    return store.exists and store.load().control_mode == "engine"


def _checkpoint_step(project: Path) -> int:
    path = project / "checkpoint.md"
    if not path.is_file():
        return -1
    match = re.search(
        r"Last completed step\*{0,2}\s*[:：]\s*(-?\d+)",
        path.read_text(encoding="utf-8", errors="replace"),
    )
    return int(match.group(1)) if match else -1


def _exec_legacy(arguments: list[str]) -> None:
    env = {**os.environ, "FACTORY": str(ROOT)}
    os.execvpe(str(LEGACY_RUNNER), [str(LEGACY_RUNNER), *arguments], env)


def _retired_social_project(project: Path) -> bool:
    return (project / "project_brief.md").is_file() and not (project / "problem").is_dir()


def compat(arguments: list[str]) -> int:
    if not arguments:
        raise SystemExit("Usage: run_paper.sh [--infer-step|--status] <project_dir>")
    flag = arguments[0]
    if flag in {"--infer-step", "--status"}:
        if len(arguments) < 2:
            raise SystemExit(f"Usage: run_paper.sh {flag} <project_dir>")
        project = Path(arguments[1]).resolve()
        if _engine_authoritative(project):
            state = SQLiteStateStore(project).load()
            if flag == "--infer-step":
                print(state.last_completed_step)
            else:
                heartbeat = (project / ".heartbeat")
                heartbeat_text = heartbeat.read_text(encoding="utf-8").strip() if heartbeat.is_file() else "none"
                print(
                    f"project={project.name} inferred={state.last_completed_step} "
                    f"checkpoint={_checkpoint_step(project)} heartbeat={heartbeat_text} "
                    f"killed={'yes' if state.status is WorkflowStatus.KILLED else 'no'} "
                    f"revision={state.revision}"
                )
            return 0
        _exec_legacy(arguments)
    project = Path(arguments[0]).resolve()
    if _retired_social_project(project):
        print(
            "ERROR: LEGACY_DOMAIN_RETIRED: social-science projects are no longer executable",
            file=sys.stderr,
        )
        return 64
    if not _engine_authoritative(project):
        _exec_legacy(arguments)
    service = FactoryService(ROOT)
    state = service.inspect(project)
    if state.status is WorkflowStatus.ARCHIVING:
        state = service.archive(project)
    else:
        state = service.run(project, archive=True)
    print(json.dumps(runtime_payload(state), ensure_ascii=False, sort_keys=True))
    return 0 if state.status not in {WorkflowStatus.FAILED, WorkflowStatus.KILLED} else 1


def _state_json(project: Path) -> str:
    payload = asdict(SQLiteStateStore(project).load())
    payload["status"] = payload["status"].value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _write_report(path: Path, report: MigrationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report.to_json() + "\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m factory_core.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("project_dir")
    init.add_argument("--project-type", default="modeling", choices=["modeling"])

    state = sub.add_parser("state")
    state.add_argument("project_dir")

    run = sub.add_parser("run")
    run.add_argument("project_dir")
    run.add_argument("--max-steps", type=int)

    worker = sub.add_parser("worker")
    worker.add_argument("project_dir")
    worker.add_argument("--ready-file", required=True)

    create = sub.add_parser("create")
    create.add_argument("base_name")
    create.add_argument("research_question")
    create.add_argument("--consult", action="store_true")
    create.add_argument("--start", action="store_true")

    start = sub.add_parser("start")
    start.add_argument("project_dir")

    archive = sub.add_parser("archive")
    archive.add_argument("project_dir")

    audit = sub.add_parser("audit")
    audit.add_argument("project_dir")
    audit.add_argument(
        "--profile",
        choices=["model", "results", "paper", "final"],
        default="final",
        help="Run a stage audit; only the final profile may authorize delivery.",
    )
    audit.add_argument(
        "--checkpoint-step",
        type=int,
        help="Bind a results audit to Step 5 or Step 6 (advanced use).",
    )
    audit.add_argument(
        "--no-compile",
        action="store_true",
        help="Audit the existing compiled PDF instead of compiling a fresh one.",
    )
    audit.add_argument(
        "--no-reuse",
        action="store_true",
        help="Run a new audit even when the same snapshot already has a valid PASS.",
    )

    solver = sub.add_parser("solver")
    solver_sub = solver.add_subparsers(dest="solver_command", required=True)
    solver_submit = solver_sub.add_parser("submit")
    solver_submit.add_argument("project_dir")
    solver_submit.add_argument("--type", dest="runtime", required=True)
    solver_submit.add_argument("script")
    solver_submit.add_argument("--max-time", type=int, default=1_800)
    solver_submit.add_argument("--args", default="")
    solver_submit.add_argument("--input", action="append", default=[])
    solver_submit.add_argument("--output", action="append", default=[])
    solver_submit.add_argument("--seed", action="append", default=[])
    solver_status = solver_sub.add_parser("status")
    solver_status.add_argument("project_dir")
    solver_status.add_argument("job_id")
    solver_status.add_argument("--json", action="store_true", dest="json_output")
    solver_wait = solver_sub.add_parser("wait")
    solver_wait.add_argument("project_dir")
    solver_wait.add_argument("job_id")
    solver_cancel = solver_sub.add_parser("cancel")
    solver_cancel.add_argument("project_dir")
    solver_cancel.add_argument("job_id")
    solver_policy = solver_sub.add_parser("policy")
    solver_policy.add_argument("project_dir")
    solver_policy.add_argument("--mode", choices=["local", "cloud", "auto"])
    solver_policy.add_argument("--threshold", type=int, default=300)
    solver_policy.add_argument("--runtimes", default="python")
    solver_policy.add_argument("--expected-revision", type=int)

    action = sub.add_parser("action")
    action.add_argument("name", choices=["pause", "resume", "kill", "resolve", "deactivate"])
    action.add_argument("project_dir")
    action.add_argument("--expected-revision", type=int)
    action.add_argument("--resolution-json", default="{}")
    action.add_argument("--no-start", action="store_true")

    migrate = sub.add_parser("migrate")
    migrate_sub = migrate.add_subparsers(dest="migration_command", required=True)
    inspect = migrate_sub.add_parser("inspect")
    inspect.add_argument("project_dir")
    inspect.add_argument("--report", required=True)
    apply = migrate_sub.add_parser("apply")
    apply.add_argument("project_dir")
    apply.add_argument("--report", required=True)
    apply.add_argument("--digest", required=True)
    apply.add_argument(
        "--runtime-generation",
        default="native_v2",
        choices=["native_v2", "legacy_adapter"],
    )
    rollback = migrate_sub.add_parser("rollback")
    rollback.add_argument("project_dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "compat":
        try:
            return compat(arguments[1:])
        except FactoryCoreError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    args = build_parser().parse_args(arguments)
    project_value = getattr(args, "project_dir", None)
    project = Path(project_value).resolve() if project_value is not None else None
    service = FactoryService(ROOT)
    try:
        if args.command == "init":
            assert project is not None
            state = SQLiteStateStore(project).initialize(
                project_id=project.name,
                project_type=args.project_type,
                runtime_generation="native_v2",
            )
            write_compatibility_projections(project, state)
            print(_state_json(project))
            return 0
        if args.command == "state":
            assert project is not None
            print(_state_json(project))
            return 0
        if args.command == "create":
            state, worker_handle = service.create_project(
                args.base_name,
                args.research_question,
                consult=args.consult,
                start=args.start,
            )
            payload = runtime_payload(state)
            payload["worker_pid"] = worker_handle.pid if worker_handle else None
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "start":
            assert project is not None
            handle = service.start(project)
            print(json.dumps({"pid": handle.pid, "log": str(handle.log_path)}, sort_keys=True))
            return 0
        if args.command == "worker":
            assert project is not None
            wait_for_worker_ready(Path(args.ready_file))
            state = service.run(project, archive=True)
            print(json.dumps(runtime_payload(state), ensure_ascii=False, sort_keys=True))
            return 0 if state.status not in {WorkflowStatus.FAILED, WorkflowStatus.KILLED} else 1
        if args.command == "run":
            assert project is not None
            state = service.run(project, max_steps=args.max_steps, archive=True)
            print(json.dumps(runtime_payload(state), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "archive":
            assert project is not None
            state = service.archive(project)
            print(json.dumps(runtime_payload(state), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "audit":
            from .audit import (
                AuditProfile,
                AuditStatus,
                IncrementalAuditService,
                build_final_audit_service,
            )

            resolved = service.resolve_project(args.project_dir)
            profile = AuditProfile(args.profile)
            if profile is AuditProfile.FINAL:
                if args.checkpoint_step is not None:
                    print(
                        "ERROR: --checkpoint-step is not valid for the final profile",
                        file=sys.stderr,
                    )
                    return 2
                outcome = build_final_audit_service(CODE_ROOT).run_project(
                    resolved,
                    compile_pdf=not args.no_compile,
                    reuse_pass=not args.no_reuse,
                )
            else:
                if args.no_compile:
                    print(
                        "ERROR: --no-compile is only valid for the final profile",
                        file=sys.stderr,
                    )
                    return 2
                if args.checkpoint_step is not None and not (
                    profile is AuditProfile.RESULTS
                    and args.checkpoint_step in {5, 6}
                ):
                    print(
                        "ERROR: --checkpoint-step is only valid as 5 or 6 for the results profile",
                        file=sys.stderr,
                    )
                    return 2
                outcome = IncrementalAuditService(CODE_ROOT).run_project(
                    resolved,
                    profile,
                    checkpoint_step=args.checkpoint_step,
                    reuse_pass=not args.no_reuse,
                )
            print(
                json.dumps(
                    outcome.record.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
            )
            if profile is AuditProfile.FINAL:
                return 0 if outcome.record.delivery_allowed else 1
            return 0 if outcome.record.status is AuditStatus.PASS else 1
        if args.command == "solver":
            assert project is not None
            if args.solver_command == "submit":
                job = service.submit_solver(
                    project,
                    runtime=args.runtime,
                    script=args.script,
                    args=tuple(shlex.split(args.args)),
                    max_time_seconds=args.max_time,
                    input_paths=tuple(args.input),
                    output_paths=tuple(args.output),
                    seeds=tuple(args.seed),
                )
                print(job["job_id"])
                return 0
            if args.solver_command == "status":
                job = service.solver_status(project, args.job_id)
                if args.json_output:
                    print(
                        json.dumps(
                            solver_evidence_payload(project, job),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                else:
                    print(job["status"].upper())
                return 0
            if args.solver_command == "wait":
                job = service.wait_solver(project, args.job_id)
                print(job["status"].upper())
                return 0 if job["status"] == "completed" else 1
            if args.solver_command == "cancel":
                job = service.cancel_solver(project, args.job_id)
                print(job["status"].upper())
                return 0
            if args.mode is None:
                print(json.dumps(service.solver_policy(project), sort_keys=True))
                return 0
            policy = service.configure_solver_policy(
                project,
                mode=args.mode,
                threshold_seconds=args.threshold,
                allowed_runtimes=[value for value in args.runtimes.split(",") if value],
                expected_revision=args.expected_revision,
            )
            print(json.dumps(policy, sort_keys=True))
            return 0
        if args.command == "action":
            assert project is not None
            state = service.inspect(project)
            revision = args.expected_revision if args.expected_revision is not None else state.revision
            if args.name == "pause":
                updated = service.pause(project, expected_revision=revision)
            elif args.name == "resume":
                if args.no_start:
                    updated = service.resume(project, expected_revision=revision)
                else:
                    updated, _worker = service.resume_and_start(
                        project, expected_revision=revision
                    )
            elif args.name == "kill":
                updated = service.kill(project, expected_revision=revision)
            elif args.name == "resolve":
                updated = service.resolve(
                    project,
                    json.loads(args.resolution_json),
                    expected_revision=revision,
                )
            else:
                updated = service.rollback_migration(project)
            print(json.dumps(runtime_payload(updated), ensure_ascii=False, sort_keys=True))
            return 0
        if args.migration_command == "inspect":
            assert project is not None
            report = service.inspect_migration(project)
            _write_report(Path(args.report), report)
            print(report.to_json())
            return 2 if report.conflicts else 0
        if args.migration_command == "rollback":
            assert project is not None
            updated = service.rollback_migration(project)
            print(json.dumps(runtime_payload(updated), ensure_ascii=False, sort_keys=True))
            return 0
        report = MigrationReport.from_json(Path(args.report).read_text(encoding="utf-8"))
        assert project is not None
        state = service.apply_migration(
            project,
            report,
            expected_digest=args.digest,
            runtime_generation=args.runtime_generation,
        )
        write_compatibility_projections(project, state)
        print(_state_json(project))
        return 0
    except (FactoryCoreError, MigrationConflict, json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
