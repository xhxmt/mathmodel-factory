#!/usr/bin/env python3
"""Validate a Phase9-PREP manifest without opening runtime state.

This command is intentionally narrower than a formal Phase 9 entry gate.  It
reads one bounded manifest and a fixed allowlist of Git facts, then writes one
deterministic JSON report to stdout.  It never opens SQLite, creates a run
generation, starts a runtime process, calls a provider, dispatches an outbox
item, or writes an output file. It does start the fixed local Git subprocesses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, Sequence


INPUT_SCHEMA = "phase9-prep-manifest-v1"
REPORT_SCHEMA = "phase9-prep-validation-report-v1"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 10

FIXED_FROZEN_BASE_COMMIT = "fb58241077ce6874bdfe2df6c23322d431930d38"
FIXED_FROZEN_BASE_TREE = "b1e2e1d0f1232f4ad7ce19f074709db5525d3f3f"
FIXED_EXPECTED_BRANCH = "codex/phase9-prep-20260831"
FIXED_EXPECTED_WORKTREE_ROOT = (
    "/home/tfisher/.codex/worktrees/phase9_prep_20260831/paper_factory"
)
FIXED_SLIM_PACKAGE_SHA256 = (
    "43940239ddca775254fb93b794fc35d44e60f7532780ce16524a0df9c2dfc26d"
)
FIXED_PROPOSED_RUN_GENERATION = "run4-forensic-proposed-20260831-01"
FIXED_RUNTIME_ROOT = (
    "/home/tfisher/.codex/phase9_runtime/run4-forensic-proposed-20260831-01"
)
FIXED_PLANNED_PATHS = {
    "database_dir": f"{FIXED_RUNTIME_ROOT}/database",
    "cas_dir": f"{FIXED_RUNTIME_ROOT}/cas",
    "spool_dir": f"{FIXED_RUNTIME_ROOT}/spool",
    "logs_dir": f"{FIXED_RUNTIME_ROOT}/logs",
    "evidence_dir": f"{FIXED_RUNTIME_ROOT}/evidence",
    "project_copy_dir": f"{FIXED_RUNTIME_ROOT}/project-copy",
}
FIXED_PROTECTED_ROOTS = {
    "main_checkout": "/home/tfisher/paper_factory",
    "phase78_frozen_worktree": (
        "/home/tfisher/.codex/worktrees/phase78_20260830/paper_factory"
    ),
    "phase78_audit_artifacts": (
        "/home/tfisher/.codex/worktrees/phase78_20260830/paper_factory/audit_artifacts"
    ),
    "phase9_source_worktree": FIXED_EXPECTED_WORKTREE_ROOT,
}
FIXED_ALLOWED_CHANGED_PATHS = (
    "CHANGELOG.md",
    "DOCUMENTATION_INDEX.md",
    "docs/operations/PHASE9_FORENSIC_EVIDENCE.template.json",
    "docs/operations/PHASE9_PREP_MANIFEST.template.json",
    "docs/operations/PHASE9_PREP_RUNBOOK.md",
    "scripts/phase9_prep_manifest.py",
    "tests/test_phase9_prep_manifest.py",
)
FIXED_CHANGED_PATH_MODES = {
    path: "100644" for path in FIXED_ALLOWED_CHANGED_PATHS
}
FIXED_GITLINKS = {
    "xhxmt.github.io": "40c9ebafa7965d64d3ac801c514b790455e77905",
}
FIXED_ATTRIBUTE_FILES = {
    ".gitattributes": "65e345e1d559dd2ddda06d3eb58eb8d95764c58c634a732d8aca61b441f42dc5",
}

HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")

CAPABILITY_KEYS = (
    "opens_sqlite",
    "creates_run_generation",
    "starts_runtime_process",
    "network_access",
    "provider_call",
    "outbox_dispatch",
    "migration",
    "delivery",
    "release",
    "cutover",
    "writes_runtime_paths",
)

DEFERRED_CHECK_KEYS = (
    "database_state",
    "migration_state",
    "outbox_state",
    "process_quiescence",
    "recorded_contract_pins",
    "official_input_freeze",
    "workflow_replay",
    "three_role_calls",
    "clean_room_acceptance",
)

PLANNED_PATH_KEYS = (
    "database_dir",
    "cas_dir",
    "spool_dir",
    "logs_dir",
    "evidence_dir",
    "project_copy_dir",
)

PROTECTED_ROOT_KEYS = (
    "main_checkout",
    "phase78_frozen_worktree",
    "phase78_audit_artifacts",
    "phase9_source_worktree",
)


class PrepManifestError(ValueError):
    """A fail-closed manifest, environment, or Git validation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise PrepManifestError(code, message)


