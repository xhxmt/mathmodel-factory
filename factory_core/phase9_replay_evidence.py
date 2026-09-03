"""Authority-backed producer for formal Phase9-A replay evidence.

The public producer is the sole production path that registers the A2_0019
evidence-write capability.  It runs the fixed, non-recursive acceptance probes
inside a networkless bubblewrap sandbox and binds the result to pre-existing
Authority runtime records before issuing a one-use replay start authorization.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path, PurePosixPath
import pwd
import secrets
import sqlite3
import stat
import subprocess
import time
from typing import Mapping
import unicodedata

from .authority_production_schema import (
    authority_database_path,
    connect_authority_rw,
    legacy_source_identity_sha256,
    verify_production_installation,
)
from .canonical import canonical_bytes, canonical_sha256
from .phase9_entry import CandidateIdentity, collect_phase9_entry_state_in_transaction
from .phase9_forensic_replay import (
    ABLATE_NO_JUDGE,
    DELIVERY_DISABLED,
    PHASE9_ACCEPTANCE_CASES,
    PHASE9_ACCEPTANCE_CASE_RECEIPT_SCHEMA,
    PHASE9_ACCEPTANCE_COMMAND_SCHEMA,
    PHASE9_ACCEPTANCE_ENVIRONMENT_SCHEMA,
    PHASE9_ACCEPTANCE_EVIDENCE_SCHEMA,
    PHASE9_ACCEPTANCE_PYTHON_SCHEMA,
    PHASE9_ACCEPTANCE_RESULT_SCHEMA,
    PHASE9_ACCEPTANCE_SPEC_SHA256,
    PHASE9_ACCEPTANCE_TEST_NODES,
    PHASE9_EVIDENCE_PRODUCER_SCHEMA,
    PHASE9_ENTRY_GATE_SCHEMA,
    PHASE9_REPLAY_EVIDENCE_ATTESTATION_SCHEMA,
    PHASE9_RUNTIME_EVIDENCE_SCHEMA,
    PHASE9_START_AUTHORIZATION_SCHEMA,
    Phase9ForensicReplayConflict,
    Phase9ForensicReplayRequestV1,
    Phase9ForensicReplayService,
    Phase9ForensicReplaySafetyError,
    ReplayEvidenceFileV1,
    _COMPONENT_RECEIPTS,
    _acceptance_command_input_sha256,
    _acceptance_runner_case_source_sha256,
    _authority_runtime_source_sha256,
    _component_authority_source_sha256,
    _component_input_sha256,
    _control,
    _dependency_fingerprint_sha256,
    _evidence_event_id,
    _evaluate_evidence,
    _integer,
    _mapping,
    _read_evidence_set,
    _replay_coordinate_sha256,
    _strict_json,
    _source_snapshot_tuple,
    _validate_attested_acceptance_run,
    _verify_entry_gate,
    phase9_replay_evidence_payload_set_sha256,
    phase9_start_authorization_target_sha256,
)
from .phase9_p0_evidence import (
    _python_identity,
    _require_pristine_formal_source,
    _trusted_python_runtime_identity,
)
from .phase9_run_generation import (
    Phase9RunGenerationError,
    _StableDirectoryTree,
    read_current_git_source_snapshot,
)
from tools import trusted_pytest_reporter as _loaded_trusted_pytest_reporter


PHASE9_REPLAY_EVIDENCE_AUTHORIZATION_SCHEMA = (
    "authority-phase9-replay-evidence-authorization-v1"
)
PHASE9_REPLAY_EVIDENCE_CONSUMPTION_SCHEMA = (
    "authority-phase9-replay-evidence-consumption-v1"
)
PHASE9_REPLAY_RUNTIME_AUTHORIZATION_SCHEMA = (
    "authority-phase9-runtime-authorization-v1"
)
PHASE9_REPLAY_RUNTIME_COMPLETION_SCHEMA = (
    "authority-phase9-runtime-completion-attestation-v1"
)
PHASE9_REPLAY_RUNTIME_RECORD_SCHEMA = "authority-phase9-runtime-record-v1"
PHASE9_REPLAY_EVIDENCE_AUTHORIZATION_TTL_SECONDS = 300


class Phase9ReplayEvidenceProducerError(RuntimeError):
    """Formal replay evidence could not be produced safely."""


def _receipt(body: Mapping[str, object], field: str) -> dict[str, object]:
    result = dict(body)
    result[field] = canonical_sha256(result)
    return result


def _trusted_connection(database: str | Path) -> sqlite3.Connection:
    connection = connect_authority_rw(authority_database_path(database))
    connection.create_function(
        "phase9_replay_evidence_write_capability", 0, lambda: 1
    )
    return connection


def _runtime_connection(database: str | Path) -> sqlite3.Connection:
    connection = _trusted_connection(database)
    connection.create_function(
        "phase9_replay_runtime_completion_capability", 0, lambda: 1
    )
    return connection


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _request_inventory(
    root: Path, request: Phase9ForensicReplayRequestV1
) -> Phase9ForensicReplayRequestV1:
    """Capture one complete stable evidence tree without following names.

    This is intentionally the same descriptor-bound traversal used by the
    finalizer.  Empty directories have no evidence semantics and are rejected
    so that the request inventory closes over the complete namespace.
    """

    files: list[ReplayEvidenceFileV1] = []
    collision_keys: set[str] = set()
    directories: set[tuple[str, ...]] = {()}
    try:
        with _StableDirectoryTree(root, label="formal Phase9 replay evidence") as tree:
            pending: list[tuple[str, ...]] = [()]
            while pending:
                parts = pending.pop()
                names = tree.list_directory(parts)
                if parts and not names:
                    raise Phase9ReplayEvidenceProducerError(
                        "formal evidence tree contains an empty directory"
                    )
                for name in names:
                    logical_path = PurePosixPath(*parts, name).as_posix()
                    collision = unicodedata.normalize("NFC", logical_path).casefold()
                    if collision in collision_keys:
                        raise Phase9ReplayEvidenceProducerError(
                            "formal evidence paths collide by Unicode normalization or case"
                        )
                    collision_keys.add(collision)
                    metadata = tree.member_stat(parts, name)
                    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                        metadata.st_mode
                    ):
                        child = (*parts, name)
                        directories.add(child)
                        tree.directory(child)
                        pending.append(child)
                        continue
                    if (
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                    ):
                        raise Phase9ReplayEvidenceProducerError(
                            "formal evidence tree contains an unsafe member"
                        )
                    raw, _opened = tree.read_regular_file(
                        parts, name, maximum_bytes=64 * 1024 * 1024
                    )
                    if not raw:
                        raise Phase9ReplayEvidenceProducerError(
                            "formal evidence tree contains an empty file"
                        )
                    files.append(
                        ReplayEvidenceFileV1(
                            logical_path,
                            len(raw),
                            hashlib.sha256(raw).hexdigest(),
                        )
                    )
            file_parts = [tuple(PurePosixPath(item.logical_path).parts) for item in files]
            for directory in directories - {()}:
                if not any(path[: len(directory)] == directory for path in file_parts):
                    raise Phase9ReplayEvidenceProducerError(
                        "formal evidence tree contains an unrepresented directory"
                    )
            tree.verify_unchanged()
    except Phase9ReplayEvidenceProducerError:
        raise
    except (OSError, Phase9RunGenerationError) as exc:
        raise Phase9ReplayEvidenceProducerError(
            "formal evidence tree cannot be inventoried safely"
        ) from exc
    files.sort(key=lambda item: item.logical_path.encode("utf-8"))
    return replace(request, evidence_files=tuple(files))


def _safe_runner_environment(work_root: Path) -> dict[str, str]:
    home = work_root / "home"
    cache = work_root / "cache"
    temporary = work_root / "tmp"
    for path in (home, cache, temporary):
        path.mkdir(mode=0o700, parents=True)
    return {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(temporary),
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def _run_fixed_acceptance_probes(
    *,
    source_repository: Path,
    evidence_root: Path,
    python_executable: str | Path,
) -> dict[str, object]:
    _require_pristine_formal_source(source_repository)
    source_snapshot = read_current_git_source_snapshot(source_repository)
    runtime = _trusted_python_runtime_identity(
        python_executable, source_root=source_repository
    )
    python = runtime["python"]
    sandbox = _python_identity("/usr/bin/bwrap")
    requested_python = Path(str(python["requested_path"]))
    venv_root = requested_python.parent.parent
    host_checkout = venv_root.parent
    if (
        requested_python.parent.name != "bin"
        or venv_root.name != ".venv"
        or source_repository == host_checkout
        or host_checkout in source_repository.parents
    ):
        raise Phase9ReplayEvidenceProducerError(
            "formal replay runner requires a separate venv and candidate source"
        )
    work_root = evidence_root.parent / (
        f".{evidence_root.name}.replay-runner-{secrets.token_hex(8)}"
    )
    work_root.mkdir(mode=0o700)
    report_path = work_root / "acceptance.xml"
    event_path = work_root / "acceptance.events.jsonl"
    event_nonce = secrets.token_hex(16)
    reporter_entry = next(
        (
            item for item in source_snapshot.tracked_inventory.entries
            if item.logical_path == "tools/trusted_pytest_reporter.py"
        ),
        None,
    )
    if reporter_entry is None or reporter_entry.raw_bytes_sha256 is None:
        raise Phase9ReplayEvidenceProducerError(
            "trusted pytest reporter is absent from the candidate Git inventory"
        )
    try:
        with _StableDirectoryTree(
            source_repository, label="formal replay candidate source"
        ) as source_tree:
            reporter_raw, _ = source_tree.read_regular_file(
                ("tools",),
                "trusted_pytest_reporter.py",
                maximum_bytes=1024 * 1024,
            )
            source_tree.verify_unchanged()
    except Phase9RunGenerationError as exc:
        raise Phase9ReplayEvidenceProducerError(
            "trusted pytest reporter cannot be read stably"
        ) from exc
    if hashlib.sha256(reporter_raw).hexdigest() != reporter_entry.raw_bytes_sha256:
        raise Phase9ReplayEvidenceProducerError(
            "trusted pytest reporter differs from the candidate Git inventory"
        )
    loaded_root = Path(__file__).resolve(strict=True).parents[1]
    loaded_reporter_path = Path(
        str(_loaded_trusted_pytest_reporter.__file__)
    ).resolve(strict=True)
    if loaded_reporter_path != loaded_root / "tools/trusted_pytest_reporter.py":
        raise Phase9ReplayEvidenceProducerError(
            "loaded trusted pytest reporter is outside the execution source root"
        )
    try:
        with _StableDirectoryTree(
            loaded_root, label="loaded replay producer source"
        ) as loaded_tree:
            loaded_reporter_raw, _ = loaded_tree.read_regular_file(
                ("tools",),
                "trusted_pytest_reporter.py",
                maximum_bytes=1024 * 1024,
            )
            loaded_tree.verify_unchanged()
    except Phase9RunGenerationError as exc:
        raise Phase9ReplayEvidenceProducerError(
            "loaded trusted pytest reporter cannot be read stably"
        ) from exc
    if loaded_reporter_raw != reporter_raw:
        raise Phase9ReplayEvidenceProducerError(
            "loaded trusted pytest reporter differs from the candidate source"
        )
    reporter_path = work_root / "trusted_pytest_reporter.py"
    _write_new(reporter_path, reporter_raw)
    environment = _safe_runner_environment(work_root)
    environment["PHASE9_TRUSTED_PYTEST_EVENT_PATH"] = str(event_path)
    environment["PHASE9_TRUSTED_PYTEST_NONCE"] = event_nonce
    host_mask = work_root / "host-checkout-mask"
    (host_mask / ".venv").mkdir(mode=0o700, parents=True)
    nodes = [PHASE9_ACCEPTANCE_TEST_NODES[case] for case in PHASE9_ACCEPTANCE_CASES]
    argv = [
        str(python["requested_path"]),
        "-I",
        "-S",
        "-B",
        str(reporter_path),
        "--runtime-site-packages",
        str(runtime["site_packages"]),
        "--source-root",
        str(source_repository),
        "--",
        "-p",
        "no:cacheprovider",
        "--noconftest",
        "-c",
        "/dev/null",
        "--rootdir",
        str(source_repository),
        "-o",
        "addopts=",
        "-vv",
        "--tb=short",
        "--color=no",
        f"--basetemp={work_root / 'basetemp'}",
        f"--junitxml={report_path}",
        *nodes,
    ]
    sandbox_argv = [
        str(sandbox["requested_path"]),
        "--unshare-all",
        "--new-session",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--ro-bind",
        str(host_mask),
        str(host_checkout),
        "--ro-bind",
        str(venv_root),
        str(venv_root),
        "--bind",
        str(work_root),
        str(work_root),
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        str(source_repository),
        "--",
        *argv,
    ]
    started_at = int(time.time())
    process = subprocess.Popen(
        sandbox_argv,
        cwd=source_repository,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        stdout, _ = process.communicate(timeout=900)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, _ = process.communicate()
        raise Phase9ReplayEvidenceProducerError(
            "fixed replay acceptance suite exceeded 900 seconds"
        ) from exc
    finished_at = int(time.time())
    raw_log = bytes(stdout)
    if process.returncode != 0:
        raise Phase9ReplayEvidenceProducerError(
            f"fixed replay acceptance suite exited {process.returncode}"
        )
    try:
        junit_xml = report_path.read_bytes()
        trusted_events = event_path.read_bytes()
    except OSError as exc:
        raise Phase9ReplayEvidenceProducerError(
            "fixed replay acceptance suite did not produce JUnit"
        ) from exc
    try:
        trusted_outcome = _loaded_trusted_pytest_reporter.validate_trusted_pytest_events(
            trusted_events,
            nonce=event_nonce,
            expected_rootdir=str(source_repository),
            expected_nodes=nodes,
        )
    except ValueError as exc:
        raise Phase9ReplayEvidenceProducerError(
            "trusted replay acceptance event protocol differs"
        ) from exc
    if trusted_outcome["counts"] != {
        "collected": 17,
        "passed": 17,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "warnings": 0,
    }:
        raise Phase9ReplayEvidenceProducerError(
            "trusted replay acceptance outcome is not an exact pass"
        )
    outcome = {
        "schema": "authority-phase9-replay-acceptance-outcome-v1",
        "execution_domain": "FORMAL_PHASE9_A",
        "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
        "cases": [
            {
                "case_id": case_id,
                "test_node": PHASE9_ACCEPTANCE_TEST_NODES[case_id],
                "status": "PASS",
            }
            for case_id in PHASE9_ACCEPTANCE_CASES
        ],
        "collected": 17,
        "passed": 17,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "exit_code": 0,
    }
    outcome["outcome_sha256"] = canonical_sha256(outcome)
    command = {
        "schema": "authority-phase9-replay-acceptance-command-v1",
        "execution_domain": "FORMAL_PHASE9_A",
        "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
        "cwd": str(source_repository),
        "python": python,
        "python_runtime": runtime,
        "sandbox": sandbox,
        "trusted_reporter_sha256": hashlib.sha256(reporter_raw).hexdigest(),
        "trusted_event_sha256": hashlib.sha256(trusted_events).hexdigest(),
        "argv": argv,
        "sandbox_argv": sandbox_argv,
        "environment": environment,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": process.returncode,
        "raw_log_sha256": hashlib.sha256(raw_log).hexdigest(),
        "junit_sha256": hashlib.sha256(junit_xml).hexdigest(),
        "outcome_sha256": outcome["outcome_sha256"],
    }
    probe = {
        "acceptance_event_log": trusted_events,
        "acceptance_event_log_sha256": hashlib.sha256(trusted_events).hexdigest(),
        "acceptance_event_nonce": event_nonce,
        "acceptance_raw_log": raw_log,
        "acceptance_raw_log_sha256": hashlib.sha256(raw_log).hexdigest(),
        "acceptance_junit_xml": junit_xml,
        "acceptance_junit_sha256": hashlib.sha256(junit_xml).hexdigest(),
        "acceptance_command_json": canonical_bytes(command).decode("utf-8"),
        "acceptance_command_sha256": canonical_sha256(command),
        "acceptance_outcome_json": canonical_bytes(outcome).decode("utf-8"),
        "acceptance_outcome_sha256": outcome["outcome_sha256"],
        "started_at": started_at,
        "finished_at": finished_at,
    }
    _validate_attested_acceptance_run(probe)  # type: ignore[arg-type]
    _require_pristine_formal_source(source_repository)
    if read_current_git_source_snapshot(source_repository) != source_snapshot:
        raise Phase9ReplayEvidenceProducerError(
            "candidate Git inventory changed during formal replay probes"
        )
    if _trusted_python_runtime_identity(
        python_executable, source_root=source_repository
    ) != runtime:
        raise Phase9ReplayEvidenceProducerError(
            "acceptance Python changed during formal replay probes"
        )
    if _python_identity("/usr/bin/bwrap") != sandbox:
        raise Phase9ReplayEvidenceProducerError(
            "bubblewrap changed during formal replay probes"
        )
    return probe


def _acceptance_aggregate_binding(
    probe: Mapping[str, object],
) -> dict[str, str]:
    return {
        "aggregate_command_sha256": str(probe["acceptance_command_sha256"]),
        "aggregate_raw_log_sha256": str(probe["acceptance_raw_log_sha256"]),
        "aggregate_junit_sha256": str(probe["acceptance_junit_sha256"]),
        "aggregate_event_log_sha256": str(
            probe["acceptance_event_log_sha256"]
        ),
        "aggregate_outcome_sha256": str(probe["acceptance_outcome_sha256"]),
    }


def _acceptance_provenance(
    request: Phase9ForensicReplayRequestV1,
    *,
    receipt_kind: str,
    dependency_kind: str,
    logical_id: str,
    component: str,
    input_sha256: str,
    dependency_input_sha256: str,
    event_sequence: int,
    predecessor_event_id: str | None = None,
    predecessor_receipt_sha256: str | None = None,
) -> dict[str, object]:
    dependency = _dependency_fingerprint_sha256(
        request,
        receipt_kind=dependency_kind,
        logical_id=logical_id,
        input_sha256=dependency_input_sha256,
    )
    return {
        "producer": {
            "schema": PHASE9_EVIDENCE_PRODUCER_SCHEMA,
            "execution_domain": "FORMAL_PHASE9_A",
            "component": component,
            "component_version": "2",
            "source_commit": request.source_commit,
            "source_tree": request.source_tree,
            "source_parent": request.source_parent,
            "source_inventory_sha256": request.source_inventory_sha256,
        },
        "replay_coordinate_sha256": _replay_coordinate_sha256(request),
        "source_run_generation": request.run_generation,
        "dependency_fingerprint_sha256": dependency,
        "event_id": _evidence_event_id(
            receipt_kind=receipt_kind,
            logical_id=logical_id,
            dependency_fingerprint_sha256=dependency,
        ),
        "event_sequence": event_sequence,
        "predecessor_event_id": predecessor_event_id,
        "predecessor_receipt_sha256": predecessor_receipt_sha256,
        "input_sha256": input_sha256,
    }


def _write_formal_acceptance_evidence(
    *,
    root: Path,
    request: Phase9ForensicReplayRequestV1,
    probe: Mapping[str, object],
    python_executable: str | Path,
    source_repository: Path,
    verdict_raw: bytes,
) -> None:
    """Derive all case evidence from the one persisted trusted runner result."""

    _validate_attested_acceptance_run(probe)  # type: ignore[arg-type]
    aggregate = _acceptance_aggregate_binding(probe)
    aggregate_command = _strict_json(
        str(probe["acceptance_command_json"]).encode("utf-8"),
        "formal aggregate acceptance command",
    )
    aggregate_argv = aggregate_command.get("argv")
    if type(aggregate_argv) is not list or not aggregate_argv:
        raise Phase9ReplayEvidenceProducerError(
            "formal aggregate acceptance argv is unavailable"
        )
    requested_python = Path(str(aggregate_argv[0]))
    python_identity = _python_identity(requested_python)
    resolved_python = Path(str(python_identity["resolved_path"]))
    metadata = resolved_python.lstat()
    python_descriptor = {
        "schema": PHASE9_ACCEPTANCE_PYTHON_SCHEMA,
        "requested_path": str(requested_python),
        "resolved_path": str(resolved_python),
        "byte_length": python_identity["byte_length"],
        "raw_bytes_sha256": python_identity["sha256"],
        "mode": stat.S_IMODE(metadata.st_mode),
    }
    environment_body = {
        "schema": PHASE9_ACCEPTANCE_ENVIRONMENT_SCHEMA,
        "inherited": False,
        "variables": {
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": str(source_repository),
        },
        "blocked_host_variable_names": [
            "ANTHROPIC_API_KEY", "AUTHORITY_DATABASE", "AUTHORITY_DB",
            "CLOUD_SOLVER_URL", "DATABASE_URL", "DEPLOYMENT_ENV",
            "OPENAI_API_KEY", "PHASE78_ENABLED", "PHASE9_ENABLED",
            "PRODUCTION_DATABASE", "PRODUCTION_DB", "PRODUCTION_OUTBOX",
            "PRODUCTION_RELEASE", "PROVIDER_API_KEY", "SOLVER_API_KEY",
        ],
        "capabilities": {
            "provider_or_network": False,
            "production_outbox_or_delivery": False,
            "release": False,
            "deployment": False,
            "migration": False,
            "cutover": False,
        },
    }
    environment = {
        **environment_body,
        "environment_sha256": canonical_sha256(environment_body),
    }
    candidate = {
        "commit": request.source_commit,
        "tree": request.source_tree,
        "parent": request.source_parent,
    }
    case_records: list[dict[str, object]] = []
    for case_id in PHASE9_ACCEPTANCE_CASES:
        test_node = PHASE9_ACCEPTANCE_TEST_NODES[case_id]
        # Every case projection references the exact aggregate runner bytes;
        # no synthetic per-case command or log is represented as executed.
        raw = bytes(probe["acceptance_raw_log"])
        raw_path = f"acceptance/{case_id}/raw.log"
        _write_new(root / raw_path, raw)
        raw_reference = {
            "logical_path": raw_path,
            "byte_length": len(raw),
            "raw_bytes_sha256": hashlib.sha256(raw).hexdigest(),
        }
        command_input_sha256 = _acceptance_command_input_sha256(
            request, case_id=case_id
        )
        command_provenance = _acceptance_provenance(
            request,
            receipt_kind="ACCEPTANCE_COMMAND",
            dependency_kind="ACCEPTANCE_CASE",
            logical_id=case_id,
            component="acceptance-command-runner",
            input_sha256=command_input_sha256,
            dependency_input_sha256=command_input_sha256,
            event_sequence=1,
        )
        command_body = {
            "schema": PHASE9_ACCEPTANCE_COMMAND_SCHEMA,
            "execution_domain": "FORMAL_PHASE9_A",
            "candidate": candidate,
            "project_id": request.project_id,
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            **command_provenance,
            "output_sha256": raw_reference["raw_bytes_sha256"],
            "case_id": case_id,
            "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
            "test_node": test_node,
            "source_inventory_sha256": request.source_inventory_sha256,
            "command_argv": list(aggregate_argv),
            "working_directory": str(aggregate_command["cwd"]),
            "python_executable": str(requested_python),
            "python_executable_descriptor": python_descriptor,
            "environment": environment,
            "started_at": int(probe["started_at"]),
            "completed_at": int(probe["finished_at"]),
            "exit_code": 0,
            "raw_log": raw_reference,
            **aggregate,
        }
        command = _receipt(command_body, "record_sha256")
        command_raw = canonical_bytes(command)
        command_path = f"acceptance/{case_id}/command.json"
        _write_new(root / command_path, command_raw)
        command_reference = {
            "logical_path": command_path,
            "byte_length": len(command_raw),
            "raw_bytes_sha256": hashlib.sha256(command_raw).hexdigest(),
            "receipt_sha256": command["record_sha256"],
        }
        result_provenance = _acceptance_provenance(
            request,
            receipt_kind="ACCEPTANCE_RESULT",
            dependency_kind="ACCEPTANCE_CASE",
            logical_id=case_id,
            component="acceptance-result-parser",
            input_sha256=str(raw_reference["raw_bytes_sha256"]),
            dependency_input_sha256=command_input_sha256,
            event_sequence=2,
            predecessor_event_id=str(command_provenance["event_id"]),
            predecessor_receipt_sha256=str(command["record_sha256"]),
        )
        result_body = {
            "schema": PHASE9_ACCEPTANCE_RESULT_SCHEMA,
            "execution_domain": "FORMAL_PHASE9_A",
            "candidate": candidate,
            "project_id": request.project_id,
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            **result_provenance,
            "output_sha256": canonical_sha256(
                {
                    "schema": "authority-phase9-pytest-case-outcome-v1",
                    "case_id": case_id,
                    "collected": 1,
                    "passed": 1,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                    "warnings": 0,
                    "exit_code": 0,
                }
            ),
            "command_record": command_reference,
            "raw_log": raw_reference,
            "case_id": case_id,
            "status": "PASS",
            "collected": 1,
            "passed": 1,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
            "warnings": 0,
            "exit_code": 0,
            **aggregate,
        }
        result = _receipt(result_body, "result_sha256")
        result_raw = canonical_bytes(result)
        result_path = f"acceptance/{case_id}/result.json"
        _write_new(root / result_path, result_raw)
        result_reference = {
            "logical_path": result_path,
            "byte_length": len(result_raw),
            "raw_bytes_sha256": hashlib.sha256(result_raw).hexdigest(),
            "receipt_sha256": result["result_sha256"],
        }
        case_provenance = _acceptance_provenance(
            request,
            receipt_kind="ACCEPTANCE_CASE",
            dependency_kind="ACCEPTANCE_CASE",
            logical_id=case_id,
            component="acceptance-case-finalizer",
            input_sha256=str(result["result_sha256"]),
            dependency_input_sha256=command_input_sha256,
            event_sequence=3,
            predecessor_event_id=str(result_provenance["event_id"]),
            predecessor_receipt_sha256=str(result["result_sha256"]),
        )
        case_body = {
            "schema": PHASE9_ACCEPTANCE_CASE_RECEIPT_SCHEMA,
            "receipt_id": f"phase9-{case_id.lower()}-receipt",
            "candidate": candidate,
            "project_id": request.project_id,
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            **case_provenance,
            "output_sha256": result["result_sha256"],
            "case_id": case_id,
            "result": "PASS",
            "command_record": command_reference,
            "raw_log": raw_reference,
            "test_result": result_reference,
            **aggregate,
            "occurred_at": request.occurred_at,
        }
        case_receipt = _receipt(case_body, "receipt_sha256")
        case_raw = canonical_bytes(case_receipt)
        case_path = f"receipts/acceptance/{case_id}.json"
        _write_new(root / case_path, case_raw)
        case_records.append(
            {
                "case_id": case_id,
                "result": "PASS",
                "receipt": {
                    "logical_path": case_path,
                    "byte_length": len(case_raw),
                    "raw_bytes_sha256": hashlib.sha256(case_raw).hexdigest(),
                    "receipt_sha256": case_receipt["receipt_sha256"],
                },
            }
        )
    verdict = _strict_json(verdict_raw, "formal verdict evidence")
    if request.replay_mode == ABLATE_NO_JUDGE:
        terminal = {
            "terminal_reason": "PERMANENT_ABLATION_NO_DELIVERY",
            "requested_resume_target": "STEP13_PACKET_REBUILD",
            "effective_verdict": "NOT_APPLICABLE",
            "exit_code": verdict["exit_code"],
        }
    else:
        terminal = {
            "terminal_reason": "FORENSIC_REPLAY_COMPLETED",
            "requested_resume_target": "STEP13_PACKET_REBUILD",
            "effective_verdict": verdict["effective_verdict"],
            "exit_code": verdict["exit_code"],
        }
    acceptance = {
        "schema": PHASE9_ACCEPTANCE_EVIDENCE_SCHEMA,
        "cases": case_records,
        "delivery": {
            "delivery_capability": DELIVERY_DISABLED,
            "release_created": False,
            "final_acceptance_created": False,
            "final_submission_created": False,
            "reusable": False,
            "delivery_override_applied": False,
        },
        "terminal": terminal,
    }
    _write_new(root / "acceptance.json", canonical_bytes(acceptance))


def authorize_formal_phase9_runtime_receipt(
    *,
    database: str | Path,
    expected_source_fence_sha256: str,
    request: Phase9ForensicReplayRequestV1,
    receipt_kind: str,
    logical_id: str,
    logical_path: str,
    invocation_id: str,
    attempt_id: str,
    process_scope_id: str,
    packet_sha256: str | None,
    dependency_fingerprint_sha256: str,
    input_sha256: str,
) -> tuple[str, str, str]:
    """Issue a short-lived one-use capability before a runtime operation."""

    if receipt_kind not in {"ROLE_PROCESS", "ROLE_PROVIDER", "PROCESS_SCOPE"}:
        raise Phase9ReplayEvidenceProducerError("unsupported runtime receipt kind")
    values = {
        "logical_id": logical_id,
        "logical_path": logical_path,
        "invocation_id": invocation_id,
        "attempt_id": attempt_id,
        "process_scope_id": process_scope_id,
    }
    if any(type(value) is not str or not value for value in values.values()):
        raise Phase9ReplayEvidenceProducerError(
            "runtime authorization identity is incomplete"
        )
    for name, value in (
        ("dependency_fingerprint_sha256", dependency_fingerprint_sha256),
        ("input_sha256", input_sha256),
    ):
        if type(value) is not str or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise Phase9ReplayEvidenceProducerError(f"runtime {name} differs")
    if packet_sha256 is not None and (
        type(packet_sha256) is not str
        or len(packet_sha256) != 64
        or any(character not in "0123456789abcdef" for character in packet_sha256)
    ):
        raise Phase9ReplayEvidenceProducerError("runtime packet hash differs")
    now = int(time.time())
    try:
        operator_uid = os.geteuid()
        operator_account = pwd.getpwuid(operator_uid).pw_name
    except (AttributeError, KeyError) as exc:
        raise Phase9ReplayEvidenceProducerError(
            "runtime authorization OS account cannot be verified"
        ) from exc
    authorization_id = f"phase9-runtime-auth:{secrets.token_hex(16)}"
    nonce = secrets.token_hex(32)
    nonce_sha256 = hashlib.sha256(nonce.encode("ascii")).hexdigest()
    body = {
        "schema": PHASE9_REPLAY_RUNTIME_AUTHORIZATION_SCHEMA,
        "authorization_id": authorization_id,
        "nonce_sha256": nonce_sha256,
        "authorization_mechanism": "CONTROLLED_OS_ACCOUNT",
        "authorized": True,
        "operation": "RECORD_PHASE9_A_RUNTIME_COMPLETION",
        "candidate": {
            "commit": request.source_commit,
            "tree": request.source_tree,
            "parent": request.source_parent,
        },
        "project_id": request.project_id,
        "workflow_id": request.workflow_id,
        "run_generation": request.run_generation,
        "source_inventory_sha256": request.source_inventory_sha256,
        "replay_coordinate_sha256": _replay_coordinate_sha256(request),
        "receipt_kind": receipt_kind,
        "logical_id": logical_id,
        "logical_path": logical_path,
        "invocation_id": invocation_id,
        "attempt_id": attempt_id,
        "process_scope_id": process_scope_id,
        "packet_sha256": packet_sha256,
        "dependency_fingerprint_sha256": dependency_fingerprint_sha256,
        "input_sha256": input_sha256,
        "operator_uid": operator_uid,
        "operator_account": operator_account,
        "issued_at": now,
        "expires_at": now + PHASE9_REPLAY_EVIDENCE_AUTHORIZATION_TTL_SECONDS,
        "authorization_scope": {
            "runtime_completion_record": True,
            "provider_or_network": False,
            "production_outbox_or_delivery": False,
            "release": False,
            "deployment": False,
            "migration": False,
            "cutover": False,
        },
    }
    authorization = _receipt(body, "authorization_receipt_sha256")
    connection = _runtime_connection(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        verify_production_installation(connection, require_ready=True)
        if legacy_source_identity_sha256(connection) != expected_source_fence_sha256:
            raise Phase9ReplayEvidenceProducerError("Authority source fence differs")
        generation = connection.execute(
            "SELECT g.* FROM authority_production_run_generations g "
            "JOIN authority_production_run_generation_current c "
            "ON c.workflow_id=g.workflow_id AND c.run_generation=g.run_generation "
            "WHERE g.workflow_id=? AND g.run_generation=?",
            (request.workflow_id, request.run_generation),
        ).fetchone()
        if generation is None or any(
            generation[name] != value
            for name, value in (
                ("project_id", request.project_id),
                ("source_commit", request.source_commit),
                ("source_tree", request.source_tree),
                ("source_parent", request.source_parent),
                ("source_inventory_sha256", request.source_inventory_sha256),
                ("delivery_capability", DELIVERY_DISABLED),
            )
        ):
            raise Phase9ReplayEvidenceProducerError(
                "runtime authorization generation is not current"
            )
        connection.execute(
            """
            INSERT INTO authority_production_phase9_replay_runtime_authorizations(
                authorization_id, nonce_sha256, project_id, workflow_id,
                run_generation, source_commit, source_tree, source_parent,
                source_inventory_sha256, replay_coordinate_sha256,
                receipt_kind, logical_id, logical_path, invocation_id,
                attempt_id, process_scope_id, packet_sha256,
                dependency_fingerprint_sha256, input_sha256, operator_uid,
                operator_account, issued_at, expires_at, authorization_json,
                authorization_receipt_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                authorization_id, nonce_sha256, request.project_id,
                request.workflow_id, request.run_generation,
                request.source_commit, request.source_tree, request.source_parent,
                request.source_inventory_sha256,
                _replay_coordinate_sha256(request), receipt_kind, logical_id,
                logical_path, invocation_id, attempt_id, process_scope_id,
                packet_sha256, dependency_fingerprint_sha256, input_sha256,
                operator_uid, operator_account, now,
                now + PHASE9_REPLAY_EVIDENCE_AUTHORIZATION_TTL_SECONDS,
                canonical_bytes(authorization).decode("utf-8"),
                authorization["authorization_receipt_sha256"],
            ),
        )
        connection.commit()
        return (
            authorization_id,
            nonce,
            str(authorization["authorization_receipt_sha256"]),
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_formal_phase9_runtime_receipt(
    *,
    database: str | Path,
    expected_source_fence_sha256: str,
    authorization_id: str,
    authorization_nonce: str,
    request: Phase9ForensicReplayRequestV1,
    receipt_kind: str,
    logical_id: str,
    logical_path: str,
    raw_bytes: bytes,
) -> str:
    """Persist one runtime-produced receipt after joining its Authority scope.

    This API is intended to be called by the role/provider/supervisor completion
    path at the moment it owns the live process identity.  The evidence producer
    only reads these immutable rows and cannot synthesize missing runtime facts.
    """

    if receipt_kind not in {"ROLE_PROCESS", "ROLE_PROVIDER", "PROCESS_SCOPE"}:
        raise Phase9ReplayEvidenceProducerError("unsupported runtime receipt kind")
    try:
        receipt = _strict_json(bytes(raw_bytes), f"runtime {receipt_kind}/{logical_id}")
    except Phase9ForensicReplaySafetyError as exc:
        raise Phase9ReplayEvidenceProducerError(str(exc)) from exc
    if receipt.get("receipt_sha256") != canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    ):
        raise Phase9ReplayEvidenceProducerError("runtime receipt self-hash differs")
    if (
        receipt.get("workflow_id") != request.workflow_id
        or receipt.get("run_generation") != request.run_generation
        or receipt.get("candidate")
        != {
            "commit": request.source_commit,
            "tree": request.source_tree,
            "parent": request.source_parent,
        }
    ):
        raise Phase9ReplayEvidenceProducerError("runtime receipt coordinate differs")
    invocation_id = str(receipt.get("invocation_id", ""))
    attempt_id = str(receipt.get("attempt_id", ""))
    process_scope_id = str(receipt.get("process_scope_id", ""))
    if not invocation_id or not attempt_id or not process_scope_id:
        raise Phase9ReplayEvidenceProducerError("runtime receipt lacks process identity")
    packet_sha256 = receipt.get("packet_sha256")
    if packet_sha256 is not None and (
        type(packet_sha256) is not str or len(packet_sha256) != 64
    ):
        raise Phase9ReplayEvidenceProducerError("runtime packet hash differs")
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    connection = _runtime_connection(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        verify_production_installation(connection, require_ready=True)
        if legacy_source_identity_sha256(connection) != expected_source_fence_sha256:
            raise Phase9ReplayEvidenceProducerError("Authority source fence differs")
        authorization = connection.execute(
            "SELECT * FROM authority_production_phase9_replay_runtime_authorizations "
            "WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()
        completed_at = int(time.time())
        nonce_sha256 = hashlib.sha256(
            authorization_nonce.encode("ascii", errors="strict")
        ).hexdigest()
        expected_authorization = {
            "nonce_sha256": nonce_sha256,
            "project_id": request.project_id,
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            "source_commit": request.source_commit,
            "source_tree": request.source_tree,
            "source_parent": request.source_parent,
            "source_inventory_sha256": request.source_inventory_sha256,
            "replay_coordinate_sha256": _replay_coordinate_sha256(request),
            "receipt_kind": receipt_kind,
            "logical_id": logical_id,
            "logical_path": logical_path,
            "invocation_id": invocation_id,
            "attempt_id": attempt_id,
            "process_scope_id": process_scope_id,
            "packet_sha256": packet_sha256,
            "dependency_fingerprint_sha256": receipt.get(
                "dependency_fingerprint_sha256"
            ),
            "input_sha256": receipt.get("input_sha256"),
        }
        if (
            authorization is None
            or any(
                authorization[name] != value
                for name, value in expected_authorization.items()
            )
            or completed_at < int(authorization["issued_at"])
            or completed_at > int(authorization["expires_at"])
        ):
            raise Phase9ReplayEvidenceProducerError(
                "runtime completion authorization differs or expired"
            )
        authorization_body = _strict_json(
            str(authorization["authorization_json"]).encode("utf-8"),
            "runtime completion authorization",
        )
        if (
            authorization_body.get("authorization_receipt_sha256")
            != authorization["authorization_receipt_sha256"]
            or canonical_sha256(
                {
                    key: value
                    for key, value in authorization_body.items()
                    if key != "authorization_receipt_sha256"
                }
            )
            != authorization["authorization_receipt_sha256"]
        ):
            raise Phase9ReplayEvidenceProducerError(
                "runtime completion authorization hash differs"
            )
        authority_source_sha256 = _authority_runtime_source_sha256(
            connection,
            request=request,
            receipt_kind=receipt_kind,
            logical_id=logical_id,
            logical_path=logical_path,
            raw_bytes_sha256=raw_sha256,
            byte_length=len(raw_bytes),
            receipt_sha256=str(receipt["receipt_sha256"]),
            dependency_fingerprint_sha256=str(
                receipt.get("dependency_fingerprint_sha256")
            ),
            input_sha256=str(receipt.get("input_sha256")),
            output_sha256=str(receipt.get("output_sha256")),
            packet_sha256=packet_sha256,
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            process_scope_id=process_scope_id,
        )
        completion_body = {
            "schema": PHASE9_REPLAY_RUNTIME_COMPLETION_SCHEMA,
            "authorization_id": authorization_id,
            "nonce_sha256": nonce_sha256,
            "authorization_receipt_sha256": authorization[
                "authorization_receipt_sha256"
            ],
            "execution_domain": "FORMAL_PHASE9_A",
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            "receipt_kind": receipt_kind,
            "logical_id": logical_id,
            "invocation_id": invocation_id,
            "attempt_id": attempt_id,
            "process_scope_id": process_scope_id,
            "packet_sha256": packet_sha256,
            "dependency_fingerprint_sha256": receipt.get(
                "dependency_fingerprint_sha256"
            ),
            "input_sha256": receipt.get("input_sha256"),
            "output_sha256": receipt.get("output_sha256"),
            "logical_path": logical_path,
            "byte_length": len(raw_bytes),
            "raw_bytes_sha256": raw_sha256,
            "receipt_sha256": receipt["receipt_sha256"],
            "authority_source_sha256": authority_source_sha256,
            "completed_at": completed_at,
        }
        runtime_completion_sha256 = canonical_sha256(completion_body)
        completion = {
            **completion_body,
            "completion_sha256": runtime_completion_sha256,
        }
        connection.execute(
            """
            INSERT INTO authority_production_phase9_replay_runtime_completions(
                completion_sha256, authorization_id, nonce_sha256,
                authorization_receipt_sha256, execution_domain, workflow_id,
                run_generation, receipt_kind, logical_id, invocation_id,
                attempt_id, process_scope_id, packet_sha256,
                dependency_fingerprint_sha256, input_sha256, output_sha256,
                logical_path, byte_length, raw_bytes_sha256, receipt_sha256,
                authority_source_sha256, completed_at, completion_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                runtime_completion_sha256, authorization_id, nonce_sha256,
                authorization["authorization_receipt_sha256"],
                "FORMAL_PHASE9_A", request.workflow_id,
                request.run_generation, receipt_kind, logical_id,
                invocation_id, attempt_id, process_scope_id, packet_sha256,
                receipt.get("dependency_fingerprint_sha256"),
                receipt.get("input_sha256"), receipt.get("output_sha256"),
                logical_path, len(raw_bytes), raw_sha256,
                receipt["receipt_sha256"], authority_source_sha256,
                completed_at, canonical_bytes(completion).decode("utf-8"),
            ),
        )
        body = {
            "schema": PHASE9_REPLAY_RUNTIME_RECORD_SCHEMA,
            "execution_domain": "FORMAL_PHASE9_A",
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            "receipt_kind": receipt_kind,
            "logical_id": logical_id,
            "invocation_id": invocation_id,
            "attempt_id": attempt_id,
            "process_scope_id": process_scope_id,
            "packet_sha256": packet_sha256,
            "dependency_fingerprint_sha256": receipt.get(
                "dependency_fingerprint_sha256"
            ),
            "input_sha256": receipt.get("input_sha256"),
            "output_sha256": receipt.get("output_sha256"),
            "logical_path": logical_path,
            "byte_length": len(raw_bytes),
            "raw_bytes_sha256": raw_sha256,
            "receipt_sha256": receipt["receipt_sha256"],
            "authority_source_sha256": authority_source_sha256,
            "runtime_completion_sha256": runtime_completion_sha256,
            "recorded_at": completed_at,
        }
        record_sha256 = canonical_sha256(body)
        record = {**body, "record_sha256": record_sha256}
        connection.execute(
            """
            INSERT INTO authority_production_phase9_replay_runtime_records(
                record_sha256, execution_domain, workflow_id, run_generation,
                receipt_kind, logical_id, invocation_id, attempt_id,
                process_scope_id, packet_sha256,
                dependency_fingerprint_sha256, input_sha256, output_sha256,
                logical_path, byte_length, raw_bytes_sha256, receipt_sha256,
                authority_source_sha256, runtime_completion_sha256,
                record_json, recorded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_sha256,
                "FORMAL_PHASE9_A",
                request.workflow_id,
                request.run_generation,
                receipt_kind,
                logical_id,
                invocation_id,
                attempt_id,
                process_scope_id,
                packet_sha256,
                receipt.get("dependency_fingerprint_sha256"),
                receipt.get("input_sha256"),
                receipt.get("output_sha256"),
                logical_path,
                len(raw_bytes),
                raw_sha256,
                receipt["receipt_sha256"],
                authority_source_sha256,
                runtime_completion_sha256,
                canonical_bytes(record).decode("utf-8"),
                body["recorded_at"],
            ),
        )
        connection.commit()
        return record_sha256
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _producer_live_precheck(
    service: Phase9ForensicReplayService,
    connection: sqlite3.Connection,
    request: Phase9ForensicReplayRequestV1,
    evaluation: Mapping[str, object],
) -> None:
    """Rebuild every mutable entry/input fence inside the caller transaction."""

    service._verify_installation(connection)
    service._control_fence(connection)
    service._verify_coordinate(connection, request)
    snapshot = service._current_source_snapshot()
    if _source_snapshot_tuple(snapshot) != (
        request.source_commit,
        request.source_tree,
        request.source_parent,
        request.source_inventory_sha256,
    ):
        raise Phase9ReplayEvidenceProducerError(
            "current source differs from the replay evidence request"
        )
    service._verify_external_generation_inputs(connection, request)
    state = service._live_entry_state(connection, request)
    service._verify_live_entry_state(
        state,
        expected_state_receipt_sha256=str(
            evaluation["entry_state_receipt_sha256"]
        ),
        runtime_counts=dict(evaluation["runtime_counts"]),
    )


def _runtime_record_body(row: sqlite3.Row) -> dict[str, object]:
    return {
        "schema": PHASE9_REPLAY_RUNTIME_RECORD_SCHEMA,
        "execution_domain": row["execution_domain"],
        "workflow_id": row["workflow_id"],
        "run_generation": row["run_generation"],
        "receipt_kind": row["receipt_kind"],
        "logical_id": row["logical_id"],
        "invocation_id": row["invocation_id"],
        "attempt_id": row["attempt_id"],
        "process_scope_id": row["process_scope_id"],
        "packet_sha256": row["packet_sha256"],
        "dependency_fingerprint_sha256": row[
            "dependency_fingerprint_sha256"
        ],
        "input_sha256": row["input_sha256"],
        "output_sha256": row["output_sha256"],
        "logical_path": row["logical_path"],
        "byte_length": row["byte_length"],
        "raw_bytes_sha256": row["raw_bytes_sha256"],
        "receipt_sha256": row["receipt_sha256"],
        "authority_source_sha256": row["authority_source_sha256"],
        "runtime_completion_sha256": row["runtime_completion_sha256"],
        "recorded_at": row["recorded_at"],
    }


def _attestation_items(
    connection: sqlite3.Connection,
    *,
    request: Phase9ForensicReplayRequestV1,
    evaluation: Mapping[str, object],
    attestation_sha256: str,
    producer_consumption_receipt_sha256: str,
    acceptance_source: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[str]]:
    items: list[dict[str, object]] = []
    runtime_record_sha256s: list[str] = []
    for receipt in sorted(
        evaluation["typed_receipts"],
        key=lambda value: (value.receipt_kind, value.logical_id),
    ):
        body = _strict_json(
            receipt.receipt_json.encode("utf-8"),
            f"formal receipt {receipt.receipt_kind}/{receipt.logical_id}",
        )
        common = {
            "attestation_sha256": attestation_sha256,
            "receipt_kind": receipt.receipt_kind,
            "logical_id": receipt.logical_id,
            "logical_path": receipt.logical_path,
            "byte_length": receipt.byte_length,
            "raw_bytes_sha256": receipt.raw_bytes_sha256,
            "receipt_sha256": receipt.receipt_sha256,
            "dependency_fingerprint_sha256": body[
                "dependency_fingerprint_sha256"
            ],
            "input_sha256": body["input_sha256"],
            "output_sha256": body["output_sha256"],
        }
        if receipt.receipt_kind == "ACCEPTANCE_CASE":
            aggregate = _acceptance_aggregate_binding(acceptance_source)
            if any(body.get(key) != value for key, value in aggregate.items()):
                raise Phase9ReplayEvidenceProducerError(
                    "acceptance case is not derived from the trusted aggregate run"
                )
            source_kind = "ACCEPTANCE_RUNNER"
            source_record_sha256 = _acceptance_runner_case_source_sha256(
                receipt.logical_id,
                PHASE9_ACCEPTANCE_TEST_NODES[receipt.logical_id],
                command_sha256=str(
                    acceptance_source["acceptance_command_sha256"]
                ),
                raw_log_sha256=str(
                    acceptance_source["acceptance_raw_log_sha256"]
                ),
                junit_sha256=str(
                    acceptance_source["acceptance_junit_sha256"]
                ),
                event_log_sha256=str(
                    acceptance_source["acceptance_event_log_sha256"]
                ),
                outcome_sha256=str(
                    acceptance_source["acceptance_outcome_sha256"]
                ),
            )
            identity = {
                "invocation_id": None,
                "attempt_id": None,
                "process_scope_id": None,
                "packet_sha256": None,
            }
        elif receipt.receipt_kind in _COMPONENT_RECEIPTS:
            source_kind = "EVIDENCE_PRODUCER"
            source_record_sha256 = producer_consumption_receipt_sha256
            identity = {
                "invocation_id": None,
                "attempt_id": None,
                "process_scope_id": None,
                "packet_sha256": None,
            }
        else:
            row = connection.execute(
                "SELECT * FROM authority_production_phase9_replay_runtime_records "
                "WHERE workflow_id=? AND run_generation=? AND receipt_kind=? "
                "AND logical_id=?",
                (
                    request.workflow_id,
                    request.run_generation,
                    receipt.receipt_kind,
                    receipt.logical_id,
                ),
            ).fetchone()
            if row is None or row["execution_domain"] != "FORMAL_PHASE9_A":
                raise Phase9ReplayEvidenceProducerError(
                    "formal replay evidence lacks an Authority runtime record"
                )
            expected_record = {
                **common,
                "invocation_id": body["invocation_id"],
                "attempt_id": body["attempt_id"],
                "process_scope_id": body["process_scope_id"],
                "packet_sha256": body.get("packet_sha256"),
            }
            for key, value in expected_record.items():
                if key == "attestation_sha256":
                    continue
                if row[key] != value:
                    raise Phase9ReplayEvidenceProducerError(
                        "Authority runtime record differs from exact evidence bytes"
                    )
            record_body = _runtime_record_body(row)
            if (
                canonical_sha256(record_body) != row["record_sha256"]
                or _strict_json(
                    str(row["record_json"]).encode("utf-8"),
                    "Authority runtime record",
                )
                != {**record_body, "record_sha256": row["record_sha256"]}
                or row["authority_source_sha256"]
                != _authority_runtime_source_sha256(
                    connection,
                    request=request,
                    receipt_kind=receipt.receipt_kind,
                    logical_id=receipt.logical_id,
                    logical_path=receipt.logical_path,
                    raw_bytes_sha256=receipt.raw_bytes_sha256,
                    byte_length=receipt.byte_length,
                    receipt_sha256=receipt.receipt_sha256,
                    dependency_fingerprint_sha256=str(
                        body["dependency_fingerprint_sha256"]
                    ),
                    input_sha256=str(body["input_sha256"]),
                    output_sha256=str(body["output_sha256"]),
                    packet_sha256=body.get("packet_sha256"),
                    invocation_id=str(row["invocation_id"]),
                    attempt_id=str(row["attempt_id"]),
                    process_scope_id=str(row["process_scope_id"]),
                )
            ):
                raise Phase9ReplayEvidenceProducerError(
                    "Authority runtime record provenance differs"
                )
            source_kind = "RUNTIME_RECORD"
            source_record_sha256 = str(row["record_sha256"])
            runtime_record_sha256s.append(source_record_sha256)
            identity = {
                "invocation_id": row["invocation_id"],
                "attempt_id": row["attempt_id"],
                "process_scope_id": row["process_scope_id"],
                "packet_sha256": row["packet_sha256"],
            }
        item_body = {
            **common,
            "source_kind": source_kind,
            "source_record_sha256": source_record_sha256,
            **identity,
        }
        item_sha256 = canonical_sha256(item_body)
        items.append({**item_body, "item_sha256": item_sha256})
    return items, runtime_record_sha256s


def _write_authority_component_receipts(
    *,
    root: Path,
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    evaluation: Mapping[str, object],
) -> None:
    for kind, (logical_path, schema, evidence_path, component) in (
        _COMPONENT_RECEIPTS.items()
    ):
        if (root / logical_path).exists():
            raise Phase9ReplayEvidenceProducerError(
                "component receipt path already exists before trusted production"
            )
        input_sha256 = _component_input_sha256(
            kind,
            request,
            values,
            entry_state_receipt_sha256=str(
                evaluation["entry_state_receipt_sha256"]
            ),
            runtime_counts=dict(evaluation["runtime_counts"]),
        )
        output_sha256 = hashlib.sha256(values[evidence_path]).hexdigest()
        dependency = _dependency_fingerprint_sha256(
            request,
            receipt_kind=kind,
            logical_id=kind.lower(),
            input_sha256=input_sha256,
        )
        body = {
            "schema": schema,
            "execution_domain": "FORMAL_PHASE9_A",
            "receipt_id": f"phase9-{kind.lower()}-component:{request.run_generation}",
            "candidate": {
                "commit": request.source_commit,
                "tree": request.source_tree,
                "parent": request.source_parent,
            },
            "project_id": request.project_id,
            "workflow_id": request.workflow_id,
            "run_generation": request.run_generation,
            "producer": {
                "schema": PHASE9_EVIDENCE_PRODUCER_SCHEMA,
                "execution_domain": "FORMAL_PHASE9_A",
                "component": component,
                "component_version": "2",
                "source_commit": request.source_commit,
                "source_tree": request.source_tree,
                "source_parent": request.source_parent,
                "source_inventory_sha256": request.source_inventory_sha256,
            },
            "replay_coordinate_sha256": _replay_coordinate_sha256(request),
            "source_run_generation": request.run_generation,
            "dependency_fingerprint_sha256": dependency,
            "event_id": _evidence_event_id(
                receipt_kind=kind,
                logical_id=kind.lower(),
                dependency_fingerprint_sha256=dependency,
            ),
            "event_sequence": 1,
            "predecessor_event_id": None,
            "predecessor_receipt_sha256": None,
            "input_sha256": input_sha256,
            "output_sha256": output_sha256,
            "component": kind,
            "evidence_logical_path": evidence_path,
            "evidence_sha256": output_sha256,
            "authority_source_sha256": _component_authority_source_sha256(
                kind,
                request,
                entry_state_receipt_sha256=str(
                    evaluation["entry_state_receipt_sha256"]
                ),
                input_sha256=input_sha256,
                output_sha256=output_sha256,
            ),
            "occurred_at": request.occurred_at,
        }
        _write_new(
            root / logical_path,
            canonical_bytes(_receipt(body, "receipt_sha256")),
        )


def _prevalidate_formal_payload(
    request: Phase9ForensicReplayRequestV1,
    values: Mapping[str, bytes],
    *,
    trusted_now: int,
) -> dict[str, object]:
    """Validate the live-gate/runtime inputs needed before launching probes."""

    entry = _control(values, "entry_gate.json", PHASE9_ENTRY_GATE_SCHEMA)
    # `_verify_entry_gate` owns the exact current schema and full coordinate.
    entry_state_sha256, _evaluated_at = _verify_entry_gate(
        entry, request, trusted_now=trusted_now
    )
    outbox = _control(
        values, "outbox_supervisor.json", PHASE9_RUNTIME_EVIDENCE_SCHEMA
    )
    _mapping(
        outbox,
        "outbox_supervisor",
        {
            "schema", "precommit_external_launch_count",
            "committed_reclaim_count", "pending_outbox_count",
            "uncertain_automatic_resend_count", "active_descendant_count",
            "process_scope_receipts",
        },
    )
    runtime_counts = {
        name: _integer(outbox.get(name), f"outbox_supervisor.{name}")
        for name in (
            "precommit_external_launch_count", "committed_reclaim_count",
            "pending_outbox_count", "uncertain_automatic_resend_count",
            "active_descendant_count",
        )
    }
    if any(value != 0 for value in runtime_counts.values()):
        raise Phase9ReplayEvidenceProducerError(
            "formal replay live runtime counts are not all zero"
        )
    return {
        "entry_state_receipt_sha256": entry_state_sha256,
        "runtime_counts": runtime_counts,
    }


def produce_formal_phase9_replay_evidence(
    *,
    database: str | Path,
    expected_source_fence_sha256: str,
    source_repository: str | Path,
    evidence_root: str | Path,
    official_input_root: str | Path,
    execution_context_receipt_path: str | Path,
    python_executable: str | Path,
    request: Phase9ForensicReplayRequestV1,
    execution_root: str | Path | None = None,
) -> Phase9ForensicReplayRequestV1:
    """Attest an exact formal evidence payload and issue one short-lived start.

    The public path always uses the system clock and a real sandboxed acceptance
    execution.  Clock injection and TEST_FIXTURE attestations are deliberately
    absent from this API.
    """

    root = Path(os.path.abspath(os.fspath(evidence_root)))
    if any(
        item.logical_path == "start_authorization.json"
        for item in request.evidence_files
    ) or (root / "start_authorization.json").exists():
        raise Phase9ReplayEvidenceProducerError(
            "formal replay evidence must not contain a caller-made start authorization"
        )
    draft = _request_inventory(root, replace(request, evidence_files=()))
    caller_acceptance_paths = {
        item.logical_path
        for item in draft.evidence_files
        if item.logical_path == "acceptance.json"
        or item.logical_path.startswith("acceptance/")
        or item.logical_path.startswith("receipts/acceptance/")
    }
    if caller_acceptance_paths:
        raise Phase9ReplayEvidenceProducerError(
            "formal replay acceptance evidence must be created by the trusted runner"
        )
    first_values, _ = _read_evidence_set(root, draft)
    first_now = int(time.time())
    try:
        precheck_evaluation = _prevalidate_formal_payload(
            draft, first_values, trusted_now=first_now
        )
    except (Phase9ForensicReplaySafetyError, Phase9ForensicReplayConflict) as exc:
        raise Phase9ReplayEvidenceProducerError(str(exc)) from exc
    service = Phase9ForensicReplayService(
        database,
        expected_source_fence_sha256=expected_source_fence_sha256,
        source_repository=source_repository,
        evidence_root=root,
        official_input_root=official_input_root,
        execution_context_receipt_path=execution_context_receipt_path,
        execution_root=execution_root,
    )
    authorization_id = f"phase9-replay-evidence-auth:{secrets.token_hex(16)}"
    nonce_sha256 = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    invocation_id = f"phase9-replay-evidence-producer:{secrets.token_hex(16)}"
    try:
        operator_uid = os.geteuid()
        operator_account = pwd.getpwuid(operator_uid).pw_name
    except (AttributeError, KeyError) as exc:
        raise Phase9ReplayEvidenceProducerError(
            "formal producer OS account cannot be verified"
        ) from exc
    expires_at = first_now + PHASE9_REPLAY_EVIDENCE_AUTHORIZATION_TTL_SECONDS
    authorization_body = {
        "schema": PHASE9_REPLAY_EVIDENCE_AUTHORIZATION_SCHEMA,
        "authorization_id": authorization_id,
        "nonce_sha256": nonce_sha256,
        "authorization_mechanism": "CONTROLLED_OS_ACCOUNT",
        "authorized": True,
        "operation": "PRODUCE_PHASE9_A_REPLAY_EVIDENCE",
        "project_id": draft.project_id,
        "workflow_id": draft.workflow_id,
        "run_generation": draft.run_generation,
        "replay_mode": draft.replay_mode,
        "source_commit": draft.source_commit,
        "source_tree": draft.source_tree,
        "source_parent": draft.source_parent,
        "source_inventory_sha256": draft.source_inventory_sha256,
        "entry_gate_result_sha256": draft.entry_gate_result_sha256,
        "entry_state_receipt_sha256": precheck_evaluation[
            "entry_state_receipt_sha256"
        ],
        "replay_coordinate_sha256": _replay_coordinate_sha256(draft),
        "intended_evidence_root": str(root.resolve(strict=True)),
        "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
        "operator_uid": operator_uid,
        "operator_account": operator_account,
        "issued_at": first_now,
        "expires_at": expires_at,
        "authorization_scope": {
            "replay_evidence_producer": True,
            "provider_or_network": False,
            "production_outbox_or_delivery": False,
            "release": False,
            "deployment": False,
            "migration": False,
            "cutover": False,
        },
    }
    authorization = _receipt(
        authorization_body, "authorization_receipt_sha256"
    )
    connection = _trusted_connection(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _producer_live_precheck(service, connection, draft, precheck_evaluation)
        connection.execute(
            """
            INSERT INTO authority_production_phase9_replay_evidence_authorizations(
                authorization_id, nonce_sha256, project_id, workflow_id,
                run_generation, replay_mode, source_commit, source_tree,
                source_parent, source_inventory_sha256,
                entry_gate_result_sha256, entry_state_receipt_sha256,
                replay_coordinate_sha256, intended_evidence_root,
                acceptance_spec_sha256, operator_uid, operator_account,
                issued_at, expires_at, authorization_json,
                authorization_receipt_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                authorization_id, nonce_sha256, draft.project_id,
                draft.workflow_id, draft.run_generation, draft.replay_mode,
                draft.source_commit, draft.source_tree, draft.source_parent,
                draft.source_inventory_sha256, draft.entry_gate_result_sha256,
                precheck_evaluation["entry_state_receipt_sha256"],
                _replay_coordinate_sha256(draft), str(root.resolve(strict=True)),
                PHASE9_ACCEPTANCE_SPEC_SHA256, operator_uid, operator_account,
                first_now, expires_at,
                canonical_bytes(authorization).decode("utf-8"),
                authorization["authorization_receipt_sha256"],
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    probe = _run_fixed_acceptance_probes(
        source_repository=Path(source_repository),
        evidence_root=root,
        python_executable=python_executable,
    )
    _write_formal_acceptance_evidence(
        root=root,
        request=draft,
        probe=probe,
        python_executable=python_executable,
        source_repository=Path(source_repository),
        verdict_raw=first_values["verdict.json"],
    )
    acceptance_request = _request_inventory(root, draft)
    acceptance_paths = {
        "acceptance.json",
        *(
            f"acceptance/{case_id}/{name}"
            for case_id in PHASE9_ACCEPTANCE_CASES
            for name in ("raw.log", "result.json", "command.json")
        ),
        *(
            f"receipts/acceptance/{case_id}.json"
            for case_id in PHASE9_ACCEPTANCE_CASES
        ),
    }
    initial_paths = {item.logical_path for item in draft.evidence_files}
    observed_acceptance_paths = {
        item.logical_path for item in acceptance_request.evidence_files
    }
    if (
        replace(acceptance_request, evidence_files=draft.evidence_files) != draft
        or observed_acceptance_paths != initial_paths | acceptance_paths
    ):
        raise Phase9ReplayEvidenceProducerError(
            "formal replay evidence changed outside trusted acceptance outputs"
        )
    draft = acceptance_request
    acceptance_values, _ = _read_evidence_set(root, draft)
    evaluation = _evaluate_evidence(
        draft,
        acceptance_values,
        trusted_now=int(time.time()),
        require_start_authorization=False,
        require_component_receipts=False,
    )
    if (
        evaluation["blockers"]
        or evaluation["entry_state_receipt_sha256"]
        != precheck_evaluation["entry_state_receipt_sha256"]
        or evaluation["runtime_counts"] != precheck_evaluation["runtime_counts"]
    ):
        raise Phase9ReplayEvidenceProducerError(
            "formal replay evidence payload is BLOCKED or changed during probes"
        )
    _write_authority_component_receipts(
        root=root,
        request=draft,
        values=acceptance_values,
        evaluation=evaluation,
    )
    second_now = int(time.time())
    if second_now > expires_at:
        raise Phase9ReplayEvidenceProducerError(
            "formal evidence authorization expired during acceptance execution"
        )
    second_request = _request_inventory(root, draft)
    component_paths = {
        value[0] for value in _COMPONENT_RECEIPTS.values()
    }
    if (
        replace(second_request, evidence_files=draft.evidence_files) != draft
        or set(item.logical_path for item in second_request.evidence_files)
        != {
            *(item.logical_path for item in draft.evidence_files),
            *component_paths,
        }
    ):
        raise Phase9ReplayEvidenceProducerError(
            "formal replay evidence changed outside trusted component receipts"
        )
    draft = second_request
    second_values, _ = _read_evidence_set(root, draft)
    second_evaluation = _evaluate_evidence(
        draft,
        second_values,
        trusted_now=second_now,
        require_start_authorization=False,
        require_component_receipts=True,
    )
    for key in (
        "blockers", "entry_state_receipt_sha256", "packet_sha256",
        "roles_sha256", "verdict_sha256", "snapshot_sha256",
        "runtime_safety_sha256", "runtime_counts", "acceptance_sha256",
        "terminal_reason", "effective_verdict", "exit_code",
    ):
        if second_evaluation[key] != evaluation[key]:
            raise Phase9ReplayEvidenceProducerError(
                "formal replay evidence semantics changed during acceptance execution"
            )
    if second_evaluation["blockers"]:
        raise Phase9ReplayEvidenceProducerError(
            "formal replay evidence semantics changed during acceptance execution"
        )
    evaluation = second_evaluation

    consumption_body = {
        "schema": PHASE9_REPLAY_EVIDENCE_CONSUMPTION_SCHEMA,
        "authorization_id": authorization_id,
        "nonce_sha256": nonce_sha256,
        "authorization_receipt_sha256": authorization[
            "authorization_receipt_sha256"
        ],
        "invocation_id": invocation_id,
        "workflow_id": draft.workflow_id,
        "run_generation": draft.run_generation,
        "consumed_at": second_now,
    }
    consumption = _receipt(consumption_body, "consumption_receipt_sha256")

    connection = _trusted_connection(database)
    start_written = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        _producer_live_precheck(service, connection, draft, evaluation)
        auth_row = connection.execute(
            "SELECT * FROM authority_production_phase9_replay_evidence_authorizations "
            "WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()
        if auth_row is None or any(
            auth_row[key] != value
            for key, value in {
                "nonce_sha256": nonce_sha256,
                "authorization_receipt_sha256": authorization[
                    "authorization_receipt_sha256"
                ],
                "authorization_json": canonical_bytes(authorization).decode(
                    "utf-8"
                ),
            }.items()
        ):
            raise Phase9ReplayEvidenceProducerError(
                "formal evidence authorization changed"
            )
        connection.execute(
            """
            INSERT INTO authority_production_phase9_replay_evidence_consumptions(
                authorization_id, nonce_sha256, invocation_id, consumed_at,
                consumption_json, consumption_receipt_sha256
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                authorization_id, nonce_sha256, invocation_id, second_now,
                canonical_bytes(consumption).decode("utf-8"),
                consumption["consumption_receipt_sha256"],
            ),
        )
        items, runtime_record_sha256s = _attestation_items(
            connection,
            request=draft,
            evaluation=evaluation,
            attestation_sha256="0" * 64,
            producer_consumption_receipt_sha256=str(
                consumption["consumption_receipt_sha256"]
            ),
            acceptance_source=probe,
        )
        runtime_record_set_sha256 = canonical_sha256(
            {
                "schema": "authority-phase9-runtime-record-set-v1",
                "record_sha256s": sorted(runtime_record_sha256s),
            }
        )
        attestation_body = {
            "schema": PHASE9_REPLAY_EVIDENCE_ATTESTATION_SCHEMA,
            "authorization_id": authorization_id,
            "authorization_receipt_sha256": authorization[
                "authorization_receipt_sha256"
            ],
            "consumption_receipt_sha256": consumption[
                "consumption_receipt_sha256"
            ],
            "invocation_id": invocation_id,
            "execution_domain": "FORMAL_PHASE9_A",
            "project_id": draft.project_id,
            "workflow_id": draft.workflow_id,
            "run_generation": draft.run_generation,
            "replay_mode": draft.replay_mode,
            "replay_coordinate_sha256": _replay_coordinate_sha256(draft),
            "source_inventory_sha256": draft.source_inventory_sha256,
            "entry_gate_result_sha256": draft.entry_gate_result_sha256,
            "entry_state_receipt_sha256": evaluation[
                "entry_state_receipt_sha256"
            ],
            "evidence_payload_set_sha256": (
                phase9_replay_evidence_payload_set_sha256(draft)
            ),
            "typed_receipt_set_sha256": evaluation[
                "typed_receipt_set_sha256"
            ],
            "runtime_record_set_sha256": runtime_record_set_sha256,
            "packet_sha256": evaluation["packet_sha256"],
            "roles_sha256": evaluation["roles_sha256"],
            "verdict_sha256": evaluation["verdict_sha256"],
            "snapshot_sha256": evaluation["snapshot_sha256"],
            "runtime_safety_sha256": evaluation["runtime_safety_sha256"],
            "acceptance_sha256": evaluation["acceptance_sha256"],
            "acceptance_spec_sha256": PHASE9_ACCEPTANCE_SPEC_SHA256,
            "acceptance_command_sha256": probe[
                "acceptance_command_sha256"
            ],
            "acceptance_event_log_sha256": probe[
                "acceptance_event_log_sha256"
            ],
            "acceptance_event_nonce": probe["acceptance_event_nonce"],
            "acceptance_raw_log_sha256": probe[
                "acceptance_raw_log_sha256"
            ],
            "acceptance_junit_sha256": probe[
                "acceptance_junit_sha256"
            ],
            "acceptance_outcome_sha256": probe[
                "acceptance_outcome_sha256"
            ],
            "started_at": probe["started_at"],
            "finished_at": probe["finished_at"],
            "attested_at": second_now,
        }
        attestation_sha256 = canonical_sha256(attestation_body)
        attestation = {
            **attestation_body,
            "attestation_sha256": attestation_sha256,
        }
        for provisional in items:
            item_body = {
                **{key: value for key, value in provisional.items() if key != "item_sha256"},
                "attestation_sha256": attestation_sha256,
            }
            item_sha256 = canonical_sha256(item_body)
            item = {**item_body, "item_sha256": item_sha256}
            connection.execute(
                """
                INSERT INTO authority_production_phase9_replay_evidence_attestation_items(
                    attestation_sha256, receipt_kind, logical_id, logical_path,
                    byte_length, raw_bytes_sha256, receipt_sha256, source_kind,
                    source_record_sha256, invocation_id, attempt_id,
                    process_scope_id, packet_sha256,
                    dependency_fingerprint_sha256, input_sha256, output_sha256,
                    item_json, item_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attestation_sha256, item["receipt_kind"], item["logical_id"],
                    item["logical_path"], item["byte_length"],
                    item["raw_bytes_sha256"], item["receipt_sha256"],
                    item["source_kind"], item["source_record_sha256"],
                    item["invocation_id"], item["attempt_id"],
                    item["process_scope_id"], item["packet_sha256"],
                    item["dependency_fingerprint_sha256"], item["input_sha256"],
                    item["output_sha256"], canonical_bytes(item).decode("utf-8"),
                    item_sha256,
                ),
            )
        connection.execute(
            """
            INSERT INTO authority_production_phase9_replay_evidence_attestations(
                attestation_sha256, authorization_id,
                authorization_receipt_sha256, consumption_receipt_sha256,
                invocation_id, execution_domain, project_id, workflow_id,
                run_generation, replay_mode, replay_coordinate_sha256,
                source_inventory_sha256, entry_gate_result_sha256,
                entry_state_receipt_sha256, evidence_payload_set_sha256,
                typed_receipt_set_sha256, runtime_record_set_sha256,
                packet_sha256, roles_sha256, verdict_sha256, snapshot_sha256,
                runtime_safety_sha256, acceptance_sha256,
                acceptance_spec_sha256, acceptance_command_json,
                acceptance_command_sha256, acceptance_event_log,
                acceptance_event_log_sha256, acceptance_event_nonce,
                acceptance_raw_log,
                acceptance_raw_log_sha256, acceptance_junit_xml,
                acceptance_junit_sha256, acceptance_outcome_json,
                acceptance_outcome_sha256, started_at, finished_at,
                attested_at, attestation_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                attestation_sha256, authorization_id,
                authorization["authorization_receipt_sha256"],
                consumption["consumption_receipt_sha256"], invocation_id,
                "FORMAL_PHASE9_A", draft.project_id, draft.workflow_id,
                draft.run_generation, draft.replay_mode,
                _replay_coordinate_sha256(draft), draft.source_inventory_sha256,
                draft.entry_gate_result_sha256,
                evaluation["entry_state_receipt_sha256"],
                phase9_replay_evidence_payload_set_sha256(draft),
                evaluation["typed_receipt_set_sha256"],
                runtime_record_set_sha256, evaluation["packet_sha256"],
                evaluation["roles_sha256"], evaluation["verdict_sha256"],
                evaluation["snapshot_sha256"],
                evaluation["runtime_safety_sha256"],
                evaluation["acceptance_sha256"], PHASE9_ACCEPTANCE_SPEC_SHA256,
                probe["acceptance_command_json"],
                probe["acceptance_command_sha256"],
                probe["acceptance_event_log"],
                probe["acceptance_event_log_sha256"],
                probe["acceptance_event_nonce"],
                probe["acceptance_raw_log"], probe["acceptance_raw_log_sha256"],
                probe["acceptance_junit_xml"], probe["acceptance_junit_sha256"],
                probe["acceptance_outcome_json"],
                probe["acceptance_outcome_sha256"], probe["started_at"],
                probe["finished_at"], second_now,
                canonical_bytes(attestation).decode("utf-8"),
            ),
        )
        start_authorization_id = f"phase9-start:{secrets.token_hex(16)}"
        start_nonce_sha256 = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        start_target_sha256 = phase9_start_authorization_target_sha256(
            draft,
            evidence_attestation_sha256=attestation_sha256,
            entry_state_receipt_sha256=str(
                evaluation["entry_state_receipt_sha256"]
            ),
        )
        start_body = {
            "schema": PHASE9_START_AUTHORIZATION_SCHEMA,
            "authorization_id": start_authorization_id,
            "authorization_mechanism": "CONTROLLED_OS_ACCOUNT",
            "authorized": True,
            "operator_uid": operator_uid,
            "operator_account": operator_account,
            "operation": "PHASE9_A_FORENSIC_REPLAY",
            "project_id": draft.project_id,
            "workflow_id": draft.workflow_id,
            "run_generation": draft.run_generation,
            "source_commit": draft.source_commit,
            "source_tree": draft.source_tree,
            "source_parent": draft.source_parent,
            "source_inventory_sha256": draft.source_inventory_sha256,
            "issued_at": second_now,
            "expires_at": second_now + 300,
            "entry_gate_result_sha256": draft.entry_gate_result_sha256,
            "entry_state_receipt_sha256": evaluation[
                "entry_state_receipt_sha256"
            ],
            "replay_coordinate_sha256": _replay_coordinate_sha256(draft),
            "nonce_sha256": start_nonce_sha256,
            "authorization_target_sha256": start_target_sha256,
            "evidence_attestation_sha256": attestation_sha256,
            "evidence_payload_set_sha256": (
                phase9_replay_evidence_payload_set_sha256(draft)
            ),
            "authorization_scope": {
                "phase9_a_forensic_replay": True,
                "provider_or_network": False,
                "production_outbox_or_delivery": False,
                "release": False,
                "deployment": False,
                "migration": False,
                "cutover": False,
            },
        }
        start = _receipt(start_body, "authorization_receipt_sha256")
        start_raw = canonical_bytes(start)
        start_descriptor = ReplayEvidenceFileV1(
            "start_authorization.json",
            len(start_raw),
            hashlib.sha256(start_raw).hexdigest(),
        )
        final_request = replace(
            draft,
            evidence_files=tuple(
                sorted(
                    (*draft.evidence_files, start_descriptor),
                    key=lambda value: value.logical_path.encode("utf-8"),
                )
            ),
        )
        connection.execute(
            """
            INSERT INTO authority_production_phase9_start_authorizations(
                authorization_id, nonce_sha256, authorization_target_sha256,
                evidence_attestation_sha256, evidence_payload_set_sha256,
                project_id, workflow_id, run_generation, source_commit,
                source_tree, source_parent, source_inventory_sha256,
                entry_gate_result_sha256, entry_state_receipt_sha256,
                start_authorization_byte_length,
                start_authorization_raw_bytes_sha256,
                final_evidence_set_sha256, operator_uid, operator_account,
                issued_at, expires_at, authorization_json,
                authorization_receipt_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                start_authorization_id, start_nonce_sha256,
                start_target_sha256, attestation_sha256,
                phase9_replay_evidence_payload_set_sha256(draft),
                draft.project_id, draft.workflow_id, draft.run_generation,
                draft.source_commit, draft.source_tree, draft.source_parent,
                draft.source_inventory_sha256, draft.entry_gate_result_sha256,
                evaluation["entry_state_receipt_sha256"], len(start_raw),
                start_descriptor.raw_bytes_sha256,
                final_request.evidence_set_sha256, operator_uid,
                operator_account, second_now, second_now + 300,
                canonical_bytes(start).decode("utf-8"),
                start["authorization_receipt_sha256"],
            ),
        )
        _write_new(root / "start_authorization.json", start_raw)
        start_written = True
        observed_final = _request_inventory(root, final_request)
        if observed_final != final_request:
            raise Phase9ReplayEvidenceProducerError(
                "formal start authorization changed the evidence inventory"
            )
        final_values, _ = _read_evidence_set(root, final_request)
        final_evaluation = _evaluate_evidence(
            final_request, final_values, trusted_now=second_now
        )
        if final_evaluation["evidence_attestation_sha256"] != attestation_sha256:
            raise Phase9ReplayEvidenceProducerError(
                "issued start authorization does not bind the attestation"
            )
        _producer_live_precheck(service, connection, final_request, final_evaluation)
        connection.commit()
        return final_request
    except Exception:
        connection.rollback()
        if start_written:
            try:
                (root / "start_authorization.json").unlink()
            except OSError:
                pass
        raise
    finally:
        connection.close()
