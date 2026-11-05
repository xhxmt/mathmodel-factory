"""Process-environment defaults a workflow run needs to be safe on this host.

A run's checkers, solvers and agents all write scratch files through
``tempfile`` and therefore honour ``TMPDIR``.  When the inherited temporary
directory is a memory filesystem (``/tmp`` is a 3.9 GiB tmpfs on the
acceptance host), one large checker can fill it and every later check fails
with ``database or disk is full`` - which the 2026-10-15 acceptance run turned
into five lost attempts of Step 10.  The run therefore points ``TMPDIR`` at a
disk-backed directory under the factory's ``run_state/`` (gitignored) unless
the operator chose one explicitly with ``FACTORY_TMPDIR``.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Iterator, Mapping

MEMORY_FILESYSTEMS = frozenset({"tmpfs", "ramfs"})
_DEFAULT_MOUNTS = Path("/proc/self/mounts")


def _unescape_mount_path(value: str) -> str:
    # /proc/mounts escapes space, tab, newline and backslash as octal.
    return (
        value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
    )


def filesystem_type(path: str | Path, mounts_text: str | None = None) -> str | None:
    """Return the filesystem type of the mount that holds ``path``.

    ``None`` means the type could not be determined (no ``/proc`` on this
    platform, or an unreadable mount table); callers then leave the
    environment unchanged rather than guess.
    """

    if mounts_text is None:
        try:
            mounts_text = _DEFAULT_MOUNTS.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    target = os.path.realpath(str(path))
    best_mount, best_type = "", None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, fs_type = _unescape_mount_path(parts[1]), parts[2]
        prefix = mount_point.rstrip("/") + "/"
        if (target == mount_point or target.startswith(prefix) or mount_point == "/") and len(
            mount_point
        ) >= len(best_mount):
            best_mount, best_type = mount_point, fs_type
    return best_type


def _usable_directory(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.W_OK | os.X_OK)


def effective_tempdir(environ: Mapping[str, str]) -> str:
    """The directory ``tempfile`` would choose under ``environ``.

    Mirrors ``tempfile``'s candidate order (``TMPDIR``, ``TEMP``, ``TMP``,
    then the platform defaults) and skips candidates that are not writable
    directories, so a stale ``TMPDIR`` is judged by where scratch files will
    really go.  It never reads ``tempfile``'s process-wide cache.
    """

    candidates = [(environ.get(name) or "").strip() for name in ("TMPDIR", "TEMP", "TMP")]
    candidates += ["/tmp", "/var/tmp", "/usr/tmp"]
    for candidate in candidates:
        if candidate and _usable_directory(candidate):
            return candidate
    return os.getcwd()


def disk_tmpdir_overrides(
    factory_root: str | Path,
    environ: Mapping[str, str] | None = None,
    *,
    mounts_text: str | None = None,
    create: bool = True,
) -> dict[str, str]:
    """Environment overrides that keep a run's scratch files on disk.

    Returns ``{}`` when nothing needs to change: the inherited ``TMPDIR`` is
    already disk-backed, or its filesystem cannot be determined.  ``create``
    makes the target directory; a read-only caller passes ``False``.
    """

    environ = os.environ if environ is None else environ
    explicit = (environ.get("FACTORY_TMPDIR") or "").strip()
    if explicit:
        target = Path(explicit)
    else:
        inherited = effective_tempdir(environ)
        if filesystem_type(inherited, mounts_text) not in MEMORY_FILESYSTEMS:
            return {}
        target = Path(factory_root).resolve() / "run_state" / "tmp"
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return {"TMPDIR": str(target)}


@contextlib.contextmanager
def disk_backed_tmpdir(factory_root: str | Path, *, mounts_text: str | None = None) -> Iterator[dict[str, str]]:
    """Apply :func:`disk_tmpdir_overrides` to this process for the block.

    Child processes started inside the block inherit the new ``TMPDIR``.  The
    previous value and ``tempfile``'s cached directory are restored on exit,
    so an in-process caller (a test, the Web control plane) is left as it was.
    """

    # Snapshot tempfile's cache before anything below could populate it.
    previous_tempdir = tempfile.tempdir
    overrides = disk_tmpdir_overrides(factory_root, mounts_text=mounts_text)
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    if overrides:
        tempfile.tempdir = None
    try:
        yield overrides
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        tempfile.tempdir = previous_tempdir