def _object(value: Any, label: str, keys: Sequence[str]) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("INVALID_OBJECT", f"{label} must be an object")
    expected = set(keys)
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        _fail(
            "INVALID_KEYS",
            f"{label} has missing={missing!r} extra={extra!r}",
        )
    return value


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        _fail("INVALID_TEXT", f"{label} must be non-empty trimmed text")
    return value


def _literal(value: Any, label: str, expected: str) -> str:
    text = _text(value, label)
    if text != expected:
        _fail("INVALID_LITERAL", f"{label} must equal {expected!r}")
    return text


def _bool(value: Any, label: str, expected: bool) -> bool:
    if type(value) is not bool or value is not expected:
        _fail("INVALID_BOOLEAN", f"{label} must be {expected!r}")
    return value


def _hex(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    text = _text(value, label)
    if pattern.fullmatch(text) is None:
        _fail("INVALID_DIGEST", f"{label} has an invalid lowercase hex identity")
    return text


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label)
    if IDENTIFIER.fullmatch(text) is None:
        _fail("INVALID_IDENTIFIER", f"{label} has an invalid identifier")
    return text


def _absolute_posix(value: Any, label: str) -> PurePosixPath:
    text = _text(value, label)
    if (
        not text.startswith("/")
        or text.startswith("//")
        or "\\" in text
        or "\x00" in text
        or posixpath.normpath(text) != text
    ):
        _fail("INVALID_PATH", f"{label} must be one normalized absolute POSIX path")
    result = PurePosixPath(text)
    if len(result.parts) < 4:
        _fail("PATH_TOO_BROAD", f"{label} is too broad")
    return result


