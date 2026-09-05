#!/usr/bin/env python3
"""Verify the required Phase 9 command/log graph and derive its summary."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.canonical import canonical_bytes, canonical_sha256
from tools.run_audit_command import (
    COMMAND_SCHEMA,
    DEPENDENCY_INVENTORY_SCHEMA,
    INVENTORY_SCHEMA,
    PREFLIGHT_FAILURE_EXIT_CODE,
    PREFLIGHT_FAILURE_SCHEMA,
    SANDBOX_SCHEMA,
    _dependency_tree_inventory,
    executed_source_inventory,
    parse_outcomes,
)
from tools.trusted_pytest_reporter import (
    TRUSTED_PYTEST_EVENT_SCHEMA,
    validate_trusted_pytest_events,
)
from tools.phase9_composite_evidence import validate_composite_events
from tools.run_full_repo_with_frontend_deps import (
    COMPOSITE_EVENT_SCHEMA,
    COMPOSITE_EVENT_TRANSPORT,
    COMPOSITE_STAGE_IDS,
    composite_stage_contract,
)


SUMMARY_SCHEMA = "paper-factory-phase9-final-test-summary-v8"
SUITE_CONTRACT_SCHEMA = "paper-factory-phase9-test-suite-contract-v3"
OUTCOMES = (
    "collected",
    "passed",
    "failed",
    "errors",
    "skipped",
    "xfailed",
    "xpassed",
    "warnings",
)
RECORD_KEYS = {
    "schema", "id", "suite", "kind", "execution_environment", "attempt_kind",
    "producer", "candidate", "source_inventory", "source_stable",
    "requested_command_argv", "command_argv",
    "source_postcheck", "command_executable", "cwd", "audit_root",
    "dependency_inventory", "dependency_stable", "dependency_postcheck",
    "execution_sandbox",
    "python_executable", "environment", "started_at",
    "completed_at", "duration_milliseconds", "exit_code", "runner_exit_code",
    "outcome_parser",
    "outcomes", "trusted_pytest", "composite_suite", "raw_log", "record_sha256",
}
PREFLIGHT_RECORD_KEYS = {
    "schema", "id", "suite", "kind", "execution_environment", "attempt_kind",
    "producer", "candidate", "source_inventory", "source_stable",
    "source_postcheck", "source_identity_repository", "requested_command_argv",
    "command_executable", "cwd", "audit_root", "python_executable",
    "started_at", "completed_at", "duration_milliseconds", "process_started",
    "exit_code", "runner_exit_code", "failure_stage", "error_type", "raw_log",
    "record_sha256",
}
PREFLIGHT_FAILURE_STAGES = {
    "SUITE_KIND",
    "COMMAND_EXECUTABLE",
    "COMMAND_SHAPE",
    "DEPENDENCY_INVENTORY",
    "TRUSTED_REPORTER_PREPARATION",
    "TEST_SOURCE_PREPARATION",
    "EVENT_PIPE_PREPARATION",
    "ENVIRONMENT_PREPARATION",
    "COMMAND_PREPARATION",
    "SANDBOX_PREPARATION",
}

# This policy is deliberately independent of the candidate-supplied JSON file.
# The file is a readable projection; it cannot shrink the release gate.
PHASE9_REQUIRED_SUITE_SPECS: dict[str, dict[str, object]] = {
    "phase9_focused": {
        "kind": "pytest",
        "required_stages": [],
        "required_targets": [
            "tests/test_phase9_entry_gate.py",
            "tests/test_phase9_run_generation.py",
            "tests/test_phase9_forensic_replay.py",
            "tests/test_phase9_runtime_authority.py",
            "tests/test_phase9_runtime.py",
            "tests/test_json_evidence_view.py",
            "tests/test_authority_production_migration.py",
            "tests/test_phase9_delivery_fence.py",
            "tests/test_phase9_p0_evidence.py",
            "tests/test_phase9_acceptance_probes.py",
            "tests/test_authority_outbox_delivery.py",
            "tests/test_phase5_shadow_supervisor.py",
        ],
        "requirements": [
            "P9-CANDIDATE-IDENTITY", "P9-TRUSTED-TIME",
            "P9-OFFICIAL-INPUT", "P9-START-AUTHORIZATION",
            "P9-GENERATION", "P9-CURRENT-POINTER",
            "P9-TERMINAL-RECEIPT", "P9-PACKET", "P9-THREE-ROLES",
            "P9-TYPED-ABLATION", "P9-LAYERED-VERDICT",
            "P9-REVISION-ATOMIC-SNAPSHOT", "P9-OUTBOX",
            "P9-SUPERVISOR", "P9-REPLAY-EVIDENCE", "P9-ROLLBACK",
            "P9-RUNTIME-DISPATCH",
        ],
    },
    "entry_ar007": {
        "kind": "pytest",
        "required_stages": [],
        "required_targets": [
            "tests/test_phase9_entry_gate.py",
            "tests/test_phase9_p0_evidence.py",
            "tests/test_phase9_delivery_fence.py",
            "tests/test_atomic_release.py",
        ],
        "requirements": [
            "P9-P0-EVIDENCE", "P9-ENTRY", "P9-NINE-RECEIPTS",
            "P9-AR007",
        ],
    },
    "a2_migration": {
        "kind": "pytest",
        "required_stages": [],
        "required_targets": [
            "tests/test_authority_production_migration.py",
            "tests/test_authority_operations.py",
        ],
        "requirements": ["P9-MIGRATION", "P9-ROLLBACK"],
    },
    "phase1_8_continuous": {
        "kind": "pytest",
        "required_stages": [],
        "required_targets": [
            "tests/test_phase1_8_durable_continuous_chain.py",
            "tests/test_m01_runtime_parity.py",
        ],
        "requirements": ["P1-8-CONTINUOUS"],
    },
    "phase7_8_regression": {
        "kind": "pytest",
        "required_stages": [],
        "required_targets": [
            "tests/test_phase78_enabled_e2e.py",
            "tests/test_phase78_bootstrap_contract.py",
            "tests/test_phase5_shadow_supervisor.py",
        ],
        "requirements": ["P7-8-REGRESSION", "P9-SUPERVISOR"],
    },
    "release_workflow": {
        "kind": "pytest",
        "required_stages": [],
        "required_targets": [
            "tests/test_phase9_delivery_fence.py",
            "tests/test_atomic_release.py",
            "tests/test_delivery_contract.py",
            "tests/test_workflow_state.py",
            "tests/test_audit_service.py",
            "tests/test_native_orchestration.py",
            "tests/test_package_submission.py",
        ],
        "requirements": ["P9-AR007", "P9-DELIVERY-FENCE"],
    },
    "full_repository": {
        "kind": "composite",
        "required_targets": ["tools/run_full_repo_with_frontend_deps.py"],
        "required_stages": list(COMPOSITE_STAGE_IDS),
        "composite_stages": composite_stage_contract(),
        "requirements": ["P9-EVIDENCE-CLOSURE", "REPOSITORY-REGRESSION"],
    },
}

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _stable_regular_bytes(path: Path, label: str) -> bytes:
    """Read one non-hardlinked file without following a pathname replacement."""

    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError(f"{label} is not one regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = path.lstat()
    if not (
        _stat_identity(before)
        == _stat_identity(opened)
        == _stat_identity(after)
        == _stat_identity(final)
    ):
        raise RuntimeError(f"{label} changed while read: {path}")
    return b"".join(chunks)


def _read_canonical(path: Path, label: str) -> tuple[dict[str, object], bytes]:
    raw = _stable_regular_bytes(path, label)
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not JSON: {path}") from exc
    if type(value) is not dict or canonical_bytes(value) + b"\n" != raw:
        raise RuntimeError(f"{label} is not canonical: {path}")
    return value, raw


def _safe_relative(value: object, label: str) -> str:
    if type(value) is not str:
        raise RuntimeError(f"{label} path is not text")
    pure = PurePosixPath(value)
    if (
        not value
        or pure.is_absolute()
        or pure.as_posix() != value
        or ".." in pure.parts
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(part.endswith((".", " ")) for part in pure.parts)
    ):
        raise RuntimeError(f"{label} path is not normalized relative POSIX")
    return value


def _artifact(audit_root: Path, value: object, label: str) -> tuple[Path, bytes]:
    if type(value) is not dict or not {"path", "bytes", "sha256"}.issubset(value):
        raise RuntimeError(f"{label} descriptor is invalid")
    relative = _safe_relative(value["path"], label)
    unresolved = audit_root.joinpath(*PurePosixPath(relative).parts)
    current = audit_root
    for part in PurePosixPath(relative).parts[:-1]:
        current /= part
        component = current.lstat()
        if not stat.S_ISDIR(component.st_mode):
            raise RuntimeError(f"{label} parent is not an ordinary directory")
    metadata = unresolved.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError(f"{label} is not one regular file")
    path = unresolved.resolve(strict=True)
    try:
        path.relative_to(audit_root)
    except ValueError as exc:
        raise RuntimeError(f"{label} escapes audit root") from exc
    if (
        type(value.get("bytes")) is not int
        or value["bytes"] < 0
        or type(value.get("sha256")) is not str
        or _HEX64.fullmatch(str(value["sha256"])) is None
    ):
        raise RuntimeError(f"{label} descriptor identity is invalid")
    raw = _stable_regular_bytes(path, label)
    if len(raw) != value["bytes"] or hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise RuntimeError(f"{label} byte identity differs")
    return path, raw


def _lexical_absolute(value: object, label: str) -> Path:
    if type(value) is not str or not Path(value).is_absolute():
        raise RuntimeError(f"{label} is not absolute")
    normalized = os.path.normpath(value)
    if normalized != value:
        raise RuntimeError(f"{label} is not lexically normalized")
    return Path(value)


def _lexically_within(root: Path, value: Path, label: str) -> None:
    try:
        value.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} is outside recorded audit root") from exc


def _tracked_file(inventory: dict[str, object], relative: str) -> dict[str, object]:
    files = inventory.get("files")
    if type(files) is not list:
        raise RuntimeError("source inventory files are invalid")
    matches = [
        item
        for item in files
        if type(item) is dict and item.get("path") == relative
    ]
    if len(matches) != 1:
        raise RuntimeError(f"source inventory does not bind exactly one {relative}")
    return matches[0]


def _verify_inventory(inventory: dict[str, object], record_name: str) -> None:
    if set(inventory) != {
        "schema", "candidate", "path_count", "files", "inventory_sha256"
    } or inventory.get("schema") != INVENTORY_SCHEMA:
        raise RuntimeError(f"source inventory schema differs: {record_name}")
    candidate = inventory.get("candidate")
    if type(candidate) is not dict or set(candidate) != {"commit", "tree", "parent"}:
        raise RuntimeError(f"source inventory candidate differs: {record_name}")
    if any(
        type(candidate.get(name)) is not str
        or _HEX40.fullmatch(str(candidate[name])) is None
        for name in ("commit", "tree", "parent")
    ):
        raise RuntimeError(f"source inventory candidate OID differs: {record_name}")
    files = inventory.get("files")
    if type(files) is not list or not files or inventory.get("path_count") != len(files):
        raise RuntimeError(f"source inventory path count differs: {record_name}")
    paths: list[str] = []
    collisions: set[str] = set()
    for item in files:
        if type(item) is not dict:
            raise RuntimeError(f"source inventory item differs: {record_name}")
        kind = item.get("type")
        expected_keys = (
            {"path", "mode", "type", "object_id", "bytes", "sha256"}
            if kind == "blob"
            else {"path", "mode", "type", "object_id"}
        )
        if set(item) != expected_keys:
            raise RuntimeError(f"source inventory item keys differ: {record_name}")
        relative = _safe_relative(item.get("path"), "inventory")
        collision = unicodedata.normalize("NFC", relative).casefold()
        if collision in collisions:
            raise RuntimeError(f"source inventory path collision: {record_name}")
        collisions.add(collision)
        paths.append(relative)
        if (
            type(item.get("object_id")) is not str
            or _HEX40.fullmatch(str(item["object_id"])) is None
        ):
            raise RuntimeError(f"source inventory object differs: {record_name}")
        if kind == "blob":
            if (
                item.get("mode") not in {"100644", "100755"}
                or type(item.get("bytes")) is not int
                or item["bytes"] < 0
                or type(item.get("sha256")) is not str
                or _HEX64.fullmatch(str(item["sha256"])) is None
            ):
                raise RuntimeError(f"source inventory blob differs: {record_name}")
        elif kind != "commit" or item.get("mode") != "160000":
            raise RuntimeError(f"source inventory type differs: {record_name}")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError(f"source inventory paths are not canonical: {record_name}")


def _parse_utc(value: object, label: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise RuntimeError(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeError(f"{label} is not a datetime") from exc
    if parsed.tzinfo != UTC:
        raise RuntimeError(f"{label} is not UTC")
    return parsed


def _runtime_candidate_identity(repository: Path) -> dict[str, str]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }

    def query(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *arguments],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            env=environment,
        ).stdout.decode("ascii").strip()

    line = query("rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(line) != 2:
        raise RuntimeError("runtime candidate must have exactly one parent")
    return {
        "commit": line[0],
        "tree": query("rev-parse", "HEAD^{tree}"),
        "parent": line[1],
    }


def _verify_environment(
    value: object, *, recorded_audit_root: Path, cwd: Path, record_name: str,
    runtime_validation: bool, composite: bool,
) -> None:
    if type(value) is not dict or set(value) != {
        "policy", "inherited", "variables", "removed_host_variable_names",
        "environment_sha256",
    }:
        raise RuntimeError(f"command environment descriptor differs: {record_name}")
    if (
        value.get("inherited") is not False
        or value.get("policy") != "paper-factory-sanitized-audit-environment-v1"
    ):
        raise RuntimeError(f"command environment is not sanitized: {record_name}")
    variables = value.get("variables")
    expected_variables = {
        "PATH", "HOME", "XDG_CACHE_HOME", "TMPDIR", "LC_ALL", "PYTHONHASHSEED",
        "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PHASE9_TRUSTED_PYTEST_REPORTER_PATH",
        "PHASE9_TRUSTED_PYTEST_SITE_PACKAGES",
        "PHASE9_TEST_SOURCE_REPOSITORY", "PHASE9_TRUSTED_PYTEST_EVENT_PATH",
        "PHASE9_TRUSTED_PYTEST_EVENT_FD", "PHASE9_TRUSTED_PYTEST_NONCE",
    }
    if composite:
        expected_variables.update(
            {
                "PHASE9_TRUSTED_COMPOSITE_EVENT_PATH",
                "PHASE9_TRUSTED_COMPOSITE_EVENT_FD",
                "PHASE9_TRUSTED_COMPOSITE_NONCE",
            }
        )
    if type(variables) is not dict or set(variables) != expected_variables:
        raise RuntimeError(f"command environment allowlist differs: {record_name}")
    fixed = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    source_repository = _lexical_absolute(
        variables.get("PHASE9_TEST_SOURCE_REPOSITORY"),
        f"test source repository: {record_name}",
    )
    if (
        runtime_validation and not (source_repository / ".git").exists()
    ):
        raise RuntimeError(
            f"test source identity repository differs: {record_name}"
        )
    if any(variables.get(name) != expected for name, expected in fixed.items()):
        raise RuntimeError(f"command environment values differ: {record_name}")
    reporter_path = _lexical_absolute(
        variables.get("PHASE9_TRUSTED_PYTEST_REPORTER_PATH"),
        f"trusted reporter path: {record_name}",
    )
    _lexically_within(
        recorded_audit_root, reporter_path,
        f"trusted reporter path: {record_name}",
    )
    site_packages = _lexical_absolute(
        variables.get("PHASE9_TRUSTED_PYTEST_SITE_PACKAGES"),
        f"trusted pytest site-packages: {record_name}",
    )
    if runtime_validation and not (site_packages / "pytest/__init__.py").is_file():
        raise RuntimeError(f"trusted pytest site-packages differs: {record_name}")
    if variables.get("PHASE9_TRUSTED_PYTEST_EVENT_PATH") != (
        "PARENT_CAPTURED_ANONYMOUS_PIPE"
    ):
        raise RuntimeError(f"trusted event transport differs: {record_name}")
    event_fd = variables.get("PHASE9_TRUSTED_PYTEST_EVENT_FD")
    if (
        type(event_fd) is not str
        or not event_fd.isdecimal()
        or int(event_fd) < 3
    ):
        raise RuntimeError(f"trusted event descriptor differs: {record_name}")
    nonce = variables.get("PHASE9_TRUSTED_PYTEST_NONCE")
    if type(nonce) is not str or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise RuntimeError(f"trusted event nonce differs: {record_name}")
    if composite:
        if variables.get("PHASE9_TRUSTED_COMPOSITE_EVENT_PATH") != (
            "PARENT_CAPTURED_ANONYMOUS_PIPE"
        ):
            raise RuntimeError(f"trusted composite transport differs: {record_name}")
        composite_fd = variables.get("PHASE9_TRUSTED_COMPOSITE_EVENT_FD")
        composite_nonce = variables.get("PHASE9_TRUSTED_COMPOSITE_NONCE")
        if (
            type(composite_fd) is not str
            or not composite_fd.isdecimal()
            or int(composite_fd) < 3
            or composite_fd == event_fd
            or type(composite_nonce) is not str
            or re.fullmatch(r"[0-9a-f]{32}", composite_nonce) is None
        ):
            raise RuntimeError(f"trusted composite coordinate differs: {record_name}")
    for name in ("HOME", "XDG_CACHE_HOME", "TMPDIR"):
        path = _lexical_absolute(
            variables.get(name), f"command {name}: {record_name}"
        )
        _lexically_within(recorded_audit_root, path, f"command {name}: {record_name}")
    removed = value.get("removed_host_variable_names")
    if (
        type(removed) is not list
        or any(type(item) is not str for item in removed)
        or removed != sorted(set(removed))
    ):
        raise RuntimeError(f"removed environment names differ: {record_name}")
    unsigned = dict(value)
    environment_sha = unsigned.pop("environment_sha256")
    if environment_sha != canonical_sha256(unsigned):
        raise RuntimeError(f"command environment hash differs: {record_name}")


def _verify_runner_and_command(
    value: dict[str, object],
    inventory: dict[str, object],
    suite: dict[str, object],
    record_name: str,
    audit_root: Path,
    recorded_audit_root: Path,
    runtime_validation: bool,
) -> tuple[
    tuple[str, ...], Path | None, Path | None, Path | None,
    Path | None, Path | None, Path,
]:
    producer = value.get("producer")
    if type(producer) is not dict or set(producer) != {
        "type", "version", "path", "bytes", "sha256"
    }:
        raise RuntimeError(f"command producer descriptor differs: {record_name}")
    if (
        producer.get("type") != "PAPER_FACTORY_AUDIT_RUNNER"
        or producer.get("version") != COMMAND_SCHEMA
        or producer.get("path") != "tools/run_audit_command.py"
    ):
        raise RuntimeError(f"command producer identity differs: {record_name}")
    tracked_runner = _tracked_file(inventory, "tools/run_audit_command.py")
    if (
        producer.get("bytes") != tracked_runner.get("bytes")
        or producer.get("sha256") != tracked_runner.get("sha256")
    ):
        raise RuntimeError(f"command producer bytes differ: {record_name}")

    argv_value = value.get("command_argv")
    if type(argv_value) is not list or not argv_value or any(
        type(item) is not str for item in argv_value
    ):
        raise RuntimeError(f"command argv is invalid: {record_name}")
    argv = list(argv_value)
    requested_value = value.get("requested_command_argv")
    if type(requested_value) is not list or not requested_value or any(
        type(item) is not str for item in requested_value
    ):
        raise RuntimeError(f"requested command argv is invalid: {record_name}")
    requested = list(requested_value)
    executable = value.get("command_executable")
    if type(executable) is not dict or set(executable) != {
        "path", "resolved_path", "bytes", "sha256"
    }:
        raise RuntimeError(f"command executable descriptor differs: {record_name}")
    executable_path = _lexical_absolute(
        argv[0], f"command executable path: {record_name}"
    )
    recorded_launcher = _lexical_absolute(
        executable.get("path"), f"recorded Python launcher: {record_name}"
    )
    recorded_resolved = _lexical_absolute(
        executable.get("resolved_path"), f"resolved Python path: {record_name}"
    )
    if recorded_launcher != executable_path:
        raise RuntimeError(f"recorded Python launcher differs: {record_name}")
    if runtime_validation:
        trusted_launcher = Path(os.path.abspath(sys.executable))
        if executable_path != trusted_launcher:
            raise RuntimeError(f"command used an untrusted Python launcher: {record_name}")
        resolved_executable = executable_path.resolve(strict=True)
        if (
            recorded_resolved != resolved_executable
        ):
            raise RuntimeError(f"command executable path differs: {record_name}")
    if value.get("python_executable") != executable:
        raise RuntimeError(f"test command is not bound to Python: {record_name}")
    if (
        type(executable.get("bytes")) is not int
        or executable["bytes"] <= 0
        or type(executable.get("sha256")) is not str
        or _HEX64.fullmatch(str(executable["sha256"])) is None
    ):
        raise RuntimeError(f"command executable identity differs: {record_name}")
    if runtime_validation:
        try:
            executable_metadata = resolved_executable.lstat()
            executable_raw = resolved_executable.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"command executable is unavailable: {record_name}") from exc
        if (
            not stat.S_ISREG(executable_metadata.st_mode)
            or len(executable_raw) != executable.get("bytes")
            or hashlib.sha256(executable_raw).hexdigest() != executable.get("sha256")
        ):
            raise RuntimeError(f"command executable bytes differ: {record_name}")

    cwd = _lexical_absolute(value["cwd"], f"command cwd: {record_name}")
    if runtime_validation:
        cwd = cwd.resolve(strict=True)
    if suite["required_targets"] == ["tools/run_full_repo_with_frontend_deps.py"]:
        requested_prefix = [argv[0], "-B"] + [
            "tools/run_full_repo_with_frontend_deps.py", "--source-root",
            str(cwd), "--dependency-target",
        ]
        expected_prefix = [argv[0], "-I", "-S", *requested_prefix[1:]]
        if argv[: len(expected_prefix)] != expected_prefix:
            raise RuntimeError(f"isolated full-suite command prefix differs: {record_name}")
        if len(argv) != len(expected_prefix) + 13:
            raise RuntimeError(f"full-suite command shape differs: {record_name}")
        dependency = Path(argv[len(expected_prefix)])
        suffix = argv[len(expected_prefix) + 1 :]
        if (
            not dependency.is_absolute()
            or (runtime_validation and not dependency.resolve(strict=True).is_dir())
            or len(suffix) != 12
            or suffix[0] != "--browser-root"
            or suffix[2] != "--browser-executable"
            or suffix[4] != "--node"
            or suffix[6] != "--npm"
            or suffix[8:10] != ["--python", argv[0]]
            or suffix[10] != "--basetemp"
        ):
            raise RuntimeError(f"full-suite command binding differs: {record_name}")
        browser_root = _lexical_absolute(
            suffix[1], f"full-suite browser root: {record_name}"
        )
        browser_executable = _lexical_absolute(
            suffix[3], f"full-suite browser executable: {record_name}"
        )
        node = _lexical_absolute(suffix[5], f"full-suite node: {record_name}")
        npm = _lexical_absolute(suffix[7], f"full-suite npm: {record_name}")
        if runtime_validation:
            try:
                browser_root.resolve(strict=True)
                browser_executable.resolve(strict=True).relative_to(
                    browser_root.resolve(strict=True)
                )
                node.resolve(strict=True)
                npm.resolve(strict=True)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"full-suite runtime is unavailable: {record_name}"
                ) from exc
        if requested != [*requested_prefix, *argv[len(expected_prefix):]]:
            raise RuntimeError(f"full-suite requested/actual command differs: {record_name}")
        basetemp = _lexical_absolute(
            suffix[11], f"full-suite basetemp: {record_name}"
        )
        _lexically_within(
            recorded_audit_root, basetemp,
            f"full-suite basetemp: {record_name}",
        )
        _tracked_file(inventory, "tools/run_full_repo_with_frontend_deps.py")
        normalized = tuple(str(item) for item in suite["required_targets"])
        dependency_path: Path | None = dependency
    else:
        expected_targets = [str(item) for item in suite["required_targets"]]
        requested_prefix = [argv[0], "-B"] + [
            "-m", "pytest", "-q", "-p", "no:cacheprovider",
        ]
        if requested[: len(requested_prefix)] != requested_prefix:
            raise RuntimeError(f"requested pytest command prefix differs: {record_name}")
        if len(requested) != len(requested_prefix) + 1 + len(expected_targets):
            raise RuntimeError(f"requested pytest command shape differs: {record_name}")
        basetemp_argument = requested[len(requested_prefix)]
        if not basetemp_argument.startswith("--basetemp="):
            raise RuntimeError(f"pytest basetemp is missing: {record_name}")
        basetemp = _lexical_absolute(
            basetemp_argument.split("=", 1)[1],
            f"pytest basetemp: {record_name}",
        )
        _lexically_within(
            recorded_audit_root, basetemp, f"pytest basetemp: {record_name}"
        )
        actual_targets = requested[len(requested_prefix) + 1 :]
        if actual_targets != expected_targets:
            raise RuntimeError(f"pytest target set differs: {record_name}")
        for target in actual_targets:
            _tracked_file(inventory, target)
        reporter_path = value["trusted_pytest"]["runtime_path"]
        runtime_site_packages = value["trusted_pytest"]["runtime_site_packages"]
        actual_prefix = [
            argv[0], "-I", "-S", "-B", reporter_path,
            "--runtime-site-packages", runtime_site_packages,
            "--source-root", str(cwd), "--", "-q", "-p", "no:cacheprovider",
            "--noconftest",
            "-c", "/dev/null", "--rootdir", str(cwd), "-o", "addopts=",
        ]
        if argv != [
            *actual_prefix, basetemp_argument, *expected_targets
        ]:
            raise RuntimeError(f"isolated pytest command shape differs: {record_name}")
        normalized = tuple(actual_targets)
        dependency_path = None
        browser_root = browser_executable = node = npm = None
    return (
        normalized, dependency_path, browser_root, browser_executable,
        node, npm, basetemp,
    )


def _verify_external_component(
    value: object, *, kind: str, root: Path, record_name: str,
) -> None:
    keys = {
        "kind", "root", "path_count", "regular_file_count", "symlink_count",
        "total_file_bytes", "tree_sha256", "files",
    }
    if kind == "PLAYWRIGHT_BROWSER_RUNTIME":
        keys.add("executable")
    if type(value) is not dict or set(value) != keys:
        raise RuntimeError(f"dependency component shape differs: {record_name}")
    if value.get("kind") != kind or value.get("root") != str(root):
        raise RuntimeError(f"dependency component identity differs: {record_name}")
    for key in ("path_count", "regular_file_count", "symlink_count", "total_file_bytes"):
        if type(value.get(key)) is not int or int(value[key]) < 0:
            raise RuntimeError(f"dependency component count differs: {record_name}")
    if (
        int(value["path_count"]) <= 0
        or int(value["regular_file_count"]) <= 0
        or int(value["total_file_bytes"]) <= 0
        or type(value.get("tree_sha256")) is not str
        or _HEX64.fullmatch(str(value["tree_sha256"])) is None
    ):
        raise RuntimeError(f"dependency component content differs: {record_name}")
    files = value.get("files")
    if type(files) is not list or len(files) != value["path_count"]:
        raise RuntimeError(f"dependency component file list differs: {record_name}")
    paths: list[str] = []
    collisions: set[str] = set()
    regular_count = 0
    symlink_count = 0
    byte_total = 0
    for item in files:
        if type(item) is not dict:
            raise RuntimeError(f"dependency component row differs: {record_name}")
        relative = _safe_relative(item.get("path"), "dependency inventory")
        collision = unicodedata.normalize("NFC", relative).casefold()
        if collision in collisions:
            raise RuntimeError(f"dependency component path collision: {record_name}")
        collisions.add(collision)
        paths.append(relative)
        item_kind = item.get("type")
        if item_kind == "file":
            if set(item) != {"path", "type", "mode", "bytes", "sha256"}:
                raise RuntimeError(f"dependency file row differs: {record_name}")
            if (
                type(item.get("bytes")) is not int
                or int(item["bytes"]) < 0
                or type(item.get("sha256")) is not str
                or _HEX64.fullmatch(str(item["sha256"])) is None
            ):
                raise RuntimeError(f"dependency file identity differs: {record_name}")
            regular_count += 1
            byte_total += int(item["bytes"])
        elif item_kind == "symlink":
            if set(item) != {"path", "type", "mode", "target"} or type(
                item.get("target")
            ) is not str:
                raise RuntimeError(f"dependency symlink row differs: {record_name}")
            symlink_count += 1
        elif item_kind == "directory":
            if set(item) != {"path", "type", "mode"}:
                raise RuntimeError(f"dependency directory row differs: {record_name}")
        else:
            raise RuntimeError(f"dependency row type differs: {record_name}")
        if type(item.get("mode")) is not str or re.fullmatch(
            r"[0-7]{4}", str(item["mode"])
        ) is None:
            raise RuntimeError(f"dependency row mode differs: {record_name}")
    if (
        paths != sorted(paths)
        or len(paths) != len(set(paths))
        or regular_count != value["regular_file_count"]
        or symlink_count != value["symlink_count"]
        or byte_total != value["total_file_bytes"]
        or value["tree_sha256"] != canonical_sha256(files)
    ):
        raise RuntimeError(f"dependency component closure differs: {record_name}")
    if kind == "PLAYWRIGHT_BROWSER_RUNTIME":
        executable = value.get("executable")
        if type(executable) is not dict or set(executable) != {
            "relative_path", "bytes", "sha256"
        }:
            raise RuntimeError(f"browser executable descriptor differs: {record_name}")
        matches = [
            item for item in files
            if item.get("type") == "file"
            and item.get("path") == executable.get("relative_path")
        ]
        if len(matches) != 1 or any(
            executable.get(field) != matches[0].get(field)
            for field in ("bytes", "sha256")
        ):
            raise RuntimeError(f"browser executable inventory differs: {record_name}")


def _verify_dependency_inventory(
    value: object,
    *,
    dependency: Path | None,
    browser_root: Path | None,
    browser_executable: Path | None,
    cwd: Path,
    source_inventory: dict[str, object],
    record_name: str,
    runtime_validation: bool,
) -> None:
    keys = {
        "schema", "kind", "lockfile_sha256", "node_modules",
        "browser_runtime", "path_count", "inventory_sha256",
    }
    if type(value) is not dict or set(value) != keys:
        raise RuntimeError(f"dependency inventory shape differs: {record_name}")
    if value.get("schema") != DEPENDENCY_INVENTORY_SCHEMA:
        raise RuntimeError(f"dependency inventory schema differs: {record_name}")
    unsigned = dict(value)
    inventory_digest = unsigned.pop("inventory_sha256")
    if inventory_digest != canonical_sha256(unsigned):
        raise RuntimeError(f"dependency inventory self-hash differs: {record_name}")
    if dependency is None:
        expected = {
            "schema": DEPENDENCY_INVENTORY_SCHEMA,
            "kind": "NONE",
            "lockfile_sha256": None,
            "node_modules": None,
            "browser_runtime": None,
            "path_count": 0,
        }
        expected["inventory_sha256"] = canonical_sha256(expected)
        if value != expected or browser_root is not None or browser_executable is not None:
            raise RuntimeError(f"unexpected dependency inventory: {record_name}")
        return
    if browser_root is None or browser_executable is None:
        raise RuntimeError(f"browser dependency coordinate is absent: {record_name}")
    if value.get("kind") != "FULL_REPOSITORY_DEPENDENCIES":
        raise RuntimeError(f"dependency inventory kind differs: {record_name}")
    locked_source = _tracked_file(source_inventory, "web/frontend/package-lock.json")
    if value.get("lockfile_sha256") != locked_source.get("sha256"):
        raise RuntimeError(f"dependency lock/source bytes differ: {record_name}")
    node = value.get("node_modules")
    browser = value.get("browser_runtime")
    _verify_external_component(
        node, kind="FRONTEND_NODE_MODULES", root=dependency,
        record_name=record_name,
    )
    _verify_external_component(
        browser, kind="PLAYWRIGHT_BROWSER_RUNTIME", root=browser_root,
        record_name=record_name,
    )
    if value.get("path_count") != int(node["path_count"]) + int(browser["path_count"]):
        raise RuntimeError(f"dependency inventory aggregate differs: {record_name}")
    executable_relative = (
        browser_executable.resolve(strict=True).relative_to(
            browser_root.resolve(strict=True)
        ).as_posix()
        if runtime_validation
        else browser_executable.relative_to(browser_root).as_posix()
    )
    if browser["executable"].get("relative_path") != executable_relative:
        raise RuntimeError(f"browser executable binding differs: {record_name}")
    if runtime_validation:
        try:
            observed = _dependency_tree_inventory(
                dependency.resolve(strict=True), cwd,
                browser_root=browser_root.resolve(strict=True),
                browser_executable=browser_executable.resolve(strict=True),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f"dependency inventory cannot be reproduced: {record_name}"
            ) from exc
        if observed != value:
            raise RuntimeError(f"runtime dependency bytes differ: {record_name}")


def _verify_execution_sandbox(
    value: object,
    *,
    command: list[str],
    environment: dict[str, object],
    cwd: Path,
    audit_root: Path,
    dependency: Path | None,
    browser_root: Path | None,
    identifier: str,
    record_name: str,
    runtime_validation: bool,
) -> None:
    keys = {
        "schema", "policy", "binary", "source_mount", "audit_mount",
        "writable_mounts", "source_runtime_overlay", "source_runtime_mounts",
        "python_environment_mount", "masked_host_project", "dependency_mount",
        "browser_runtime_mount", "git_object_mount", "system_tmp_mount",
        "frontend_overlay", "network_namespace", "event_transport",
        "composite_event_transport",
        "launcher_argv", "sandbox_sha256",
    }
    if type(value) is not dict or set(value) != keys:
        raise RuntimeError(f"execution sandbox descriptor differs: {record_name}")
    unsigned = dict(value)
    declared_hash = unsigned.pop("sandbox_sha256")
    if declared_hash != canonical_sha256(unsigned):
        raise RuntimeError(f"execution sandbox hash differs: {record_name}")
    binary = value.get("binary")
    if type(binary) is not dict or set(binary) != {"path", "bytes", "sha256"}:
        raise RuntimeError(f"execution sandbox binary differs: {record_name}")
    if (
        binary.get("path") != "/usr/bin/bwrap"
        or type(binary.get("bytes")) is not int
        or int(binary["bytes"]) <= 0
        or type(binary.get("sha256")) is not str
        or _HEX64.fullmatch(str(binary["sha256"])) is None
        or value.get("schema") != SANDBOX_SCHEMA
        or value.get("policy")
        != "READ_ONLY_SOURCE_AND_DEPENDENCIES_NETWORKLESS"
        or value.get("network_namespace") != "UNSHARED"
        or value.get("source_mount") != {"path": str(cwd), "access": "READ_ONLY"}
        or value.get("audit_mount")
        != {"path": str(audit_root), "access": "READ_ONLY"}
    ):
        raise RuntimeError(f"execution sandbox policy differs: {record_name}")
    environment_root = Path(command[0]).parent.parent
    host_project = (
        environment_root.parent.resolve(strict=True)
        if runtime_validation
        else environment_root.parent
    )
    if (
        value.get("python_environment_mount")
        != {"path": str(environment_root), "access": "READ_ONLY"}
        or value.get("masked_host_project") != str(host_project)
    ):
        raise RuntimeError(f"execution sandbox Python isolation differs: {record_name}")
    variables = environment.get("variables")
    if type(variables) is not dict:
        raise RuntimeError(f"execution sandbox environment differs: {record_name}")
    git_mount = value.get("git_object_mount")
    if git_mount is not None and (
        type(git_mount) is not dict
        or set(git_mount) != {"path", "access"}
        or git_mount.get("access") != "READ_ONLY"
        or type(git_mount.get("path")) is not str
        or not Path(str(git_mount["path"])).is_absolute()
    ):
        raise RuntimeError(f"execution sandbox Git mount differs: {record_name}")
    if runtime_validation:
        expected_git_mount = None
        if (cwd / ".git").exists() or (cwd / ".git").is_symlink():
            result = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=cwd,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "PATH": "/usr/bin:/bin", "LC_ALL": "C",
                    "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                },
            )
            common = Path(result.stdout.strip())
            if not common.is_absolute():
                common = cwd / common
            common = common.resolve(strict=True)
            try:
                common.relative_to(cwd)
            except ValueError:
                expected_git_mount = {
                    "path": str(common), "access": "READ_ONLY"
                }
        if git_mount != expected_git_mount:
            raise RuntimeError(
                f"execution sandbox live Git mount differs: {record_name}"
            )
    overlay = (
        None
        if dependency is None
        else str(audit_root / "runtime" / f"{identifier}-frontend-overlay")
    )
    expected_dependency_mount = (
        None
        if dependency is None
        else {"path": str(dependency), "access": "READ_ONLY"}
    )
    if (
        value.get("dependency_mount") != expected_dependency_mount
        or value.get("frontend_overlay") != overlay
    ):
        raise RuntimeError(f"execution sandbox dependency isolation differs: {record_name}")
    expected_browser_mount = (
        None
        if browser_root is None
        else {"path": str(browser_root), "access": "READ_ONLY"}
    )
    if value.get("browser_runtime_mount") != expected_browser_mount:
        raise RuntimeError(f"execution sandbox browser isolation differs: {record_name}")
    event_fd = variables.get("PHASE9_TRUSTED_PYTEST_EVENT_FD")
    expected_event_transport = {
        "kind": "PARENT_CAPTURED_ANONYMOUS_PIPE",
        "write_fd": int(str(event_fd)) if str(event_fd).isdecimal() else -1,
        "tested_process_access": "WRITE_ONLY_APPEND_STREAM",
    }
    if value.get("event_transport") != expected_event_transport:
        raise RuntimeError(f"execution sandbox event transport differs: {record_name}")
    composite_fd = variables.get("PHASE9_TRUSTED_COMPOSITE_EVENT_FD")
    expected_composite_transport = (
        None
        if dependency is None
        else {
            "kind": "PARENT_CAPTURED_ANONYMOUS_PIPE",
            "write_fd": (
                int(str(composite_fd)) if str(composite_fd).isdecimal() else -1
            ),
            "tested_process_access": "WRITE_ONLY_APPEND_STREAM",
        }
    )
    if value.get("composite_event_transport") != expected_composite_transport:
        raise RuntimeError(
            f"execution sandbox composite transport differs: {record_name}"
        )
    if dependency is None:
        basetemp_argument = next(
            (
                item.removeprefix("--basetemp=")
                for item in command
                if item.startswith("--basetemp=")
            ),
            None,
        )
    else:
        basetemp_argument = command[-1]
    basetemp = _lexical_absolute(
        basetemp_argument, f"sandbox basetemp: {record_name}"
    )
    expected_basetemp = (
        audit_root / "runtime" / f"{identifier}-pytest" / "basetemp"
    )
    if basetemp != expected_basetemp:
        raise RuntimeError(f"sandbox basetemp coordinate differs: {record_name}")
    writable = [
        ("HOME", _lexical_absolute(variables.get("HOME"), "sandbox HOME")),
        (
            "XDG_CACHE_HOME",
            _lexical_absolute(variables.get("XDG_CACHE_HOME"), "sandbox cache"),
        ),
        ("TMPDIR", _lexical_absolute(variables.get("TMPDIR"), "sandbox tmp")),
        ("PYTEST_BASETEMP_PARENT", basetemp.parent),
        (
            "SOURCE_ONGOING",
            audit_root / "runtime" / f"{identifier}-source-state" / "ongoing",
        ),
        (
            "SOURCE_RUN_STATE",
            audit_root / "runtime" / f"{identifier}-source-state" / "run_state",
        ),
        (
            "SOURCE_LOGS",
            audit_root / "runtime" / f"{identifier}-source-state" / "logs",
        ),
        (
            "SOURCE_PAPERS",
            audit_root / "runtime" / f"{identifier}-source-state" / "papers",
        ),
    ]
    expected_writable = [
        {"purpose": label, "path": str(path), "access": "READ_WRITE"}
        for label, path in writable
    ]
    if value.get("writable_mounts") != expected_writable:
        raise RuntimeError(f"execution sandbox writable mounts differ: {record_name}")
    expected_system_tmp = {
        "source": str(writable[2][1]),
        "target": "/tmp",
        "access": "READ_WRITE_PRIVATE",
    }
    if value.get("system_tmp_mount") != expected_system_tmp:
        raise RuntimeError(f"execution sandbox /tmp mount differs: {record_name}")
    source_overlay = audit_root / "runtime" / f"{identifier}-source-overlay"
    expected_state_mounts = [
        {
            "purpose": label,
            "source": str(source),
            "target": str(cwd / source.name),
            "access": "READ_WRITE_PRIVATE",
        }
        for label, source in writable[-4:]
    ]
    if (
        value.get("source_runtime_overlay") != str(source_overlay)
        or value.get("source_runtime_mounts") != expected_state_mounts
    ):
        raise RuntimeError(f"execution sandbox source-state mounts differ: {record_name}")
    expected = [
        "/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
        "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
        "--ro-bind", str(cwd), str(cwd),
        "--ro-bind", str(source_overlay), str(source_overlay),
        "--overlay-src", str(cwd), "--overlay-src", str(source_overlay),
        "--ro-overlay", str(cwd),
        "--tmpfs", str(host_project),
        "--ro-bind", str(environment_root), str(environment_root),
    ]
    if git_mount is not None:
        expected.extend(
            ["--ro-bind", str(git_mount["path"]), str(git_mount["path"])]
        )
    for _label, item in writable:
        expected.extend(["--bind", str(item), str(item)])
    for mount in expected_state_mounts:
        expected.extend(["--bind", mount["source"], mount["target"]])
    expected.extend(["--bind", str(writable[2][1]), "/tmp"])
    if dependency is not None:
        frontend = cwd / "web/frontend"
        expected.extend(
            [
                "--ro-bind", str(dependency), str(dependency),
                "--ro-bind", str(overlay), str(overlay),
                "--overlay-src", str(frontend),
                "--overlay-src", str(overlay),
                "--ro-overlay", str(frontend),
            ]
        )
    if browser_root is not None:
        expected.extend(
            ["--ro-bind", str(browser_root), str(browser_root)]
        )
    expected.extend(["--chdir", str(cwd), "--clearenv"])
    for name, item in sorted(variables.items()):
        expected.extend(["--setenv", str(name), str(item)])
    expected.extend(["--", *command])
    if value.get("launcher_argv") != expected:
        raise RuntimeError(f"execution sandbox argv differs: {record_name}")
    if runtime_validation:
        sandbox_path = Path("/usr/bin/bwrap")
        try:
            info = sandbox_path.lstat()
            raw = sandbox_path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"execution sandbox is unavailable: {record_name}") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or len(raw) != binary["bytes"]
            or hashlib.sha256(raw).hexdigest() != binary["sha256"]
        ):
            raise RuntimeError(f"execution sandbox bytes differ: {record_name}")


def _suite_contract(
    path: Path,
    *,
    expected_specs: dict[str, dict[str, object]],
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    value, _ = _read_canonical(path, "suite contract")
    if value.get("schema") != SUITE_CONTRACT_SCHEMA or set(value) != {
        "schema",
        "required_environments",
        "suites",
    }:
        raise RuntimeError("suite contract schema/keys differ")
    if value["required_environments"] != ["fresh", "source"]:
        raise RuntimeError("suite contract must require fresh and source")
    suites = value["suites"]
    if type(suites) is not list or not suites:
        raise RuntimeError("suite contract is empty")
    result: dict[str, dict[str, object]] = {}
    for item in suites:
        if type(item) is not dict:
            raise RuntimeError("suite contract entry differs")
        expected_keys = {
            "id",
            "kind",
            "requirements",
            "description",
            "required_targets",
            "required_stages",
        }
        if item.get("kind") == "composite":
            expected_keys.add("composite_stages")
        if set(item) != expected_keys:
            raise RuntimeError("suite contract entry differs")
        identifier = item["id"]
        if type(identifier) is not str or identifier in result:
            raise RuntimeError("suite contract ID is invalid or duplicated")
        if item["kind"] not in {"pytest", "composite"}:
            raise RuntimeError(f"suite contract kind is invalid: {identifier}")
        if type(item["requirements"]) is not list or not item["requirements"]:
            raise RuntimeError(f"suite requirements are empty: {identifier}")
        if type(item["required_targets"]) is not list or not item["required_targets"]:
            raise RuntimeError(f"suite targets are empty: {identifier}")
        if (
            type(item["required_stages"]) is not list
            or any(type(stage) is not str for stage in item["required_stages"])
            or len(item["required_stages"]) != len(set(item["required_stages"]))
            or (
                item["kind"] == "composite"
                and item["required_stages"] != list(COMPOSITE_STAGE_IDS)
            )
            or (
                item["kind"] == "composite"
                and item.get("composite_stages") != composite_stage_contract()
            )
            or (item["kind"] == "pytest" and item["required_stages"])
        ):
            raise RuntimeError(f"suite stage contract differs: {identifier}")
        result[identifier] = item
    if set(result) != set(expected_specs):
        raise RuntimeError("suite contract cannot shrink or expand the required suite set")
    for identifier, expected in expected_specs.items():
        actual = result[identifier]
        if any(actual.get(field) != expected[field] for field in expected):
            raise RuntimeError(
                f"suite contract differs from independent policy: {identifier}"
            )
    return value, result


def _verify_trusted_pytest(
    value: object,
    *,
    record: dict[str, object],
    inventory: dict[str, object],
    suite: dict[str, object],
    audit_root: Path,
    recorded_audit_root: Path,
    cwd: Path,
    raw_log: bytes,
    runtime_validation: bool,
    record_name: str,
    reconcile_terminal: bool = True,
) -> dict[str, object] | None:
    keys = {
        "schema", "module", "source_path", "source_bytes", "source_sha256",
        "runtime_path", "runtime_site_packages", "nonce", "candidate_conftest",
        "candidate_pytest_config", "event_transport", "validation", "validation_error",
        "outcomes", "node_outcomes", "event_artifact",
    }
    if type(value) is not dict or set(value) != keys:
        raise RuntimeError(f"trusted pytest descriptor differs: {record_name}")
    tracked = _tracked_file(inventory, "tools/trusted_pytest_reporter.py")
    if (
        value.get("schema") != TRUSTED_PYTEST_EVENT_SCHEMA
        or value.get("module") != "phase9_trusted_reporter"
        or value.get("source_path") != "tools/trusted_pytest_reporter.py"
        or value.get("source_bytes") != tracked.get("bytes")
        or value.get("source_sha256") != tracked.get("sha256")
        or value.get("candidate_conftest") != "DISABLED"
        or value.get("candidate_pytest_config") != "DISABLED"
        or value.get("event_transport") != "PARENT_CAPTURED_ANONYMOUS_PIPE"
    ):
        raise RuntimeError(f"trusted pytest producer bytes differ: {record_name}")
    runtime_path = _lexical_absolute(
        value.get("runtime_path"), f"trusted reporter runtime: {record_name}"
    )
    _lexically_within(
        recorded_audit_root, runtime_path,
        f"trusted reporter runtime: {record_name}",
    )
    if runtime_path.name != "phase9_trusted_reporter.py":
        raise RuntimeError(f"trusted reporter runtime name differs: {record_name}")
    variables = record["environment"]["variables"]
    if variables["PHASE9_TRUSTED_PYTEST_REPORTER_PATH"] != str(runtime_path):
        raise RuntimeError(f"trusted reporter import/runtime differs: {record_name}")
    if variables["PHASE9_TRUSTED_PYTEST_SITE_PACKAGES"] != value.get(
        "runtime_site_packages"
    ):
        raise RuntimeError(f"trusted pytest runtime binding differs: {record_name}")
    if runtime_validation:
        runtime_raw = _stable_regular_bytes(runtime_path, "trusted reporter runtime")
        if (
            len(runtime_raw) != value["source_bytes"]
            or hashlib.sha256(runtime_raw).hexdigest() != value["source_sha256"]
        ):
            raise RuntimeError(f"trusted reporter runtime bytes differ: {record_name}")
    nonce = value.get("nonce")
    if nonce != variables.get("PHASE9_TRUSTED_PYTEST_NONCE"):
        raise RuntimeError(f"trusted reporter nonce differs: {record_name}")
    event_path, event_raw = _artifact(
        audit_root, value.get("event_artifact"), "trusted pytest events"
    )
    identifier = str(record["id"])
    if event_path.relative_to(audit_root).as_posix() != (
        f"evidence/pytest_events/{identifier}.jsonl"
    ):
        raise RuntimeError(f"trusted event artifact path differs: {record_name}")
    is_full = suite["required_targets"] == [
        "tools/run_full_repo_with_frontend_deps.py"
    ]
    try:
        result = validate_trusted_pytest_events(
            event_raw,
            nonce=str(nonce),
            expected_rootdir=str(cwd),
            expected_targets=() if is_full else tuple(suite["required_targets"]),
            full_repository=is_full,
            expected_transport="PARENT_CAPTURED_ANONYMOUS_PIPE",
        )
    except (UnicodeError, ValueError) as exc:
        if (
            value.get("validation") != "NONPASS"
            or value.get("validation_error") != type(exc).__name__
            or value.get("outcomes") is not None
            or value.get("node_outcomes") is not None
            or record.get("attempt_kind") != "failed"
        ):
            raise RuntimeError(
                f"trusted pytest failure binding differs: {record_name}"
            ) from exc
        return None
    counts = result["counts"]
    if (
        value.get("validation") != "PASS"
        or value.get("validation_error") is not None
        or value.get("outcomes") != counts
        or value.get("node_outcomes") != result["node_outcomes"]
        or (reconcile_terminal and parse_outcomes(raw_log, "pytest") != counts)
    ):
        raise RuntimeError(f"trusted pytest/terminal reconciliation differs: {record_name}")
    return result


def _verify_composite_suite(
    value: object,
    *,
    record: dict[str, object],
    suite: dict[str, object],
    audit_root: Path,
    cwd: Path,
    raw_log: bytes,
    source_inventory: dict[str, object],
    dependency_inventory: dict[str, object],
    dependency: Path | None,
    browser_root: Path | None,
    browser_executable: Path | None,
    node: Path | None,
    npm: Path | None,
    basetemp: Path,
    runtime_validation: bool,
    record_name: str,
) -> dict[str, object] | None:
    is_full = record.get("suite") == "full_repository"
    if not is_full:
        if value is not None:
            raise RuntimeError(f"unexpected composite evidence: {record_name}")
        return None
    keys = {
        "schema", "nonce", "event_transport", "validation", "validation_error",
        "stage_results", "browser_test_summary", "browser_node_outcomes",
        "browser_complete_pass", "build_output",
        "contract_sha256", "event_artifact",
    }
    if type(value) is not dict or set(value) != keys:
        raise RuntimeError(f"composite descriptor differs: {record_name}")
    variables = record["environment"]["variables"]
    if (
        value.get("schema") != COMPOSITE_EVENT_SCHEMA
        or value.get("event_transport") != COMPOSITE_EVENT_TRANSPORT
        or value.get("nonce") != variables.get("PHASE9_TRUSTED_COMPOSITE_NONCE")
    ):
        raise RuntimeError(f"composite descriptor identity differs: {record_name}")
    event_path, event_raw = _artifact(
        audit_root, value.get("event_artifact"), "trusted composite events"
    )
    identifier = str(record["id"])
    if event_path.relative_to(audit_root).as_posix() != (
        f"evidence/composite_events/{identifier}.jsonl"
    ):
        raise RuntimeError(f"composite event path differs: {record_name}")
    if any(
        item is None
        for item in (
            dependency, browser_root, browser_executable, node, npm,
        )
    ):
        raise RuntimeError(f"composite runtime coordinates are absent: {record_name}")
    try:
        result = validate_composite_events(
            event_raw,
            raw_log=raw_log,
            nonce=str(value["nonce"]),
            source=cwd,
            dependency=dependency,
            browser_root=browser_root,
            browser_executable=browser_executable,
            python=Path(str(record["requested_command_argv"][0])),
            node=node,
            npm=npm,
            basetemp=basetemp,
            environment=variables,
            dependency_inventory=dependency_inventory,
            source_inventory=source_inventory,
            expected_stage_contract=suite["composite_stages"],
            runtime_validation=runtime_validation,
        )
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        if (
            value.get("validation") != "NONPASS"
            or value.get("validation_error") != type(exc).__name__
            or any(
                value.get(field) is not None
                for field in (
                    "stage_results", "browser_test_summary",
                    "browser_node_outcomes", "browser_complete_pass",
                    "build_output", "contract_sha256",
                )
            )
            or record.get("attempt_kind") != "failed"
        ):
            raise RuntimeError(
                f"composite validation failure binding differs: {record_name}"
            ) from exc
        return None
    if (
        value.get("validation") != "PASS"
        or value.get("validation_error") is not None
        or value.get("stage_results") != result["stage_results"]
        or value.get("browser_test_summary") != result["browser_test_summary"]
        or value.get("browser_node_outcomes") != result["browser_node_outcomes"]
        or value.get("browser_complete_pass") != result["browser_complete_pass"]
        or value.get("build_output") != result["build_output"]
        or value.get("contract_sha256") != result["contract_sha256"]
    ):
        raise RuntimeError(f"composite stage reconciliation differs: {record_name}")
    return result


def _verified_preflight_record(
    value: dict[str, object],
    raw_record: bytes,
    path: Path,
    audit_root: Path,
    suites: dict[str, dict[str, object]],
    *,
    runtime_validation: bool,
) -> dict[str, object]:
    """Verify a process-not-started failure without inventing execution facts."""

    if set(value) != PREFLIGHT_RECORD_KEYS:
        raise RuntimeError(f"preflight record schema differs: {path.name}")
    unsigned = dict(value)
    declared = unsigned.pop("record_sha256", None)
    if declared != canonical_sha256(unsigned):
        raise RuntimeError(f"preflight record self-hash differs: {path.name}")
    suite = value.get("suite")
    environment = value.get("execution_environment")
    if suite not in suites or environment not in {"source", "fresh"}:
        raise RuntimeError(f"preflight suite/environment differs: {path.name}")
    failure_stage = value.get("failure_stage")
    expected_kind = suites[str(suite)]["kind"]
    kind_matches_stage = (
        value.get("kind") != expected_kind
        if failure_stage == "SUITE_KIND"
        else value.get("kind") == expected_kind
    )
    if (
        not kind_matches_stage
        or value.get("attempt_kind") != "preflight_failed"
        or value.get("process_started") is not False
        or value.get("exit_code") is not None
        or value.get("runner_exit_code") != PREFLIGHT_FAILURE_EXIT_CODE
    ):
        raise RuntimeError(f"preflight result classification differs: {path.name}")
    identifier = value.get("id")
    if (
        type(identifier) is not str
        or _SAFE_IDENTIFIER.fullmatch(identifier) is None
        or not identifier.startswith(f"{environment}_{suite}_")
        or path.relative_to(audit_root).as_posix()
        != f"command_records/{identifier}.json"
    ):
        raise RuntimeError(f"preflight path/id binding differs: {path.name}")
    error_type = value.get("error_type")
    if (
        failure_stage not in PREFLIGHT_FAILURE_STAGES
        or type(error_type) is not str
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", error_type) is None
    ):
        raise RuntimeError(f"preflight failure classification differs: {path.name}")
    if value.get("source_postcheck") not in {"MATCH", "DIFFERS", "ERROR"} or type(
        value.get("source_stable")
    ) is not bool:
        raise RuntimeError(f"preflight source postcheck differs: {path.name}")
    if value.get("source_stable") is True and value.get("source_postcheck") != "MATCH":
        raise RuntimeError(f"preflight source stability differs: {path.name}")
    if value.get("source_stable") is False and value.get("source_postcheck") == "MATCH":
        raise RuntimeError(f"preflight source stability differs: {path.name}")
    started = _parse_utc(value.get("started_at"), "preflight started_at")
    completed = _parse_utc(value.get("completed_at"), "preflight completed_at")
    if (
        completed < started
        or type(value.get("duration_milliseconds")) is not int
        or value["duration_milliseconds"] < 0
    ):
        raise RuntimeError(f"preflight timing differs: {path.name}")

    cwd = _lexical_absolute(value.get("cwd"), f"preflight cwd: {path.name}")
    recorded_audit_root = _lexical_absolute(
        value.get("audit_root"), f"preflight audit root: {path.name}"
    )
    source_repository = _lexical_absolute(
        value.get("source_identity_repository"),
        f"preflight source identity repository: {path.name}",
    )
    log_path, raw_log = _artifact(audit_root, value.get("raw_log"), "raw log")
    inventory_path, raw_inventory = _artifact(
        audit_root, value.get("source_inventory"), "source inventory"
    )
    if log_path.relative_to(audit_root).as_posix() != f"test_logs/{identifier}.log":
        raise RuntimeError(f"preflight raw log path/id differs: {path.name}")
    if inventory_path.relative_to(audit_root).as_posix() != (
        f"evidence/source_inventories/{identifier}.json"
    ):
        raise RuntimeError(f"preflight inventory path/id differs: {path.name}")
    inventory, _ = _read_canonical(inventory_path, "source inventory")
    _verify_inventory(inventory, path.name)
    inventory_unsigned = dict(inventory)
    inventory_sha = inventory_unsigned.pop("inventory_sha256", None)
    descriptor = value.get("source_inventory")
    if (
        type(descriptor) is not dict
        or set(descriptor) != {
            "path", "bytes", "sha256", "inventory_sha256", "path_count"
        }
        or inventory_sha != canonical_sha256(inventory_unsigned)
        or descriptor.get("inventory_sha256") != inventory_sha
        or descriptor.get("path_count") != inventory.get("path_count")
        or inventory.get("candidate") != value.get("candidate")
        or raw_inventory != canonical_bytes(inventory) + b"\n"
    ):
        raise RuntimeError(f"preflight inventory binding differs: {path.name}")

    producer = value.get("producer")
    tracked_runner = _tracked_file(inventory, "tools/run_audit_command.py")
    if (
        type(producer) is not dict
        or set(producer) != {"type", "version", "path", "bytes", "sha256"}
        or producer.get("type") != "PAPER_FACTORY_AUDIT_RUNNER"
        or producer.get("version") != COMMAND_SCHEMA
        or producer.get("path") != "tools/run_audit_command.py"
        or producer.get("bytes") != tracked_runner.get("bytes")
        or producer.get("sha256") != tracked_runner.get("sha256")
    ):
        raise RuntimeError(f"preflight producer binding differs: {path.name}")

    requested = value.get("requested_command_argv")
    if type(requested) is not list or not requested or any(
        type(item) is not str for item in requested
    ):
        raise RuntimeError(f"preflight requested argv differs: {path.name}")
    if suite == "full_repository":
        if (
            len(requested) != 19
            or requested[1:4]
            != ["-B", "tools/run_full_repo_with_frontend_deps.py", "--source-root"]
            or requested[4] != str(cwd)
            or requested[5] != "--dependency-target"
            or requested[7] != "--browser-root"
            or requested[9] != "--browser-executable"
            or requested[11] != "--node"
            or requested[13] != "--npm"
            or requested[15:18] != ["--python", requested[0], "--basetemp"]
        ):
            raise RuntimeError(f"preflight full-suite argv differs: {path.name}")
        for index, label in (
            (6, "dependency root"),
            (8, "browser root"),
            (10, "browser executable"),
            (12, "Node executable"),
            (14, "npm executable"),
            (18, "basetemp"),
        ):
            _lexical_absolute(requested[index], f"preflight {label}: {path.name}")
        try:
            Path(requested[10]).relative_to(Path(requested[8]))
        except ValueError as exc:
            raise RuntimeError(
                f"preflight browser coordinate differs: {path.name}"
            ) from exc
        expected_basetemp = (
            recorded_audit_root / "runtime" / f"{identifier}-pytest" / "basetemp"
        )
        if Path(requested[18]) != expected_basetemp:
            raise RuntimeError(f"preflight basetemp differs: {path.name}")
    else:
        prefix = [requested[0], "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if (
            requested[: len(prefix)] != prefix
            or len(requested) <= len(prefix) + 1
            or not requested[len(prefix)].startswith("--basetemp=")
            or not set(suites[str(suite)]["required_targets"]).issubset(
                requested[len(prefix) + 1 :]
            )
        ):
            raise RuntimeError(f"preflight pytest argv differs: {path.name}")
        basetemp = _lexical_absolute(
            requested[len(prefix)].split("=", 1)[1],
            f"preflight basetemp: {path.name}",
        )
        if basetemp != (
            recorded_audit_root / "runtime" / f"{identifier}-pytest" / "basetemp"
        ):
            raise RuntimeError(f"preflight basetemp differs: {path.name}")

    executable = value.get("command_executable")
    python_executable = value.get("python_executable")
    if failure_stage in {"COMMAND_EXECUTABLE", "SUITE_KIND"}:
        if executable is not None or python_executable is not None:
            raise RuntimeError(f"failed executable unexpectedly bound: {path.name}")
    else:
        if (
            type(executable) is not dict
            or set(executable) != {"path", "resolved_path", "bytes", "sha256"}
            or python_executable != executable
            or executable.get("path") != requested[0]
            or type(executable.get("bytes")) is not int
            or executable["bytes"] <= 0
            or type(executable.get("sha256")) is not str
            or _HEX64.fullmatch(str(executable["sha256"])) is None
        ):
            raise RuntimeError(f"preflight executable binding differs: {path.name}")
        resolved_executable = _lexical_absolute(
            executable.get("resolved_path"),
            f"preflight resolved executable: {path.name}",
        )
        if runtime_validation:
            launcher = _lexical_absolute(
                executable.get("path"), f"preflight executable: {path.name}"
            )
            trusted_launcher = Path(os.path.abspath(sys.executable))
            try:
                executable_raw = _stable_regular_bytes(
                    resolved_executable, "preflight executable"
                )
            except OSError as exc:
                raise RuntimeError(
                    f"preflight executable is unavailable: {path.name}"
                ) from exc
            if (
                launcher != trusted_launcher
                or launcher.resolve(strict=True) != resolved_executable
                or len(executable_raw) != executable["bytes"]
                or hashlib.sha256(executable_raw).hexdigest()
                != executable["sha256"]
            ):
                raise RuntimeError(f"preflight executable bytes differ: {path.name}")

    failure_event = {
        "schema": PREFLIGHT_FAILURE_SCHEMA,
        "event": "preflight_failure",
        "failure_stage": failure_stage,
        "error_type": error_type,
        "process_started": False,
        "runner_exit_code": PREFLIGHT_FAILURE_EXIT_CODE,
    }
    if raw_log != canonical_bytes(failure_event) + b"\n":
        raise RuntimeError(f"preflight raw log differs: {path.name}")

    if runtime_validation:
        try:
            live_repository = source_repository.resolve(strict=True)
            live_cwd = cwd.resolve(strict=True)
            runtime_identity = _runtime_candidate_identity(live_repository)
            observed_inventory, observed_raw = executed_source_inventory(
                live_repository,
                live_cwd,
                execution_environment=str(environment),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"preflight runtime source is unavailable: {path.name}") from exc
        if (
            runtime_identity != inventory.get("candidate")
            or observed_inventory != inventory
            or observed_raw != raw_inventory
        ):
            raise RuntimeError(f"preflight runtime source differs: {path.name}")

    return {
        "id": identifier,
        "suite": suite,
        "environment": environment,
        "attempt_kind": "preflight_failed",
        "candidate": value["candidate"],
        "source_inventory_sha256": inventory_sha,
        "source_inventory": descriptor,
        "dependency_inventory": None,
        "dependency_inventory_sha256": None,
        "command_record_path": path.relative_to(audit_root).as_posix(),
        "command_record_bytes": len(raw_record),
        "command_record_sha256": hashlib.sha256(raw_record).hexdigest(),
        "command_argv": requested,
        "cwd": value["cwd"],
        "audit_root": value["audit_root"],
        "python_executable": python_executable,
        "exit_code": None,
        "runner_exit_code": PREFLIGHT_FAILURE_EXIT_CODE,
        "outcomes": None,
        "node_outcomes": None,
        "composite_suite": None,
        "composite_contract_sha256": None,
        "trusted_pytest": None,
        "process_started": False,
        "failure_stage": failure_stage,
        "error_type": error_type,
        "raw_log": value["raw_log"],
    }


def _verified_record(
    path: Path,
    audit_root: Path,
    suites: dict[str, dict[str, object]],
    *,
    runtime_validation: bool,
) -> dict[str, object]:
    value, raw_record = _read_canonical(path, "command record")
    if value.get("schema") == PREFLIGHT_FAILURE_SCHEMA:
        return _verified_preflight_record(
            value,
            raw_record,
            path,
            audit_root,
            suites,
            runtime_validation=runtime_validation,
        )
    if value.get("schema") != COMMAND_SCHEMA or set(value) != RECORD_KEYS:
        raise RuntimeError(f"command record schema differs: {path.name}")
    unsigned = dict(value)
    declared = unsigned.pop("record_sha256", None)
    if declared != canonical_sha256(unsigned):
        raise RuntimeError(f"command record self-hash differs: {path.name}")
    suite = value.get("suite")
    environment = value.get("execution_environment")
    if suite not in suites or environment not in {"source", "fresh"}:
        raise RuntimeError(f"command record suite/environment differs: {path.name}")
    if value.get("kind") != suites[str(suite)]["kind"]:
        raise RuntimeError(f"command record parser kind differs: {path.name}")
    if value.get("attempt_kind") not in {"final", "failed"}:
        raise RuntimeError(f"command record attempt kind differs: {path.name}")
    if value.get("source_postcheck") not in {"MATCH", "DIFFERS", "ERROR"}:
        raise RuntimeError(f"source postcheck differs: {path.name}")
    if value.get("dependency_postcheck") not in {"MATCH", "DIFFERS", "ERROR"}:
        raise RuntimeError(f"dependency postcheck differs: {path.name}")
    if value.get("attempt_kind") == "final" and (
        value.get("source_stable") is not True
        or value.get("source_postcheck") != "MATCH"
        or value.get("dependency_stable") is not True
        or value.get("dependency_postcheck") != "MATCH"
    ):
        raise RuntimeError(f"executed source or dependency changed: {path.name}")
    identifier = value.get("id")
    if (
        type(identifier) is not str
        or _SAFE_IDENTIFIER.fullmatch(identifier) is None
        or path.relative_to(audit_root).as_posix() != (
        f"command_records/{identifier}.json"
        )
    ):
        raise RuntimeError(f"command record path/id differs: {path.name}")
    expected_final_id = f"{environment}_{suite}_final"
    if not identifier.startswith(f"{environment}_{suite}_") or (
        value.get("attempt_kind") == "final" and identifier != expected_final_id
    ):
        raise RuntimeError(f"command record id/suite binding differs: {path.name}")
    cwd_path = _lexical_absolute(value.get("cwd"), f"command cwd: {path.name}")
    recorded_audit_root = _lexical_absolute(
        value.get("audit_root"), f"recorded audit root: {path.name}"
    )
    _verify_environment(
        value.get("environment"), recorded_audit_root=recorded_audit_root,
        cwd=cwd_path, record_name=path.name,
        runtime_validation=runtime_validation,
        composite=suite == "full_repository",
    )
    started = _parse_utc(value.get("started_at"), "command started_at")
    completed = _parse_utc(value.get("completed_at"), "command completed_at")
    if completed < started or type(value.get("duration_milliseconds")) is not int \
            or value["duration_milliseconds"] < 0:
        raise RuntimeError(f"command timing differs: {path.name}")

    if type(value.get("raw_log")) is not dict or set(value["raw_log"]) != {
        "path", "bytes", "sha256"
    }:
        raise RuntimeError(f"raw log descriptor differs: {path.name}")
    if type(value.get("source_inventory")) is not dict or set(
        value["source_inventory"]
    ) != {"path", "bytes", "sha256", "inventory_sha256", "path_count"}:
        raise RuntimeError(f"source inventory descriptor differs: {path.name}")
    log_path, raw_log = _artifact(audit_root, value.get("raw_log"), "raw log")
    inventory_path, _raw_inventory = _artifact(
        audit_root, value.get("source_inventory"), "source inventory"
    )
    if log_path.relative_to(audit_root).parts[0] != "test_logs":
        raise RuntimeError(f"raw log is outside test_logs: {path.name}")
    if inventory_path.relative_to(audit_root).parts[:2] != (
        "evidence", "source_inventories"
    ):
        raise RuntimeError(f"source inventory is outside its evidence root: {path.name}")
    inventory, _ = _read_canonical(inventory_path, "source inventory")
    _verify_inventory(inventory, path.name)
    inventory_unsigned = dict(inventory)
    inventory_sha = inventory_unsigned.pop("inventory_sha256", None)
    if inventory_sha != canonical_sha256(inventory_unsigned):
        raise RuntimeError(f"source inventory self-hash differs: {path.name}")
    if inventory_sha != value["source_inventory"].get("inventory_sha256"):
        raise RuntimeError(f"source inventory descriptor hash differs: {path.name}")
    if inventory.get("candidate") != value.get("candidate"):
        raise RuntimeError(f"source inventory candidate differs: {path.name}")
    if (
        value["source_inventory"].get("path_count") != inventory.get("path_count")
        or type(value["source_inventory"].get("path_count")) is not int
    ):
        raise RuntimeError(f"source inventory descriptor count differs: {path.name}")
    expected_log_path = f"test_logs/{identifier}.log"
    expected_inventory_path = f"evidence/source_inventories/{identifier}.json"
    if value["raw_log"].get("path") != expected_log_path:
        raise RuntimeError(f"raw log path/id differs: {path.name}")
    if value["source_inventory"].get("path") != expected_inventory_path:
        raise RuntimeError(f"source inventory path/id differs: {path.name}")

    if runtime_validation:
        variables = value["environment"]["variables"]
        source_repository = Path(
            str(variables["PHASE9_TEST_SOURCE_REPOSITORY"])
        ).resolve(strict=True)
        try:
            runtime_identity = _runtime_candidate_identity(source_repository)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"test source identity repository is unreadable: {path.name}"
            ) from exc
        if runtime_identity != inventory.get("candidate"):
            raise RuntimeError(f"runtime candidate identity differs: {path.name}")
        observed_inventory, observed_raw = executed_source_inventory(
            source_repository,
            cwd_path.resolve(strict=True),
            execution_environment=str(environment),
        )
        if observed_raw != _raw_inventory or observed_inventory != inventory:
            raise RuntimeError(f"runtime source bytes differ: {path.name}")

    kind = str(suites[str(suite)]["kind"])
    (
        normalized_argv,
        dependency,
        browser_root,
        browser_executable,
        node,
        npm,
        basetemp,
    ) = _verify_runner_and_command(
        value, inventory, suites[str(suite)], path.name, audit_root,
        recorded_audit_root, runtime_validation
    )
    dependency_descriptor = value.get("dependency_inventory")
    if type(dependency_descriptor) is not dict or set(dependency_descriptor) != {
        "path", "bytes", "sha256", "inventory_sha256", "path_count",
    }:
        raise RuntimeError(f"dependency inventory descriptor differs: {path.name}")
    dependency_path, raw_dependency_inventory = _artifact(
        audit_root, dependency_descriptor, "dependency inventory"
    )
    if dependency_path.relative_to(audit_root).parts[:2] != (
        "evidence", "dependency_inventories"
    ):
        raise RuntimeError(f"dependency inventory root differs: {path.name}")
    if dependency_descriptor.get("path") != (
        f"evidence/dependency_inventories/{identifier}.json"
    ):
        raise RuntimeError(f"dependency inventory path/id differs: {path.name}")
    dependency_body, _ = _read_canonical(
        dependency_path, "dependency inventory"
    )
    if (
        dependency_descriptor.get("inventory_sha256")
        != dependency_body.get("inventory_sha256")
        or dependency_descriptor.get("path_count") != dependency_body.get("path_count")
        or raw_dependency_inventory != canonical_bytes(dependency_body) + b"\n"
    ):
        raise RuntimeError(f"dependency inventory binding differs: {path.name}")
    _verify_dependency_inventory(
        dependency_body, dependency=dependency, browser_root=browser_root,
        browser_executable=browser_executable, cwd=cwd_path,
        source_inventory=inventory,
        record_name=path.name, runtime_validation=runtime_validation,
    )
    _verify_execution_sandbox(
        value.get("execution_sandbox"), command=list(value["command_argv"]),
        environment=value["environment"], cwd=cwd_path,
        audit_root=recorded_audit_root, dependency=dependency,
        browser_root=browser_root,
        identifier=str(identifier), record_name=path.name,
        runtime_validation=runtime_validation,
    )
    missing_targets = set(suites[str(suite)]["required_targets"]) - set(
        normalized_argv
    )
    if missing_targets:
        raise RuntimeError(
            f"command does not execute required suite targets: {path.name}: "
            f"{sorted(missing_targets)}"
        )
    composite_result = _verify_composite_suite(
        value.get("composite_suite"),
        record=value,
        suite=suites[str(suite)],
        audit_root=audit_root,
        cwd=cwd_path,
        raw_log=raw_log,
        source_inventory=inventory,
        dependency_inventory=dependency_body,
        dependency=dependency,
        browser_root=browser_root,
        browser_executable=browser_executable,
        node=node,
        npm=npm,
        basetemp=basetemp,
        runtime_validation=runtime_validation,
        record_name=path.name,
    )
    pytest_log = (
        raw_log if composite_result is None else composite_result["python_log"]
    )
    trusted_result = _verify_trusted_pytest(
        value.get("trusted_pytest"),
        record=value,
        inventory=inventory,
        suite=suites[str(suite)],
        audit_root=audit_root,
        recorded_audit_root=recorded_audit_root,
        cwd=cwd_path,
        raw_log=pytest_log,
        runtime_validation=runtime_validation,
        record_name=path.name,
        reconcile_terminal=(suite != "full_repository" or composite_result is not None),
    )
    trusted_counts = (
        None if trusted_result is None else trusted_result["counts"]
    )
    trusted_node_outcomes = (
        None if trusted_result is None else trusted_result["node_outcomes"]
    )
    outcomes = parse_outcomes(pytest_log, "pytest")
    if value.get("outcome_parser") != "paper-factory-composite-and-trusted-events-v5":
        raise RuntimeError(f"outcome parser identity differs: {path.name}")
    if value.get("outcomes") != outcomes:
        raise RuntimeError(f"raw-log outcomes differ from record: {path.name}")
    if any(type(outcomes.get(name)) is not int for name in OUTCOMES):
        raise RuntimeError(f"outcomes are incomplete: {path.name}")
    if (
        type(value.get("exit_code")) is not int
        or type(value.get("runner_exit_code")) is not int
        or type(value.get("source_stable")) is not bool
        or type(value.get("dependency_stable")) is not bool
    ):
        raise RuntimeError(f"command result types differ: {path.name}")
    if value["attempt_kind"] == "final" and (
        value.get("exit_code") != 0
        or value.get("runner_exit_code") != 0
        or outcomes["collected"] <= 0
        or outcomes["passed"] != outcomes["collected"]
        or any(outcomes[name] for name in ("failed", "errors", "skipped", "xfailed", "xpassed"))
        or trusted_counts != outcomes
        or (
            suite == "full_repository"
            and (
                composite_result is None
                or composite_result["overall_exit_code"] != 0
                or composite_result["browser_complete_pass"] is not True
            )
        )
    ):
        raise RuntimeError(f"final record is not a complete PASS: {path.name}")
    if value["attempt_kind"] == "failed" and (
        value.get("exit_code") == 0
        and value.get("runner_exit_code") == 0
        and value.get("source_stable") is True
        and value.get("dependency_stable") is True
        and outcomes["collected"] > 0
        and outcomes["passed"] == outcomes["collected"]
        and not any(
            outcomes[name]
            for name in ("failed", "errors", "skipped", "xfailed", "xpassed")
        )
            and trusted_counts == outcomes
            and (
                suite != "full_repository"
                or (
                    composite_result is not None
                    and composite_result["overall_exit_code"] == 0
                    and composite_result["browser_complete_pass"] is True
                )
            )
    ):
        raise RuntimeError(f"failed-attempt record is actually a PASS: {path.name}")

    return {
        "id": value["id"],
        "suite": suite,
        "environment": environment,
        "attempt_kind": value["attempt_kind"],
        "candidate": value["candidate"],
        "source_inventory_sha256": inventory_sha,
        "source_inventory": value["source_inventory"],
        "dependency_inventory": value["dependency_inventory"],
        "dependency_inventory_sha256": dependency_body["inventory_sha256"],
        "command_record_path": path.relative_to(audit_root).as_posix(),
        "command_record_bytes": len(raw_record),
        "command_record_sha256": hashlib.sha256(raw_record).hexdigest(),
        "command_argv": value["command_argv"],
        "cwd": value["cwd"],
        "audit_root": value["audit_root"],
        "python_executable": value["python_executable"],
        "exit_code": value["exit_code"],
        "runner_exit_code": value["runner_exit_code"],
        "outcomes": outcomes,
        "node_outcomes": trusted_node_outcomes,
        "composite_suite": value["composite_suite"],
        "composite_contract_sha256": (
            None if composite_result is None else composite_result["contract_sha256"]
        ),
        "trusted_pytest": value["trusted_pytest"],
        "raw_log": value["raw_log"],
    }


def _direct_regular_files(root: Path, label: str) -> list[Path]:
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"{label} root is not an ordinary directory")
    result: list[Path] = []
    folded: set[str] = set()
    with os.scandir(root) as entries:
        for entry in entries:
            relative = _safe_relative(entry.name, label)
            collision = unicodedata.normalize("NFC", relative).casefold()
            if collision in folded:
                raise RuntimeError(f"{label} has a path collision")
            folded.add(collision)
            item = root / entry.name
            info = item.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError(f"{label} contains a link, directory, or special entry")
            result.append(item)
    return sorted(result)


def _optional_direct_regular_files(root: Path, label: str) -> list[Path]:
    return [] if not root.exists() else _direct_regular_files(root, label)


def _verify_reverse_artifact_closure(
    audit_root: Path, records: list[dict[str, object]]
) -> None:
    log_references = [str(item["raw_log"]["path"]) for item in records]
    inventory_references = [
        str(item["source_inventory"]["path"]) for item in records
    ]
    dependency_references = [
        str(item["dependency_inventory"]["path"])
        for item in records
        if item["dependency_inventory"] is not None
    ]
    event_references = [
        str(item["trusted_pytest"]["event_artifact"]["path"])
        for item in records
        if item["trusted_pytest"] is not None
    ]
    composite_references = [
        str(item["composite_suite"]["event_artifact"]["path"])
        for item in records
        if item["composite_suite"] is not None
    ]
    if len(log_references) != len(set(log_references)):
        raise RuntimeError("multiple command records alias one raw log")
    if len(inventory_references) != len(set(inventory_references)):
        raise RuntimeError("multiple command records alias one source inventory")
    if len(dependency_references) != len(set(dependency_references)):
        raise RuntimeError("multiple command records alias one dependency inventory")
    if len(event_references) != len(set(event_references)):
        raise RuntimeError("multiple command records alias one trusted event stream")
    if len(composite_references) != len(set(composite_references)):
        raise RuntimeError("multiple command records alias one composite event stream")
    actual_logs = {
        path.relative_to(audit_root).as_posix()
        for path in _direct_regular_files(audit_root / "test_logs", "test logs")
    }
    actual_inventories = {
        path.relative_to(audit_root).as_posix()
        for path in _direct_regular_files(
            audit_root / "evidence/source_inventories", "source inventories"
        )
    }
    actual_dependencies = {
        path.relative_to(audit_root).as_posix()
        for path in _optional_direct_regular_files(
            audit_root / "evidence/dependency_inventories",
            "dependency inventories",
        )
    }
    actual_events = {
        path.relative_to(audit_root).as_posix()
        for path in _optional_direct_regular_files(
            audit_root / "evidence/pytest_events", "trusted pytest events"
        )
    }
    composite_root = audit_root / "evidence/composite_events"
    actual_composite = (
        set()
        if not composite_root.exists()
        else {
            path.relative_to(audit_root).as_posix()
            for path in _direct_regular_files(
                composite_root, "trusted composite events"
            )
        }
    )
    if actual_logs != set(log_references):
        raise RuntimeError("raw log reverse closure differs from command records")
    if actual_inventories != set(inventory_references):
        raise RuntimeError("source inventory reverse closure differs from command records")
    if actual_dependencies != set(dependency_references):
        raise RuntimeError(
            "dependency inventory reverse closure differs from command records"
        )
    if actual_events != set(event_references):
        raise RuntimeError("trusted pytest event reverse closure differs")
    if actual_composite != set(composite_references):
        raise RuntimeError("trusted composite event reverse closure differs")


def _build_summary_for_policy(
    *,
    audit_root: Path,
    records_root: Path,
    suite_contract_path: Path,
    expected_suite_specs: dict[str, dict[str, object]],
    runtime_validation: bool,
) -> dict[str, object]:
    audit_root = audit_root.resolve(strict=True)
    records_root = records_root.resolve(strict=True)
    try:
        records_root.relative_to(audit_root)
    except ValueError as exc:
        raise RuntimeError("records directory must be inside audit root") from exc
    suite_contract, suites = _suite_contract(
        suite_contract_path,
        expected_specs=expected_suite_specs,
    )
    record_paths = _direct_regular_files(records_root, "command records")
    if any(path.suffix != ".json" for path in record_paths):
        raise RuntimeError("command records contain a non-JSON artifact")
    records = [
        _verified_record(
            path, audit_root, suites, runtime_validation=runtime_validation
        )
        for path in record_paths
    ]
    if not records:
        raise RuntimeError("no command records found")
    ids = [str(item["id"]) for item in records]
    if len(ids) != len(set(ids)):
        raise RuntimeError("command record IDs are duplicated")
    candidates = {canonical_bytes(item["candidate"]) for item in records}
    inventories = {str(item["source_inventory_sha256"]) for item in records}
    interpreters = {
        canonical_bytes(item["python_executable"])
        for item in records
        if item["python_executable"] is not None
    }
    recorded_roots = {str(item["audit_root"]) for item in records}
    if (
        len(candidates) != 1
        or len(inventories) != 1
        or len(interpreters) != 1
        or len(recorded_roots) != 1
    ):
        raise RuntimeError("command records bind different candidate source bytes")
    _verify_reverse_artifact_closure(audit_root, records)

    finals = [item for item in records if item["attempt_kind"] == "final"]
    attempts = [item for item in records if item["attempt_kind"] != "final"]
    expected = {(suite, env) for suite in suites for env in ("source", "fresh")}
    actual = {(str(item["suite"]), str(item["environment"])) for item in finals}
    if actual != expected or len(finals) != len(expected):
        raise RuntimeError(
            f"required final suite set differs: missing={sorted(expected-actual)}, "
            f"extra={sorted(actual-expected)}"
        )
    pairs: list[dict[str, object]] = []
    for suite in suites:
        source = next(
            item for item in finals if item["suite"] == suite and item["environment"] == "source"
        )
        fresh = next(
            item for item in finals if item["suite"] == suite and item["environment"] == "fresh"
        )
        equal = source["outcomes"] == fresh["outcomes"]
        node_equal = source["node_outcomes"] == fresh["node_outcomes"]
        dependency_equal = (
            source["dependency_inventory_sha256"]
            == fresh["dependency_inventory_sha256"]
        )
        composite_equal = (
            source["composite_contract_sha256"]
            == fresh["composite_contract_sha256"]
        )
        browser_node_equal = (
            source["composite_suite"] is None
            or source["composite_suite"]["browser_node_outcomes"]
            == fresh["composite_suite"]["browser_node_outcomes"]
        )
        pairs.append(
            {
                "suite": suite,
                "requirements": suites[suite]["requirements"],
                "source_id": source["id"],
                "fresh_id": fresh["id"],
                "source_outcomes": source["outcomes"],
                "fresh_outcomes": fresh["outcomes"],
                "exact_outcome_match": equal,
                "exact_node_outcome_match": node_equal,
                "exact_dependency_match": dependency_equal,
                "exact_browser_node_match": browser_node_equal,
                "exact_composite_stage_match": composite_equal,
            }
        )
    non_pass = {
        name: sum(int(item["outcomes"][name]) for item in finals)
        for name in ("failed", "errors", "skipped", "xfailed", "xpassed")
    }
    exact = all(
        bool(pair["exact_outcome_match"])
        and bool(pair["exact_node_outcome_match"])
        and bool(pair["exact_dependency_match"])
        and bool(pair["exact_browser_node_match"])
        and bool(pair["exact_composite_stage_match"])
        for pair in pairs
    )
    body: dict[str, object] = {
        "schema": SUMMARY_SCHEMA,
        "candidate": finals[0]["candidate"],
        "source_inventory_sha256": finals[0]["source_inventory_sha256"],
        "suite_contract_sha256": canonical_sha256(suite_contract),
        "result": "PASS" if exact and not any(non_pass.values()) else "NONPASS",
        "source_fresh_exact": exact,
        "non_pass_totals": non_pass,
        "warning_total": sum(int(item["outcomes"]["warnings"]) for item in finals),
        "required_suite_count": len(suites),
        "final_record_count": len(finals),
        "failed_attempt_count": len(attempts),
        "pairs": pairs,
        "records": finals,
        "failed_attempts": attempts,
    }
    body["summary_sha256"] = canonical_sha256(body)
    return body


def build_summary(
    *, audit_root: Path, records_root: Path, suite_contract_path: Path
) -> dict[str, object]:
    """Build the formal summary under the non-overridable seven-suite policy."""

    return _build_summary_for_policy(
        audit_root=audit_root,
        records_root=records_root,
        suite_contract_path=suite_contract_path,
        expected_suite_specs=PHASE9_REQUIRED_SUITE_SPECS,
        runtime_validation=True,
    )


def _rebuild_packaged_summary(
    *, audit_root: Path, records_root: Path, suite_contract_path: Path
) -> dict[str, object]:
    """Rebuild bundled evidence without consulting vanished execution paths."""

    return _build_summary_for_policy(
        audit_root=audit_root,
        records_root=records_root,
        suite_contract_path=suite_contract_path,
        expected_suite_specs=PHASE9_REQUIRED_SUITE_SPECS,
        runtime_validation=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", required=True, type=Path)
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--suite-contract", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    body = build_summary(
        audit_root=args.audit_root,
        records_root=args.records,
        suite_contract_path=args.suite_contract,
    )
    output = args.output.resolve()
    root = args.audit_root.resolve(strict=True)
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("summary output must be inside audit root") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise RuntimeError("summary evidence is append-only")
    output.write_bytes(canonical_bytes(body) + b"\n")
    print(canonical_bytes(body).decode())
    return 0 if body["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
