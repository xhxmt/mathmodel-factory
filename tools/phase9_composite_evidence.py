"""Strict validation for Phase 9 full-repository composite stage evidence."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import unicodedata

from factory_core.canonical import canonical_bytes, canonical_sha256
from tools.run_full_repo_with_frontend_deps import (
    BUILD_OUTPUT_SCHEMA,
    COMPOSITE_EVENT_SCHEMA,
    COMPOSITE_EVENT_TRANSPORT,
    COMPOSITE_STAGE_IDS,
    EXPECTED_FRONTEND_SCRIPTS,
    PHASE6_BROWSER_TARGETS,
    _frontend_script_descriptor,
    _verify_locked_dependencies,
    composite_stage_contract,
    expand_composite_stage_command,
    inventory_frontend_build_output,
    parse_node_test_outcomes,
    parse_node_test_summary,
)


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _stable_executable_bytes(path: Path) -> bytes:
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not os.access(path, os.X_OK)
    ):
        raise ValueError("runtime executable is not one executable regular file")
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
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if not identity(before) == identity(opened) == identity(after) == identity(final):
        raise ValueError("runtime executable changed while read")
    return b"".join(chunks)


def _runtime_descriptor(
    value: object,
    *,
    expected_path: Path,
    label: str,
    runtime_validation: bool,
    expected_version_argv: list[str] | None = None,
) -> dict[str, object]:
    keys = {
        "path", "resolved_path", "bytes", "sha256", "version_argv", "version"
    }
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"{label} runtime descriptor differs")
    if expected_version_argv is None:
        expected_version_argv = [str(expected_path), "--version"]
    if (
        value.get("path") != str(expected_path)
        or value.get("version_argv") != expected_version_argv
    ):
        raise ValueError(f"{label} runtime command differs")
    resolved = Path(str(value.get("resolved_path")))
    if (
        not resolved.is_absolute()
        or type(value.get("bytes")) is not int
        or int(value["bytes"]) <= 0
        or type(value.get("sha256")) is not str
        or _HEX64.fullmatch(str(value["sha256"])) is None
        or type(value.get("version")) is not str
        or not value["version"]
        or "\n" in str(value["version"])
    ):
        raise ValueError(f"{label} runtime identity differs")
    if runtime_validation:
        observed = expected_path.resolve(strict=True)
        raw = _stable_executable_bytes(observed)
        if (
            resolved != observed
            or len(raw) != value["bytes"]
            or hashlib.sha256(raw).hexdigest() != value["sha256"]
        ):
            raise ValueError(f"{label} runtime bytes differ")
        probe = subprocess.run(
            expected_version_argv,
            cwd="/",
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        version = probe.stdout.decode("utf-8", errors="strict").strip()
        if probe.returncode != 0 or version != value["version"]:
            raise ValueError(f"{label} runtime version differs")
    return value


def _parse_utc(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("composite stage time is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("composite stage time is invalid") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("composite stage time is not UTC")
    return parsed


def _tracked_blob_sha256(inventory: dict[str, object], relative: str) -> str:
    matches = [
        item
        for item in inventory.get("files", [])
        if type(item) is dict
        and item.get("path") == relative
        and item.get("type") == "blob"
    ]
    if len(matches) != 1 or _HEX64.fullmatch(str(matches[0].get("sha256"))) is None:
        raise ValueError(f"composite source inventory does not bind {relative}")
    return str(matches[0]["sha256"])


def _validate_frontend_scripts(
    value: object,
    *,
    source: Path,
    source_inventory: dict[str, object],
    runtime_validation: bool,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "package_json_sha256", "build", "test:phase6", "browser_targets"
    }:
        raise ValueError("composite frontend script descriptor differs")
    if (
        value.get("package_json_sha256")
        != _tracked_blob_sha256(source_inventory, "web/frontend/package.json")
        or value.get("build") != EXPECTED_FRONTEND_SCRIPTS["build"]
        or value.get("test:phase6") != EXPECTED_FRONTEND_SCRIPTS["test:phase6"]
        or value.get("browser_targets") != list(PHASE6_BROWSER_TARGETS)
    ):
        raise ValueError("composite frontend scripts/targets differ")
    for target in PHASE6_BROWSER_TARGETS:
        _tracked_blob_sha256(source_inventory, f"web/frontend/{target}")
    if runtime_validation and _frontend_script_descriptor(source) != value:
        raise ValueError("composite frontend scripts cannot be reproduced")
    return value


def _validate_build_output(
    value: object,
    *,
    expected_root: Path,
    runtime_validation: bool,
) -> dict[str, object]:
    keys = {
        "schema", "root", "path_count", "total_file_bytes", "tree_sha256", "files"
    }
    if type(value) is not dict or set(value) != keys:
        raise ValueError("frontend build-output descriptor differs")
    if (
        value.get("schema") != BUILD_OUTPUT_SCHEMA
        or value.get("root") != str(expected_root)
        or type(value.get("path_count")) is not int
        or int(value["path_count"]) <= 0
        or type(value.get("total_file_bytes")) is not int
        or int(value["total_file_bytes"]) <= 0
        or type(value.get("tree_sha256")) is not str
        or _HEX64.fullmatch(str(value["tree_sha256"])) is None
    ):
        raise ValueError("frontend build-output identity differs")
    files = value.get("files")
    if type(files) is not list or len(files) != value["path_count"]:
        raise ValueError("frontend build-output inventory differs")
    paths: list[str] = []
    total = 0
    for item in files:
        if type(item) is not dict or set(item) != {
            "path", "mode", "bytes", "sha256"
        }:
            raise ValueError("frontend build-output row differs")
        path = item.get("path")
        pure = PurePosixPath(path) if type(path) is str else None
        if (
            type(path) is not str
            or not path
            or pure is None
            or pure.is_absolute()
            or pure.as_posix() != path
            or ".." in pure.parts
            or "\\" in path
            or unicodedata.normalize("NFC", path) != path
            or type(item.get("mode")) is not str
            or item["mode"] not in {"100644", "100755"}
            or type(item.get("bytes")) is not int
            or int(item["bytes"]) < 0
            or type(item.get("sha256")) is not str
            or _HEX64.fullmatch(str(item["sha256"])) is None
        ):
            raise ValueError("frontend build-output row identity differs")
        paths.append(path)
        total += int(item["bytes"])
    if (
        paths != sorted(paths)
        or len(paths) != len(set(paths))
        or len({unicodedata.normalize("NFC", path).casefold() for path in paths})
        != len(paths)
        or total != value["total_file_bytes"]
        or canonical_sha256(files) != value["tree_sha256"]
        or not any(
            path == "index.html" and int(item["bytes"]) > 0
            for path, item in zip(paths, files, strict=True)
        )
        or not any(
            path.startswith("assets/") and int(item["bytes"]) > 0
            for path, item in zip(paths, files, strict=True)
        )
    ):
        raise ValueError("frontend build-output closure differs")
    if runtime_validation and inventory_frontend_build_output(expected_root) != value:
        raise ValueError("frontend build-output inventory cannot be reproduced")
    return value


def validate_composite_events(
    raw: bytes,
    *,
    raw_log: bytes,
    nonce: str,
    source: Path,
    dependency: Path,
    browser_root: Path,
    browser_executable: Path,
    python: Path,
    node: Path,
    npm: Path,
    basetemp: Path,
    environment: dict[str, str],
    source_inventory: dict[str, object],
    dependency_inventory: dict[str, object],
    expected_stage_contract: list[dict[str, object]],
    runtime_validation: bool,
) -> dict[str, object]:
    """Validate commands, cwd/env, runtimes, results, and every raw-log byte."""

    if expected_stage_contract != composite_stage_contract():
        raise ValueError("composite stage contract differs from the trusted definition")

    if not raw or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ValueError("composite event stream is missing or ambiguous")
    try:
        event = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("composite event stream is not strict JSON") from exc
    if type(event) is not dict or canonical_bytes(event) + b"\n" != raw:
        raise ValueError("composite event stream is not canonical")
    event_keys = {
        "schema", "nonce", "event_transport", "source_root",
        "dependency_target", "browser_root", "browser_executable", "basetemp",
        "environment_policy", "dependency_preflight", "frontend_scripts",
        "build_output", "runtimes", "stages", "overall_exit_code",
    }
    if (
        set(event) != event_keys
        or event.get("schema") != COMPOSITE_EVENT_SCHEMA
        or event.get("nonce") != nonce
        or event.get("event_transport") != COMPOSITE_EVENT_TRANSPORT
        or event.get("source_root") != str(source)
        or event.get("dependency_target") != str(dependency)
        or event.get("browser_root") != str(browser_root)
        or event.get("browser_executable") != str(browser_executable)
        or event.get("basetemp") != str(basetemp)
        or event.get("environment_policy") != "EXPLICIT_SANITIZED_ALLOWLIST"
    ):
        raise ValueError("composite event identity differs")
    preflight = event.get("dependency_preflight")
    if type(preflight) is not dict or set(preflight) != {
        "lockfile_sha256", "locked_packages_verified",
        "locked_optional_packages_absent",
    }:
        raise ValueError("composite dependency preflight differs")
    if (
        preflight.get("lockfile_sha256") != dependency_inventory.get("lockfile_sha256")
        or type(preflight.get("locked_packages_verified")) is not int
        or int(preflight["locked_packages_verified"]) <= 0
        or type(preflight.get("locked_optional_packages_absent")) is not int
        or int(preflight["locked_optional_packages_absent"]) < 0
    ):
        raise ValueError("composite dependency preflight identity differs")
    if runtime_validation:
        observed = _verify_locked_dependencies(source, dependency)
        if observed != (
            preflight["lockfile_sha256"],
            preflight["locked_packages_verified"],
            preflight["locked_optional_packages_absent"],
        ):
            raise ValueError("composite dependency preflight cannot be reproduced")

    frontend_scripts = _validate_frontend_scripts(
        event.get("frontend_scripts"),
        source=source,
        source_inventory=source_inventory,
        runtime_validation=runtime_validation,
    )

    runtimes = event.get("runtimes")
    expected_runtimes = {
        "python": python,
        "node": node,
        "npm": npm,
        "browser": browser_executable,
    }
    if type(runtimes) is not dict or set(runtimes) != set(expected_runtimes):
        raise ValueError("composite runtime set differs")
    checked_runtimes = {
        name: _runtime_descriptor(
            runtimes[name], expected_path=path, label=name,
            runtime_validation=runtime_validation,
        )
        for name, path in expected_runtimes.items()
        if name != "npm"
    }
    npm_value = runtimes["npm"]
    npm_resolved = (
        npm_value.get("resolved_path") if type(npm_value) is dict else None
    )
    checked_runtimes["npm"] = _runtime_descriptor(
        npm_value,
        expected_path=npm,
        label="npm",
        runtime_validation=runtime_validation,
        expected_version_argv=[
            str(checked_runtimes["node"]["resolved_path"]),
            str(npm_resolved),
            "--version",
        ],
    )
    if re.search(
        r"(?:chromium|chrome|headless\s*shell)",
        str(checked_runtimes["browser"]["version"]), re.IGNORECASE,
    ) is None:
        raise ValueError("composite browser runtime is not Chromium/Chrome")

    reporter = environment["PHASE9_TRUSTED_PYTEST_REPORTER_PATH"]
    site_packages = environment["PHASE9_TRUSTED_PYTEST_SITE_PACKAGES"]
    command_bindings = {
        "${PYTHON}": str(python),
        "${TRUSTED_REPORTER}": reporter,
        "${RUNTIME_SITE_PACKAGES}": site_packages,
        "${SOURCE_ROOT}": str(source),
        "${BASE_TEMP}": str(basetemp),
        "${NODE}": str(checked_runtimes["node"]["resolved_path"]),
        "${NPM_CLI}": str(checked_runtimes["npm"]["resolved_path"]),
    }
    expected_commands = {
        str(stage["id"]): expand_composite_stage_command(
            str(stage["id"]), command_bindings
        )
        for stage in expected_stage_contract
    }
    expected_cwds = {
        str(stage["id"]): (
            source if stage["cwd"] == "." else source / str(stage["cwd"])
        )
        for stage in expected_stage_contract
    }
    expected_environments = {
        "python_pytest": environment,
        "frontend_production_build": environment,
        "phase6_browser": {
            **environment, "PHASE6_CHROMIUM_EXECUTABLE": str(browser_executable)
        },
    }
    stages = event.get("stages")
    if type(stages) is not list or [
        item.get("id") if type(item) is dict else None for item in stages
    ] != list(COMPOSITE_STAGE_IDS):
        raise ValueError("composite stage inventory/order differs")

    prefix = b"".join(
        f"{name}={value}\n".encode("utf-8")
        for name, value in (
            ("frontend_lock_sha256", preflight["lockfile_sha256"]),
            ("locked_packages_verified", preflight["locked_packages_verified"]),
            (
                "locked_optional_packages_absent",
                preflight["locked_optional_packages_absent"],
            ),
            ("frontend_dependency_inventory", "PASS"),
            ("node_version", checked_runtimes["node"]["version"]),
            ("npm_version", checked_runtimes["npm"]["version"]),
            ("browser_version", checked_runtimes["browser"]["version"]),
            ("frontend_build_script", frontend_scripts["build"]),
            ("phase6_browser_script", frontend_scripts["test:phase6"]),
        )
    )
    reconstructed = bytearray(prefix)
    stage_outputs: dict[str, bytes] = {}
    stage_results: list[dict[str, object]] = []
    for stage in stages:
        stage_id = str(stage["id"])
        if set(stage) != {
            "id", "argv", "cwd", "environment", "started_at", "completed_at",
            "duration_milliseconds", "exit_code", "raw_log",
        }:
            raise ValueError("composite stage shape differs")
        started = _parse_utc(stage.get("started_at"))
        completed = _parse_utc(stage.get("completed_at"))
        if (
            completed < started
            or type(stage.get("duration_milliseconds")) is not int
            or int(stage["duration_milliseconds"]) < 0
            or type(stage.get("exit_code")) is not int
            or stage.get("argv") != expected_commands[stage_id]
            or stage.get("cwd") != str(expected_cwds[stage_id])
            or stage.get("environment") != dict(
                sorted(expected_environments[stage_id].items())
            )
        ):
            raise ValueError("composite stage command/cwd/environment differs")
        reconstructed.extend(
            b"[phase9-composite] "
            + canonical_bytes(
                {
                    "schema": COMPOSITE_EVENT_SCHEMA,
                    "event": "stage_start",
                    "stage": stage_id,
                }
            )
            + b"\n"
        )
        raw_descriptor = stage.get("raw_log")
        if type(raw_descriptor) is not dict or set(raw_descriptor) != {
            "offset", "bytes", "sha256"
        }:
            raise ValueError("composite stage raw-log descriptor differs")
        if (
            type(raw_descriptor.get("offset")) is not int
            or raw_descriptor["offset"] != len(reconstructed)
            or type(raw_descriptor.get("bytes")) is not int
            or int(raw_descriptor["bytes"]) < 0
            or type(raw_descriptor.get("sha256")) is not str
            or _HEX64.fullmatch(str(raw_descriptor["sha256"])) is None
        ):
            raise ValueError("composite stage raw-log coordinate differs")
        start = int(raw_descriptor["offset"])
        end = start + int(raw_descriptor["bytes"])
        output = raw_log[start:end]
        if len(output) != raw_descriptor["bytes"] or hashlib.sha256(output).hexdigest() != (
            raw_descriptor["sha256"]
        ):
            raise ValueError("composite stage raw-log slice differs")
        stage_outputs[stage_id] = output
        reconstructed.extend(output)
        if output and not output.endswith(b"\n"):
            reconstructed.extend(b"\n")
        reconstructed.extend(
            b"[phase9-composite] "
            + canonical_bytes(
                {
                    "schema": COMPOSITE_EVENT_SCHEMA,
                    "event": "stage_finish",
                    "stage": stage_id,
                    "exit_code": stage["exit_code"],
                }
            )
            + b"\n"
        )
        stage_results.append(
            {"id": stage_id, "exit_code": stage["exit_code"], "raw_log": raw_descriptor}
        )
    if bytes(reconstructed) != raw_log:
        raise ValueError("composite raw log has unbound or missing bytes")
    first_nonzero = next(
        (int(stage["exit_code"]) for stage in stages if stage["exit_code"] != 0), 0
    )
    if event.get("overall_exit_code") != first_nonzero:
        raise ValueError("composite overall result differs from stage results")
    build_stage = next(stage for stage in stages if stage["id"] == "frontend_production_build")
    build_output_value = event.get("build_output")
    if build_stage["exit_code"] == 0:
        build_output = _validate_build_output(
            build_output_value,
            expected_root=basetemp / "frontend-production-build",
            runtime_validation=runtime_validation,
        )
    else:
        if build_output_value is not None:
            raise ValueError("failed frontend build unexpectedly claims an output")
        build_output = None
    try:
        browser_summary = parse_node_test_summary(stage_outputs["phase6_browser"])
        browser_node_outcomes = parse_node_test_outcomes(
            stage_outputs["phase6_browser"], browser_summary
        )
    except (UnicodeError, ValueError):
        browser_summary = None
        browser_node_outcomes = None
    browser_stage = next(stage for stage in stages if stage["id"] == "phase6_browser")
    browser_complete_pass = bool(
        browser_stage["exit_code"] == 0
        and browser_summary is not None
        and browser_summary["tests"] > 0
        and browser_summary["pass"] == browser_summary["tests"]
        and not any(
            browser_summary[name]
            for name in ("fail", "cancelled", "skipped", "todo")
        )
    )
    if browser_stage["exit_code"] == 0 and not browser_complete_pass:
        raise ValueError("zero-exit browser stage is not a complete PASS")
    contract = {
        "schema": COMPOSITE_EVENT_SCHEMA,
        "stage_ids": list(COMPOSITE_STAGE_IDS),
        "stage_contract": expected_stage_contract,
        "commands": {
            str(stage["id"]): stage["command"]
            for stage in expected_stage_contract
        },
        "cwds": {
            str(stage["id"]): stage["cwd"]
            for stage in expected_stage_contract
        },
        "environment_keys": {
            stage_id: sorted(expected_environments[stage_id])
            for stage_id in COMPOSITE_STAGE_IDS
        },
        "runtime_identities": {
            name: {
                key: descriptor[key]
                for key in ("resolved_path", "bytes", "sha256", "version")
            }
            for name, descriptor in checked_runtimes.items()
        },
        "frontend_scripts": frontend_scripts,
        "browser_test_targets": list(PHASE6_BROWSER_TARGETS),
        "build_output": (
            None
            if build_output is None
            else {key: build_output[key] for key in build_output if key != "root"}
        ),
        "dependency_inventory_sha256": dependency_inventory["inventory_sha256"],
        "browser_test_summary": browser_summary,
        "browser_node_outcomes": browser_node_outcomes,
    }
    return {
        "stage_results": stage_results,
        "python_log": stage_outputs["python_pytest"],
        "browser_test_summary": browser_summary,
        "browser_node_outcomes": browser_node_outcomes,
        "browser_complete_pass": browser_complete_pass,
        "build_output": build_output,
        "contract_sha256": canonical_sha256(contract),
        "contract": contract,
        "overall_exit_code": first_nonzero,
    }