def _within(child: PurePosixPath, parent: PurePosixPath) -> bool:
    return child == parent or parent in child.parents


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_KEY", f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _read_bounded_regular_file(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise PrepManifestError("MANIFEST_UNREADABLE", "manifest cannot be inspected") from error
    if not stat.S_ISREG(before.st_mode):
        _fail("MANIFEST_NOT_REGULAR", "manifest must be a regular file, not a link")
    if before.st_nlink != 1:
        _fail("MANIFEST_LINK_COUNT", "manifest must have exactly one hard link")
    if not 0 < before.st_size <= MAX_MANIFEST_BYTES:
        _fail("MANIFEST_SIZE", "manifest must be bounded and non-empty")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if hasattr(os, "O_NOATIME"):
        flags |= os.O_NOATIME
    try:
        descriptor = os.open(path, flags)
    except PermissionError:
        flags &= ~getattr(os, "O_NOATIME", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise PrepManifestError("MANIFEST_UNREADABLE", "manifest cannot be opened") from error
    except OSError as error:
        raise PrepManifestError("MANIFEST_UNREADABLE", "manifest cannot be opened") from error

    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
            or opened.st_ctime_ns != before.st_ctime_ns
            or opened.st_mode != before.st_mode
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            _fail("MANIFEST_CHANGED", "manifest identity changed before read")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            named_after = path.lstat()
        except OSError as error:
            raise PrepManifestError(
                "MANIFEST_CHANGED", "manifest path changed during read"
            ) from error
    except PrepManifestError:
        raise
    except OSError as error:
        raise PrepManifestError(
            "MANIFEST_UNREADABLE", "manifest could not be read safely"
        ) from error
    finally:
        os.close(descriptor)

    if (
        len(raw) != opened.st_size
        or len(raw) > MAX_MANIFEST_BYTES
        or after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or after.st_ctime_ns != opened.st_ctime_ns
        or after.st_nlink != 1
        or not stat.S_ISREG(after.st_mode)
        or named_after.st_dev != opened.st_dev
        or named_after.st_ino != opened.st_ino
        or named_after.st_size != opened.st_size
        or named_after.st_mtime_ns != opened.st_mtime_ns
        or named_after.st_ctime_ns != opened.st_ctime_ns
        or named_after.st_nlink != 1
        or not stat.S_ISREG(named_after.st_mode)
    ):
        _fail("MANIFEST_CHANGED", "manifest changed during read")
    return raw


def load_manifest(path: Path) -> dict[str, Any]:
    raw = _read_bounded_regular_file(path)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise PrepManifestError("MANIFEST_NOT_UTF8", "manifest must be strict UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_duplicate_rejecting_object)
    except PrepManifestError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise PrepManifestError("MANIFEST_NOT_JSON", "manifest must be valid JSON") from error
    if type(value) is not dict:
        _fail("INVALID_OBJECT", "manifest root must be an object")
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def validate_manifest(
    value: dict[str, Any], *, enforce_fixed_contract: bool = True
) -> dict[str, Any]:
    root = _object(
        value,
        "manifest",
        (
            "schema_version",
            "identity",
            "pro_review",
            "formal_boundary",
            "capabilities",
            "deferred_checks",
            "planned_paths",
            "protected_roots",
        ),
    )
    _literal(root["schema_version"], "schema_version", INPUT_SCHEMA)

    identity = _object(
        root["identity"],
        "identity",
        (
            "frozen_base_commit",
            "frozen_base_tree",
            "expected_branch",
            "expected_worktree_root",
        ),
    )
    frozen_commit = _hex(
        identity["frozen_base_commit"], "identity.frozen_base_commit", HEX40
    )
    frozen_tree = _hex(
        identity["frozen_base_tree"], "identity.frozen_base_tree", HEX40
    )
    expected_branch = _text(identity["expected_branch"], "identity.expected_branch")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", expected_branch) is None:
        _fail("INVALID_BRANCH", "identity.expected_branch is invalid")
    expected_root = _absolute_posix(
        identity["expected_worktree_root"], "identity.expected_worktree_root"
    )

    pro_review = _object(
        root["pro_review"],
        "pro_review",
        ("status", "scope", "slim_package_sha256"),
    )
    _literal(pro_review["status"], "pro_review.status", "PENDING")
    _literal(
        pro_review["scope"],
        "pro_review.scope",
        "NORMAL_LINUX_DEFAULT_OFF_NON_AUTHORITATIVE_NO_DISPATCH",
    )
    _hex(pro_review["slim_package_sha256"], "pro_review.slim_package_sha256", HEX64)

    boundary = _object(
        root["formal_boundary"],
        "formal_boundary",
        (
            "phase_label",
            "formal_phase9_authorized",
            "proposed_run_generation",
            "run_generation_state",
            "run_mode",
            "modeling_consultation_contract",
            "delivery_capability",
            "old_generation_access",
            "initial_resume_target",
        ),
    )
    _literal(boundary["phase_label"], "formal_boundary.phase_label", "PHASE9_PREP")
    _bool(
        boundary["formal_phase9_authorized"],
        "formal_boundary.formal_phase9_authorized",
        False,
    )
    _identifier(
        boundary["proposed_run_generation"],
        "formal_boundary.proposed_run_generation",
    )
    _literal(
        boundary["run_generation_state"],
        "formal_boundary.run_generation_state",
        "NOT_CREATED",
    )
    _literal(boundary["run_mode"], "formal_boundary.run_mode", "FORENSIC_REPLAY")
    _literal(
        boundary["modeling_consultation_contract"],
        "formal_boundary.modeling_consultation_contract",
        "LEGACY_NOT_APPLICABLE",
    )
    _literal(
        boundary["delivery_capability"],
        "formal_boundary.delivery_capability",
        "DISABLED",
    )
    _literal(
        boundary["old_generation_access"],
        "formal_boundary.old_generation_access",
        "READ_ONLY",
    )
    _literal(
        boundary["initial_resume_target"],
        "formal_boundary.initial_resume_target",
        "STEP13_PACKET_REBUILD",
    )

    capabilities = _object(root["capabilities"], "capabilities", CAPABILITY_KEYS)
    for key in CAPABILITY_KEYS:
        _bool(capabilities[key], f"capabilities.{key}", False)

    deferred = _object(
        root["deferred_checks"], "deferred_checks", DEFERRED_CHECK_KEYS
    )
    for key in DEFERRED_CHECK_KEYS:
        _literal(deferred[key], f"deferred_checks.{key}", "DEFERRED")

    protected_raw = _object(
        root["protected_roots"], "protected_roots", PROTECTED_ROOT_KEYS
    )
    protected = {
        key: _absolute_posix(protected_raw[key], f"protected_roots.{key}")
        for key in PROTECTED_ROOT_KEYS
    }
    if protected["phase9_source_worktree"] != expected_root:
        _fail(
            "SOURCE_ROOT_MISMATCH",
            "phase9 source root must equal identity.expected_worktree_root",
        )
    if not _within(
        protected["phase78_audit_artifacts"],
        protected["phase78_frozen_worktree"],
    ):
        _fail(
            "AUDIT_ROOT_MISMATCH",
            "phase78 audit artifacts must stay under the frozen worktree",
        )

    planned_raw = _object(root["planned_paths"], "planned_paths", PLANNED_PATH_KEYS)
    planned = {
        key: _absolute_posix(planned_raw[key], f"planned_paths.{key}")
        for key in PLANNED_PATH_KEYS
    }
    planned_values = list(planned.values())
    if len(set(planned_values)) != len(planned_values):
        _fail("PLANNED_PATH_COLLISION", "planned paths must be pairwise distinct")
    for index, left in enumerate(planned_values):
        for right in planned_values[index + 1 :]:
            if _within(left, right) or _within(right, left):
                _fail(
                    "PLANNED_PATH_OVERLAP",
                    "planned paths must not be ancestors of one another",
                )
        for protected_root in protected.values():
            if _within(left, protected_root) or _within(protected_root, left):
                _fail(
                    "PLANNED_PATH_PROTECTED",
                    "planned paths must stay outside every protected root",
                )

    common = PurePosixPath(posixpath.commonpath([str(path) for path in planned_values]))
    if len(common.parts) < 6 or any(path.parent != common for path in planned_values):
        _fail(
            "PLANNED_PATH_SCOPE",
            "planned paths must be sibling directories under one narrow runtime root",
        )
    for protected_root in protected.values():
        if _within(common, protected_root) or _within(protected_root, common):
            _fail("PLANNED_PATH_PROTECTED", "runtime root is protected")

    if enforce_fixed_contract:
        fixed_values_match = (
            frozen_commit == FIXED_FROZEN_BASE_COMMIT
            and frozen_tree == FIXED_FROZEN_BASE_TREE
            and expected_branch == FIXED_EXPECTED_BRANCH
            and str(expected_root) == FIXED_EXPECTED_WORKTREE_ROOT
            and pro_review["slim_package_sha256"] == FIXED_SLIM_PACKAGE_SHA256
            and boundary["proposed_run_generation"] == FIXED_PROPOSED_RUN_GENERATION
            and str(common) == FIXED_RUNTIME_ROOT
            and {key: str(value) for key, value in planned.items()}
            == FIXED_PLANNED_PATHS
            and {key: str(value) for key, value in protected.items()}
            == FIXED_PROTECTED_ROOTS
        )
        if not fixed_values_match:
            _fail(
                "FIXED_CONTRACT_MISMATCH",
                "manifest cannot redefine the frozen Phase9-PREP contract",
            )

    allowed_changed_paths = (
        list(FIXED_ALLOWED_CHANGED_PATHS) if enforce_fixed_contract else []
    )
    expected_gitlinks = dict(FIXED_GITLINKS) if enforce_fixed_contract else {}
    expected_attribute_files = (
        dict(FIXED_ATTRIBUTE_FILES) if enforce_fixed_contract else {}
    )
    expected_changed_path_modes = (
        dict(FIXED_CHANGED_PATH_MODES) if enforce_fixed_contract else {}
    )

    return {
        "frozen_commit": frozen_commit,
        "frozen_tree": frozen_tree,
        "expected_branch": expected_branch,
        "expected_root": str(expected_root),
        "deferred_checks": list(DEFERRED_CHECK_KEYS),
        "proposed_run_generation": boundary["proposed_run_generation"],
        "runtime_root": str(common),
        "allowed_changed_paths": allowed_changed_paths,
        "expected_gitlinks": expected_gitlinks,
        "expected_attribute_files": expected_attribute_files,
        "expected_changed_path_modes": expected_changed_path_modes,
    }


def _git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }


def _git_arguments_allowed(arguments: tuple[str, ...]) -> bool:
    fixed = {
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "HEAD^{commit}"),
        ("rev-parse", "HEAD^{tree}"),
        ("rev-parse", "--git-path", "info/attributes"),
        ("symbolic-ref", "-q", "--short", "HEAD"),
        (
            "diff",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            "--",
        ),
        (
            "diff",
            "--cached",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            "--",
        ),
        ("ls-files", "--stage", "-z"),
        ("ls-files", "-v", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
        (
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ),
    }
    if arguments in fixed:
        return True
    if (
        len(arguments) == 2
        and arguments[0] == "rev-parse"
        and re.fullmatch(r"[0-9a-f]{40}\^\{(?:commit|tree)\}", arguments[1])
    ):
        return True
    if (
        len(arguments) == 4
        and arguments[:2] == ("merge-base", "--is-ancestor")
        and HEX40.fullmatch(arguments[2]) is not None
        and arguments[3] == "HEAD"
    ):
        return True
    if (
        len(arguments) == 9
        and arguments[:7]
        == (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
        )
        and re.fullmatch(r"[0-9a-f]{40}\.\.HEAD", arguments[7])
        and arguments[8] == "--"
    ):
        return True
    return False


def _git(
    repository: Path,
    arguments: Sequence[str],
    *,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    arguments = tuple(arguments)
    if not _git_arguments_allowed(arguments):
        _fail("GIT_COMMAND_FORBIDDEN", "Git command is outside the read-only allowlist")
    command = (
        "git",
        "--no-optional-locks",
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "submodule.recurse=false",
        "-C",
        str(repository),
        *arguments,
    )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        raise PrepManifestError("GIT_UNAVAILABLE", "read-only Git command failed") from error

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}

    def terminate() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, GIT_TIMEOUT_SECONDS)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, GIT_TIMEOUT_SECONDS)
            for key, _ in events:
                chunk = os.read(key.fd, 64 * 1024)
                if chunk:
                    buffer = buffers[key.data]
                    buffer.extend(chunk)
                    if len(buffer) > MAX_GIT_OUTPUT_BYTES:
                        _fail(
                            "GIT_OUTPUT_LIMIT",
                            "read-only Git output exceeded its bound",
                        )
                else:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, GIT_TIMEOUT_SECONDS)
        returncode = process.wait(timeout=remaining)
    except PrepManifestError:
        terminate()
        raise
    except (OSError, subprocess.TimeoutExpired) as error:
        terminate()
        raise PrepManifestError("GIT_UNAVAILABLE", "read-only Git command failed") from error
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()

    result = subprocess.CompletedProcess(
        command,
        returncode,
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
    )
    if result.returncode not in allowed_returncodes:
        _fail("GIT_COMMAND_FAILED", "read-only Git command returned an error")
    return result


