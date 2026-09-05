"""Bounded Step13 runtime coordination and immutable execution observations.

This runner deliberately does not turn a successful component execution into
an Authority terminal. Formal finalization consumes Authority-backed receipts;
the ordinary Step13 path remains the math-only preliminary review.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .adapters.models.backends import CodexCliBackend
from .adapters.infrastructure.process import ProcessSupervisor
from .canonical import canonical_sha256
from .domain import ExecutionResult, StepContext
from .deadline import deadline_scope, ensure_deadline
from .governance.overrides import NullOverrideProvider
from .steps.registry import build_native_registry


class Phase9RuntimeError(RuntimeError):
    pass


def _source_identity(source: Path) -> dict:
    identity = subprocess.check_output(
        ["git", "show", "-s", "--format=%H %T %P"], cwd=source, timeout=10,
        text=True,
    ).strip().split()
    paths = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=source, timeout=10,
    ).decode().split("\0")
    files = []
    for name in sorted(set(paths) - {""}):
        path = source / name
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            files.append({"path": name, "size": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest(), "mode": path.stat().st_mode})
    git_status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=source, timeout=10,
    ).decode()
    return {"git_commit_tree_parents": identity, "working_source_files": files,
            "git_status_porcelain": git_status,
            "formal_source_inventory": False}


def _write_new(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class _ObservedDispatcher:
    """One pinned backend; every transport attempt retains its own observation."""

    def __init__(self, source: Path, records: Path, check_inputs, model: str, effort: str,
                 *, authority=None, runtime_id=None):
        self.backend = CodexCliBackend(source)
        self.records = records
        self.check_inputs = check_inputs
        self.model = model
        self.effort = effort
        self.calls = []
        self.authority = authority
        self.runtime_id = runtime_id

    def execute(self, request, *, step_key, defaults):
        del step_key, defaults
        self.check_inputs()
        invocation = uuid.uuid4().hex
        attempt_root = self.records / invocation
        attempt_root.mkdir()
        role = Path(request.output_file).stem
        configured = replace(request, model=self.model, effort=self.effort)
        started = time.time_ns()
        request_record = {
            "role": role, "invocation_id": invocation,
            "requested_model": self.model, "requested_effort": self.effort,
            "prompt_sha256": hashlib.sha256(request.prompt.encode()).hexdigest(),
            "timeout_seconds": request.timeout_seconds,
            "started_at_unix_ns": started,
            "delivery_capability": "DISABLED",
        }
        _write_new(attempt_root / "request.json", request_record)
        intent = None
        supervisor = None
        if self.authority is not None:
            intent = self.authority.reserve_attempt(self.runtime_id, role, canonical_sha256(request_record))
            _write_new(attempt_root / "dispatch_intent.json", intent)
            supervisor = _AuthorityProcessSupervisor(
                self.authority, intent, attempt_root,
                [Path(path) for path in (configured.output_file, configured.final_response_file) if path is not None],
            )
            self.backend.supervisor = supervisor
        record = {
            "role": role, "invocation_id": invocation,
            "requested_model": self.model, "requested_effort": self.effort,
            # A CLI configuration banner is not an independent response ID.
            "response_model_identity": "unavailable",
            "started_at_unix_ns": started, "result": None, "outputs": {},
            "status": "DISPATCH_ENTERED",
        }
        self.calls.append(record)
        try:
            result = self.backend.execute(configured)
        except BaseException:
            record.update(status="OUTCOME_UNCERTAIN", completed_at_unix_ns=time.time_ns())
            _write_new(attempt_root / "completion.json", record)
            if intent is not None:
                self.authority.observe(intent, {"outcome": "UNCERTAIN", "completion_record_sha256": canonical_sha256(record)})
            raise
        record.update(status="RETURNED", result=asdict(result),
                      completed_at_unix_ns=time.time_ns())
        for label, path in (("output", request.output_file),
                            ("final_response", request.final_response_file)):
            if path is not None and Path(path).is_file():
                raw = Path(path).read_bytes()
                (attempt_root / (label + ".raw")).write_bytes(raw)
                record["outputs"][label] = {"sha256": hashlib.sha256(raw).hexdigest(),
                                             "byte_length": len(raw)}
        log = request.project_dir / str(result.metadata.get("log", ""))
        if log.is_file():
            raw = log.read_bytes()
            (attempt_root / "backend.log").write_bytes(raw)
            record["log_sha256"] = hashlib.sha256(raw).hexdigest()
        _write_new(attempt_root / "completion.json", record)
        self.check_inputs()
        if intent is not None:
            # Milliseconds are encoded as integers; Authority canonical JSON
            # deliberately rejects floating-point data.
            outcome = ("SUCCEEDED" if result.returncode == 0 and any(v["byte_length"] > 0 for v in record["outputs"].values())
                       and supervisor.launch_record is not None
                       and supervisor.active_count == 0 else "FAILED")
            self.authority.observe(intent, {
                "outcome": outcome, "exit_code": result.returncode,
                "process_pid": result.metadata.get("process_pid", 0),
                "process_group_active_count": supervisor.active_count,
                "launch_sha256": (supervisor.launch_record or {}).get("launch_sha256"),
                "outputs": record["outputs"], "response_model_identity": "unavailable",
                "completion_record_sha256": hashlib.sha256((attempt_root / "completion.json").read_bytes()).hexdigest(),
            })
        return replace(result, metadata={**result.metadata, "model_id": self.model,
                                        "model": self.model, "backend": "codex"})


class _AuthorityProcessSupervisor(ProcessSupervisor):
    """Bind actual launch identity to the intent committed by the dispatcher."""

    def __init__(self, authority, intent, records, writable_files):
        self.authority, self.intent, self.records = authority, intent, records
        self.writable_files = writable_files
        self.launch_record = None
        self.active_count = None

    def run(self, request):
        def started(pid):
            self.launch_record = self.authority.launch(self.intent, pid)
            _write_new(self.records / "launch.json", self.launch_record)

        scratch = Path(os.environ.get("TMPDIR", ""))
        if not scratch.is_absolute() or not scratch.is_dir() or scratch.resolve() != scratch:
            raise Phase9RuntimeError("formal process sandbox requires an explicit canonical TMPDIR")
        if scratch == self.authority.finalizer.source_repository or scratch in self.authority.finalizer.source_repository.parents:
            raise Phase9RuntimeError("formal scratch must not make executing source writable")
        call_scratch = scratch / ("phase9-provider-" + uuid.uuid4().hex)
        call_scratch.mkdir()
        writable = []
        for path in self.writable_files:
            if not path.is_absolute() or path.resolve() != path or path.is_symlink():
                raise Phase9RuntimeError("provider output path is not canonical")
            if not (path.is_relative_to(self.authority.project_root) or path.is_relative_to(scratch)):
                raise Phase9RuntimeError("provider output is outside its project/scratch coordinate")
            if path.exists():
                raise Phase9RuntimeError("provider output reservation is not fresh")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=False)
            writable.extend(["--bind", str(path), str(path)])
        # A private PID namespace closes descendants even if a provider starts
        # another session. Authority DB, inputs, earlier outputs and evidence
        # remain read-only. Only this call's output files and scratch can change.
        argv = ["/usr/bin/bwrap", "--die-with-parent", "--unshare-user", "--unshare-pid",
                "--ro-bind", "/", "/", *writable,
                "--bind", str(call_scratch), str(call_scratch),
                "--setenv", "TMPDIR", str(call_scratch), "--setenv", "XDG_CACHE_HOME", str(call_scratch / "cache"),
                "--proc", "/proc", "--dev", "/dev",
                "--", *request.argv]
        _write_new(self.records / "sandbox.json", {
            "schema": "authority-phase9-provider-sandbox-v1",
            "pid_namespace": "PRIVATE", "source_read_only": True,
            "writable_directories": [str(call_scratch)], "writable_files": [str(path) for path in self.writable_files],
            "authority_database_read_only": True,
            "sandbox_sha256": hashlib.sha256(Path("/usr/bin/bwrap").read_bytes()).hexdigest(),
        })
        result = super().run(replace(request, argv=argv, on_started=started))
        active = []
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal():
                continue
            try:
                raw = (path / "stat").read_text()
                fields = raw[raw.rfind(")") + 2:].split()
                if int(fields[2]) == result.pid and fields[0] != "Z":
                    active.append(int(path.name))
            except FileNotFoundError:
                continue
        self.active_count = len(active)
        _write_new(self.records / "process_group.json", {
            "process_pid": result.pid, "active_group_members": active,
            "scope": "OWNED_PROCESS_GROUP_AND_PRIVATE_PID_NAMESPACE",
            "escaped_descendants_verified": True,
        })
        return result


def run_step13_components(
    *, source: Path, project: Path, records: Path, mode: str,
    timeout_seconds: int, total_timeout_seconds: int,
    model: str = "gpt-6-astra", effort: str = "medium",
    prepare_only: bool = False,
) -> dict:
    """Execute preparation, applicable roles, and terminal validation in a copy.

    The evidence domain is ISOLATED_COMPONENT_RUN. This cannot consume a
    Phase9 entry/finalizer authorization or write an Authority completion.
    Callers must supply a new ordinary evidence directory for every attempt.
    """
    if mode not in {"NORMAL_STEP13", "FORENSIC_THREE_ROLE"}:
        raise Phase9RuntimeError("unsupported Step13 review mode")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise Phase9RuntimeError("per-call time limit must be between 1 and 3600 seconds")
    if type(total_timeout_seconds) is not int or not 1 <= total_timeout_seconds <= 7200:
        raise Phase9RuntimeError("total time limit must be between 1 and 7200 seconds")
    for path in (source, project, records.parent):
        if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_dir():
            raise Phase9RuntimeError("runtime coordinates must be canonical ordinary directories")
    if source == project or source in project.parents or project in source.parents:
        raise Phase9RuntimeError("runtime copy must be outside the source tree")
    if any(records == root or root in records.parents or records in root.parents
           for root in (source, project)):
        raise Phase9RuntimeError("execution observations must not overlap source or project")
    if os.getenv("ABLATE_NO_JUDGE", "0").lower() in {"1", "true", "yes", "on"}:
        raise Phase9RuntimeError("ablation is a separate formal route")
    records.mkdir()  # exclusive reservation: no reusing a completed attempt
    calls_root = records / "calls"
    calls_root.mkdir()
    started = time.time_ns()
    deadline = int(time.time()) + total_timeout_seconds
    result = None
    dispatcher = None
    terminal = {
        "schema": "phase9-step13-component-run-v1", "status": "BLOCKED",
        "execution_domain": "ISOLATED_COMPONENT_RUN",
        "formal_phase9_completed": False, "delivery_capability": "DISABLED",
        "mode": mode, "requested_model": model, "requested_effort": effort,
        "response_model_identity": "unavailable", "started_at_unix_ns": started,
        "step14_16": "NOT_STARTED", "model_dispatch_count": 0,
    }
    _write_new(records / "started.json", {**terminal, "pid": os.getpid(),
                                          "source": str(source), "project": str(project)})
    with deadline_scope(deadline):
        try:
            source_identity = _source_identity(source)
            _write_new(records / "source_identity.json", source_identity)
            step = build_native_registry(source).get(13).lifecycle
            step.override_provider = NullOverrideProvider()
            context = StepContext(project, project.name, 13, 1, timeout_seconds, 0,
                                  deadline_epoch=deadline)
            result = step.prepare_packets(context)
            if result.returncode:
                terminal["reason"] = "PACKET_PREPARATION_BLOCKED"
                return terminal
            from scripts.judge_packet import packet_fingerprints

            fingerprints = packet_fingerprints(project)
            _write_new(records / "packet_fingerprints.json", fingerprints)

            def check_inputs():
                if int(time.time()) >= deadline:
                    raise Phase9RuntimeError("total runtime deadline exhausted")
                if packet_fingerprints(project) != fingerprints:
                    raise Phase9RuntimeError("review input content changed during execution")
                if _source_identity(source) != source_identity:
                    raise Phase9RuntimeError("executing source changed during execution")

            ensure_deadline()
            if _source_identity(source) != source_identity:
                raise Phase9RuntimeError("executing source changed during preparation")
            if prepare_only:
                terminal.update(status="PREPARED", reason="NO_MODEL_REQUESTED")
                return terminal
            dispatcher = _ObservedDispatcher(source, calls_root, check_inputs, model, effort)
            step.dispatcher = dispatcher
            result = (step.execute_precheck(context) if mode == "NORMAL_STEP13"
                      else step.execute_prepared(context))
            check_inputs()
            verdict = result.metadata.get("judge_verdict")
            terminal.update(
                component_verdict=verdict,
                reason="COMPONENT_REVIEW_FINISHED; FORMAL_AUTHORITY_TERMINAL_NOT_CREATED",
            )
            # A zero component return code may still request recovery or be indeterminate.
            if result.returncode == 0 and verdict in {"PASS", "PRECHECK_PASS"} and not result.metadata.get("resume_after_step"):
                terminal["status"] = "COMPONENT_PASS"
            return terminal
        except Exception as exc:
            terminal.update(status="BLOCKED", reason=type(exc).__name__ + ": " + str(exc))
            return terminal
        finally:
            terminal["model_dispatch_count"] = len(dispatcher.calls) if dispatcher else 0
            terminal["result"] = asdict(result) if result is not None else None
            terminal["completed_at_unix_ns"] = time.time_ns()
            _write_new(records / "terminal.json", terminal)
