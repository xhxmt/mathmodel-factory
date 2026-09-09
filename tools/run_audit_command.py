#!/usr/bin/env python3
"""Run one sanitized audit command and bind it to executed tracked bytes."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory_core.canonical import canonical_bytes, canonical_sha256
from tools.trusted_pytest_reporter import (
    TRUSTED_PYTEST_EVENT_SCHEMA,
    validate_trusted_pytest_events,
)
from tools.run_full_repo_with_frontend_deps import (
    COMPOSITE_EVENT_SCHEMA,
    COMPOSITE_EVENT_TRANSPORT,
    COMPOSITE_STAGE_IDS,
    _verify_locked_dependencies,
    composite_stage_contract,
)
from tools.phase9_composite_evidence import validate_composite_events


COMMAND_SCHEMA = "paper-factory-phase9-audit-command-v8"
PREFLIGHT_FAILURE_SCHEMA = "paper-factory-phase9-audit-preflight-failure-v1"
PREFLIGHT_FAILURE_EXIT_CODE = 125
INVENTORY_SCHEMA = "paper-factory-executed-source-inventory-v1"
DEPENDENCY_INVENTORY_SCHEMA = "paper-factory-executed-dependency-inventory-v2"
SANDBOX_SCHEMA = "paper-factory-audit-execution-sandbox-v1"
PARENT_OBSERVER_SCHEMA = "paper-factory-audit-parent-observer-v1"
TRUSTED_REPORTER_PATH = "tools/trusted_pytest_reporter.py"
_OUTCOMES = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
NON_RECORDABLE_INVOCATION_VALIDATION = "NON_RECORDABLE_INVOCATION_VALIDATION"
_SENSITIVE_ENV = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|AUTH|PROVIDER|ANTHROPIC|OPENAI|"
    r"GEMINI|VERTEX|GCP|GOOGLE_APPLICATION|AWS|AZURE|SOLVER|OUTBOX|RELEASE|"
    r"DEPLOY|CUTOVER|PRODUCTION|PHASE78|PHASE9)",
    re.IGNORECASE,
)


def _utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_outcomes(raw: bytes, kind: str) -> dict[str, int]:
    """Independently parse the final complete pytest/bootstrap summary."""

    text = raw.decode("utf-8", errors="replace")
    if kind == "bootstrap":
        values: dict[str, int] = {}
        for name in (*_OUTCOMES, "warnings", "collected"):
            matches = re.findall(rf"^{name}=(\d+)\s*$", text, re.MULTILINE)
            values[name] = int(matches[-1]) if matches else 0
        if not values["collected"]:
            values["collected"] = sum(values[name] for name in _OUTCOMES)
        return values
    if kind != "pytest":
        raise RuntimeError(f"unsupported outcome parser: {kind}")
    # A pytest result is only evidence when its terminal summary is present.  This
    # deliberately rejects arbitrary ``PASS`` text, progress-only/truncated logs,
    # and an earlier summary followed by an unrecorded second command.
    meaningful = [
        re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line.strip())
        for line in text.splitlines()
        if line.strip()
    ]
    if not meaningful:
        return {name: 0 for name in (*_OUTCOMES, "warnings", "collected")}
    decorated = re.compile(
        r"=+\s*(?:(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?)"
        r"(?:,\s*)?)+)\s+in\s+\d+(?:\.\d+)?s(?:\s*\([^)]*\))?\s*=+\Z",
        re.IGNORECASE,
    )
    quiet = re.compile(
        r"(?:(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?)"
        r"(?:,\s*)?)+)\s+in\s+\d+(?:\.\d+)?s(?:\s*\([^)]*\))?\Z",
        re.IGNORECASE,
    )
    # Exactly one terminal summary is required and it must be the final
    # meaningful line.  This rejects an atexit hook (or a second command) that
    # appends a forged PASS after pytest's genuine non-pass summary.
    summaries = [
        (index, line)
        for index, line in enumerate(meaningful)
        if decorated.fullmatch(line) or quiet.fullmatch(line)
    ]
    if len(summaries) != 1 or summaries[0][0] != len(meaningful) - 1:
        return {name: 0 for name in (*_OUTCOMES, "warnings", "collected")}
    terminal = summaries[0][1]
    patterns = {
        "passed": r"(\d+)\s+passed\b",
        "failed": r"(\d+)\s+failed\b",
        "errors": r"(\d+)\s+errors?\b",
        "skipped": r"(\d+)\s+skipped\b",
        "xfailed": r"(\d+)\s+xfailed\b",
        "xpassed": r"(\d+)\s+xpassed\b",
        "warnings": r"(\d+)\s+warnings?\b",
    }
    result: dict[str, int] = {}
    for name, pattern in patterns.items():
        matches = re.findall(pattern, terminal, re.IGNORECASE)
        result[name] = int(matches[-1]) if matches else 0
    result["collected"] = sum(result[name] for name in _OUTCOMES)
    return result


def parse_progress_nonpass(raw: bytes) -> dict[str, int]:
    """Count non-pass symbols in pytest's parent-captured progress lines."""

    text = raw.decode("utf-8", errors="replace")
    counts = {name: 0 for name in ("failed", "errors", "skipped", "xfailed", "xpassed")}
    symbols = {
        "F": "failed",
        "E": "errors",
        "s": "skipped",
        "x": "xfailed",
        "X": "xpassed",
    }
    progress = re.compile(r"^([.FEsxX]+)(?:\s+\[\s*\d+%\])?$")
    for raw_line in text.splitlines():
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw_line.strip())
        match = progress.fullmatch(line)
        if match is None:
            continue
        for symbol in match.group(1):
            if symbol in symbols:
                counts[symbols[symbol]] += 1
    return counts


def _git(repository: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    environment = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repository,
        input=input_bytes,
        check=True,
        stdin=subprocess.DEVNULL if input_bytes is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    ).stdout