def _decode_git_line(raw: bytes, label: str) -> str:
    try:
        value = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise PrepManifestError("GIT_OUTPUT_ENCODING", f"{label} was not UTF-8") from error
    if not value or "\n" in value or "\r" in value:
        _fail("GIT_OUTPUT_SHAPE", f"{label} must be exactly one line")
    return value


def _nul_records(raw: bytes, label: str) -> tuple[bytes, ...]:
    if raw and not raw.endswith(b"\0"):
        _fail("GIT_OUTPUT_SHAPE", f"{label} was not NUL terminated")
    records = tuple(item for item in raw.split(b"\0") if item)
    if any(b"\0" in item for item in records):
        _fail("GIT_OUTPUT_SHAPE", f"{label} contained an invalid record")
    return records


def _index_gitlinks(
    raw: bytes, watched_paths: set[str]
) -> tuple[dict[str, str], int, tuple[str, ...], dict[str, str]]:
    gitlinks: dict[str, str] = {}
    attribute_files: set[str] = set()
    watched_raw = {path.encode("utf-8"): path for path in watched_paths}
    watched_modes: dict[str, str] = {}
    records = _nul_records(raw, "index inventory")
    for record in records:
        try:
            metadata, path_raw = record.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ")
        except ValueError:
            _fail("GIT_OUTPUT_SHAPE", "index inventory record was malformed")
        if stage != b"0":
            _fail("UNMERGED_INDEX", "index contains a non-zero merge stage")
        if path_raw in watched_raw:
            try:
                mode_text = mode.decode("ascii", errors="strict")
            except UnicodeDecodeError as error:
                raise PrepManifestError(
                    "GIT_OUTPUT_ENCODING", "index mode was not ASCII"
                ) from error
            watched_modes[watched_raw[path_raw]] = mode_text
        if path_raw == b".gitattributes" or path_raw.endswith(b"/.gitattributes"):
            try:
                attribute_files.add(path_raw.decode("utf-8", errors="strict"))
            except UnicodeDecodeError as error:
                raise PrepManifestError(
                    "GIT_OUTPUT_ENCODING", "attribute path was not UTF-8"
                ) from error
        if mode == b"160000":
            try:
                path = path_raw.decode("utf-8", errors="strict")
                object_text = object_id.decode("ascii", errors="strict")
            except UnicodeDecodeError as error:
                raise PrepManifestError(
                    "GIT_OUTPUT_ENCODING", "gitlink inventory was not UTF-8/ASCII"
                ) from error
            if (
                not path
                or path in gitlinks
                or HEX40.fullmatch(object_text) is None
            ):
                _fail("GITLINK_INVENTORY_INVALID", "gitlink inventory is invalid")
            gitlinks[path] = object_text
    return (
        gitlinks,
        len(records),
        tuple(sorted(attribute_files)),
        watched_modes,
    )


