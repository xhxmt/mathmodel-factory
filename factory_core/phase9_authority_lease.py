"""Cross-process serialization for Authority state and delivery commits.

The lease is attached to the stable project-directory inode and, when present,
the ``state.db`` inode.  It does not create a lock file (or a database) for
legacy projects.  The directory lease closes the absent-database creation race;
the database lease additionally protects against unsafe aliases/replacements.
Every Phase9 writer and every delivery-equivalent filesystem commit must
participate so that classification and side effect have one ordering.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
from typing import Iterator


class AuthorityStateLeaseError(RuntimeError):
    """The Authority database inode cannot be leased safely."""


_THREAD_LEASES = threading.local()


def _leases_for_current_process() -> dict[tuple[int, int], tuple[int, int]]:
    """Return only leases acquired by this thread in this process.

    ``threading.local`` state is copied by ``fork``.  A child must not treat a
    lease held by its parent as a same-thread nested acquisition: doing so
    would bypass the independent ``flock`` that provides process ordering.
    """

    process_id = os.getpid()
    if getattr(_THREAD_LEASES, "process_id", None) != process_id:
        _THREAD_LEASES.process_id = process_id
        _THREAD_LEASES.held = {}
    return _THREAD_LEASES.held


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
    )


def _snapshot_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Bind every stable file attribute used by the snapshot copier."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_metadata(
    metadata: os.stat_result, *, kind: str, single_link: bool
) -> None:
    expected = stat.S_ISDIR if kind == "project directory" else stat.S_ISREG
    if stat.S_ISLNK(metadata.st_mode) or not expected(metadata.st_mode) or (
        single_link and metadata.st_nlink != 1
    ):
        raise AuthorityStateLeaseError(
            f"Authority {kind} lease target has an unsafe type or link count"
        )


@contextmanager
def _exclusive_inode_lease(
    path: Path, *, kind: str, single_link: bool
) -> Iterator[None]:
    try:
        path_metadata = path.lstat()
    except FileNotFoundError as exc:
        raise AuthorityStateLeaseError(f"Authority {kind} lease target is missing") from exc
    _validate_metadata(path_metadata, kind=kind, single_link=single_link)

    leases = _leases_for_current_process()
    key = (path_metadata.st_dev, path_metadata.st_ino)
    nested = leases.get(key)
    if nested is not None:
        current = path.lstat()
        _validate_metadata(current, kind=kind, single_link=single_link)
        if _identity(current) != _identity(path_metadata):
            raise AuthorityStateLeaseError(
                f"Authority {kind} changed during a nested commit lease"
            )
        leases[key] = (nested[0], nested[1] + 1)
        try:
            yield
        finally:
            descriptor, depth = leases[key]
            if depth == 1:
                del leases[key]
            else:
                leases[key] = (descriptor, depth - 1)
        return

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if kind == "project directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuthorityStateLeaseError(
            f"Authority {kind} lease cannot be opened safely: {exc}"
        ) from exc
    locked = False
    try:
        opened = os.fstat(descriptor)
        _validate_metadata(opened, kind=kind, single_link=single_link)
        if _identity(opened) != _identity(path_metadata):
            raise AuthorityStateLeaseError(
                f"Authority {kind} changed while its commit lease was opened"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        try:
            current = path.lstat()
        except FileNotFoundError as exc:
            raise AuthorityStateLeaseError(
                f"Authority {kind} disappeared while acquiring its commit lease"
            ) from exc
        _validate_metadata(current, kind=kind, single_link=single_link)
        if _identity(current) != _identity(opened):
            raise AuthorityStateLeaseError(
                f"Authority {kind} changed while acquiring its commit lease"
            )
        leases[key] = (descriptor, 1)
        try:
            yield
        finally:
            del leases[key]
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def authority_state_commit_lease(project: str | Path) -> Iterator[None]:
    """Hold an exclusive lease on a project and its Authority database.

    Projects without ``.factory/state.db`` retain the project-directory lease,
    so a participating writer cannot create a database during a legacy commit.
    No lock file or database is created.  When a database exists, inode checks
    before and after ``flock`` prevent path replacement from silently moving
    the lease away from the database subsequently classified by the caller.
    """

    root = Path(project).resolve()
    with _exclusive_inode_lease(
        root, kind="project directory", single_link=False
    ):
        database = root / ".factory" / "state.db"
        try:
            database.lstat()
        except FileNotFoundError:
            yield
            return
        with _exclusive_inode_lease(
            database, kind="database", single_link=True
        ):
            yield


@contextmanager
def authority_database_commit_lease(database: str | Path) -> Iterator[None]:
    """Serialize sanctioned mutations of a project-local Authority database.

    The production Authority APIs also accept explicit databases which are not
    project workflow state.  Preserve that contract unchanged.  Only the
    canonical ``<project>/.factory/state.db`` coordinate participates in the
    project/delivery lease shared by Phase9 writers and side-effect commits.
    """

    raw = Path(database)
    lexical = Path(os.path.abspath(os.fspath(raw)))
    if lexical.name != "state.db" or lexical.parent.name != ".factory":
        yield
        return
    factory = lexical.parent
    project = lexical.parent.parent
    try:
        project_lexical_before = project.lstat()
        project_resolved = project.resolve(strict=True)
        project_before = project_resolved.lstat()
        factory_before = factory.lstat()
        database_before = lexical.lstat()
        _validate_metadata(
            project_lexical_before, kind="project directory", single_link=False
        )
        _validate_metadata(
            project_before, kind="project directory", single_link=False
        )
        _validate_metadata(
            factory_before, kind="project directory", single_link=False
        )
        _validate_metadata(database_before, kind="database", single_link=True)
        resolved = lexical.resolve(strict=True)
        expected = (project_resolved / ".factory" / "state.db").resolve(
            strict=True
        )
    except OSError as exc:
        raise AuthorityStateLeaseError(
            "project-local Authority database cannot be resolved safely"
        ) from exc
    if resolved != expected:
        raise AuthorityStateLeaseError(
            "project-local Authority database coordinate is ambiguous"
        )
    with authority_state_commit_lease(project_resolved):
        try:
            project_lexical_current = project.lstat()
            project_current = project_resolved.lstat()
            factory_current = factory.lstat()
            database_current = lexical.lstat()
            current_resolved = lexical.resolve(strict=True)
            current_expected = (
                project_resolved / ".factory" / "state.db"
            ).resolve(strict=True)
        except OSError as exc:
            raise AuthorityStateLeaseError(
                "project-local Authority database changed before its commit lease"
            ) from exc
        _validate_metadata(
            project_lexical_current, kind="project directory", single_link=False
        )
        _validate_metadata(
            project_current, kind="project directory", single_link=False
        )
        _validate_metadata(
            factory_current, kind="project directory", single_link=False
        )
        _validate_metadata(database_current, kind="database", single_link=True)
        if (
            _identity(project_lexical_current)
            != _identity(project_lexical_before)
            or _identity(project_current) != _identity(project_before)
            or _identity(factory_current) != _identity(factory_before)
            or _identity(database_current) != _identity(database_before)
            or current_resolved != resolved
            or current_expected != expected
        ):
            raise AuthorityStateLeaseError(
                "project-local Authority database changed before its commit lease"
            )
        yield


def _snapshot_component_metadata(
    path: Path, *, required: bool
) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        if required:
            raise AuthorityStateLeaseError(
                "Authority database snapshot component is missing"
            ) from exc
        return None
    except OSError as exc:
        raise AuthorityStateLeaseError(
            "Authority database snapshot component cannot be inspected"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise AuthorityStateLeaseError(
            "Authority database snapshot component has an unsafe type or link count"
        )
    return metadata


def _open_snapshot_component(
    path: Path, expected: os.stat_result
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise AuthorityStateLeaseError(
            "Authority database snapshot component cannot be opened safely"
        ) from exc
    if (
        stat.S_ISLNK(opened.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or _snapshot_identity(opened) != _snapshot_identity(expected)
    ):
        os.close(descriptor)
        raise AuthorityStateLeaseError(
            "Authority database snapshot component changed while being opened"
        )
    return descriptor, opened


def _read_snapshot_component(
    source: Path,
    expected: os.stat_result,
    *,
    destination: Path | None,
) -> None:
    """Copy one component, or validate it by a complete stable read."""

    descriptor, opened = _open_snapshot_component(source, expected)
    destination_descriptor: int | None = None
    try:
        if destination is not None:
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            if destination_descriptor is not None:
                view = memoryview(block)
                while view:
                    written = os.write(destination_descriptor, view)
                    if written <= 0:
                        raise OSError("short snapshot write")
                    view = view[written:]
        after = os.fstat(descriptor)
        final = source.lstat()
    except OSError as exc:
        raise AuthorityStateLeaseError(
            "Authority database snapshot component changed or became unreadable"
        ) from exc
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(descriptor)
    expected_identity = _snapshot_identity(expected)
    if (
        _snapshot_identity(opened) != expected_identity
        or _snapshot_identity(after) != expected_identity
        or _snapshot_identity(final) != expected_identity
    ):
        raise AuthorityStateLeaseError(
            "Authority database snapshot component changed during read"
        )
    if destination is not None:
        copied = destination.lstat()
        if (
            not stat.S_ISREG(copied.st_mode)
            or stat.S_ISLNK(copied.st_mode)
            or copied.st_nlink != 1
            or stat.S_IMODE(copied.st_mode) != 0o600
            or copied.st_size != expected.st_size
        ):
            raise AuthorityStateLeaseError(
                "private Authority database snapshot has unsafe metadata"
            )


def _require_component_state(
    path: Path, expected: os.stat_result | None
) -> None:
    current = _snapshot_component_metadata(path, required=expected is not None)
    if expected is None:
        if current is not None:
            raise AuthorityStateLeaseError(
                "Authority database sidecar appeared during snapshot"
            )
        return
    if current is None or _snapshot_identity(current) != _snapshot_identity(expected):
        raise AuthorityStateLeaseError(
            "Authority database snapshot component identity changed"
        )


@contextmanager
def isolated_authority_snapshot_ro(
    database: str | Path, *, timeout_seconds: float = 2.0
) -> Iterator[sqlite3.Connection]:
    """Query a private main/WAL snapshot without touching source sidecars.

    The calling thread must already hold :func:`authority_state_commit_lease`
    for the project containing ``database``.  The main database and optional
    WAL are copied to a private 0700 directory as 0600 regular files.  SHM is
    read only to validate its stable inode and bytes; it is deliberately not
    copied because SQLite reconstructs private shared memory.  A rollback
    journal is always an ambiguous hot-state signal and therefore fails closed.
    """

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise AuthorityStateLeaseError(
            "Authority database snapshot timeout must be positive seconds"
        )
    source = Path(database)
    main = _snapshot_component_metadata(source, required=True)
    assert main is not None
    held = getattr(_THREAD_LEASES, "held", {})
    if (main.st_dev, main.st_ino) not in held:
        raise AuthorityStateLeaseError(
            "Authority database snapshot requires its commit lease"
        )
    parent_before = source.parent.lstat()
    if stat.S_ISLNK(parent_before.st_mode) or not stat.S_ISDIR(parent_before.st_mode):
        raise AuthorityStateLeaseError(
            "Authority database snapshot directory has an unsafe type"
        )
    wal_path = Path(f"{source}-wal")
    shm_path = Path(f"{source}-shm")
    journal_path = Path(f"{source}-journal")
    wal = _snapshot_component_metadata(wal_path, required=False)
    shm = _snapshot_component_metadata(shm_path, required=False)
    try:
        journal_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AuthorityStateLeaseError(
            "Authority rollback journal cannot be inspected"
        ) from exc
    else:
        raise AuthorityStateLeaseError(
            "Authority rollback journal prevents a stable read snapshot"
        )

    try:
        with tempfile.TemporaryDirectory(
            prefix="paper-factory-phase9-ro-"
        ) as temporary:
            private_root = Path(temporary)
            private_root.chmod(0o700)
            root_metadata = private_root.lstat()
            if (
                stat.S_ISLNK(root_metadata.st_mode)
                or not stat.S_ISDIR(root_metadata.st_mode)
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
            ):
                raise AuthorityStateLeaseError(
                    "private Authority snapshot directory has unsafe metadata"
                )
            private_database = private_root / "state.db"
            _read_snapshot_component(
                source, main, destination=private_database
            )
            if wal is not None:
                _read_snapshot_component(
                    wal_path,
                    wal,
                    destination=Path(f"{private_database}-wal"),
                )
            if shm is not None:
                _read_snapshot_component(shm_path, shm, destination=None)
            _require_component_state(source, main)
            _require_component_state(wal_path, wal)
            _require_component_state(shm_path, shm)
            try:
                journal_path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise AuthorityStateLeaseError(
                    "Authority rollback journal appeared during snapshot"
                )
            parent_after = source.parent.lstat()
            if _snapshot_identity(parent_after) != _snapshot_identity(parent_before):
                raise AuthorityStateLeaseError(
                    "Authority database directory changed during snapshot"
                )
            try:
                connection = sqlite3.connect(
                    f"{private_database.resolve(strict=True).as_uri()}?mode=ro",
                    uri=True,
                    timeout=float(timeout_seconds),
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(
                    f"PRAGMA busy_timeout={max(1, int(float(timeout_seconds) * 1000))}"
                )
            except (OSError, sqlite3.Error) as exc:
                raise AuthorityStateLeaseError(
                    "private Authority database snapshot cannot be opened"
                ) from exc
            try:
                yield connection
            finally:
                connection.close()
    except AuthorityStateLeaseError:
        raise
    except OSError as exc:
        raise AuthorityStateLeaseError(
            "private Authority database snapshot cannot be created or removed"
        ) from exc
