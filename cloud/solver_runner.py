"""Bounded subprocess execution for the quarantined Cloud Solver service."""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


MAX_JOB_ID_LENGTH = 64
MAX_PATH_LENGTH = 240
MAX_PATH_DEPTH = 8
MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_SCRIPT_BYTES = 1024 * 1024
MAX_WORKING_FILE_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_WORKING_FILES = 64
MAX_EXECUTION_TIME = 3600
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_SINGLE_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_FILES = 256
MAX_OUTPUT_DIRECTORIES = 64
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_ADDRESS_SPACE_BYTES = 6 * 1024 * 1024 * 1024
MAX_OPEN_FILES = 128
MAX_PROCESSES = 32
EXEC_WRAPPER = Path(__file__).with_name("solver_exec_wrapper.py")

JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?$")
PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
THREAD_ENV_KEYS = {
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_MAX_THREADS",
}
SEED_ENV_KEYS = {"PYTHONHASHSEED", "SOLVER_RANDOM_SEED"}
ALLOWED_ENV_KEYS = THREAD_ENV_KEYS | SEED_ENV_KEYS
RESULT_SUFFIXES = {".json", ".csv", ".txt", ".md", ".log", ".xlsx", ".png", ".pdf"}


class InputValidationError(ValueError):
    """A submitted field violates the Cloud Solver input contract."""


class OutputLimitError(RuntimeError):
    """A solver created unsafe output content or exceeded a hard limit."""


def validate_job_id(job_id: str) -> str:
    if len(job_id.encode("utf-8")) > MAX_JOB_ID_LENGTH or not JOB_ID_PATTERN.fullmatch(job_id):
        raise InputValidationError(
            "job_id must be 1-64 ASCII letters, digits, underscores, or hyphens"
        )
    return job_id


def validate_relative_path(value: str, *, allow_nested: bool) -> PurePosixPath:
    if not value or len(value.encode("utf-8")) > MAX_PATH_LENGTH:
        raise InputValidationError("file path is empty or exceeds the path-length limit")
    if "\\" in value or "\x00" in value or "//" in value:
        raise InputValidationError("file paths must use safe POSIX relative syntax")

    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InputValidationError("absolute paths and traversal segments are forbidden")
    if not allow_nested and len(path.parts) != 1:
        raise InputValidationError("script_name must be a plain filename")
    if len(path.parts) > MAX_PATH_DEPTH:
        raise InputValidationError("file path exceeds the directory-depth limit")
    if any(".." in part or not PATH_SEGMENT_PATTERN.fullmatch(part) for part in path.parts):
        raise InputValidationError("file path contains a forbidden segment")
    return path


def validate_env_vars(env_vars: Mapping[str, str] | None) -> dict[str, str]:
    validated: dict[str, str] = {}
    for key, value in (env_vars or {}).items():
        if key not in ALLOWED_ENV_KEYS:
            raise InputValidationError(f"environment variable is not allowed: {key}")
        if not isinstance(value, str) or len(value) > 32:
            raise InputValidationError(f"environment variable has an invalid value: {key}")
        if key in THREAD_ENV_KEYS:
            if not value.isdigit() or not 1 <= int(value) <= 4:
                raise InputValidationError(f"{key} must be an integer from 1 to 4")
        elif key == "PYTHONHASHSEED":
            if value != "random" and (
                not value.isdigit() or not 0 <= int(value) <= 4_294_967_295
            ):
                raise InputValidationError("PYTHONHASHSEED must be random or a 32-bit integer")
        elif key == "SOLVER_RANDOM_SEED" and (
            not value.isdigit() or not 0 <= int(value) <= 4_294_967_295
        ):
            raise InputValidationError("SOLVER_RANDOM_SEED must be a 32-bit integer")
        validated[key] = value
    return validated


def validate_submission_files(
    script_name: str,
    script_content: str,
    working_files: Mapping[str, str] | None,
) -> None:
    script_path = validate_relative_path(script_name, allow_nested=False)
    script_bytes = len(script_content.encode("utf-8"))
    if not script_content or script_bytes > MAX_SCRIPT_BYTES:
        raise InputValidationError("script content is empty or exceeds the script-size limit")

    files = working_files or {}
    if len(files) > MAX_WORKING_FILES:
        raise InputValidationError("working file count exceeds the limit")

    total_bytes = script_bytes
    normalized_paths = {str(script_path)}
    for filename, content in files.items():
        normalized = str(validate_relative_path(filename, allow_nested=True))
        if normalized in normalized_paths:
            raise InputValidationError("submitted file paths must be unique")
        normalized_paths.add(normalized)
        if not isinstance(content, str):
            raise InputValidationError("working file content must be text")
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > MAX_WORKING_FILE_BYTES:
            raise InputValidationError("working file exceeds the single-file limit")
        total_bytes += content_bytes
        if total_bytes > MAX_INPUT_BYTES:
            raise InputValidationError("submitted files exceed the total-input limit")