def _index_flags(raw: bytes, expected_count: int) -> None:
    records = _nul_records(raw, "index flags")
    if len(records) != expected_count:
        _fail("INDEX_INVENTORY_MISMATCH", "index inventories disagree")
    for record in records:
        if len(record) < 3 or record[:2] != b"H ":
            _fail(
                "INDEX_SPECIAL_FLAG",
                "index contains assume-unchanged, skip-worktree, or non-cached state",
            )


def _gitlink_worktree_states(
    repository: Path, expected_gitlinks: dict[str, str]
) -> dict[str, dict[str, str]]:
    states: dict[str, dict[str, str]] = {}
    for relative, object_id in sorted(expected_gitlinks.items()):
        pure_path = PurePosixPath(relative)
        if pure_path.is_absolute() or ".." in pure_path.parts or pure_path.as_posix() != relative:
            _fail("GITLINK_INVENTORY_INVALID", "gitlink path is invalid")
        path = repository.joinpath(*pure_path.parts)
        try:
            info = path.lstat()
        except FileNotFoundError:
            state = "UNINITIALIZED_ABSENT"
        except OSError as error:
            raise PrepManifestError(
                "GITLINK_WORKTREE_UNAVAILABLE", "gitlink worktree cannot be inspected"
            ) from error
        else:
            if not stat.S_ISDIR(info.st_mode):
                _fail(
                    "GITLINK_WORKTREE_PRESENT",
                    "gitlink worktree must be absent or an empty directory",
                )
            try:
                with os.scandir(path) as entries:
                    populated = next(entries, None) is not None
            except OSError as error:
                raise PrepManifestError(
                    "GITLINK_WORKTREE_UNAVAILABLE",
                    "gitlink worktree cannot be enumerated",
                ) from error
            if populated:
                _fail(
                    "GITLINK_WORKTREE_PRESENT",
                    "initialized or populated gitlink worktrees are forbidden",
                )
            state = "UNINITIALIZED_EMPTY"
        states[relative] = {"object_id": object_id, "worktree_state": state}
    return states