def _candidate_identity(repository: Path) -> dict[str, str]:
    line = _git(repository, "rev-list", "--parents", "-n", "1", "HEAD").decode(
        "ascii"
    ).strip().split()
    if len(line) != 2:
        raise RuntimeError("audit candidate must have exactly one parent")
    tree = _git(repository, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    return {"commit": line[0], "tree": tree, "parent": line[1]}


def _safe_git_path(raw: bytes) -> str:
    path = raw.decode("utf-8", errors="strict")
    pure = PurePosixPath(path)
    if (
        not path
        or pure.is_absolute()
        or pure.as_posix() != path
        or ".." in pure.parts
        or "\\" in path
        or unicodedata.normalize("NFC", path) != path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(part.endswith((".", " ")) for part in pure.parts)
    ):
        raise RuntimeError(f"candidate contains unsafe Git path: {path!r}")
    return path


def _blob_oid(raw: bytes) -> str:
    header = f"blob {len(raw)}\0".encode("ascii")
    return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()


def _read_regular(path: Path, mode: str) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError(f"tracked path is not one non-hardlinked file: {path}")
    executable = bool(stat.S_IMODE(before.st_mode) & 0o111)
    if executable != (mode == "100755"):
        raise RuntimeError(f"tracked executable mode differs: {path}")
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
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    final = path.lstat()
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise RuntimeError(f"tracked path changed while read: {path}")
    if identity(final) != identity(after):
        raise RuntimeError(f"tracked pathname changed while read: {path}")
    return b"".join(chunks)


def _verify_execution_root_closure(
    source_root: Path, records: list[dict[str, object]]
) -> None:
    """Reject any non-Git byte that could alter collection or imports."""

    expected_files = {
        str(item["path"])
        for item in records
        if item.get("type") == "blob"
    }
    gitlinks = {
        str(item["path"])
        for item in records
        if item.get("type") == "commit"
    }
    permitted_directories = {""}
    for relative in expected_files | gitlinks:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            permitted_directories.add(parent.as_posix())
            parent = parent.parent
        if relative in gitlinks:
            permitted_directories.add(relative)

    actual_files: set[str] = set()

    def traversal_error(error: OSError) -> None:
        raise RuntimeError("execution source tree cannot be enumerated") from error

    for current, directories, files in os.walk(
        source_root, topdown=True, followlinks=False, onerror=traversal_error
    ):
        current_path = Path(current)
        current_relative = current_path.relative_to(source_root).as_posix()
        if current_relative == ".":
            current_relative = ""
        filtered: list[str] = []
        for name in directories:
            if current_relative == "" and name == ".git":
                continue
            path = current_path / name
            metadata = path.lstat()
            relative = path.relative_to(source_root).as_posix()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(
                    f"execution source contains a link or special directory: {relative}"
                )
            if relative not in permitted_directories:
                raise RuntimeError(
                    f"execution source contains an untracked directory: {relative}"
                )
            filtered.append(name)
        directories[:] = filtered
        for name in files:
            if current_relative == "" and name == ".git":
                continue
            path = current_path / name
            metadata = path.lstat()
            relative = path.relative_to(source_root).as_posix()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise RuntimeError(
                    f"execution source contains a link, hardlink, or special file: {relative}"
                )
            actual_files.add(relative)
    if actual_files != expected_files:
        raise RuntimeError(
            "execution source file closure differs from the immutable Git tree"
        )


def executed_source_inventory(
    repository: Path,
    source_root: Path,
    *,
    execution_environment: str,
) -> tuple[dict[str, object], bytes]:
    """Verify each executable source byte against the immutable HEAD tree."""

    identity_before = _candidate_identity(repository)
    if execution_environment == "source":
        execution_repository = source_root
        top = Path(
            _git(execution_repository, "rev-parse", "--show-toplevel")
            .decode()
            .strip()
        ).resolve()
        if top != source_root:
            raise RuntimeError("source execution cwd is not the candidate worktree root")
        if _candidate_identity(execution_repository) != identity_before:
            raise RuntimeError("source execution Git identity differs from candidate")
        dirty = _git(
            execution_repository,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
        )
        if dirty:
            raise RuntimeError("source execution refuses tracked or index dirty bytes")
    elif execution_environment == "fresh":
        if (source_root / ".git").exists() or (source_root / ".git").is_symlink():
            raise RuntimeError("fresh execution root must not contain Git metadata")
    else:
        raise RuntimeError("execution environment must be source or fresh")

    records: list[dict[str, object]] = []
    collision_keys: set[str] = set()
    raw_tree = _git(repository, "ls-tree", "-rz", "--full-tree", "HEAD")
    for item in raw_tree.split(b"\0"):
        if not item:
            continue
        header, raw_path = item.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        relative = _safe_git_path(raw_path)
        collision = unicodedata.normalize("NFC", relative).casefold()
        if collision in collision_keys:
            raise RuntimeError("candidate Git paths collide by case or Unicode")
        collision_keys.add(collision)
        if kind == "commit" and mode == "160000":
            records.append(
                {"path": relative, "mode": mode, "type": kind, "object_id": oid}
            )
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise RuntimeError(f"unsupported tracked entry: {relative}: {mode} {kind}")
        path = source_root.joinpath(*PurePosixPath(relative).parts)
        try:
            raw = _read_regular(path, mode)
        except OSError as exc:
            raise RuntimeError(f"tracked path cannot be read: {relative}") from exc
        if _blob_oid(raw) != oid:
            raise RuntimeError(f"tracked bytes differ from candidate Git blob: {relative}")
        records.append(
            {
                "path": relative,
                "mode": mode,
                "type": kind,
                "object_id": oid,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    _verify_execution_root_closure(source_root, records)
    identity_after = _candidate_identity(repository)
    if identity_after != identity_before:
        raise RuntimeError("candidate identity changed while inventory was read")
    body: dict[str, object] = {
        "schema": INVENTORY_SCHEMA,
        "candidate": identity_before,
        "path_count": len(records),
        "files": records,
    }
    body["inventory_sha256"] = canonical_sha256(body)
    raw = canonical_bytes(body) + b"\n"
    return body, raw


def _prepare_test_source_repository(
    repository: Path,
    audit_root: Path,
    identifier: str,
    expected_inventory: dict[str, object],
) -> Path:
    """Create a clean immutable Git checkout used only by nested test producers.

    The executed source tree remains independently byte-inventoried. Keeping
    this test-only checkout separate prevents legitimate private runtime-state
    mounts from appearing as untracked input to the formal P0 producer.
    """

    target = audit_root / "runtime" / f"{identifier}-test-source-repository"
    if target.exists() or target.is_symlink():
        raise RuntimeError("test source repository is append-only")
    target.parent.mkdir(parents=True, exist_ok=True)
    identity = _candidate_identity(repository)
    environment = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--quiet",
            "--no-local",
            "--no-checkout",
            "--no-tags",
            "--",
            str(repository),
            str(target),
        ],
        cwd=audit_root,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    _git(target, "checkout", "--quiet", "--detach", identity["commit"])
    observed_inventory, _ = executed_source_inventory(
        target, target, execution_environment="source"
    )
    if observed_inventory != expected_inventory:
        raise RuntimeError("test source repository bytes differ from executed source")
    ignored = _git(
        target, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    )
    if ignored:
        raise RuntimeError("test source repository contains ignored files")
    return target


def _external_tree_component(root: Path, *, kind: str) -> dict[str, object]:
    """Bind every byte and safe in-root link below one external runtime root."""

    root = root.resolve(strict=True)
    root_before = root.lstat()
    if not stat.S_ISDIR(root_before.st_mode) or stat.S_ISLNK(root_before.st_mode):
        raise RuntimeError(f"{kind} root is not an ordinary directory")
    records: list[dict[str, object]] = []
    collision_keys: set[str] = set()
    directory_identities: dict[Path, tuple[int, ...]] = {}

    def identity(item: os.stat_result) -> tuple[int, ...]:
        return (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )

    def traversal_error(error: OSError) -> None:
        raise RuntimeError(f"{kind} tree cannot be enumerated") from error

    for current, directories, files in os.walk(
        root, topdown=True, followlinks=False, onerror=traversal_error
    ):
        current_path = Path(current)
        current_metadata = current_path.lstat()
        if not stat.S_ISDIR(current_metadata.st_mode) or stat.S_ISLNK(
            current_metadata.st_mode
        ):
            raise RuntimeError(f"{kind} directory changed during traversal")
        directory_identities[current_path] = identity(current_metadata)
        relative_current = current_path.relative_to(root).as_posix()
        if relative_current != ".":
            records.append(
                {
                    "path": _safe_git_path(relative_current.encode("utf-8")),
                    "type": "directory",
                    "mode": f"{stat.S_IMODE(current_metadata.st_mode):04o}",
                }
            )

        kept_directories: list[str] = []
        for name in sorted(directories):
            item = current_path / name
            metadata = item.lstat()
            relative = _safe_git_path(
                item.relative_to(root).as_posix().encode("utf-8")
            )
            if stat.S_ISLNK(metadata.st_mode):
                target_before = os.readlink(item)
                try:
                    target_before.encode("utf-8", errors="strict")
                    resolved = item.resolve(strict=True)
                    resolved.relative_to(root)
                except (UnicodeError, OSError, ValueError) as exc:
                    raise RuntimeError(
                        f"{kind} link escapes or is unreadable: {relative}"
                    ) from exc
                target_after = os.readlink(item)
                final = item.lstat()
                if target_after != target_before or identity(final) != identity(metadata):
                    raise RuntimeError(
                        f"{kind} link changed while read: {relative}"
                    )
                records.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                        "target": target_before,
                    }
                )
            elif stat.S_ISDIR(metadata.st_mode):
                kept_directories.append(name)
            else:
                raise RuntimeError(
                    f"{kind} contains a special directory entry: {relative}"
                )
        directories[:] = kept_directories

        for name in sorted(files):
            item = current_path / name
            metadata = item.lstat()
            relative = _safe_git_path(
                item.relative_to(root).as_posix().encode("utf-8")
            )
            if stat.S_ISLNK(metadata.st_mode):
                target_before = os.readlink(item)
                try:
                    target_before.encode("utf-8", errors="strict")
                    resolved = item.resolve(strict=True)
                    resolved.relative_to(root)
                except (UnicodeError, OSError, ValueError) as exc:
                    raise RuntimeError(
                        f"{kind} link escapes or is unreadable: {relative}"
                    ) from exc
                target_after = os.readlink(item)
                final = item.lstat()
                if target_after != target_before or identity(final) != identity(metadata):
                    raise RuntimeError(
                        f"{kind} link changed while read: {relative}"
                    )
                records.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                        "target": target_before,
                    }
                )
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError(
                    f"{kind} contains a hardlink or special file: {relative}"
                )
            mode = "100755" if stat.S_IMODE(metadata.st_mode) & 0o111 else "100644"
            raw = _read_regular(item, mode)
            records.append(
                {
                    "path": relative,
                    "type": "file",
                    "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )

    for directory, before in directory_identities.items():
        after = directory.lstat()
        if identity(after) != before:
            raise RuntimeError(f"{kind} directory changed during traversal")
    root_after = root.lstat()
    if identity(root_after) != identity(root_before):
        raise RuntimeError(f"{kind} root changed during traversal")
    ordered = sorted(records, key=lambda item: str(item["path"]))
    paths = [str(item["path"]) for item in ordered]
    for relative in paths:
        collision = unicodedata.normalize("NFC", relative).casefold()
        if collision in collision_keys:
            raise RuntimeError(f"{kind} paths collide by case or Unicode")
        collision_keys.add(collision)
    if len(paths) != len(set(paths)):
        raise RuntimeError(f"{kind} paths are duplicated")
    regular = [item for item in ordered if item["type"] == "file"]
    links = [item for item in ordered if item["type"] == "symlink"]
    return {
        "kind": kind,
        "root": str(root),
        "path_count": len(ordered),
        "regular_file_count": len(regular),
        "symlink_count": len(links),
        "total_file_bytes": sum(int(item["bytes"]) for item in regular),
        "tree_sha256": canonical_sha256(ordered),
        "files": ordered,
    }


def _dependency_tree_inventory(
    dependency_root: Path,
    source_root: Path,
    *,
    browser_root: Path,
    browser_executable: Path,
) -> dict[str, object]:
    """Bind Node dependencies and the explicit Playwright browser runtime."""

    dependency_root = dependency_root.resolve(strict=True)
    browser_root = browser_root.resolve(strict=True)
    browser_executable = browser_executable.resolve(strict=True)
    try:
        browser_relative = browser_executable.relative_to(browser_root).as_posix()
    except ValueError as exc:
        raise RuntimeError("browser executable is outside browser runtime root") from exc
    lock = source_root / "web/frontend/package-lock.json"
    lock_metadata = lock.lstat()
    lock_mode = "100755" if stat.S_IMODE(lock_metadata.st_mode) & 0o111 else "100644"
    lock_raw = _read_regular(lock, lock_mode)
    try:
        lock_wire = json.loads(lock_raw)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("frontend dependency lock is not JSON") from exc
    if type(lock_wire) is not dict or type(lock_wire.get("packages")) is not dict:
        raise RuntimeError("frontend dependency lock packages map is invalid")
    node = _external_tree_component(dependency_root, kind="FRONTEND_NODE_MODULES")
    browser = _external_tree_component(browser_root, kind="PLAYWRIGHT_BROWSER_RUNTIME")
    executable_rows = [
        item
        for item in browser["files"]
        if item.get("path") == browser_relative and item.get("type") == "file"
    ]
    if len(executable_rows) != 1 or not os.access(browser_executable, os.X_OK):
        raise RuntimeError("browser executable is not bound by browser runtime inventory")
    browser["executable"] = {
        "relative_path": browser_relative,
        "bytes": executable_rows[0]["bytes"],
        "sha256": executable_rows[0]["sha256"],
    }
    body: dict[str, object] = {
        "schema": DEPENDENCY_INVENTORY_SCHEMA,
        "kind": "FULL_REPOSITORY_DEPENDENCIES",
        "lockfile_sha256": hashlib.sha256(lock_raw).hexdigest(),
        "node_modules": node,
        "browser_runtime": browser,
        "path_count": int(node["path_count"]) + int(browser["path_count"]),
    }
    body["inventory_sha256"] = canonical_sha256(body)
    return body


def _no_dependency_inventory() -> dict[str, object]:
    body: dict[str, object] = {
        "schema": DEPENDENCY_INVENTORY_SCHEMA,
        "kind": "NONE",
        "lockfile_sha256": None,
        "node_modules": None,
        "browser_runtime": None,
        "path_count": 0,
    }
    body["inventory_sha256"] = canonical_sha256(body)
    return body


def _within(root: Path, value: Path, label: str) -> tuple[Path, str]:
    path = value.resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"{label} must be inside audit root") from exc
    return path, relative


def _canonical_existing_directory(value: Path, label: str) -> Path:
    """Resolve no aliases when selecting a source or evidence trust root."""

    if not value.is_absolute() or value != Path(os.path.abspath(os.fspath(value))):
        raise RuntimeError(f"{label} must be canonical and absolute")
    metadata = value.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"{label} must be an ordinary directory")
    if value.resolve(strict=True) != value:
        raise RuntimeError(f"{label} must not use path aliases")
    return value