def _write_exclusive_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o444)


def _configured_run_identity() -> tuple[int, int] | None:
    raw_uid = (os.getenv("SOLVER_RUN_UID") or "").strip()
    raw_gid = (os.getenv("SOLVER_RUN_GID") or raw_uid).strip()
    if not raw_uid:
        return None
    if not raw_uid.isdigit() or not raw_gid.isdigit():
        raise RuntimeError("SOLVER_RUN_UID and SOLVER_RUN_GID must be numeric")
    uid, gid = int(raw_uid), int(raw_gid)
    if uid == 0 or gid == 0:
        raise RuntimeError("solver subprocess identity must be unprivileged")
    if os.geteuid() != 0:
        raise RuntimeError("configured solver identity requires a root control process")
    return uid, gid


def prepare_workspace(
    job_root: Path,
    script_name: str,
    script_content: str,
    working_files: Mapping[str, str] | None,
) -> tuple[Path, Path, Path, tuple[int, int] | None]:
    validate_submission_files(script_name, script_content, working_files)
    job_root.mkdir(mode=0o755, parents=False, exist_ok=False)
    input_dir = job_root / "input"
    output_dir = job_root / "output"
    input_dir.mkdir(mode=0o755)
    output_dir.mkdir(mode=0o700)
    (output_dir / ".tmp").mkdir(mode=0o700)

    script_path = input_dir / str(validate_relative_path(script_name, allow_nested=False))
    _write_exclusive_text(script_path, script_content)
    for filename, content in (working_files or {}).items():
        relative = validate_relative_path(filename, allow_nested=True)
        _write_exclusive_text(input_dir / str(relative), content)

    for directory, subdirectories, _filenames in os.walk(input_dir, topdown=False):
        Path(directory).chmod(0o555)
        for subdirectory in subdirectories:
            (Path(directory) / subdirectory).chmod(0o555)

    identity = _configured_run_identity()
    if identity is not None:
        uid, gid = identity
        os.chown(output_dir, uid, gid)
        for path in output_dir.iterdir():
            os.chown(path, uid, gid)
    return input_dir, output_dir, script_path, identity


def build_solver_environment(
    input_dir: Path,
    output_dir: Path,
    env_vars: Mapping[str, str] | None,
) -> dict[str, str]:
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(input_dir),
        "HOME": str(output_dir),
        "TMPDIR": str(output_dir / ".tmp"),
        "SOLVER_INPUT_DIR": str(input_dir),
        "SOLVER_OUTPUT_DIR": str(output_dir),
    }
    environment.update(validate_env_vars(env_vars))
    return environment