def _validate_attribute_files(
    repository: Path,
    expected_files: dict[str, str],
    indexed_files: tuple[str, ...],
) -> dict[str, str]:
    if tuple(sorted(expected_files)) != indexed_files:
        _fail("ATTRIBUTE_INVENTORY_MISMATCH", "tracked attribute inventory changed")
    observed: dict[str, str] = {}
    for relative, expected_sha256 in sorted(expected_files.items()):
        pure_path = PurePosixPath(relative)
        if pure_path.is_absolute() or ".." in pure_path.parts or pure_path.as_posix() != relative:
            _fail("ATTRIBUTE_INVENTORY_INVALID", "tracked attribute path is invalid")
        try:
            raw = _read_bounded_regular_file(repository.joinpath(*pure_path.parts))
        except PrepManifestError as error:
            raise PrepManifestError(
                "ATTRIBUTE_FILE_INVALID", "tracked attribute file is not stable"
            ) from error
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_sha256 != expected_sha256:
            _fail("ATTRIBUTE_FILE_MISMATCH", "tracked attribute bytes changed")
        observed[relative] = actual_sha256

    info_text = _decode_git_line(
        _git(repository, ("rev-parse", "--git-path", "info/attributes")).stdout,
        "local attribute path",
    )
    info_path = Path(info_text)
    if not info_path.is_absolute():
        info_path = repository / info_path
    try:
        info_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise PrepManifestError(
            "LOCAL_ATTRIBUTES_UNAVAILABLE", "local attributes cannot be inspected"
        ) from error
    else:
        _fail(
            "LOCAL_ATTRIBUTES_FORBIDDEN",
            "repository-local info/attributes must be absent",
        )
    return observed