def _non_recordable_invocation(stage: str, exc: Exception) -> None:
    """Classify failures before an append-only evidence coordinate is trusted."""

    detail = " ".join(str(exc).split())[:512]
    raise RuntimeError(
        f"{NON_RECORDABLE_INVOCATION_VALIDATION}: {stage}: "
        f"{type(exc).__name__}: {detail}"
    ) from exc


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise RuntimeError(
                f"audit evidence is append-only and already exists: {path}"
            ) from exc
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("audit evidence write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _persist_inventory(
    audit_root: Path, identifier: str, body: dict[str, object], raw: bytes
) -> dict[str, object]:
    # One immutable inventory artifact per invocation keeps the evidence graph
    # one-to-one.  The body hash remains identical for source/fresh executions
    # of the same candidate, while paths cannot alias one another.
    relative = f"evidence/source_inventories/{identifier}.json"
    path = audit_root / relative
    _write_new(path, raw)
    return {
        "path": relative,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "inventory_sha256": body["inventory_sha256"],
        "path_count": body["path_count"],
    }


def _persist_dependency_inventory(
    audit_root: Path, identifier: str, body: dict[str, object]
) -> dict[str, object]:
    relative = f"evidence/dependency_inventories/{identifier}.json"
    raw = canonical_bytes(body) + b"\n"
    _write_new(audit_root / relative, raw)
    return {
        "path": relative,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "inventory_sha256": body["inventory_sha256"],
        "path_count": body["path_count"],
    }


def _tracked_blob(
    inventory: dict[str, object], relative: str
) -> dict[str, object]:
    matches = [
        item
        for item in inventory.get("files", [])
        if type(item) is dict and item.get("path") == relative
    ]
    if len(matches) != 1 or matches[0].get("type") != "blob":
        raise RuntimeError(f"executed source does not bind {relative}")
    return matches[0]


