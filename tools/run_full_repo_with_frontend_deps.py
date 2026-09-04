#!/usr/bin/env python3
"""Run the trusted full-repository Python/build/browser composite suite."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Callable
import unicodedata


COMPOSITE_EVENT_SCHEMA = "paper-factory-phase9-composite-stage-events-v2"
COMPOSITE_EVENT_TRANSPORT = "PARENT_CAPTURED_ANONYMOUS_PIPE"
BUILD_OUTPUT_SCHEMA = "paper-factory-phase9-frontend-build-output-v1"
_COMPOSITE_STAGE_DEFINITIONS = (
    (
        "python_pytest",
        (
            "${PYTHON}", "-I", "-S", "-B", "${TRUSTED_REPORTER}",
            "--runtime-site-packages", "${RUNTIME_SITE_PACKAGES}",
            "--source-root", "${SOURCE_ROOT}", "--", "-q", "-p",
            "no:cacheprovider", "--noconftest", "-c", "/dev/null",
            "--rootdir", "${SOURCE_ROOT}", "-o", "addopts=",
            "--basetemp=${BASE_TEMP}", "tests",
        ),
        ".",
        ("tests",),
        None,
    ),
    (
        "frontend_production_build",
        (
            "${NODE}", "${NPM_CLI}", "run", "build", "--",
            "--configLoader", "runner", "--outDir",
            "${BASE_TEMP}/frontend-production-build", "--emptyOutDir",
        ),
        "web/frontend",
        ("web/frontend/package.json",),
        ("build", "vite build"),
    ),
    (
        "phase6_browser",
        ("${NODE}", "${NPM_CLI}", "run", "test:phase6"),
        "web/frontend",
        (
            "web/frontend/tests/phase6-controller.test.mjs",
            "web/frontend/tests/phase6-build-browser.test.mjs",
        ),
        (
            "test:phase6",
            "node --test --test-concurrency=1 "
            "tests/phase6-controller.test.mjs "
            "tests/phase6-build-browser.test.mjs",
        ),
    ),
)


def composite_stage_contract() -> list[dict[str, object]]:
    """Return the single machine-readable composite command/target contract."""

    return [
        {
            "id": identifier,
            "command": list(command),
            "cwd": cwd,
            "targets": list(targets),
            "npm_script": (
                None if npm_script is None else {
                    "name": npm_script[0], "body": npm_script[1]
                }
            ),
        }
        for identifier, command, cwd, targets, npm_script
        in _COMPOSITE_STAGE_DEFINITIONS
    ]


def expand_composite_stage_command(
    stage_id: str, bindings: dict[str, str]
) -> list[str]:
    """Expand one contracted symbolic command without accepting extra syntax."""

    matches = [
        stage for stage in composite_stage_contract() if stage["id"] == stage_id
    ]
    if len(matches) != 1:
        raise RuntimeError(f"unknown composite stage: {stage_id}")
    result: list[str] = []
    for item in matches[0]["command"]:
        expanded = str(item)
        for placeholder, value in bindings.items():
            expanded = expanded.replace(placeholder, value)
        if "${" in expanded:
            raise RuntimeError(
                f"unbound composite command placeholder in stage: {stage_id}"
            )
        result.append(expanded)
    return result


COMPOSITE_STAGE_IDS = tuple(
    str(stage["id"]) for stage in composite_stage_contract()
)
_PHASE6_STAGE = next(
    stage for stage in composite_stage_contract() if stage["id"] == "phase6_browser"
)
PHASE6_BROWSER_SOURCE_TARGETS = tuple(
    str(target) for target in _PHASE6_STAGE["targets"]
)
PHASE6_BROWSER_TARGETS = tuple(
    target.removeprefix("web/frontend/") for target in PHASE6_BROWSER_SOURCE_TARGETS
)
EXPECTED_FRONTEND_SCRIPTS = {
    str(stage["npm_script"]["name"]): str(stage["npm_script"]["body"])
    for stage in composite_stage_contract()
    if stage["npm_script"] is not None
}
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_BROWSER_VERSION = re.compile(r"(?:chromium|chrome|headless\s*shell)", re.IGNORECASE)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_node_test_summary(raw: bytes) -> dict[str, int]:
    """Parse one complete ``node --test`` TAP summary."""

    text = raw.decode("utf-8", errors="strict")
    result: dict[str, int] = {}
    for name in ("tests", "pass", "fail", "cancelled", "skipped", "todo"):
        matches = re.findall(rf"^# {name} (\d+)\s*$", text, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError("browser test summary is missing or ambiguous")
        result[name] = int(matches[0])
    return result


def parse_node_test_outcomes(
    raw: bytes, summary: dict[str, int]
) -> list[dict[str, object]]:
    """Parse stable top-level TAP test identities and reconcile all outcomes."""

    text = raw.decode("utf-8", errors="strict")
    rows: list[dict[str, object]] = []
    pattern = re.compile(r"^(ok|not ok) ([1-9][0-9]*) - (.+?)\s*$", re.MULTILINE)
    for status, sequence_text, description in pattern.findall(text):
        directive_match = re.search(
            r"\s+#\s+(SKIP|TODO)(?:\s+.*)?\Z", description, re.IGNORECASE
        )
        directive = (
            None if directive_match is None else directive_match.group(0).strip()
        )
        name = (
            description
            if directive_match is None
            else description[: directive_match.start()].rstrip()
        )
        if not name:
            raise ValueError("browser test identity is empty")
        rows.append(
            {
                "sequence": int(sequence_text),
                "name": name,
                "outcome": "passed" if status == "ok" else "failed",
                "directive": directive,
            }
        )
    if (
        len(rows) != summary["tests"]
        or [row["sequence"] for row in rows]
        != list(range(1, summary["tests"] + 1))
    ):
        raise ValueError("browser test node inventory differs from TAP summary")
    skipped = sum(
        1
        for row in rows
        if row["directive"] is not None
        and str(row["directive"]).upper().startswith("# SKIP")
    )
    todo = sum(
        1
        for row in rows
        if row["directive"] is not None
        and str(row["directive"]).upper().startswith("# TODO")
    )
    passed = sum(
        1
        for row in rows
        if row["outcome"] == "passed" and row["directive"] is None
    )
    not_ok = sum(1 for row in rows if row["outcome"] == "failed")
    if (
        passed != summary["pass"]
        or skipped != summary["skipped"]
        or todo != summary["todo"]
        or not_ok != summary["fail"] + summary["cancelled"]
    ):
        raise ValueError("browser test node outcomes differ from TAP summary")
    return rows


def require_browser_complete_pass(raw: bytes) -> dict[str, object]:
    """Reject every semantic TAP non-pass even when npm itself exits zero."""

    summary = parse_node_test_summary(raw)
    outcomes = parse_node_test_outcomes(raw, summary)
    if (
        summary["tests"] <= 0
        or summary["pass"] != summary["tests"]
        or any(summary[name] for name in ("fail", "cancelled", "skipped", "todo"))
    ):
        raise RuntimeError("browser TAP result is not a complete PASS")
    return {"summary": summary, "node_outcomes": outcomes}


def _verify_locked_dependencies(source: Path, dependency: Path) -> tuple[str, int, int]:
    """Verify every required lock entry against the mounted dependency tree.

    Development dependencies are required because the composite build and
    browser stages execute Vite and Playwright. Only platform-optional lock
    entries may be absent.
    """

    lock_path = source / "web/frontend/package-lock.json"
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    packages = lock.get("packages")
    if type(packages) is not dict or "" not in packages:
        raise RuntimeError("frontend package-lock packages map is invalid")
    verified = 0
    optional_absent = 0
    for lock_key, expected in sorted(packages.items()):
        if not lock_key.startswith("node_modules/"):
            continue
        if type(expected) is not dict or type(expected.get("version")) is not str:
            raise RuntimeError(f"lock entry lacks an exact version: {lock_key}")
        relative = lock_key.removeprefix("node_modules/")
        package_file = dependency / relative / "package.json"
        if not package_file.is_file() or package_file.is_symlink():
            if expected.get("optional") is True:
                optional_absent += 1
                continue
            raise RuntimeError(f"locked dependency is absent or unsafe: {lock_key}")
        installed = json.loads(package_file.read_bytes())
        if installed.get("version") != expected["version"]:
            raise RuntimeError(f"locked dependency version differs: {lock_key}")
        verified += 1
    if verified == 0:
        raise RuntimeError("no locked frontend dependency was verified")
    return hashlib.sha256(lock_bytes).hexdigest(), verified, optional_absent


def _ordinary_executable(path: Path, label: str) -> Path:
    if not path.is_absolute() or path != Path(os.path.abspath(os.fspath(path))):
        raise RuntimeError(f"{label} path must be canonical and absolute")
    resolved = path.resolve(strict=True)
    metadata = resolved.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise RuntimeError(f"{label} is not an executable regular file")
    return resolved


def _runtime_descriptor(
    path: Path,
    *,
    label: str,
    environment: dict[str, str],
    version_argv: list[str] | None = None,
) -> dict[str, object]:
    resolved = _ordinary_executable(path, label)
    raw = resolved.read_bytes()
    probe_argv = [str(path), "--version"] if version_argv is None else version_argv
    completed = subprocess.run(
        probe_argv,
        cwd="/",
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = completed.stdout.decode("utf-8", errors="strict").strip()
    if completed.returncode != 0 or not output or "\n" in output or len(output) > 4096:
        raise RuntimeError(f"{label} version probe failed")
    return {
        "path": str(path),
        "resolved_path": str(resolved),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "version_argv": probe_argv,
        "version": output,
    }


def _frontend_script_descriptor(source: Path) -> dict[str, object]:
    package_path = source / "web/frontend/package.json"
    raw = package_path.read_bytes()
    package = json.loads(raw)
    scripts = package.get("scripts") if type(package) is dict else None
    if type(scripts) is not dict or any(
        scripts.get(name) != expected
        for name, expected in EXPECTED_FRONTEND_SCRIPTS.items()
    ):
        raise RuntimeError("frontend build/browser scripts differ from the suite contract")
    if any(not (source / "web/frontend" / target).is_file() for target in PHASE6_BROWSER_TARGETS):
        raise RuntimeError("documented Phase 6 browser target is absent")
    return {
        "package_json_sha256": hashlib.sha256(raw).hexdigest(),
        "build": scripts["build"],
        "test:phase6": scripts["test:phase6"],
        "browser_targets": list(PHASE6_BROWSER_TARGETS),
    }


def _stable_build_file(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError("frontend build output contains a link or special file")
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

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    if not identity(before) == identity(opened) == identity(after) == identity(final):
        raise RuntimeError("frontend build output changed while read")
    return b"".join(chunks)


def inventory_frontend_build_output(root: Path) -> dict[str, object]:
    """Bind one safe, nonempty Vite production output without archiving it."""

    root = Path(os.path.abspath(os.fspath(root)))
    root_before = root.lstat()
    if not stat.S_ISDIR(root_before.st_mode) or stat.S_ISLNK(root_before.st_mode):
        raise RuntimeError("frontend production build output is not an ordinary directory")
    files: list[dict[str, object]] = []
    collisions: set[str] = set()
    directories: dict[Path, tuple[int, ...]] = {}

    def directory_identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def traversal_error(error: OSError) -> None:
        raise RuntimeError("frontend production build output cannot be enumerated") from error

    for current, names, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=traversal_error
    ):
        current_path = Path(current)
        metadata = current_path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("frontend production build output directory is unsafe")
        directories[current_path] = directory_identity(metadata)
        for name in sorted(names):
            child = current_path / name
            child_metadata = child.lstat()
            if not stat.S_ISDIR(child_metadata.st_mode) or stat.S_ISLNK(
                child_metadata.st_mode
            ):
                raise RuntimeError("frontend production build output contains an unsafe directory")
        names[:] = sorted(names)
        for name in sorted(filenames):
            item = current_path / name
            relative = item.relative_to(root).as_posix()
            relative.encode("utf-8", errors="strict")
            if (
                not relative
                or relative.startswith("/")
                or ".." in Path(relative).parts
                or "\\" in relative
                or unicodedata.normalize("NFC", relative) != relative
            ):
                raise RuntimeError("frontend production build output path is unsafe")
            collision = relative.casefold()
            if collision in collisions:
                raise RuntimeError("frontend production build output paths collide")
            collisions.add(collision)
            raw = _stable_build_file(item)
            mode = "100755" if item.stat().st_mode & 0o111 else "100644"
            files.append(
                {
                    "path": relative,
                    "mode": mode,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
    for directory, before in directories.items():
        if directory_identity(directory.lstat()) != before:
            raise RuntimeError("frontend production build output changed during inventory")
    if directory_identity(root.lstat()) != directory_identity(root_before):
        raise RuntimeError("frontend production build output root changed during inventory")
    files.sort(key=lambda item: str(item["path"]))
    index = [item for item in files if item["path"] == "index.html"]
    assets = [
        item for item in files
        if str(item["path"]).startswith("assets/") and int(item["bytes"]) > 0
    ]
    if len(index) != 1 or int(index[0]["bytes"]) <= 0 or not assets:
        raise RuntimeError("frontend production build lacks nonempty index.html/assets")
    return {
        "schema": BUILD_OUTPUT_SCHEMA,
        "root": str(root),
        "path_count": len(files),
        "total_file_bytes": sum(int(item["bytes"]) for item in files),
        "tree_sha256": hashlib.sha256(_canonical_bytes(files)).hexdigest(),
        "files": files,
    }


def _run_stage(
    *,
    stage_id: str,
    argv: list[str],
    cwd: Path,
    environment: dict[str, str],
    pass_fds: tuple[int, ...],
    write_log,
    postcondition: Callable[[bytes], dict[str, object]] | None = None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    start_boundary = _canonical_bytes(
        {"schema": COMPOSITE_EVENT_SCHEMA, "event": "stage_start", "stage": stage_id}
    )
    write_log(b"[phase9-composite] " + start_boundary + b"\n")
    output_offset = write_log(b"")
    started_at = _utc()
    started_ns = time.monotonic_ns()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            pass_fds=pass_fds,
        )
        output = completed.stdout
        exit_code = int(completed.returncode)
    except OSError as exc:
        output = (
            f"[phase9-composite] stage process could not start: {type(exc).__name__}\n"
        ).encode("utf-8")
        exit_code = 127
    if type(output) is not bytes:
        raise RuntimeError("composite stage output is not bytes")
    postcondition_result: dict[str, object] | None = None
    if exit_code == 0 and postcondition is not None:
        try:
            postcondition_result = postcondition(output)
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            output += (
                "[phase9-composite] stage postcondition failed: "
                f"{type(exc).__name__}\n"
            ).encode("utf-8")
            exit_code = 88
    write_log(output)
    if output and not output.endswith(b"\n"):
        write_log(b"\n")
    duration_ms = (time.monotonic_ns() - started_ns) // 1_000_000
    ended_at = _utc()
    end_boundary = _canonical_bytes(
        {
            "schema": COMPOSITE_EVENT_SCHEMA,
            "event": "stage_finish",
            "stage": stage_id,
            "exit_code": exit_code,
        }
    )
    write_log(b"[phase9-composite] " + end_boundary + b"\n")
    return {
        "id": stage_id,
        "argv": argv,
        "cwd": str(cwd),
        "environment": dict(sorted(environment.items())),
        "started_at": started_at,
        "completed_at": ended_at,
        "duration_milliseconds": duration_ms,
        "exit_code": exit_code,
        "raw_log": {
            "offset": output_offset,
            "bytes": len(output),
            "sha256": hashlib.sha256(output).hexdigest(),
        },
    }, postcondition_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--dependency-target", required=True, type=Path)
    parser.add_argument("--browser-root", required=True, type=Path)
    parser.add_argument("--browser-executable", required=True, type=Path)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--npm", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--basetemp", required=True, type=Path)
    args = parser.parse_args(argv)

    source = args.source_root.resolve(strict=True)
    dependency = args.dependency_target.resolve(strict=True)
    browser_root = args.browser_root.resolve(strict=True)
    python = Path(os.path.abspath(os.fspath(args.python)))
    node = Path(os.path.abspath(os.fspath(args.node)))
    npm = Path(os.path.abspath(os.fspath(args.npm)))
    browser = Path(os.path.abspath(os.fspath(args.browser_executable)))
    basetemp = Path(os.path.abspath(os.fspath(args.basetemp)))
    for path, label in (
        (python, "Python"),
        (node, "Node"),
        (npm, "npm"),
        (browser, "browser"),
    ):
        _ordinary_executable(path, label)
    dependency_metadata = dependency.lstat()
    browser_metadata = browser_root.lstat()
    if (
        not stat.S_ISDIR(dependency_metadata.st_mode)
        or stat.S_ISLNK(dependency_metadata.st_mode)
        or not stat.S_ISDIR(browser_metadata.st_mode)
        or stat.S_ISLNK(browser_metadata.st_mode)
    ):
        raise RuntimeError("dependency or browser root is not an ordinary directory")
    try:
        browser.resolve(strict=True).relative_to(browser_root)
    except ValueError as exc:
        raise RuntimeError("browser executable is outside the explicit browser root") from exc

    lock_sha256, locked_packages, optional_absent = _verify_locked_dependencies(
        source, dependency
    )
    frontend_scripts = _frontend_script_descriptor(source)
    link = source / "web/frontend/node_modules"
    if not link.is_symlink() or link.resolve(strict=True) != dependency:
        raise RuntimeError(
            "frontend dependency must be mounted by the trusted audit sandbox"
        )
    if not stat.S_ISLNK(link.lstat().st_mode):
        raise RuntimeError("frontend dependency sandbox mount differs")

    reporter = os.environ.get("PHASE9_TRUSTED_PYTEST_REPORTER_PATH")
    runtime_site_packages = os.environ.get("PHASE9_TRUSTED_PYTEST_SITE_PACKAGES")
    pytest_fd_text = os.environ.get("PHASE9_TRUSTED_PYTEST_EVENT_FD")
    composite_fd_text = os.environ.get("PHASE9_TRUSTED_COMPOSITE_EVENT_FD")
    composite_nonce = os.environ.get("PHASE9_TRUSTED_COMPOSITE_NONCE")
    if (
        not reporter
        or not runtime_site_packages
        or os.environ.get("PHASE9_TRUSTED_PYTEST_EVENT_PATH")
        != COMPOSITE_EVENT_TRANSPORT
        or os.environ.get("PHASE9_TRUSTED_COMPOSITE_EVENT_PATH")
        != COMPOSITE_EVENT_TRANSPORT
        or not pytest_fd_text
        or not pytest_fd_text.isdecimal()
        or int(pytest_fd_text) < 3
        or not composite_fd_text
        or not composite_fd_text.isdecimal()
        or int(composite_fd_text) < 3
        or int(composite_fd_text) == int(pytest_fd_text)
        or not os.environ.get("PHASE9_TRUSTED_PYTEST_NONCE")
        or not composite_nonce
        or _NONCE.fullmatch(composite_nonce) is None
    ):
        raise RuntimeError("trusted composite reporter coordinate is absent")
    pytest_fd = int(pytest_fd_text)
    composite_fd = int(composite_fd_text)
    if not stat.S_ISFIFO(os.fstat(pytest_fd).st_mode) or not stat.S_ISFIFO(
        os.fstat(composite_fd).st_mode
    ):
        raise RuntimeError("trusted parent event channel is not a pipe")

    # Some sandbox launchers synthesize PWD even though it is not part of the
    # parent's explicit audit environment.  Never let that implicit variable
    # become part of a stage's environment contract.
    base_environment = {
        name: value for name, value in os.environ.items() if name != "PWD"
    }
    node_runtime = _ordinary_executable(node, "Node")
    npm_cli = _ordinary_executable(npm, "npm CLI")
    runtimes = {
        "python": _runtime_descriptor(
            python, label="Python", environment=base_environment
        ),
        "node": _runtime_descriptor(node, label="Node", environment=base_environment),
        "npm": _runtime_descriptor(
            npm,
            label="npm",
            environment=base_environment,
            version_argv=[str(node_runtime), str(npm_cli), "--version"],
        ),
        "browser": _runtime_descriptor(
            browser, label="browser", environment=base_environment
        ),
    }
    if _BROWSER_VERSION.search(str(runtimes["browser"]["version"])) is None:
        raise RuntimeError("browser version probe does not identify Chromium/Chrome")

    log_offset = 0

    def write_log(raw: bytes) -> int:
        nonlocal log_offset
        start = log_offset
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        log_offset += len(raw)
        return start

    for name, value in (
        ("frontend_lock_sha256", lock_sha256),
        ("locked_packages_verified", locked_packages),
        ("locked_optional_packages_absent", optional_absent),
        ("frontend_dependency_inventory", "PASS"),
        ("node_version", runtimes["node"]["version"]),
        ("npm_version", runtimes["npm"]["version"]),
        ("browser_version", runtimes["browser"]["version"]),
        ("frontend_build_script", frontend_scripts["build"]),
        ("phase6_browser_script", frontend_scripts["test:phase6"]),
    ):
        write_log(f"{name}={value}\n".encode("utf-8"))

    frontend = source / "web/frontend"
    browser_environment = dict(base_environment)
    browser_environment["PHASE6_CHROMIUM_EXECUTABLE"] = str(browser)
    build_output_root = basetemp / "frontend-production-build"
    build_output: dict[str, object] | None = None

    def verify_build_output_after_browser(output: bytes) -> dict[str, object]:
        """Prove the browser stage did not alter the production build."""

        browser_result = require_browser_complete_pass(output)
        if build_output is None:
            # The build stage already failed and therefore determines the
            # composite result.  There is no trusted initial tree to compare.
            return {"build_output": None, "browser_result": browser_result}
        observed = inventory_frontend_build_output(build_output_root)
        if observed != build_output:
            raise RuntimeError(
                "frontend production build changed during browser tests"
            )
        return {
            "build_output": observed,
            "browser_result": browser_result,
        }

    command_bindings = {
        "${PYTHON}": str(python),
        "${TRUSTED_REPORTER}": reporter,
        "${RUNTIME_SITE_PACKAGES}": runtime_site_packages,
        "${SOURCE_ROOT}": str(source),
        "${BASE_TEMP}": str(basetemp),
        "${NODE}": str(node_runtime),
        "${NPM_CLI}": str(npm_cli),
    }
    stage_controls = {
        "python_pytest": (base_environment, (pytest_fd,), None),
        "frontend_production_build": (
            base_environment,
            (),
            lambda _output: inventory_frontend_build_output(build_output_root),
        ),
        "phase6_browser": (
            browser_environment,
            (),
            verify_build_output_after_browser,
        ),
    }
    stage_specs = tuple(
        (
            str(stage["id"]),
            expand_composite_stage_command(str(stage["id"]), command_bindings),
            source if stage["cwd"] == "." else source / str(stage["cwd"]),
            *stage_controls[str(stage["id"])],
        )
        for stage in composite_stage_contract()
    )
    stages: list[dict[str, object]] = []
    for (
        stage_id,
        stage_argv,
        stage_cwd,
        stage_environment,
        pass_fds,
        postcondition,
    ) in stage_specs:
        stage, postcondition_result = _run_stage(
            stage_id=stage_id,
            argv=stage_argv,
            cwd=stage_cwd,
            environment=stage_environment,
            pass_fds=pass_fds,
            write_log=write_log,
            postcondition=postcondition,
        )
        stages.append(stage)
        if stage_id == "frontend_production_build":
            build_output = postcondition_result
    nonzero = [int(stage["exit_code"]) for stage in stages if stage["exit_code"] != 0]
    overall_exit_code = nonzero[0] if nonzero else 0
    event = {
        "schema": COMPOSITE_EVENT_SCHEMA,
        "nonce": composite_nonce,
        "event_transport": COMPOSITE_EVENT_TRANSPORT,
        "source_root": str(source),
        "dependency_target": str(dependency),
        "browser_root": str(browser_root),
        "browser_executable": str(browser),
        "basetemp": str(basetemp),
        "environment_policy": "EXPLICIT_SANITIZED_ALLOWLIST",
        "dependency_preflight": {
            "lockfile_sha256": lock_sha256,
            "locked_packages_verified": locked_packages,
            "locked_optional_packages_absent": optional_absent,
        },
        "frontend_scripts": frontend_scripts,
        "build_output": build_output,
        "runtimes": runtimes,
        "stages": stages,
        "overall_exit_code": overall_exit_code,
    }
    with os.fdopen(composite_fd, "wb", buffering=0, closefd=True) as sink:
        sink.write(_canonical_bytes(event) + b"\n")
    return overall_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