def collect_git_facts(repository: Path, identity: dict[str, Any]) -> dict[str, Any]:
    try:
        requested_root = repository.resolve(strict=True)
    except OSError as error:
        raise PrepManifestError("REPOSITORY_UNAVAILABLE", "repository path is unavailable") from error
    if not requested_root.is_dir():
        _fail("REPOSITORY_UNAVAILABLE", "repository path must be a directory")

    top_level = _decode_git_line(
        _git(requested_root, ("rev-parse", "--show-toplevel")).stdout,
        "repository root",
    )
    try:
        actual_root = Path(top_level).resolve(strict=True)
    except OSError as error:
        raise PrepManifestError("REPOSITORY_UNAVAILABLE", "Git root is unavailable") from error
    if actual_root != requested_root or top_level != identity["expected_root"]:
        _fail("REPOSITORY_ROOT_MISMATCH", "Git root does not match the manifest")

    commit = _decode_git_line(
        _git(actual_root, ("rev-parse", "HEAD^{commit}")).stdout,
        "HEAD commit",
    )
    tree = _decode_git_line(
        _git(actual_root, ("rev-parse", "HEAD^{tree}")).stdout,
        "HEAD tree",
    )
    if HEX40.fullmatch(commit) is None or HEX40.fullmatch(tree) is None:
        _fail("GIT_IDENTITY_INVALID", "Git returned an invalid object identity")

    frozen_commit = _decode_git_line(
        _git(
            actual_root,
            ("rev-parse", f"{identity['frozen_commit']}^{{commit}}"),
        ).stdout,
        "frozen commit",
    )
    frozen_tree = _decode_git_line(
        _git(
            actual_root,
            ("rev-parse", f"{identity['frozen_commit']}^{{tree}}"),
        ).stdout,
        "frozen tree",
    )
    if frozen_commit != identity["frozen_commit"] or frozen_tree != identity["frozen_tree"]:
        _fail("FROZEN_IDENTITY_MISMATCH", "frozen source identity does not match")

    ancestor = _git(
        actual_root,
        ("merge-base", "--is-ancestor", identity["frozen_commit"], "HEAD"),
        allowed_returncodes=(0, 1),
    )
    if ancestor.returncode != 0:
        _fail("FROZEN_BASE_NOT_ANCESTOR", "HEAD is not derived from the frozen base")

    branch_result = _git(
        actual_root,
        ("symbolic-ref", "-q", "--short", "HEAD"),
        allowed_returncodes=(0, 1),
    )
    branch = (
        _decode_git_line(branch_result.stdout, "branch")
        if branch_result.returncode == 0
        else "DETACHED"
    )
    if branch != identity["expected_branch"]:
        _fail("BRANCH_MISMATCH", "current branch does not match the manifest")

    changed_raw = _git(
        actual_root,
        (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            f"{identity['frozen_commit']}..HEAD",
            "--",
        ),
    ).stdout
    try:
        changed_paths = tuple(
            item.decode("utf-8", errors="strict")
            for item in _nul_records(changed_raw, "changed path inventory")
        )
    except UnicodeDecodeError as error:
        raise PrepManifestError(
            "GIT_OUTPUT_ENCODING", "changed path inventory was not UTF-8"
        ) from error
    if (
        len(changed_paths) != len(set(changed_paths))
        or set(changed_paths) != set(identity["allowed_changed_paths"])
    ):
        _fail(
            "PREP_CHANGE_SCOPE_MISMATCH",
            "base-to-HEAD changes do not match the fixed prep path allowlist",
        )

    index_raw = _git(actual_root, ("ls-files", "--stage", "-z")).stdout
    (
        index_gitlinks,
        index_entry_count,
        indexed_attribute_files,
        changed_path_modes,
    ) = _index_gitlinks(index_raw, set(identity["allowed_changed_paths"]))
    if changed_path_modes != identity["expected_changed_path_modes"]:
        _fail(
            "PREP_PATH_MODE_MISMATCH",
            "prep files must be present as ordinary 100644 index entries",
        )
    if index_gitlinks != identity["expected_gitlinks"]:
        _fail("GITLINK_INVENTORY_MISMATCH", "gitlink inventory or pointer changed")
    index_flags_raw = _git(actual_root, ("ls-files", "-v", "-z")).stdout
    _index_flags(index_flags_raw, index_entry_count)
    attribute_files = _validate_attribute_files(
        actual_root,
        identity["expected_attribute_files"],
        indexed_attribute_files,
    )
    gitlink_states = _gitlink_worktree_states(actual_root, index_gitlinks)

    untracked_raw = _git(
        actual_root,
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ).stdout
    ignored_raw = _git(
        actual_root,
        ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
    ).stdout
    untracked_entries = _nul_records(untracked_raw, "untracked inventory")
    ignored_entries = _nul_records(ignored_raw, "ignored inventory")
    if untracked_entries:
        _fail("UNTRACKED_FILES", "worktree has untracked files")
    if ignored_entries:
        _fail("IGNORED_UNTRACKED_FILES", "worktree has ignored untracked files")

    diff_flags = (
        "--quiet",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=none",
        "--",
    )
    unstaged = _git(
        actual_root,
        ("diff", *diff_flags),
        allowed_returncodes=(0, 1),
    )
    staged = _git(
        actual_root,
        ("diff", "--cached", *diff_flags),
        allowed_returncodes=(0, 1),
    )
    if unstaged.returncode != 0:
        _fail("UNSTAGED_TRACKED_CHANGES", "worktree has unstaged tracked changes")
    if staged.returncode != 0:
        _fail("STAGED_CHANGES", "worktree has staged changes")

    final_changed_raw = _git(
        actual_root,
        (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            f"{identity['frozen_commit']}..HEAD",
            "--",
        ),
    ).stdout
    final_index_raw = _git(actual_root, ("ls-files", "--stage", "-z")).stdout
    final_index_flags_raw = _git(actual_root, ("ls-files", "-v", "-z")).stdout
    final_untracked_raw = _git(
        actual_root,
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ).stdout
    final_ignored_raw = _git(
        actual_root,
        ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
    ).stdout
    final_unstaged = _git(
        actual_root,
        ("diff", *diff_flags),
        allowed_returncodes=(0, 1),
    )
    final_staged = _git(
        actual_root,
        ("diff", "--cached", *diff_flags),
        allowed_returncodes=(0, 1),
    )
    final_attribute_files = _validate_attribute_files(
        actual_root,
        identity["expected_attribute_files"],
        indexed_attribute_files,
    )
    final_gitlink_states = _gitlink_worktree_states(actual_root, index_gitlinks)
    final_commit = _decode_git_line(
        _git(actual_root, ("rev-parse", "HEAD^{commit}")).stdout,
        "final HEAD commit",
    )
    final_tree = _decode_git_line(
        _git(actual_root, ("rev-parse", "HEAD^{tree}")).stdout,
        "final HEAD tree",
    )
    final_branch_result = _git(
        actual_root,
        ("symbolic-ref", "-q", "--short", "HEAD"),
        allowed_returncodes=(0, 1),
    )
    final_branch = (
        _decode_git_line(final_branch_result.stdout, "final branch")
        if final_branch_result.returncode == 0
        else "DETACHED"
    )
    if (
        final_changed_raw != changed_raw
        or final_index_raw != index_raw
        or final_index_flags_raw != index_flags_raw
        or final_untracked_raw != untracked_raw
        or final_ignored_raw != ignored_raw
        or final_unstaged.returncode != 0
        or final_staged.returncode != 0
        or final_attribute_files != attribute_files
        or final_gitlink_states != gitlink_states
        or final_commit != commit
        or final_tree != tree
        or final_branch != branch
    ):
        _fail(
            "REPOSITORY_CHANGED_DURING_READ",
            "Git identity or inventory changed during validation",
        )

    return {
        "branch": branch,
        "head_commit": commit,
        "head_tree": tree,
        "frozen_base_commit": frozen_commit,
        "frozen_base_tree": frozen_tree,
        "frozen_base_is_ancestor": True,
        "base_to_head_changed_paths": list(changed_paths),
        "base_to_head_changed_paths_sha256": hashlib.sha256(changed_raw).hexdigest(),
        "base_to_head_changed_path_modes": changed_path_modes,
        "index_entry_count": index_entry_count,
        "index_special_flags": False,
        "attribute_files": attribute_files,
        "local_info_attributes_present": False,
        "gitlinks": gitlink_states,
        "unstaged_tracked_changes": False,
        "staged_changes": False,
        "untracked_count": 0,
        "untracked_paths_sha256": hashlib.sha256(untracked_raw).hexdigest(),
        "ignored_untracked_count": 0,
        "ignored_untracked_paths_sha256": hashlib.sha256(ignored_raw).hexdigest(),
    }