def _prepare_trusted_pytest_reporter(
    *, audit_root: Path, cwd: Path, identifier: str,
    inventory: dict[str, object],
) -> tuple[Path, str, dict[str, object]]:
    """Copy the bound reporter outside the candidate import/config namespace."""

    source_path = cwd / TRUSTED_REPORTER_PATH
    source_raw = _read_regular(source_path, "100644")
    tracked = _tracked_blob(inventory, TRUSTED_REPORTER_PATH)
    if (
        tracked.get("bytes") != len(source_raw)
        or tracked.get("sha256") != hashlib.sha256(source_raw).hexdigest()
    ):
        raise RuntimeError("trusted pytest reporter differs from executed inventory")
    runtime_dir = audit_root / "runtime" / f"{identifier}-trusted-reporter"
    if runtime_dir.exists() or runtime_dir.is_symlink():
        raise RuntimeError("trusted pytest reporter runtime is append-only")
    runtime_dir.mkdir(parents=True)
    runtime_path = runtime_dir / "phase9_trusted_reporter.py"
    _write_new(runtime_path, source_raw)
    nonce = secrets.token_hex(16)
    runtime_site_packages = (
        Path(sys.executable).parent.parent
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    ).resolve(strict=True)
    if not (runtime_site_packages / "pytest/__init__.py").is_file():
        raise RuntimeError("trusted pytest runtime is unavailable")
    descriptor = {
        "schema": TRUSTED_PYTEST_EVENT_SCHEMA,
        "module": "phase9_trusted_reporter",
        "source_path": TRUSTED_REPORTER_PATH,
        "source_bytes": len(source_raw),
        "source_sha256": hashlib.sha256(source_raw).hexdigest(),
        "runtime_path": str(runtime_path),
        "runtime_site_packages": str(runtime_site_packages),
        "nonce": nonce,
        "event_transport": "PARENT_CAPTURED_ANONYMOUS_PIPE",
        "candidate_conftest": "DISABLED",
        "candidate_pytest_config": "DISABLED",
    }
    return runtime_dir, nonce, descriptor


def _isolated_pytest_command(
    command: list[str], *, cwd: Path, reporter_path: Path,
    runtime_site_packages: Path,
) -> list[str]:
    prefix = command[:7]
    if prefix != [
        command[0], "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"
    ]:
        raise RuntimeError("pytest command cannot be isolated")
    return [
        command[0], "-I", "-S", "-B", str(reporter_path),
        "--runtime-site-packages", str(runtime_site_packages),
        "--source-root", str(cwd), "--", "-q", "-p", "no:cacheprovider",
        "--noconftest",
        "-c", "/dev/null",
        "--rootdir", str(cwd),
        "-o", "addopts=",
        *command[7:],
    ]