def _validate_output_path(root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    parts = relative.parts
    if parts and parts[0] == ".tmp":
        if len(parts) == 1:
            return
        relative = PurePosixPath(*parts[1:])
    try:
        validate_relative_path(relative.as_posix(), allow_nested=True)
    except InputValidationError as exc:
        raise OutputLimitError("solver output contains an unsafe path") from exc


def _inspect_output_tree(root: Path) -> tuple[int, int]:
    file_count = 0
    directory_count = 0
    total_bytes = 0
    pending = [root]
    while pending:
        directory_path = pending.pop()
        with os.scandir(directory_path) as entries:
            for entry in entries:
                path = Path(entry.path)
                _validate_output_path(root, path)
                if entry.is_symlink():
                    raise OutputLimitError("symbolic links are forbidden in solver output")
                file_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(file_stat.st_mode):
                    directory_count += 1
                    if directory_count > MAX_OUTPUT_DIRECTORIES:
                        raise OutputLimitError("solver output directory count exceeds the limit")
                    pending.append(path)
                    continue
                if not stat.S_ISREG(file_stat.st_mode):
                    raise OutputLimitError("only regular files are allowed in solver output")
                file_count += 1
                total_bytes += file_stat.st_size
                if file_stat.st_size > MAX_SINGLE_OUTPUT_BYTES:
                    raise OutputLimitError("a solver output file exceeds the single-file limit")
                if file_count > MAX_OUTPUT_FILES:
                    raise OutputLimitError("solver output file count exceeds the limit")
                if total_bytes > MAX_OUTPUT_BYTES:
                    raise OutputLimitError("solver output exceeds the total-size limit")
    return file_count, total_bytes


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def _terminate_solver_identity(identity: tuple[int, int] | None) -> None:
    """Kill descendants that attempted to escape the solver process group."""
    if identity is None:
        return
    uid, _gid = identity
    for _attempt in range(3):
        matched = False
        for process_dir in Path("/proc").iterdir():
            if not process_dir.name.isdigit():
                continue
            try:
                status_text = (process_dir / "status").read_text(encoding="utf-8")
                uid_line = next(
                    line for line in status_text.splitlines() if line.startswith("Uid:")
                )
                process_uids = {int(value) for value in uid_line.split()[1:]}
                if uid not in process_uids:
                    continue
                os.kill(int(process_dir.name), signal.SIGKILL)
                matched = True
            except (FileNotFoundError, ProcessLookupError, PermissionError, StopIteration, ValueError):
                continue
        if not matched:
            _reap_orphaned_children()
            return
        time.sleep(0.05)
        _reap_orphaned_children()


def _reap_orphaned_children() -> None:
    """Reap solver grandchildren when the API is container PID 1."""
    while True:
        try:
            child_pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if child_pid == 0:
            return


def run_solver(
    solver_type: str,
    script_path: Path,
    input_dir: Path,
    output_dir: Path,
    stdout_path: Path,
    stderr_path: Path,
    *,
    max_time: int,
    env_vars: Mapping[str, str] | None,
    identity: tuple[int, int] | None,
) -> dict[str, Any]:
    if solver_type != "python":
        raise InputValidationError(f"runtime is not available in this image: {solver_type}")
    if not 1 <= max_time <= MAX_EXECUTION_TIME:
        raise InputValidationError("max_time is outside the allowed range")

    environment = build_solver_environment(input_dir, output_dir, env_vars)
    command = [
        sys.executable,
        # The trusted wrapper must start without importing task-controlled
        # sitecustomize/usercustomize modules from PYTHONPATH.  The resource
        # limits it installs survive the subsequent exec of the user script.
        "-I",
        "-S",
        str(EXEC_WRAPPER),
        "--max-time",
        str(max_time),
        "--address-space-bytes",
        str(MAX_ADDRESS_SPACE_BYTES),
        "--file-size-bytes",
        str(MAX_SINGLE_OUTPUT_BYTES),
        "--open-files",
        str(MAX_OPEN_FILES),
        "--processes",
        str(MAX_PROCESSES),
        str(script_path),
    ]
    started_at = time.monotonic()
    limit_error: str | None = None
    timed_out = False

    with stdout_path.open("x", encoding="utf-8") as stdout_file, stderr_path.open(
        "x", encoding="utf-8"
    ) as stderr_file:
        process_kwargs: dict[str, Any] = {}
        if identity is not None:
            uid, gid = identity
            process_kwargs.update(user=uid, group=gid, extra_groups=[])
        process = subprocess.Popen(
            command,
            cwd=str(output_dir),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            umask=0o077,
            **process_kwargs,
        )
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started_at
                if elapsed > max_time:
                    timed_out = True
                    _terminate_process_group(process)
                    break
                try:
                    _inspect_output_tree(output_dir)
                    if (
                        stdout_path.stat().st_size > MAX_LOG_BYTES
                        or stderr_path.stat().st_size > MAX_LOG_BYTES
                    ):
                        raise OutputLimitError("solver logs exceed the size limit")
                except OutputLimitError as exc:
                    limit_error = str(exc)
                    _terminate_process_group(process)
                    break
                time.sleep(0.2)
        finally:
            try:
                if process.poll() is None:
                    _terminate_process_group(process)
                return_code = process.wait(timeout=2)
            finally:
                _terminate_solver_identity(identity)

    duration = time.monotonic() - started_at
    if timed_out:
        return {
            "status": "timeout",
            "exit_code": None,
            "duration": duration,
            "error_code": "EXECUTION_TIMEOUT",
            "error_message": f"Execution exceeded {max_time}s timeout",
        }
    if limit_error:
        return {
            "status": "failed",
            "exit_code": return_code,
            "duration": duration,
            "error_code": "OUTPUT_LIMIT_EXCEEDED",
            "error_message": limit_error,
        }

    try:
        _inspect_output_tree(output_dir)
        if stdout_path.stat().st_size > MAX_LOG_BYTES or stderr_path.stat().st_size > MAX_LOG_BYTES:
            raise OutputLimitError("solver logs exceed the size limit")
    except OutputLimitError as exc:
        return {
            "status": "failed",
            "exit_code": return_code,
            "duration": duration,
            "error_code": "OUTPUT_LIMIT_EXCEEDED",
            "error_message": str(exc),
        }
    return {
        "status": "completed" if return_code == 0 else "failed",
        "exit_code": return_code,
        "duration": duration,
        "error_code": None if return_code == 0 else "PROCESS_FAILED",
        "error_message": None if return_code == 0 else "Solver process exited unsuccessfully",
    }


def collect_output_files(output_dir: Path) -> Iterable[tuple[Path, Path]]:
    _inspect_output_tree(output_dir)
    for path in sorted(output_dir.rglob("*")):
        if path.is_symlink():
            raise OutputLimitError("symbolic links are forbidden in solver output")
        if path.is_file() and path.suffix.lower() in RESULT_SUFFIXES:
            relative_path = path.relative_to(output_dir)
            if ".tmp" in relative_path.parts:
                continue
            try:
                validate_relative_path(relative_path.as_posix(), allow_nested=True)
            except InputValidationError as exc:
                raise OutputLimitError("solver output contains an unsafe path") from exc
            yield path, relative_path