def build_report(manifest: dict[str, Any], git_facts: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    manifest_sha256 = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    report_without_hash = {
        "schema_version": REPORT_SCHEMA,
        "status": "PREP_MANIFEST_VALID",
        "manifest_sha256": manifest_sha256,
        "repository": git_facts,
        "boundary": {
            "formal_phase9_authorized": False,
            "manifest_declared_run_generation_state": "NOT_CREATED",
            "proposed_run_generation": identity["proposed_run_generation"],
            "validator_created_run_generation": False,
            "validator_created_runtime_paths": False,
            "validator_opened_sqlite": False,
            "validator_started_runtime_process": False,
            "validator_accessed_network": False,
            "validator_called_provider": False,
            "validator_dispatched_outbox": False,
            "validator_performed_migration": False,
            "delivery_authorized": False,
            "release_authorized": False,
            "cutover_authorized": False,
        },
        "deferred_checks": identity["deferred_checks"],
        "planned_runtime_root": identity["runtime_root"],
        "next_gate": "PRO_NORMAL_FLOW_PASS_AND_FORMAL_READ_ONLY_STATE_GATE",
    }
    report = dict(report_without_hash)
    report["report_sha256"] = hashlib.sha256(
        canonical_bytes(report_without_hash)
    ).hexdigest()
    return report


def validate_environment() -> None:
    raw = os.environ.get("PHASE78_ENABLED")
    if raw is None:
        return
    normalized = raw.strip().casefold()
    if normalized in {"", "0", "false", "no", "off"}:
        return
    _fail("PHASE78_ENABLED", "PHASE78_ENABLED must be unset or explicitly false")


def validate_python_runtime() -> None:
    required_flags = (
        "isolated",
        "no_site",
        "dont_write_bytecode",
        "safe_path",
    )
    if not all(bool(getattr(sys.flags, flag, False)) for flag in required_flags):
        _fail(
            "PYTHON_ISOLATION_REQUIRED",
            "invoke with the fixed /usr/bin/python3 -I -S -B command",
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Bounded Phase9-PREP JSON manifest")
    parser.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="Exact isolated Phase9-PREP Git worktree root",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_python_runtime()
        validate_environment()
        manifest = load_manifest(args.manifest)
        identity = validate_manifest(manifest)
        git_facts = collect_git_facts(args.repo, identity)
        report = build_report(manifest, git_facts, identity)
    except PrepManifestError as error:
        invalid = {
            "schema_version": REPORT_SCHEMA,
            "status": "INVALID",
            "error_code": error.code,
            "formal_phase9_authorized": False,
        }
        sys.stdout.buffer.write(canonical_bytes(invalid) + b"\n")
        return 2
    sys.stdout.buffer.write(canonical_bytes(report) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