def _persist_trusted_events(
    audit_root: Path, identifier: str, raw: bytes
) -> dict[str, object]:
    relative = f"evidence/pytest_events/{identifier}.jsonl"
    _write_new(audit_root / relative, raw)
    return {
        "path": relative,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _persist_composite_events(
    audit_root: Path, identifier: str, raw: bytes
) -> dict[str, object]:
    relative = f"evidence/composite_events/{identifier}.jsonl"
    _write_new(audit_root / relative, raw)
    return {
        "path": relative,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _command_executable(command: list[str]) -> dict[str, object]:
    raw = Path(command[0])
    if not raw.is_absolute():
        raise RuntimeError("audit command executable must be an absolute path")
    launcher = Path(os.path.abspath(os.fspath(raw)))
    trusted_launcher = Path(os.path.abspath(sys.executable))
    if launcher != trusted_launcher:
        raise RuntimeError(
            "audit command must use the exact Python launcher executing the trusted runner"
        )
    path = raw.resolve(strict=True)
    trusted_python = trusted_launcher.resolve(strict=True)
    if path != trusted_python:
        raise RuntimeError("audit Python binary identity differs")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise RuntimeError("audit command executable must be an executable regular file")
    content = _read_regular(path, "100755")
    return {
        "path": str(launcher),
        "resolved_path": str(path),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _producer_descriptor() -> dict[str, object]:
    raw = Path(__file__).read_bytes()
    return {
        "type": "PAPER_FACTORY_AUDIT_RUNNER",
        "version": COMMAND_SCHEMA,
        "path": "tools/run_audit_command.py",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _persist_preflight_failure(
    *,
    identifier: str,
    suite: str,
    kind: str,
    execution_environment: str,
    repository: Path,
    cwd: Path,
    audit_root: Path,
    record_path: Path,
    log_path: Path,
    log_relative: str,
    requested_command: list[str],
    executable: dict[str, object] | None,
    before_inventory: dict[str, object],
    before_raw: bytes,
    failure_stage: str,
    error_type: str,
    started_at: str,
    started_ns: int,
) -> int:
    """Persist a process-not-started failure without fabricating stage evidence."""

    try:
        after_inventory, after_raw = executed_source_inventory(
            repository, cwd, execution_environment=execution_environment
        )
        source_stable = after_raw == before_raw and after_inventory == before_inventory
        source_postcheck = "MATCH" if source_stable else "DIFFERS"
    except (OSError, RuntimeError, subprocess.SubprocessError):
        source_stable = False
        source_postcheck = "ERROR"
    failure_event = {
        "schema": PREFLIGHT_FAILURE_SCHEMA,
        "event": "preflight_failure",
        "failure_stage": failure_stage,
        "error_type": error_type,
        "process_started": False,
        "runner_exit_code": PREFLIGHT_FAILURE_EXIT_CODE,
    }
    raw_log = canonical_bytes(failure_event) + b"\n"
    completed_at = _utc()
    duration_ms = (time.monotonic_ns() - started_ns) // 1_000_000
    inventory_record = _persist_inventory(
        audit_root, identifier, before_inventory, before_raw
    )
    _write_new(log_path, raw_log)
    record: dict[str, object] = {
        "schema": PREFLIGHT_FAILURE_SCHEMA,
        "id": identifier,
        "suite": suite,
        "kind": kind,
        "execution_environment": execution_environment,
        "attempt_kind": "preflight_failed",
        "producer": _producer_descriptor(),
        "candidate": before_inventory["candidate"],
        "source_inventory": inventory_record,
        "source_stable": source_stable,
        "source_postcheck": source_postcheck,
        "source_identity_repository": str(repository),
        "requested_command_argv": requested_command,
        "command_executable": executable,
        "cwd": str(cwd),
        "audit_root": str(audit_root),
        "python_executable": executable,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_milliseconds": duration_ms,
        "process_started": False,
        "exit_code": None,
        "runner_exit_code": PREFLIGHT_FAILURE_EXIT_CODE,
        "failure_stage": failure_stage,
        "error_type": error_type,
        "raw_log": {
            "path": log_relative,
            "bytes": len(raw_log),
            "sha256": hashlib.sha256(raw_log).hexdigest(),
        },
    }
    record["record_sha256"] = canonical_sha256(record)
    _write_new(record_path, canonical_bytes(record) + b"\n")
    return PREFLIGHT_FAILURE_EXIT_CODE


def _validate_command_shape(
    command: list[str], *, suite: str, cwd: Path, audit_root: Path,
    identifier: str,
) -> tuple[Path | None, Path | None, Path | None, Path | None, Path | None, Path]:
    """Allow only the two recorded, non-shrinking offline test command forms."""

    python = command[0]
    if suite == "full_repository":
        if len(command) != 19 or command[:4] != [
            python,
            "-B",
            "tools/run_full_repo_with_frontend_deps.py",
            "--source-root",
        ]:
            raise RuntimeError("full-repository audit command shape differs")
        if command[4] != str(cwd) or command[5] != "--dependency-target":
            raise RuntimeError("full-repository source binding differs")
        dependency = Path(command[6])
        browser_root = Path(command[8])
        browser_executable = Path(command[10])
        node = Path(command[12])
        npm = Path(command[14])
        if (
            not dependency.is_absolute()
            or dependency != Path(os.path.abspath(os.fspath(dependency)))
            or dependency.resolve(strict=True) != dependency
            or not dependency.resolve(strict=True).is_dir()
            or command[7] != "--browser-root"
            or not browser_root.is_absolute()
            or browser_root != Path(os.path.abspath(os.fspath(browser_root)))
            or browser_root.resolve(strict=True) != browser_root
            or not browser_root.is_dir()
            or command[9] != "--browser-executable"
            or not browser_executable.is_absolute()
            or browser_executable != Path(
                os.path.abspath(os.fspath(browser_executable))
            )
            or browser_executable.resolve(strict=True) != browser_executable
            or not browser_executable.is_file()
            or command[11] != "--node"
            or not node.is_absolute()
            or node != Path(os.path.abspath(os.fspath(node)))
            or not node.resolve(strict=True).is_file()
            or command[13] != "--npm"
            or not npm.is_absolute()
            or npm != Path(os.path.abspath(os.fspath(npm)))
            or not npm.resolve(strict=True).is_file()
            or command[15:18] != ["--python", python, "--basetemp"]
        ):
            raise RuntimeError("full-repository runtime/dependency binding differs")
        try:
            browser_executable.relative_to(browser_root)
        except ValueError as exc:
            raise RuntimeError("full-repository browser binding differs") from exc
        raw_basetemp = Path(command[18])
        basetemp = raw_basetemp.resolve()
    else:
        prefix = [python, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if command[: len(prefix)] != prefix or len(command) <= len(prefix) + 1:
            raise RuntimeError("pytest audit command shape differs")
        option = command[len(prefix)]
        if not option.startswith("--basetemp="):
            raise RuntimeError("pytest audit command requires an isolated basetemp")
        raw_basetemp = Path(option.split("=", 1)[1])
        basetemp = raw_basetemp.resolve()
        targets = command[len(prefix) + 1 :]
        if any(
            target.startswith("-")
            or "::" in target
            or not target.startswith("tests/")
            or not target.endswith(".py")
            or not (cwd / target).is_file()
            for target in targets
        ):
            raise RuntimeError("pytest audit targets must be complete test modules")
        if len(targets) != len(set(targets)):
            raise RuntimeError("pytest audit targets are duplicated")
    try:
        basetemp.relative_to(audit_root)
    except ValueError as exc:
        raise RuntimeError("audit command basetemp must be inside the audit root") from exc
    if (
        not raw_basetemp.is_absolute()
        or raw_basetemp != Path(os.path.abspath(os.fspath(raw_basetemp)))
        or basetemp != raw_basetemp
    ):
        raise RuntimeError("audit command basetemp must be a canonical absolute path")
    expected_basetemp = (
        audit_root / "runtime" / f"{identifier}-pytest" / "basetemp"
    )
    if basetemp != expected_basetemp:
        raise RuntimeError("audit command basetemp must use its dedicated mount parent")
    if suite == "full_repository":
        return (
            dependency.resolve(strict=True),
            browser_root.resolve(strict=True),
            browser_executable,
            node,
            npm,
            basetemp,
        )
    return None, None, None, None, None, basetemp


def _strict_child_path_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _sandbox_descriptor_and_argv(
    *,
    command: list[str],
    cwd: Path,
    audit_root: Path,
    child_env: dict[str, str],
    dependency_root: Path | None,
    browser_root: Path | None,
    basetemp: Path,
    identifier: str,
    event_write_fd: int,
    composite_write_fd: int | None,
) -> tuple[dict[str, object], list[str]]:
    """Build a mandatory networkless, read-only-source bubblewrap invocation."""

    sandbox_path = Path("/usr/bin/bwrap")
    if not sandbox_path.exists() or sandbox_path.is_symlink():
        raise RuntimeError("trusted bubblewrap sandbox is unavailable")
    sandbox_raw = _read_regular(sandbox_path, "100755")
    sandbox_binary = {
        "path": str(sandbox_path),
        "bytes": len(sandbox_raw),
        "sha256": hashlib.sha256(sandbox_raw).hexdigest(),
    }
    if _strict_child_path_overlap(cwd, audit_root):
        raise RuntimeError("execution source and writable audit root must be disjoint")

    launcher = Path(os.path.abspath(command[0]))
    environment_root = launcher.parent.parent
    environment_config = environment_root / "pyvenv.cfg"
    if (
        launcher.parent.name != "bin"
        or not environment_config.is_file()
        or environment_root.is_symlink()
    ):
        raise RuntimeError("audit Python must belong to a concrete virtual environment")
    host_project_root = environment_root.parent.resolve(strict=True)
    if _strict_child_path_overlap(host_project_root, cwd) or _strict_child_path_overlap(
        host_project_root, audit_root
    ):
        raise RuntimeError("Python host project overlaps candidate or audit root")

    external_git_mount: Path | None = None
    if (cwd / ".git").exists() or (cwd / ".git").is_symlink():
        common_raw = _git(cwd, "rev-parse", "--git-common-dir").decode(
            "utf-8", errors="strict"
        ).strip()
        common_path = Path(common_raw)
        if not common_path.is_absolute():
            common_path = cwd / common_path
        git_common = common_path.resolve(strict=True)
        common_metadata = git_common.lstat()
        if not stat.S_ISDIR(common_metadata.st_mode) or git_common.is_symlink():
            raise RuntimeError("candidate Git common directory is not ordinary")
        try:
            git_common.relative_to(cwd)
        except ValueError:
            external_git_mount = git_common

    pytest_parent = basetemp.parent
    if basetemp.name != "basetemp":
        raise RuntimeError("pytest basetemp leaf differs")
    state_root = audit_root / "runtime" / f"{identifier}-source-state"
    state_mounts = [
        ("SOURCE_ONGOING", state_root / "ongoing", cwd / "ongoing"),
        ("SOURCE_RUN_STATE", state_root / "run_state", cwd / "run_state"),
        ("SOURCE_LOGS", state_root / "logs", cwd / "logs"),
        ("SOURCE_PAPERS", state_root / "papers", cwd / "papers"),
    ]
    if any(target.exists() or target.is_symlink() for _label, _source, target in state_mounts):
        raise RuntimeError("candidate source contains a runtime-state path")
    writable = [
        ("HOME", Path(child_env["HOME"])),
        ("XDG_CACHE_HOME", Path(child_env["XDG_CACHE_HOME"])),
        ("TMPDIR", Path(child_env["TMPDIR"])),
        # Pytest removes and recreates --basetemp.  Mount its private parent,
        # leaving the requested leaf absent, so that normal lifecycle remains
        # possible without granting write access to the audit evidence root.
        ("PYTEST_BASETEMP_PARENT", pytest_parent),
        *((label, source) for label, source, _target in state_mounts),
    ]
    for _label, path in writable:
        if path.exists() or path.is_symlink():
            raise RuntimeError("sandbox writable directory must be a new empty path")
        path.mkdir(parents=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("sandbox writable mount is not an ordinary directory")
    for index, (_label, path) in enumerate(writable):
        if any(
            _strict_child_path_overlap(path, other)
            for _other_label, other in writable[index + 1 :]
        ):
            raise RuntimeError("sandbox writable mounts overlap")

    source_overlay = audit_root / "runtime" / f"{identifier}-source-overlay"
    if source_overlay.exists() or source_overlay.is_symlink():
        raise RuntimeError("audit source overlay is append-only")
    for _label, _source, target in state_mounts:
        (source_overlay / target.name).mkdir(parents=True)

    sandbox_argv = [
        str(sandbox_path),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
        # Mount private /tmp first so it cannot hide later candidate/evidence
        # mounts when those paths also live below /tmp.
        "--bind",
        str(Path(child_env["TMPDIR"])),
        "/tmp",
        "--ro-bind",
        str(audit_root),
        str(audit_root),
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        str(cwd),
        str(cwd),
        "--ro-bind",
        str(source_overlay),
        str(source_overlay),
        "--overlay-src",
        str(cwd),
        "--overlay-src",
        str(source_overlay),
        "--ro-overlay",
        str(cwd),
        # The Python environment lives below the main checkout.  Mask the rest
        # of that checkout so an editable install cannot import a different
        # candidate, then expose only the immutable environment itself.
        "--tmpfs",
        str(host_project_root),
        "--ro-bind",
        str(environment_root),
        str(environment_root),
    ]
    if external_git_mount is not None:
        sandbox_argv.extend(
            ["--ro-bind", str(external_git_mount), str(external_git_mount)]
        )
    if dependency_root is not None:
        # Composite runtimes may be explicitly supplied outside the source and
        # dependency trees, including below the masked system /tmp directory.
        for option in ("--node", "--npm"):
            executable = command[command.index(option) + 1]
            sandbox_argv.extend(["--ro-bind", executable, executable])
    for _label, path in writable:
        sandbox_argv.extend(["--bind", str(path), str(path)])
    for _label, source, target in state_mounts:
        sandbox_argv.extend(["--bind", str(source), str(target)])
    overlay_path: Path | None = None
    if dependency_root is not None:
        frontend = cwd / "web/frontend"
        if not frontend.is_dir() or frontend.is_symlink():
            raise RuntimeError("candidate frontend root is unavailable")
        overlay_path = audit_root / "runtime" / f"{identifier}-frontend-overlay"
        if overlay_path.exists() or overlay_path.is_symlink():
            raise RuntimeError("audit sandbox overlay is append-only")
        overlay_path.mkdir(parents=True)
        (overlay_path / "node_modules").symlink_to(
            dependency_root, target_is_directory=True
        )
        sandbox_argv.extend(
            [
                "--ro-bind",
                str(dependency_root),
                str(dependency_root),
                "--ro-bind",
                str(overlay_path),
                str(overlay_path),
                "--overlay-src",
                str(frontend),
                "--overlay-src",
                str(overlay_path),
                "--ro-overlay",
                str(frontend),
            ]
        )
    if browser_root is not None:
        sandbox_argv.extend(
            ["--ro-bind", str(browser_root), str(browser_root)]
        )
    sandbox_argv.extend(["--chdir", str(cwd), "--clearenv"])
    for name, value in sorted(child_env.items()):
        sandbox_argv.extend(["--setenv", name, value])
    sandbox_argv.extend(["--", *command])
    body: dict[str, object] = {
        "schema": SANDBOX_SCHEMA,
        "policy": "READ_ONLY_SOURCE_AND_DEPENDENCIES_NETWORKLESS",
        "binary": sandbox_binary,
        "source_mount": {"path": str(cwd), "access": "READ_ONLY"},
        "source_runtime_overlay": str(source_overlay),
        "source_runtime_mounts": [
            {
                "purpose": label,
                "source": str(source),
                "target": str(target),
                "access": "READ_WRITE_PRIVATE",
            }
            for label, source, target in state_mounts
        ],
        "audit_mount": {"path": str(audit_root), "access": "READ_ONLY"},
        "writable_mounts": [
            {"purpose": label, "path": str(path), "access": "READ_WRITE"}
            for label, path in writable
        ],
        "python_environment_mount": {
            "path": str(environment_root),
            "access": "READ_ONLY",
        },
        "masked_host_project": str(host_project_root),
        "git_object_mount": (
            None
            if external_git_mount is None
            else {"path": str(external_git_mount), "access": "READ_ONLY"}
        ),
        "system_tmp_mount": {
            "source": child_env["TMPDIR"],
            "target": "/tmp",
            "access": "READ_WRITE_PRIVATE",
        },
        "dependency_mount": (
            None
            if dependency_root is None
            else {"path": str(dependency_root), "access": "READ_ONLY"}
        ),
        "browser_runtime_mount": (
            None
            if browser_root is None
            else {"path": str(browser_root), "access": "READ_ONLY"}
        ),
        "frontend_overlay": None if overlay_path is None else str(overlay_path),
        "network_namespace": "UNSHARED",
        "event_transport": {
            "kind": "PARENT_CAPTURED_ANONYMOUS_PIPE",
            "write_fd": event_write_fd,
            "tested_process_access": "WRITE_ONLY_APPEND_STREAM",
        },
        "composite_event_transport": (
            None
            if composite_write_fd is None
            else {
                "kind": "PARENT_CAPTURED_ANONYMOUS_PIPE",
                "write_fd": composite_write_fd,
                "tested_process_access": "WRITE_ONLY_APPEND_STREAM",
            }
        ),
        "launcher_argv": sandbox_argv,
    }
    body["sandbox_sha256"] = canonical_sha256(body)
    return body, sandbox_argv


def _sanitized_environment(
    audit_root: Path, source_root: Path, repository: Path, *, identifier: str,
    reporter_root: Path, event_fd: int, reporter_nonce: str,
    runtime_site_packages: Path,
    composite_fd: int | None = None,
    composite_nonce: str | None = None,
) -> tuple[dict[str, str], dict[str, object]]:
    writable_root = audit_root / "runtime" / f"{identifier}-writable"
    home = writable_root / "home"
    cache = writable_root / "cache"
    temporary = writable_root / "tmp"
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(temporary),
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PHASE9_TRUSTED_PYTEST_REPORTER_PATH": str(
            reporter_root / "phase9_trusted_reporter.py"
        ),
        "PHASE9_TRUSTED_PYTEST_SITE_PACKAGES": str(runtime_site_packages),
        "PHASE9_TRUSTED_PYTEST_EVENT_PATH": "PARENT_CAPTURED_ANONYMOUS_PIPE",
        "PHASE9_TRUSTED_PYTEST_EVENT_FD": str(event_fd),
        "PHASE9_TRUSTED_PYTEST_NONCE": reporter_nonce,
        # This test-layer-only coordinate lets no-.git archive tests validate the
        # immutable candidate identity.  Production modules never consume it.
        "PHASE9_TEST_SOURCE_REPOSITORY": str(repository),
    }
    if composite_fd is not None:
        if composite_nonce is None:
            raise RuntimeError("composite event nonce is absent")
        environment.update(
            {
                "PHASE9_TRUSTED_COMPOSITE_EVENT_PATH": (
                    "PARENT_CAPTURED_ANONYMOUS_PIPE"
                ),
                "PHASE9_TRUSTED_COMPOSITE_EVENT_FD": str(composite_fd),
                "PHASE9_TRUSTED_COMPOSITE_NONCE": composite_nonce,
            }
        )
    removed = sorted(name for name in os.environ if _SENSITIVE_ENV.search(name))
    descriptor: dict[str, object] = {
        "policy": "paper-factory-sanitized-audit-environment-v1",
        "inherited": False,
        "variables": environment,
        "removed_host_variable_names": removed,
    }
    descriptor["environment_sha256"] = canonical_sha256(descriptor)
    return environment, descriptor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--environment", required=True, choices=("source", "fresh"))
    parser.add_argument("--kind", required=True, choices=("pytest", "composite"))
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--audit-root", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if _SAFE_ID.fullmatch(args.id) is None or _SAFE_ID.fullmatch(args.suite) is None:
        parser.error("id and suite must use lowercase safe identifiers")
    if not args.id.startswith(f"{args.environment}_{args.suite}"):
        parser.error("id must bind its environment and suite")
    requested_command = list(args.command)
    if requested_command and requested_command[0] == "--":
        requested_command.pop(0)
    if not requested_command:
        parser.error("a command is required after --")
    expected_record = f"command_records/{args.id}.json"
    expected_log = f"test_logs/{args.id}.log"
    try:
        repository = _canonical_existing_directory(args.repository, "repository")
        audit_root = _canonical_existing_directory(args.audit_root, "audit root")
        cwd = _canonical_existing_directory(args.cwd, "execution cwd")
        log_path = audit_root / expected_log
        record_path = audit_root / expected_record
        if args.log != log_path:
            raise RuntimeError("raw log path must be canonical for its id")
        if args.record != record_path:
            raise RuntimeError("command record path must be canonical for its id")
        for parent, label in (
            (log_path.parent, "raw log parent"),
            (record_path.parent, "command record parent"),
        ):
            if parent.exists() or parent.is_symlink():
                metadata = parent.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(
                    metadata.st_mode
                ):
                    raise RuntimeError(f"{label} is not an ordinary directory")
        if (
            log_path.exists()
            or log_path.is_symlink()
            or record_path.exists()
            or record_path.is_symlink()
        ):
            raise RuntimeError(
                "command records and logs are append-only; choose new paths"
            )
        before_inventory, before_raw = executed_source_inventory(
            repository, cwd, execution_environment=args.environment
        )
    except Exception as exc:
        _non_recordable_invocation("TRUST_COORDINATE_OR_SOURCE", exc)
    log_relative = expected_log
    preflight_started_at = _utc()
    preflight_started_ns = time.monotonic_ns()
    executable: dict[str, object] | None = None
    event_read_fd: int | None = None
    event_write_fd: int | None = None
    composite_read_fd: int | None = None
    composite_write_fd: int | None = None

    def preflight_failure(exc: Exception, stage: str) -> int:
        for descriptor in {
            event_read_fd,
            event_write_fd,
            composite_read_fd,
            composite_write_fd,
        }:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return _persist_preflight_failure(
            identifier=args.id,
            suite=args.suite,
            kind=args.kind,
            execution_environment=args.environment,
            repository=repository,
            cwd=cwd,
            audit_root=audit_root,
            record_path=record_path,
            log_path=log_path,
            log_relative=log_relative,
            requested_command=requested_command,
            executable=executable,
            before_inventory=before_inventory,
            before_raw=before_raw,
            failure_stage=stage,
            error_type=type(exc).__name__,
            started_at=preflight_started_at,
            started_ns=preflight_started_ns,
        )

    expected_kind = "composite" if args.suite == "full_repository" else "pytest"
    if args.kind != expected_kind:
        return preflight_failure(
            RuntimeError("formal audit command kind differs from its suite"),
            "SUITE_KIND",
        )

    try:
        executable = _command_executable(requested_command)
    except Exception as exc:
        return preflight_failure(exc, "COMMAND_EXECUTABLE")
    try:
        (
            dependency_root,
            browser_root,
            browser_executable,
            node_executable,
            npm_executable,
            basetemp,
        ) = _validate_command_shape(
            requested_command, suite=args.suite, cwd=cwd, audit_root=audit_root,
            identifier=args.id,
        )
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return preflight_failure(exc, "COMMAND_SHAPE")
    try:
        dependency_before = (
            _no_dependency_inventory()
            if dependency_root is None
            else _dependency_tree_inventory(
                dependency_root,
                cwd,
                browser_root=browser_root,
                browser_executable=browser_executable,
            )
        )
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        return preflight_failure(exc, "DEPENDENCY_INVENTORY")
    try:
        reporter_root, reporter_nonce, trusted_reporter = (
            _prepare_trusted_pytest_reporter(
                audit_root=audit_root,
                cwd=cwd,
                identifier=args.id,
                inventory=before_inventory,
            )
        )
    except Exception as exc:
        return preflight_failure(exc, "TRUSTED_REPORTER_PREPARATION")
    try:
        test_source_repository = _prepare_test_source_repository(
            repository, audit_root, args.id, before_inventory
        )
    except Exception as exc:
        return preflight_failure(exc, "TEST_SOURCE_PREPARATION")
    try:
        event_read_fd, event_write_fd = os.pipe()
        if args.suite == "full_repository":
            composite_read_fd, composite_write_fd = os.pipe()
            composite_nonce = secrets.token_hex(16)
        else:
            composite_nonce = None
    except Exception as exc:
        return preflight_failure(exc, "EVENT_PIPE_PREPARATION")
    try:
        assert event_write_fd is not None
        child_env, environment_record = _sanitized_environment(
            audit_root,
            cwd,
            test_source_repository,
            identifier=args.id,
            reporter_root=reporter_root,
            event_fd=event_write_fd,
            reporter_nonce=reporter_nonce,
            runtime_site_packages=Path(
                str(trusted_reporter["runtime_site_packages"])
            ),
            composite_fd=composite_write_fd,
            composite_nonce=composite_nonce,
        )
    except Exception as exc:
        return preflight_failure(exc, "ENVIRONMENT_PREPARATION")
    try:
        command = (
            [requested_command[0], "-I", "-S", *requested_command[1:]]
            if args.suite == "full_repository"
            else _isolated_pytest_command(
                requested_command,
                cwd=cwd,
                reporter_path=reporter_root / "phase9_trusted_reporter.py",
                runtime_site_packages=Path(
                    str(trusted_reporter["runtime_site_packages"])
                ),
            )
        )
    except Exception as exc:
        return preflight_failure(exc, "COMMAND_PREPARATION")
    try:
        assert event_write_fd is not None
        execution_sandbox, sandbox_argv = _sandbox_descriptor_and_argv(
            command=command,
            cwd=cwd,
            audit_root=audit_root,
            child_env=child_env,
            dependency_root=dependency_root,
            browser_root=browser_root,
            basetemp=basetemp,
            identifier=args.id,
            event_write_fd=event_write_fd,
            composite_write_fd=composite_write_fd,
        )
    except Exception as exc:
        return preflight_failure(exc, "SANDBOX_PREPARATION")
    producer = _producer_descriptor()
    inventory_record = _persist_inventory(
        audit_root, args.id, before_inventory, before_raw
    )
    dependency_inventory_record = _persist_dependency_inventory(
        audit_root, args.id, dependency_before
    )

    started = _utc()
    before = time.monotonic_ns()
    chunks: list[bytes] = []
    event_chunks: list[bytes] = []
    composite_chunks: list[bytes] = []
    event_capture_error: str | None = None
    composite_capture_error: str | None = None

    def capture_events() -> None:
        nonlocal event_capture_error
        try:
            while True:
                chunk = os.read(event_read_fd, 65536)
                if not chunk:
                    break
                event_chunks.append(chunk)
        except OSError:
            event_capture_error = "EVENT_PIPE_READ_ERROR"
        finally:
            os.close(event_read_fd)

    def capture_composite_events() -> None:
        nonlocal composite_capture_error
        assert composite_read_fd is not None
        try:
            while True:
                chunk = os.read(composite_read_fd, 65536)
                if not chunk:
                    break
                composite_chunks.append(chunk)
        except OSError:
            composite_capture_error = "COMPOSITE_EVENT_PIPE_READ_ERROR"
        finally:
            os.close(composite_read_fd)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("xb") as output:
        try:
            process = subprocess.Popen(
                sandbox_argv,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=tuple(
                    descriptor
                    for descriptor in (event_write_fd, composite_write_fd)
                    if descriptor is not None
                ),
            )
        except OSError as exc:
            os.close(event_write_fd)
            os.close(event_read_fd)
            if composite_write_fd is not None:
                os.close(composite_write_fd)
            if composite_read_fd is not None:
                os.close(composite_read_fd)
            event_capture_error = "EVENT_PIPE_PROCESS_START_ERROR"
            if composite_read_fd is not None:
                composite_capture_error = "COMPOSITE_EVENT_PIPE_PROCESS_START_ERROR"
            chunk = (
                f"[audit-runner] command preparation failed: {type(exc).__name__}\n"
            ).encode("utf-8")
            output.write(chunk)
            chunks.append(chunk)
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            process_exit_code = 127
        else:
            # The tested process inherits only the write end.  Drain the read
            # end concurrently so a large suite cannot block on pipe capacity.
            # Candidate code cannot truncate or replace bytes already observed
            # by this parent process.
            os.close(event_write_fd)
            if composite_write_fd is not None:
                os.close(composite_write_fd)
            event_reader = threading.Thread(
                target=capture_events,
                name=f"phase9-events-{args.id}",
                daemon=False,
            )
            event_reader.start()
            composite_reader = None
            if composite_read_fd is not None:
                composite_reader = threading.Thread(
                    target=capture_composite_events,
                    name=f"phase9-composite-events-{args.id}",
                    daemon=False,
                )
                composite_reader.start()
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read1(65536)
                if not chunk:
                    break
                output.write(chunk)
                output.flush()
                chunks.append(chunk)
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            process_exit_code = process.wait()
            event_reader.join()
            if composite_reader is not None:
                composite_reader.join()
        os.fsync(output.fileno())
    duration_ms = (time.monotonic_ns() - before) // 1_000_000
    ended = _utc()
    raw_log = b"".join(chunks)
    event_raw = b"".join(event_chunks)
    if event_capture_error is not None or not event_raw:
        event_raw = canonical_bytes(
            {
                "schema": "paper-factory-trusted-pytest-capture-error-v1",
                "error": event_capture_error or "EVENT_STREAM_UNAVAILABLE",
            }
        ) + b"\n"
    trusted_event_record = _persist_trusted_events(
        audit_root, args.id, event_raw
    )
    composite_result: dict[str, object] | None = None
    composite_error: str | None = None
    composite_descriptor: dict[str, object] | None = None
    pytest_log = raw_log
    if args.suite == "full_repository":
        composite_raw = b"".join(composite_chunks)
        if composite_capture_error is not None or not composite_raw:
            composite_raw = canonical_bytes(
                {
                    "schema": "paper-factory-trusted-composite-capture-error-v1",
                    "error": composite_capture_error or "COMPOSITE_EVENT_STREAM_UNAVAILABLE",
                }
            ) + b"\n"
        composite_artifact = _persist_composite_events(
            audit_root, args.id, composite_raw
        )
        try:
            assert composite_nonce is not None
            assert dependency_root is not None
            assert browser_root is not None
            assert browser_executable is not None
            assert node_executable is not None
            assert npm_executable is not None
            composite_result = validate_composite_events(
                composite_raw,
                raw_log=raw_log,
                nonce=composite_nonce,
                source=cwd,
                dependency=dependency_root,
                browser_root=browser_root,
                browser_executable=browser_executable,
                python=Path(requested_command[0]),
                node=node_executable,
                npm=npm_executable,
                basetemp=basetemp,
                environment=child_env,
                source_inventory=before_inventory,
                dependency_inventory=dependency_before,
                expected_stage_contract=composite_stage_contract(),
                runtime_validation=True,
            )
            pytest_log = composite_result["python_log"]
        except (AssertionError, OSError, RuntimeError, UnicodeError, ValueError) as exc:
            composite_error = type(exc).__name__
        composite_descriptor = {
            "schema": COMPOSITE_EVENT_SCHEMA,
            "nonce": composite_nonce,
            "event_transport": COMPOSITE_EVENT_TRANSPORT,
            "validation": "PASS" if composite_error is None else "NONPASS",
            "validation_error": composite_error,
            "stage_results": (
                None if composite_result is None else composite_result["stage_results"]
            ),
            "browser_test_summary": (
                None
                if composite_result is None
                else composite_result["browser_test_summary"]
            ),
            "browser_node_outcomes": (
                None
                if composite_result is None
                else composite_result["browser_node_outcomes"]
            ),
            "browser_complete_pass": (
                None
                if composite_result is None
                else composite_result["browser_complete_pass"]
            ),
            "build_output": (
                None if composite_result is None else composite_result["build_output"]
            ),
            "contract_sha256": (
                None if composite_result is None else composite_result["contract_sha256"]
            ),
            "event_artifact": composite_artifact,
        }
    expected_targets = (
        () if args.suite == "full_repository" else tuple(requested_command[8:])
    )
    trusted_result: dict[str, object] | None = None
    trusted_error: str | None = None
    try:
        trusted_result = validate_trusted_pytest_events(
            event_raw,
            nonce=reporter_nonce,
            expected_rootdir=str(cwd),
            expected_targets=expected_targets,
            full_repository=args.suite == "full_repository",
            expected_transport="PARENT_CAPTURED_ANONYMOUS_PIPE",
        )
    except (UnicodeError, ValueError) as exc:
        trusted_error = type(exc).__name__
    try:
        _, after_raw = executed_source_inventory(
            repository, cwd, execution_environment=args.environment
        )
        source_stable = after_raw == before_raw
        source_postcheck = "MATCH" if source_stable else "DIFFERS"
    except (OSError, RuntimeError, subprocess.SubprocessError):
        source_stable = False
        source_postcheck = "ERROR"
    runner_exit_code = process_exit_code
    if not source_stable and runner_exit_code == 0:
        runner_exit_code = 86
    try:
        dependency_after = (
            _no_dependency_inventory()
            if dependency_root is None
            else _dependency_tree_inventory(
                dependency_root,
                cwd,
                browser_root=browser_root,
                browser_executable=browser_executable,
            )
        )
        dependency_stable = dependency_after == dependency_before
        dependency_postcheck = "MATCH" if dependency_stable else "DIFFERS"
    except (OSError, RuntimeError, ValueError):
        dependency_stable = False
        dependency_postcheck = "ERROR"
    if not dependency_stable and runner_exit_code == 0:
        runner_exit_code = 86
    outcomes = parse_outcomes(pytest_log, "pytest")
    trusted_counts = None if trusted_result is None else trusted_result["counts"]
    complete_pass = (
        process_exit_code == 0
        and composite_error is None
        and (
            composite_result is None
            or composite_result["browser_complete_pass"] is True
        )
        and trusted_error is None
        and trusted_counts == outcomes
        and outcomes["collected"] > 0
        and outcomes["passed"] == outcomes["collected"]
        and not any(outcomes[name] for name in (*_OUTCOMES[1:],))
        and source_stable
        and dependency_stable
    )
    if not complete_pass and runner_exit_code == 0:
        runner_exit_code = 87
    record: dict[str, object] = {
        "schema": COMMAND_SCHEMA,
        "id": args.id,
        "suite": args.suite,
        "kind": args.kind,
        "execution_environment": args.environment,
        "attempt_kind": "final" if complete_pass else "failed",
        "producer": producer,
        "candidate": before_inventory["candidate"],
        "source_inventory": inventory_record,
        "source_stable": source_stable,
        "source_postcheck": source_postcheck,
        "dependency_inventory": dependency_inventory_record,
        "dependency_stable": dependency_stable,
        "dependency_postcheck": dependency_postcheck,
        "execution_sandbox": execution_sandbox,
        "requested_command_argv": requested_command,
        "command_argv": command,
        "command_executable": executable,
        "cwd": str(cwd),
        "audit_root": str(audit_root),
        "python_executable": executable,
        "environment": environment_record,
        "started_at": started,
        "completed_at": ended,
        "duration_milliseconds": duration_ms,
        "exit_code": process_exit_code,
        "runner_exit_code": runner_exit_code,
        "outcome_parser": "paper-factory-composite-and-trusted-events-v5",
        "outcomes": outcomes,
        "composite_suite": composite_descriptor,
        "trusted_pytest": {
            **trusted_reporter,
            "validation": "PASS" if trusted_error is None else "NONPASS",
            "validation_error": trusted_error,
            "outcomes": trusted_counts,
            "node_outcomes": (
                None
                if trusted_result is None
                else trusted_result["node_outcomes"]
            ),
            "event_artifact": trusted_event_record,
        },
        "raw_log": {
            "path": log_relative,
            "bytes": len(raw_log),
            "sha256": hashlib.sha256(raw_log).hexdigest(),
        },
    }
    record["record_sha256"] = canonical_sha256(record)
    _write_new(record_path, canonical_bytes(record) + b"\n")
    return runner_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
